"""Exercises the real FastAPI lifecycle with mocked scraper, dedup, resume store,
and Claude scoring — no internet access or API keys needed.
Run with: pytest tests/test_api.py -v
"""
import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import main, resume_parser, scraper as scraper_mod


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Clear in-memory rate-limit counters before each test so tests don't bleed."""
    try:
        main.limiter._storage.reset()
    except Exception:
        pass
    yield


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

def _fake_scrape_all(location, is_remote, hours_old, on_progress=None, search_terms=None):
    if on_progress:
        on_progress("Scraping: AI Engineer")
    rows = [
        dict(title="Senior AI Engineer", company="Acme AI", location="Remote, US",
             date_posted=date.today(), is_remote=True, min_amount=180000, max_amount=240000,
             job_url="http://x/1", description="PyTorch, RAG, langchain, fine-tuning, embeddings, mlops, AWS."),
        dict(title="Machine Learning Engineer", company="DataCo", location="NYC",
             date_posted=date.today() - timedelta(days=5), is_remote=False, min_amount=150000, max_amount=200000,
             job_url="http://x/2", description="TensorFlow, deep learning, computer vision, kubernetes."),
        dict(title="Sales Account Executive", company="SellCorp", location="SF",
             date_posted=date.today(), is_remote=False, min_amount=80000, max_amount=120000,
             job_url="http://x/3", description="Sell our AI product to enterprises."),
    ]
    df = pd.DataFrame(rows)
    df["search_term"] = "AI Engineer"
    return df


_FAKE_KW = {"search_titles": ["AI Engineer", "ML Engineer"], "skill_signals": ["RAG", "LangChain"], "total_yoe": 5}


class _TextBlock:
    def __init__(self, text):
        self.text = text


class _ClaudeMessage:
    def __init__(self, text):
        self.content = [_TextBlock(text)]


class _ClaudeClient:
    def __init__(self, text):
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        return _ClaudeMessage(self._text)


def _wait_for_done(client: TestClient, job_id: str, attempts: int = 50) -> dict:
    body = None
    for _ in range(attempts):
        body = client.get(f"/api/analyze/{job_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.1)
    return body or {}


def _fake_score_with_claude(df, resume_text, skill_signals, total_yoe, client, *args):
    """Returns all rows with ats_score=80, sorted descending (no threshold, no slice)."""
    if df.empty:
        return df
    df = df.copy()
    df["ats_score"] = 80
    df["missing_keywords"] = [[] for _ in range(len(df))]
    return df.sort_values("ats_score", ascending=False).reset_index(drop=True)


def _install_cache_fakes(monkeypatch, get_recent_jobs=None, count_jobs=3, stale=False):
    if get_recent_jobs is None:
        get_recent_jobs = lambda hours_old: _fake_scrape_all(
            "United States", False, hours_old, search_terms=main.config.DEFAULT_STEM_SEARCH_TITLES
        )
    monkeypatch.setattr(main.job_cache, "count_jobs", lambda: count_jobs)
    monkeypatch.setattr(main.job_cache, "get_cache_age_minutes", lambda: 5)
    monkeypatch.setattr(main.job_cache, "is_cache_stale", lambda max_age_minutes=60: stale)
    monkeypatch.setattr(main.job_cache, "refresh_job_cache", lambda *args, **kwargs: {
        "status": "done",
        "raw_count": count_jobs,
        "inserted_count": count_jobs,
        "updated_count": 0,
        "message": "ok",
    })
    monkeypatch.setattr(main.job_cache, "get_recent_jobs", get_recent_jobs)
    monkeypatch.setattr(
        main.dedup,
        "split_seen",
        lambda df, resume_hash: (df.reset_index(drop=True), df.iloc[0:0].copy()),
    )
    monkeypatch.setattr(main.dedup, "mark_seen", lambda df, resume_hash=None: None)


# ---------------------------------------------------------------------------
# Base fixture used by existing tests
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    _install_cache_fakes(monkeypatch)
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
    monkeypatch.setattr(main.resume_parser, "extract_keywords",
                        lambda text, client: _FAKE_KW)
    monkeypatch.setattr(main.scoring, "score_with_claude", _fake_score_with_claude)
    with TestClient(main.app) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_full_lifecycle(client):
    resume_text = b"Experienced engineer skilled in pytorch, llm, rag, langchain, aws, mlops, embeddings, python, kubernetes."
    files = {"resume": ("resume.txt", resume_text, "text/plain")}
    data = {"location": "United States", "is_remote": "false", "hours_old": "168"}

    r = client.post("/api/analyze", files=files, data=data)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    body = None
    for _ in range(50):
        r = client.get(f"/api/analyze/{job_id}")
        body = r.json()
        if body["status"] in ("done", "error"):
            break
        time.sleep(0.1)

    assert body["status"] == "done", body
    assert body["error"] is None
    results = body["results"]
    assert len(results) >= 2, f"expected at least 2 matches, got {len(results)}"
    assert results[0]["title"] == "Senior AI Engineer"

    r = client.get(f"/api/analyze/{job_id}/export.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert len(r.content) > 1000


def test_unknown_job_returns_404(client):
    r = client.get("/api/analyze/doesnotexist")
    assert r.status_code == 404


def test_results_sorted_by_ats_score(monkeypatch):
    """Results must be sorted descending by ats_score even when scores are low
    (i.e. below what used to be the 75/50 thresholds that no longer exist)."""

    def low_score_claude(df, resume_text, skill_signals, total_yoe, client, *args):
        if df.empty:
            return df
        df = df.copy()
        # Assign scores below the old 75/50 cutoffs — both should still appear
        scores = {"http://x/1": 45, "http://x/2": 30}
        df["ats_score"] = df["job_url"].map(scores).fillna(0).astype(int)
        df["missing_keywords"] = [[] for _ in range(len(df))]
        return df.sort_values("ats_score", ascending=False).reset_index(drop=True)

    with TestClient(main.app) as c:
        _install_cache_fakes(monkeypatch)
        monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
        monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
        monkeypatch.setattr(main.resume_parser, "extract_keywords",
                            lambda text, client: _FAKE_KW)
        monkeypatch.setattr(main.scoring, "score_with_claude", low_score_claude)

        resume_text = b"Experienced engineer skilled in pytorch, llm, rag, langchain, aws, mlops, embeddings, python, kubernetes."
        files = {"resume": ("resume.txt", resume_text, "text/plain")}
        data = {"location": "United States", "is_remote": "false", "hours_old": "168"}

        r = c.post("/api/analyze", files=files, data=data)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]

        body = None
        for _ in range(50):
            r = c.get(f"/api/analyze/{job_id}")
            body = r.json()
            if body["status"] in ("done", "error"):
                break
            time.sleep(0.1)

        assert body["status"] == "done", body
        results = body["results"]
        assert len(results) >= 2, f"both jobs should appear with no score threshold, got {len(results)}"
        assert results[0]["ats_score"] == 45
        assert results[1]["ats_score"] == 30
        assert results[0]["title"] == "Senior AI Engineer"


def test_resume_reuse(monkeypatch):
    """Second upload with same filename must skip extract_keywords (use cached resume)."""
    extract_calls = []
    stored_resume: dict = {}

    def fake_extract_keywords(text, client):
        extract_calls.append(text)
        return _FAKE_KW

    def fake_save_resume(**kw):
        stored_resume.update(kw)

    def fake_load_resume():
        if stored_resume.get("filename"):
            return {
                "filename": stored_resume["filename"],
                "text": stored_resume["text"],
                "search_titles": stored_resume.get("search_titles", "[]"),
                "skill_signals": stored_resume.get("skill_signals", "[]"),
                "analysis_version": stored_resume.get(
                    "analysis_version", main.config.RESUME_ANALYSIS_VERSION
                ),
                "total_yoe": stored_resume.get("total_yoe", 0),
                "email": stored_resume.get("email"),
                "phone": stored_resume.get("phone"),
            }
        return None

    _install_cache_fakes(monkeypatch)
    monkeypatch.setattr(main.resume_store, "load_resume", fake_load_resume)
    monkeypatch.setattr(main.resume_store, "save_resume", fake_save_resume)
    monkeypatch.setattr(main.resume_parser, "extract_keywords", fake_extract_keywords)
    monkeypatch.setattr(main.scoring, "score_with_claude", _fake_score_with_claude)

    resume_bytes = b"Experienced engineer skilled in pytorch, llm, rag, langchain, aws, mlops, embeddings, python, kubernetes."
    files = {"resume": ("resume.txt", resume_bytes, "text/plain")}
    data = {"location": "United States", "is_remote": "false", "hours_old": "168"}

    with TestClient(main.app) as c:
        # First upload — should parse + extract
        r = c.post("/api/analyze", files=files, data=data)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        for _ in range(50):
            body = c.get(f"/api/analyze/{job_id}").json()
            if body["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        assert body["status"] == "done", body
        assert len(extract_calls) == 1, "extract_keywords should be called on first upload"

        # Second upload — same filename, should use cache
        r = c.post("/api/analyze", files=files, data=data)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        for _ in range(50):
            body = c.get(f"/api/analyze/{job_id}").json()
            if body["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        assert body["status"] == "done", body
        assert len(extract_calls) == 1, (
            f"extract_keywords should NOT be called on second upload (same filename), "
            f"but was called {len(extract_calls)} times total"
        )


def test_timeout_mid_loop_returns_partial_results(monkeypatch):
    """If the 72h fallback window times out, the job should still complete with
    whatever results were scored in the first (24h) window."""
    windows = []

    def cached_by_window(hours_old):
        windows.append(hours_old)
        return _fake_scrape_all("United States", False, hours_old)

    _install_cache_fakes(monkeypatch, get_recent_jobs=cached_by_window)
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
    monkeypatch.setattr(main.resume_parser, "extract_keywords",
                        lambda text, client: _FAKE_KW)
    monkeypatch.setattr(main.scoring, "score_with_claude", _fake_score_with_claude)

    resume_bytes = b"Experienced engineer skilled in pytorch, llm, rag, langchain, aws, mlops, embeddings, python, kubernetes."
    files = {"resume": ("resume.txt", resume_bytes, "text/plain")}
    # hours_old=24 means windows=[24, 72]; first succeeds, second raises TimeoutError
    data = {"location": "United States", "is_remote": "false", "hours_old": "24"}

    with TestClient(main.app) as c:
        r = c.post("/api/analyze", files=files, data=data)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]

        body = None
        for _ in range(50):
            r = c.get(f"/api/analyze/{job_id}")
            body = r.json()
            if body["status"] in ("done", "error"):
                break
            time.sleep(0.1)

    assert body["status"] == "done", f"Expected done, got: {body}"
    assert body["error"] is None
    results = body["results"]
    assert len(results) >= 1, "Should have results from the cached window"
    assert windows[:3] == [24, 72, 168]


def test_yoe_extracted_and_stored(monkeypatch):
    """total_yoe returned by extract_keywords must be passed to save_resume."""
    stored = {}

    def fake_save_resume(**kw):
        stored.update(kw)

    _install_cache_fakes(monkeypatch)
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", fake_save_resume)
    monkeypatch.setattr(main.resume_parser, "extract_keywords",
                        lambda text, client: {**_FAKE_KW, "total_yoe": 7})
    monkeypatch.setattr(main.scoring, "score_with_claude", _fake_score_with_claude)

    resume_bytes = b"7 years of experience with pytorch, llm, rag, langchain, aws."
    files = {"resume": ("resume.txt", resume_bytes, "text/plain")}
    data = {"location": "United States", "is_remote": "false"}

    with TestClient(main.app) as c:
        r = c.post("/api/analyze", files=files, data=data)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        for _ in range(50):
            body = c.get(f"/api/analyze/{job_id}").json()
            if body["status"] in ("done", "error"):
                break
            time.sleep(0.1)

    assert body["status"] == "done", body
    assert stored.get("total_yoe") == 7, f"expected total_yoe=7 in save_resume call, got: {stored}"


def test_search_title_sanitizer_rejects_skill_phrases():
    bad_titles = [
        "multi agent",
        "mcp tool",
        "langgraph openai",
        "openai agents",
        "agents sdk",
        "semantic reranking",
    ]
    assert resume_parser.sanitize_search_titles(bad_titles) == []


def test_search_title_sanitizer_preserves_role_titles():
    titles = [
        "AI Engineer",
        "Machine Learning Engineer",
        "GenAI Engineer",
        "Applied AI Engineer",
        "LLM Engineer",
        "Data Scientist",
        "Applied Scientist",
        "ML Platform Engineer",
    ]
    assert resume_parser.sanitize_search_titles(titles) == titles


def test_extract_keywords_empty_claude_response_uses_safe_fallback():
    analysis = resume_parser.extract_keywords(
        "Python SQL Snowflake dbt RAG. 8 years of experience building ML systems.",
        _ClaudeClient(""),
    )

    for title in main.config.DEFAULT_SEARCH_TITLES:
        assert title in analysis["search_titles"]
    assert "Python" in analysis["skill_signals"]
    assert "SQL" in analysis["skill_signals"]
    assert analysis["total_yoe"] == 8


def test_extract_keywords_markdown_json_parses_correctly():
    raw = """```json
{"search_titles": ["Research Scientist", "mcp tool"], "skill_signals": ["LangGraph", "RAG"], "total_yoe": 9}
```"""
    analysis = resume_parser.extract_keywords("resume text", _ClaudeClient(raw))

    assert "Research Scientist" in analysis["search_titles"]
    assert "mcp tool" not in [title.lower() for title in analysis["search_titles"]]
    assert "LangGraph" in analysis["skill_signals"]
    assert analysis["total_yoe"] == 9


def test_extract_keywords_malformed_json_uses_safe_fallback():
    analysis = resume_parser.extract_keywords(
        "No obvious keywords here.",
        _ClaudeClient("Here is JSON: {not valid"),
    )

    assert analysis["search_titles"] == main.config.DEFAULT_SEARCH_TITLES
    assert analysis["skill_signals"] == main.config.DEFAULT_SKILL_SIGNALS
    assert analysis["total_yoe"] is None


def test_unusable_search_titles_fall_back_to_default_roles():
    analysis = resume_parser.normalize_resume_analysis({
        "search_titles": ["mcp tool", "semantic reranking"],
        "skill_signals": ["MCP", "semantic search"],
        "total_yoe": 4,
    })

    assert analysis["search_titles"] == main.config.DEFAULT_SEARCH_TITLES
    assert analysis["skill_signals"] == ["MCP", "semantic search"]
    assert analysis["total_yoe"] == 4


def test_get_resume_returns_cleaned_search_titles(monkeypatch):
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: {
        "filename": "resume.txt",
        "search_titles": json.dumps(["mcp tool", "semantic reranking"]),
        "skill_signals": json.dumps(["MCP", "semantic search"]),
        "total_yoe": 4,
        "email": "candidate@example.com",
        "stored_at": "2026-06-25T12:00:00+00:00",
    })

    with TestClient(main.app) as c:
        r = c.get("/api/resume")

    assert r.status_code == 200
    body = r.json()
    assert body["search_titles"] == main.config.DEFAULT_SEARCH_TITLES
    assert body["skill_signals"] == ["MCP", "semantic search"]


def test_old_cached_resume_analysis_is_reextracted(monkeypatch):
    extract_calls = []
    stored_resume = {
        "filename": "resume.txt",
        "text": (
            "Experienced AI engineer with Python, RAG, LangGraph, OpenAI, "
            "semantic search, vector databases, and Snowflake."
        ),
        "search_titles": json.dumps(["mcp tool", "semantic reranking"]),
        "skill_signals": json.dumps(["MCP", "semantic search"]),
        "analysis_version": main.config.RESUME_ANALYSIS_VERSION - 1,
        "total_yoe": 5,
        "email": "candidate@example.com",
        "phone": None,
    }

    def fake_extract_keywords(text, client):
        extract_calls.append(text)
        return {
            "search_titles": ["AI Engineer", "Machine Learning Engineer", "Data Scientist"],
            "skill_signals": ["LangGraph", "OpenAI", "RAG"],
            "total_yoe": 6,
        }

    def fake_save_resume(**kw):
        stored_resume.update(kw)

    _install_cache_fakes(monkeypatch)
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: stored_resume.copy())
    monkeypatch.setattr(main.resume_store, "save_resume", fake_save_resume)
    monkeypatch.setattr(main.resume_parser, "extract_keywords", fake_extract_keywords)
    monkeypatch.setattr(main.scoring, "score_with_claude", _fake_score_with_claude)

    with TestClient(main.app) as c:
        r = c.post("/api/analyze", data={"location": "United States", "is_remote": "false"})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]

        body = None
        for _ in range(50):
            body = c.get(f"/api/analyze/{job_id}").json()
            if body["status"] in ("done", "error"):
                break
            time.sleep(0.1)

    assert body["status"] == "done", body
    assert len(extract_calls) == 1
    assert stored_resume["analysis_version"] == main.config.RESUME_ANALYSIS_VERSION
    assert "AI Engineer" in body["message"]
    assert "GenAI Engineer" in body["message"]
    assert "mcp tool" not in body["message"].lower()


def test_default_search_titles_are_always_included():
    analysis = resume_parser.normalize_resume_analysis({
        "search_titles": ["AI Engineer", "Data Scientist", "Research Scientist"],
        "skill_signals": ["RAG"],
        "total_yoe": 3,
    })

    for title in main.config.DEFAULT_SEARCH_TITLES:
        assert title in analysis["search_titles"]
    assert "Research Scientist" in analysis["search_titles"]


def test_empty_24h_expands_to_72h_and_168h(monkeypatch):
    windows = []

    def cached_by_window(hours_old):
        windows.append(hours_old)
        if hours_old < 168:
            return pd.DataFrame()
        return _fake_scrape_all("United States", False, hours_old)

    _install_cache_fakes(monkeypatch, get_recent_jobs=cached_by_window)
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
    monkeypatch.setattr(main.resume_parser, "extract_keywords", lambda text, client: _FAKE_KW)
    monkeypatch.setattr(main.scoring, "score_with_claude", _fake_score_with_claude)

    with TestClient(main.app) as c:
        r = c.post(
            "/api/analyze",
            files={"resume": ("resume.txt", b"Python RAG LangChain AWS MLOps", "text/plain")},
            data={"location": "United States", "is_remote": "false", "hours_old": "24"},
        )
        assert r.status_code == 200, r.text
        body = _wait_for_done(c, r.json()["job_id"])

    assert body["status"] == "done", body
    assert windows[:3] == [24, 72, 168]
    assert len(body["results"]) >= 1


def test_scraper_never_runs_with_empty_search_titles(monkeypatch, tmp_path):
    captured_terms = []

    def capture_terms(location, is_remote, hours_old, on_progress=None, search_terms=None):
        captured_terms.append(list(search_terms or []))
        return _fake_scrape_all(location, is_remote, hours_old, on_progress, search_terms)

    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))
    monkeypatch.setattr(main.job_cache.scraper, "scrape_all", capture_terms)
    result = main.job_cache.refresh_job_cache(force=True, location="United States", is_remote=False)

    assert result["status"] == "done"
    assert captured_terms
    assert captured_terms[0] == main.config.DEFAULT_STEM_SEARCH_TITLES


def test_job_cache_upserts_and_reads_recent_jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))
    now = datetime.now(timezone.utc).isoformat()
    df = pd.DataFrame([
        {
            "title": "AI Engineer",
            "company": "Acme",
            "location": "Remote",
            "site": "linkedin",
            "description": "Python RAG",
            "date_posted": date.today(),
            "job_url": "http://cache/1",
        }
    ])

    counts = main.job_cache.upsert_jobs(df, scraped_at=now)
    assert counts == {"raw_count": 1, "inserted_count": 1, "updated_count": 0}

    df2 = pd.concat([
        df.assign(title="Senior AI Engineer"),
        pd.DataFrame([{
            "title": "Data Scientist",
            "company": "DataCo",
            "location": "NYC",
            "site": "indeed",
            "description": "SQL ML",
            "date_posted": date.today(),
            "job_url": "http://cache/2",
        }]),
    ], ignore_index=True)
    counts = main.job_cache.upsert_jobs(df2, scraped_at=now)

    recent = main.job_cache.get_recent_jobs(24)
    assert counts == {"raw_count": 2, "inserted_count": 1, "updated_count": 1}
    assert len(recent) == 2
    assert "Senior AI Engineer" in set(recent["title"])


def test_stale_cache_refreshes_with_broad_stem_titles(monkeypatch, tmp_path):
    captured_terms = []

    def capture_scrape(location, is_remote, hours_old, on_progress=None, search_terms=None):
        captured_terms.append(list(search_terms or []))
        return _fake_scrape_all(location, is_remote, hours_old, on_progress, search_terms)

    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))
    monkeypatch.setattr(main.job_cache.scraper, "scrape_all", capture_scrape)

    assert main.job_cache.is_cache_stale()
    result = main.job_cache.refresh_job_cache(force=False, location="United States", is_remote=False)

    assert result["status"] == "done"
    assert result["raw_count"] == 3
    assert main.job_cache.count_jobs() == 3
    assert main.job_cache.get_cache_age_minutes() is not None
    assert captured_terms == [main.config.DEFAULT_STEM_SEARCH_TITLES]


def test_user_run_uses_cached_jobs_instead_of_live_scraper(monkeypatch):
    def live_scraper_should_not_run(*args, **kwargs):
        raise AssertionError("user run should not call the live scraper")

    _install_cache_fakes(monkeypatch)
    monkeypatch.setattr(main.job_cache.scraper, "scrape_all", live_scraper_should_not_run)
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
    monkeypatch.setattr(main.resume_parser, "extract_keywords", lambda text, client: _FAKE_KW)
    monkeypatch.setattr(main.scoring, "score_with_claude", _fake_score_with_claude)

    with TestClient(main.app) as c:
        r = c.post(
            "/api/analyze",
            files={"resume": ("resume.txt", b"Python RAG LangChain AWS MLOps", "text/plain")},
            data={"location": "United States", "is_remote": "false", "hours_old": "24"},
        )
        assert r.status_code == 200, r.text
        body = _wait_for_done(c, r.json()["job_id"])

    assert body["status"] == "done", body
    assert len(body["results"]) >= 1


def test_seen_jobs_are_per_resume_hash(monkeypatch, tmp_path):
    monkeypatch.setattr(main.dedup, "_DB_PATH", str(tmp_path / "seen.db"))
    jobs = _fake_scrape_all("United States", False, 24)
    hash_a = main.dedup.resume_hash("resume A")
    hash_b = main.dedup.resume_hash("resume B")

    main.dedup.mark_seen(jobs.head(1), hash_a)
    unseen_a, seen_a = main.dedup.split_seen(jobs, hash_a)
    unseen_b, seen_b = main.dedup.split_seen(jobs, hash_b)

    assert len(seen_a) == 1
    assert len(unseen_a) == len(jobs) - 1
    assert seen_b.empty
    assert len(unseen_b) == len(jobs)


def test_dedup_empty_falls_back_to_seen_jobs(monkeypatch):
    _install_cache_fakes(monkeypatch)
    monkeypatch.setattr(
        main.dedup,
        "split_seen",
        lambda df, resume_hash: (df.iloc[0:0].copy(), df.assign(seen_before=True).reset_index(drop=True)),
    )
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
    monkeypatch.setattr(main.resume_parser, "extract_keywords", lambda text, client: _FAKE_KW)
    monkeypatch.setattr(main.scoring, "score_with_claude", _fake_score_with_claude)

    with TestClient(main.app) as c:
        r = c.post(
            "/api/analyze",
            files={"resume": ("resume.txt", b"Python RAG LangChain AWS MLOps", "text/plain")},
            data={"location": "United States", "is_remote": "false", "hours_old": "24"},
        )
        assert r.status_code == 200, r.text
        body = _wait_for_done(c, r.json()["job_id"])

    assert body["status"] == "done", body
    assert len(body["results"]) >= 1
    assert "previously seen jobs" in body["message"]


def test_prefilter_empty_bypasses_to_recent_shortlist(monkeypatch):
    _install_cache_fakes(monkeypatch)
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
    monkeypatch.setattr(main.resume_parser, "extract_keywords", lambda text, client: _FAKE_KW)
    monkeypatch.setattr(main.scoring, "prefilter", lambda df, resume_tokens: df.iloc[0:0].copy())
    monkeypatch.setattr(main.scoring, "score_with_claude", _fake_score_with_claude)

    with TestClient(main.app) as c:
        r = c.post(
            "/api/analyze",
            files={"resume": ("resume.txt", b"Python RAG LangChain AWS MLOps", "text/plain")},
            data={"location": "United States", "is_remote": "false", "hours_old": "24"},
        )
        assert r.status_code == 200, r.text
        body = _wait_for_done(c, r.json()["job_id"])

    assert body["status"] == "done", body
    assert len(body["results"]) >= 1
    assert "keyword prefilter was relaxed" in body["message"]


def test_scoring_failure_returns_fallback_ranked_jobs(monkeypatch):
    def scoring_failure(*args, **kwargs):
        raise RuntimeError("Claude unavailable")

    _install_cache_fakes(monkeypatch)
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
    monkeypatch.setattr(main.resume_parser, "extract_keywords", lambda text, client: _FAKE_KW)
    monkeypatch.setattr(main.scoring, "score_with_claude", scoring_failure)

    with TestClient(main.app) as c:
        r = c.post(
            "/api/analyze",
            files={"resume": ("resume.txt", b"Python RAG LangChain AWS MLOps embeddings", "text/plain")},
            data={"location": "United States", "is_remote": "false", "hours_old": "24"},
        )
        assert r.status_code == 200, r.text
        body = _wait_for_done(c, r.json()["job_id"])

    assert body["status"] == "done", body
    assert len(body["results"]) >= 1
    assert all("ats_score" in row for row in body["results"])
    assert "fallback scoring used" in body["message"]


def test_glassdoor_location_filtering(monkeypatch):
    """Glassdoor must be excluded when location is broad (no comma) and included
    when it looks like a specific city (contains a comma)."""
    captured_site_lists = []

    def capture_scrape_jobs(**kwargs):
        captured_site_lists.append(list(kwargs.get("site_name", [])))
        return pd.DataFrame()

    monkeypatch.setattr(scraper_mod, "scrape_jobs", capture_scrape_jobs)

    # Broad location — Glassdoor should be excluded from every scrape_jobs call
    scraper_mod.scrape_all("United States", False, 24, search_terms=["AI Engineer"])
    assert captured_site_lists, "Expected at least one scrape_jobs call"
    assert all("glassdoor" not in sites for sites in captured_site_lists), (
        f"Glassdoor should be excluded for broad location, got site lists: {captured_site_lists}"
    )
    assert all("zip_recruiter" not in sites for sites in captured_site_lists), (
        f"ZipRecruiter should be disabled by default, got site lists: {captured_site_lists}"
    )

    captured_site_lists.clear()

    # Specific city — Glassdoor should be present
    scraper_mod.scrape_all("New York, NY", False, 24, search_terms=["AI Engineer"])
    assert captured_site_lists, "Expected at least one scrape_jobs call"
    assert all("glassdoor" in sites for sites in captured_site_lists), (
        f"Glassdoor should be included for specific location, got site lists: {captured_site_lists}"
    )
    assert all("zip_recruiter" not in sites for sites in captured_site_lists), (
        f"ZipRecruiter should be disabled by default, got site lists: {captured_site_lists}"
    )

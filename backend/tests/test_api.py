"""Exercises the real FastAPI lifecycle with mocked scraper, dedup, resume store,
and Claude scoring — no internet access or API keys needed.
Run with: pytest tests/test_api.py -v
"""
import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import freshness, main, resume_parser, scraper as scraper_mod
from app.sources import ats_sources, google_jobs_serpapi


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
             date_posted=date.today() - timedelta(days=2), is_remote=False, min_amount=150000, max_amount=200000,
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
    data = {"location": "United States", "is_remote": "false", "hours_old": "24"}

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
    """Low-score jobs should be separated from default strong-match results."""

    def low_score_claude(df, resume_text, skill_signals, total_yoe, client, *args):
        if df.empty:
            return df
        df = df.copy()
        # Assign scores below the old 75/50 cutoffs — both should still appear
        scores = {"http://x/1": 20, "http://x/2": 10}
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
        data = {"location": "United States", "is_remote": "false", "hours_old": "24"}

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
        low_confidence = body["low_confidence_results"]
        assert results == []
        assert len(low_confidence) == 1
        assert low_confidence[0]["match_band"] == "broader"
        assert low_confidence[0]["title"] == "Senior AI Engineer"


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
    data = {"location": "United States", "is_remote": "false", "hours_old": "24"}

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
    assert windows[:2] == [24, 72]


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


def test_empty_24h_expands_to_72h_only(monkeypatch):
    windows = []

    def cached_by_window(hours_old):
        windows.append(hours_old)
        if hours_old < 72:
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
    assert windows[:2] == [24, 72]
    assert 168 not in windows
    assert len(body["results"]) >= 1


def test_scraper_never_runs_with_empty_search_titles(monkeypatch, tmp_path):
    captured_terms = []

    def capture_terms(location, is_remote, hours_old, on_progress=None, search_terms=None):
        captured_terms.append(list(search_terms or []))
        return _fake_scrape_all(location, is_remote, hours_old, on_progress, search_terms)

    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))
    monkeypatch.setattr(main.job_cache.scraper, "scrape_all", capture_terms)
    monkeypatch.setattr(main.job_cache, "fetch_google_jobs", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(main.job_cache, "fetch_company_ats_jobs", lambda: pd.DataFrame())
    result = main.job_cache.refresh_job_cache(force=True, location="United States", is_remote=False)

    assert result["status"] == "done"
    assert captured_terms
    assert captured_terms[0] == main.config.DEFAULT_STEM_SEARCH_TITLES


def test_parse_relative_posted_at_minutes_and_hours():
    ref = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)

    cases = [
        ("2 minutes ago", 2, "minute", "Posted 2 min ago"),
        ("49 minutes ago", 49, "minute", "Posted 49 min ago"),
        ("1 hour ago", 60, "hour", "Posted 1 hr ago"),
        ("21 hours ago", 1260, "hour", "Posted 21 hrs ago"),
    ]

    for raw, minutes, precision, label in cases:
        parsed = freshness.parse_relative_posted_at(raw, ref)
        assert parsed["posted_age_minutes"] == minutes
        assert parsed["posted_precision"] == precision
        assert freshness.build_posted_age_label(parsed["posted_at_ts"], precision, ref) == label


def test_date_only_jobspy_job_uses_day_precision(monkeypatch, tmp_path):
    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))
    now = datetime.now(timezone.utc).isoformat()
    df = pd.DataFrame([{
        "title": "AI Engineer",
        "company": "DateOnly",
        "location": "Remote",
        "site": "indeed",
        "description": "Python RAG",
        "date_posted": date.today(),
        "job_url": "http://date-only/1",
    }])

    main.job_cache.upsert_jobs(df, scraped_at=now)
    recent = main.job_cache.get_recent_jobs(24)

    assert recent.iloc[0]["posted_precision"] == "day"
    assert recent.iloc[0]["posted_age_label"] == "Posted today"


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


def test_job_cache_excludes_linkedin_easy_apply_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))
    now = datetime.now(timezone.utc).isoformat()
    df = pd.DataFrame([
        {
            "title": "AI Engineer",
            "company": "LinkedOnly",
            "location": "Remote",
            "source": "linkedin",
            "source_type": "linkedin",
            "job_url": "https://www.linkedin.com/jobs/view/1",
            "apply_url": "https://www.linkedin.com/jobs/view/1",
            "description": "LLM RAG",
            "date_posted": date.today(),
            "easy_apply": True,
        }
    ])

    main.job_cache.upsert_jobs(df, scraped_at=now)
    recent = main.job_cache.get_recent_jobs(24)

    assert len(recent) == 1
    assert bool(recent.iloc[0]["is_linkedin_easy_apply"])
    assert recent.iloc[0]["excluded_reason"] == "linkedin_easy_apply"


def test_linkedin_job_with_external_apply_url_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))
    now = datetime.now(timezone.utc).isoformat()
    df = pd.DataFrame([
        {
            "title": "AI Engineer",
            "company": "DirectLinked",
            "location": "Remote",
            "source": "linkedin",
            "source_type": "linkedin",
            "job_url": "https://www.linkedin.com/jobs/view/2",
            "apply_url": "https://directlinked.com/careers/ai-engineer",
            "description": "LLM RAG",
            "date_posted": date.today(),
        }
    ])

    main.job_cache.upsert_jobs(df, scraped_at=now)
    recent = main.job_cache.get_recent_jobs(24)

    assert len(recent) == 1
    assert not bool(recent.iloc[0]["is_linkedin_easy_apply"])


def test_stale_cache_refreshes_with_broad_stem_titles(monkeypatch, tmp_path):
    captured_terms = []

    def capture_scrape(location, is_remote, hours_old, on_progress=None, search_terms=None):
        captured_terms.append(list(search_terms or []))
        return _fake_scrape_all(location, is_remote, hours_old, on_progress, search_terms)

    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))
    monkeypatch.setattr(main.job_cache.scraper, "scrape_all", capture_scrape)
    monkeypatch.setattr(main.job_cache, "fetch_google_jobs", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(main.job_cache, "fetch_company_ats_jobs", lambda: pd.DataFrame())

    assert main.job_cache.is_cache_stale()
    result = main.job_cache.refresh_job_cache(force=False, location="United States", is_remote=False)

    assert result["status"] == "done"
    assert result["raw_count"] == 3
    assert main.job_cache.count_jobs() == 3
    assert main.job_cache.get_cache_age_minutes() is not None
    assert captured_terms == [main.config.DEFAULT_STEM_SEARCH_TITLES]


def test_greenhouse_lever_ashby_google_job_normalization():
    greenhouse = ats_sources.normalize_greenhouse_job({
        "title": "Applied AI Engineer",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
        "location": {"name": "Remote"},
        "content": "<p>Build LLM RAG systems.</p>",
        "updated_at": "2026-06-26T12:00:00Z",
    }, "Acme", "acme")
    lever = ats_sources.normalize_lever_job({
        "text": "ML Engineer",
        "hostedUrl": "https://jobs.lever.co/acme/2",
        "categories": {"location": "New York, NY"},
        "lists": [{"content": "<div>Deploy ML models.</div>"}],
    }, "Acme")
    ashby = ats_sources.normalize_ashby_job({
        "title": "GenAI Engineer",
        "jobUrl": "https://jobs.ashbyhq.com/acme/3",
        "locationName": "Remote - US",
        "descriptionHtml": "<p>Build agentic AI applications.</p>",
    }, "Acme")
    google = google_jobs_serpapi.normalize_google_job({
        "title": "LLM Engineer",
        "company_name": "Acme",
        "location": "Remote",
        "description": "RAG and vector search",
        "apply_options": [{"link": "https://acme.com/jobs/4"}],
    }, "LLM Engineer")

    assert greenhouse["source_type"] == "greenhouse"
    assert greenhouse["is_remote"] is True
    assert "Build LLM RAG" in greenhouse["description"]
    assert lever["source_type"] == "lever"
    assert lever["job_url"] == "https://jobs.lever.co/acme/2"
    assert ashby["source_type"] == "ashby"
    assert ashby["is_remote"] is True
    assert google["source_type"] == "google_jobs"
    assert google["apply_url"] == "https://acme.com/jobs/4"


def test_serpapi_posted_at_and_applicants_are_normalized():
    ref = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    row = google_jobs_serpapi.normalize_google_job({
        "title": "AI Engineer",
        "company_name": "Acme",
        "location": "Remote",
        "description": "Build LLM systems.",
        "detected_extensions": {
            "posted_at": "49 minutes ago",
            "applicants": "Less than 25 applicants",
        },
        "apply_options": [{"link": "https://acme.com/jobs/ai"}],
        "share_link": "https://www.google.com/search?ibp=htl;jobs",
    }, "AI Engineer", ref)

    assert row["posted_at_raw"] == "49 minutes ago"
    assert row["posted_age_minutes"] == 49
    assert row["posted_precision"] == "minute"
    assert row["posted_age_label"] == "Posted 49 min ago"
    assert row["applicants_label"] == "Less than 25 applicants"
    assert row["applicant_precision"] == "range"
    assert row["job_url"] == "https://acme.com/jobs/ai"


def test_missing_applicant_count_stays_null():
    signal = freshness.extract_applicant_signal({}, "Build AI systems.", {})

    assert signal["applicants_count"] is None
    assert signal["applicants_label"] is None
    assert signal["applicant_precision"] == "unknown"


def test_source_counts_are_stored_on_cache_refresh(monkeypatch, tmp_path):
    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))
    jobspy = pd.DataFrame([
        {
            "title": "AI Engineer",
            "company": "LinkedCo",
            "location": "Remote",
            "source": "linkedin",
            "source_type": "linkedin",
            "job_url": "http://linkedin/1",
            "description": "LLM RAG",
        },
        {
            "title": "ML Engineer",
            "company": "IndeedCo",
            "location": "Remote",
            "source": "indeed",
            "source_type": "indeed",
            "job_url": "http://indeed/2",
            "description": "ML systems",
        },
    ])
    google = pd.DataFrame([{
        "title": "GenAI Engineer",
        "company": "GoogleJobsCo",
        "location": "Remote",
        "source": "google_jobs",
        "source_type": "google_jobs",
        "job_url": "http://googlejobs/3",
        "description": "GenAI",
    }])
    ats = pd.DataFrame([
        {
            "title": "Applied AI Engineer",
            "company": "GreenCo",
            "location": "Remote",
            "source": "greenhouse",
            "source_type": "greenhouse",
            "job_url": "http://greenhouse/4",
            "apply_url": "http://greenhouse/4",
            "description": "Applied AI",
        },
        {
            "title": "LLM Engineer",
            "company": "LeverCo",
            "location": "Remote",
            "source": "lever",
            "source_type": "lever",
            "job_url": "http://lever/5",
            "apply_url": "http://lever/5",
            "description": "LLM",
        },
    ])

    monkeypatch.setattr(main.job_cache, "fetch_jobspy_jobs", lambda *args, **kwargs: jobspy)
    monkeypatch.setattr(main.job_cache, "fetch_google_jobs", lambda *args, **kwargs: google)
    monkeypatch.setattr(main.job_cache, "fetch_company_ats_jobs", lambda: ats)

    result = main.job_cache.refresh_job_cache(force=True)
    counts = result["source_counts"]

    assert result["status"] == "done"
    assert counts["linkedin_count"] == 1
    assert counts["indeed_count"] == 1
    assert counts["google_jobs_count"] == 1
    assert counts["greenhouse_count"] == 1
    assert counts["lever_count"] == 1
    assert counts["total_cache_jobs"] == 5
    assert main.job_cache.get_latest_source_counts()["greenhouse_count"] == 1


def test_direct_ats_url_wins_over_indeed_duplicate():
    df = pd.DataFrame([
        {
            "title": "Applied AI Engineer",
            "company": "Acme",
            "location": "Remote",
            "source": "indeed",
            "source_type": "indeed",
            "job_url": "https://indeed.com/viewjob?jk=1",
            "apply_url": "https://indeed.com/viewjob?jk=1",
        },
        {
            "title": "Applied AI Engineer",
            "company": "Acme",
            "location": "Remote",
            "source": "greenhouse",
            "source_type": "greenhouse",
            "job_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        },
    ])

    deduped = main.job_cache.dedupe_prefer_sources(df)
    assert len(deduped) == 1
    assert deduped.iloc[0]["source_type"] == "greenhouse"


def test_direct_ats_url_wins_over_linkedin_duplicate():
    df = pd.DataFrame([
        {
            "title": "Applied AI Engineer",
            "company": "Acme",
            "location": "Remote",
            "source": "linkedin",
            "source_type": "linkedin",
            "job_url": "https://www.linkedin.com/jobs/view/1",
            "apply_url": "https://www.linkedin.com/jobs/view/1",
        },
        {
            "title": "Applied AI Engineer",
            "company": "Acme",
            "location": "Remote",
            "source": "greenhouse",
            "source_type": "greenhouse",
            "job_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        },
    ])

    deduped = main.job_cache.dedupe_prefer_sources(df)
    assert len(deduped) == 1
    assert deduped.iloc[0]["source_type"] == "greenhouse"


def test_job_cache_prunes_jobs_older_than_72h(monkeypatch, tmp_path):
    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))
    now = datetime.now(timezone.utc)
    df = pd.DataFrame([
        {
            "title": "Fresh AI Engineer",
            "company": "FreshCo",
            "location": "Remote",
            "source": "greenhouse",
            "source_type": "greenhouse",
            "job_url": "http://prune/fresh",
            "date_posted": date.today(),
            "description": "LLM RAG",
        },
        {
            "title": "Old AI Engineer",
            "company": "OldCo",
            "location": "Remote",
            "source": "greenhouse",
            "source_type": "greenhouse",
            "job_url": "http://prune/old",
            "date_posted": date.today() - timedelta(days=4),
            "description": "LLM RAG",
        },
    ])

    main.job_cache.upsert_jobs(df, scraped_at=now.isoformat())
    pruned = main.job_cache.prune_old_jobs(72)
    recent = main.job_cache.get_recent_jobs(72)

    assert pruned == 1
    assert set(recent["job_url"]) == {"http://prune/fresh"}


def test_unknown_date_job_older_by_scraped_at_is_pruned(monkeypatch, tmp_path):
    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))
    old_scrape = (datetime.now(timezone.utc) - timedelta(hours=80)).isoformat()
    df = pd.DataFrame([
        {
            "title": "Unknown Date AI Engineer",
            "company": "OldUnknown",
            "location": "Remote",
            "source": "greenhouse",
            "source_type": "greenhouse",
            "job_url": "http://prune/unknown",
            "date_posted": None,
            "description": "LLM RAG",
        }
    ])

    main.job_cache.upsert_jobs(df, scraped_at=old_scrape)
    pruned = main.job_cache.prune_old_jobs(72)

    assert pruned == 1
    assert main.job_cache.count_jobs() == 0


def test_source_failure_does_not_fail_full_cache_refresh(monkeypatch, tmp_path):
    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))

    def failing_jobspy(*args, **kwargs):
        raise RuntimeError("linkedin rate limited")

    ats = pd.DataFrame([{
        "title": "Applied AI Engineer",
        "company": "GreenCo",
        "location": "Remote",
        "source": "greenhouse",
        "source_type": "greenhouse",
        "job_url": "http://greenhouse/fallback",
        "description": "LLM RAG",
    }])
    monkeypatch.setattr(main.job_cache, "fetch_jobspy_jobs", failing_jobspy)
    monkeypatch.setattr(main.job_cache, "fetch_google_jobs", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(main.job_cache, "fetch_company_ats_jobs", lambda: ats)

    result = main.job_cache.refresh_job_cache(force=True)
    assert result["status"] == "done"
    assert result["raw_count"] == 1
    assert result["source_counts"]["greenhouse_count"] == 1


def test_cache_refresh_not_dominated_by_indeed_when_ats_returns_jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(main.job_cache, "_DB_PATH", str(tmp_path / "job_cache.db"))
    indeed = pd.DataFrame([{
        "title": "AI Engineer",
        "company": "IndeedOnly",
        "location": "Remote",
        "source": "indeed",
        "source_type": "indeed",
        "job_url": "http://indeed/only",
        "description": "AI",
    }])
    ats = pd.DataFrame([
        {
            "title": "Applied AI Engineer",
            "company": "GreenCo",
            "location": "Remote",
            "source": "greenhouse",
            "source_type": "greenhouse",
            "job_url": "http://greenhouse/1",
            "description": "LLM",
        },
        {
            "title": "ML Engineer",
            "company": "LeverCo",
            "location": "Remote",
            "source": "lever",
            "source_type": "lever",
            "job_url": "http://lever/2",
            "description": "ML",
        },
    ])
    monkeypatch.setattr(main.job_cache, "fetch_jobspy_jobs", lambda *args, **kwargs: indeed)
    monkeypatch.setattr(main.job_cache, "fetch_google_jobs", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(main.job_cache, "fetch_company_ats_jobs", lambda: ats)

    result = main.job_cache.refresh_job_cache(force=True)
    counts = result["source_counts"]
    assert counts["indeed_count"] == 1
    assert counts["greenhouse_count"] + counts["lever_count"] == 2
    assert counts["indeed_count"] < counts["total_cache_jobs"]


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
    assert "Seen filter disabled" in body["message"]
    assert "previously seen jobs" not in body["message"]


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


def test_role_relevance_gate_for_applied_ai_targets():
    profile = main.config.DEFAULT_TARGET_PROFILE
    cases = [
        (
            {
                "title": "Applied AI Engineer",
                "description": "Build LLM RAG systems with embeddings, vector search, Python, and MLOps.",
            },
            False,
            "applied_ai_ml",
        ),
        (
            {
                "title": "GenAI Engineer",
                "description": "Deploy agentic AI workflows and LLM applications with LangChain.",
            },
            False,
            "applied_ai_ml",
        ),
        (
            {
                "title": "ML Engineer",
                "description": "Train and deploy machine learning models with PyTorch and model serving.",
            },
            False,
            "applied_ai_ml",
        ),
        (
            {
                "title": "Data Engineer",
                "description": "Build SQL ETL pipelines and dashboards for finance reporting.",
            },
            True,
            "data_engineering",
        ),
        (
            {
                "title": "Data Engineer",
                "description": "Build feature engineering pipelines, vector search, and MLOps model deployment.",
            },
            False,
            "data_engineering",
        ),
        (
            {
                "title": "Pharma Technology Consultant Manager",
                "description": "Lead client advisory teams and technology transformation programs.",
            },
            True,
            "consulting",
        ),
        (
            {
                "title": "Splunk Engineer",
                "description": "Own observability dashboards, alerts, and admin operations.",
            },
            True,
            "infra_admin",
        ),
    ]

    for job, excluded, family in cases:
        result = main.scoring.classify_job_relevance(job, profile)
        assert result["exclude_by_default"] is excluded, (job, result)
        assert result["job_family"] == family


def test_duplicate_title_company_location_is_deduped():
    df = pd.DataFrame([
        {
            "title": "Pharma Technology Consultant Manager",
            "company": "PwC",
            "location": "New York, NY",
            "job_url": "http://dup/1",
        },
        {
            "title": "Pharma Technology Consultant Manager",
            "company": "PWC",
            "location": "New York NY",
            "job_url": "http://dup/2",
        },
        {
            "title": "Applied AI Engineer",
            "company": "Acme",
            "location": "Remote",
            "job_url": "http://dup/3",
        },
    ])

    deduped = main.scoring.dedupe_display_jobs(df)
    assert len(deduped) == 2
    assert "http://dup/1" in set(deduped["job_url"])
    assert "http://dup/3" in set(deduped["job_url"])


def test_pipeline_separates_strong_and_low_confidence_matches(monkeypatch):
    jobs = pd.DataFrame([
        {
            "title": "Applied AI Engineer",
            "company": "Acme",
            "location": "Remote",
            "date_posted": date.today(),
            "is_remote": True,
            "job_url": "http://rel/ai",
            "description": "Build LLM RAG systems with embeddings vector search Python MLOps.",
        },
        {
            "title": "Data Engineer",
            "company": "PipelinesCo",
            "location": "Remote",
            "date_posted": date.today(),
            "is_remote": True,
            "job_url": "http://rel/de",
            "description": "Build SQL ETL pipelines and warehouse models for dashboards.",
        },
        {
            "title": "Pharma Technology Consultant Manager",
            "company": "PwC",
            "location": "New York, NY",
            "date_posted": date.today(),
            "is_remote": False,
            "job_url": "http://rel/pharma",
            "description": "Lead advisory technology transformation for pharma clients.",
        },
    ])

    def score_by_title(df, resume_text, skill_signals, total_yoe, client, *args):
        df = df.copy()
        scores = {"http://rel/ai": 82, "http://rel/de": 85, "http://rel/pharma": 90}
        df["ats_score"] = df["job_url"].map(scores).fillna(20).astype(int)
        df["missing_keywords"] = [[] for _ in range(len(df))]
        return df

    _install_cache_fakes(monkeypatch, get_recent_jobs=lambda hours_old: jobs, count_jobs=len(jobs))
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
    monkeypatch.setattr(main.resume_parser, "extract_keywords", lambda text, client: _FAKE_KW)
    monkeypatch.setattr(main.scoring, "score_with_claude", score_by_title)

    with TestClient(main.app) as c:
        r = c.post(
            "/api/analyze",
            files={"resume": ("resume.txt", b"Applied AI LLM RAG MLOps Python", "text/plain")},
            data={"location": "United States", "is_remote": "false", "hours_old": "24"},
        )
        assert r.status_code == 200, r.text
        body = _wait_for_done(c, r.json()["job_id"])

    assert body["status"] == "done", body
    assert [row["job_url"] for row in body["results"]] == ["http://rel/ai"]
    low_urls = {row["job_url"] for row in body["low_confidence_results"]}
    assert "http://rel/ai" not in low_urls
    assert "http://rel/de" not in {row["job_url"] for row in body["results"]}
    assert "http://rel/pharma" not in {row["job_url"] for row in body["results"]}
    assert "strong AI/ML" in body["message"]


def test_seen_logic_disabled_does_not_mark_displayed_jobs(monkeypatch):
    marked_urls = []
    jobs = pd.DataFrame([
        {
            "title": "Applied AI Engineer",
            "company": "Acme",
            "location": "Remote",
            "date_posted": date.today(),
            "is_remote": True,
            "job_url": "http://seen/ai",
            "description": "Build LLM RAG systems with embeddings vector search Python MLOps.",
        },
        {
            "title": "Splunk Engineer",
            "company": "OpsCo",
            "location": "Remote",
            "date_posted": date.today(),
            "is_remote": True,
            "job_url": "http://seen/splunk",
            "description": "Splunk observability dashboards alerts and administration.",
        },
    ])

    def fake_mark_seen(df, resume_hash=None):
        marked_urls.extend(df["job_url"].tolist())

    _install_cache_fakes(monkeypatch, get_recent_jobs=lambda hours_old: jobs, count_jobs=len(jobs))
    monkeypatch.setattr(main.dedup, "mark_seen", fake_mark_seen)
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
    monkeypatch.setattr(main.resume_parser, "extract_keywords", lambda text, client: _FAKE_KW)
    monkeypatch.setattr(main.scoring, "score_with_claude", _fake_score_with_claude)

    with TestClient(main.app) as c:
        r = c.post(
            "/api/analyze",
            files={"resume": ("resume.txt", b"Applied AI LLM RAG MLOps Python", "text/plain")},
            data={"location": "United States", "is_remote": "false", "hours_old": "24"},
        )
        assert r.status_code == 200, r.text
        body = _wait_for_done(c, r.json()["job_id"])

    assert body["status"] == "done", body
    assert marked_urls == []
    assert "Seen filter disabled" in body["message"]


def _run_with_cached_jobs(monkeypatch, jobs: pd.DataFrame, score_map: dict[str, int] | None = None):
    def score_jobs(df, resume_text, skill_signals, total_yoe, client, *args):
        df = df.copy()
        scores = score_map or {}
        df["ats_score"] = df["job_url"].map(scores).fillna(90).astype(int)
        df["missing_keywords"] = [[] for _ in range(len(df))]
        return df

    _install_cache_fakes(monkeypatch, get_recent_jobs=lambda hours_old: jobs, count_jobs=len(jobs))
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
    monkeypatch.setattr(main.resume_parser, "extract_keywords", lambda text, client: _FAKE_KW)
    monkeypatch.setattr(main.scoring, "score_with_claude", score_jobs)

    with TestClient(main.app) as c:
        r = c.post(
            "/api/analyze",
            files={"resume": ("resume.txt", b"Applied AI LLM RAG MLOps Python", "text/plain")},
            data={"location": "United States", "is_remote": "false", "hours_old": "24"},
        )
        assert r.status_code == 200, r.text
        return _wait_for_done(c, r.json()["job_id"])


def _many_good_jobs(n: int = 35) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "title": f"Applied AI Engineer {i}",
            "company": f"GoodCo {i}",
            "location": "Remote",
            "date_posted": date.today(),
            "is_remote": True,
            "job_url": f"http://limit/{i}",
            "description": "Build LLM RAG systems with embeddings vector search Python MLOps.",
        }
        for i in range(n)
    ])


def _run_limit_case(monkeypatch, data: dict) -> dict:
    jobs = _many_good_jobs()
    _install_cache_fakes(monkeypatch, get_recent_jobs=lambda hours_old: jobs, count_jobs=len(jobs))
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
    monkeypatch.setattr(main.resume_parser, "extract_keywords", lambda text, client: _FAKE_KW)
    monkeypatch.setattr(main.scoring, "score_with_claude", _fake_score_with_claude)

    payload = {"location": "United States", "is_remote": "false", "hours_old": "24"}
    payload.update(data)
    with TestClient(main.app) as c:
        r = c.post(
            "/api/analyze",
            files={"resume": ("resume.txt", b"Applied AI LLM RAG MLOps Python", "text/plain")},
            data=payload,
        )
        assert r.status_code == 200, r.text
        return _wait_for_done(c, r.json()["job_id"])


def test_missing_result_limit_defaults_to_10(monkeypatch):
    body = _run_limit_case(monkeypatch, {})

    assert body["status"] == "done", body
    assert len(body["results"]) <= 10
    assert len(body["results"]) == 10
    assert "Result limit: 10" in body["message"]


def test_invalid_result_limit_falls_back_to_10(monkeypatch):
    body = _run_limit_case(monkeypatch, {"result_limit": "not-a-number"})

    assert body["status"] == "done", body
    assert len(body["results"]) == 10
    assert "Result limit: 10" in body["message"]


def test_result_limit_20_returns_at_most_20(monkeypatch):
    body = _run_limit_case(monkeypatch, {"result_limit": "20"})

    assert body["status"] == "done", body
    assert len(body["results"]) == 20
    assert "Result limit: 20" in body["message"]


def test_result_limit_30_returns_at_most_30(monkeypatch):
    body = _run_limit_case(monkeypatch, {"result_limit": "30"})

    assert body["status"] == "done", body
    assert len(body["results"]) == 30
    assert "Result limit: 30" in body["message"]


def test_jobs_older_than_72h_excluded_from_main_and_broader_results(monkeypatch):
    jobs = pd.DataFrame([
        {
            "title": "Applied AI Engineer",
            "company": "FreshCo",
            "location": "Remote",
            "date_posted": date.today(),
            "job_url": "http://fresh/ai",
            "description": "Build LLM RAG systems with MLOps.",
        },
        {
            "title": "Applied AI Engineer",
            "company": "OldCo",
            "location": "Remote",
            "date_posted": date.today() - timedelta(days=4),
            "job_url": "http://old/ai",
            "description": "Build LLM RAG systems with MLOps.",
        },
    ])

    body = _run_with_cached_jobs(monkeypatch, jobs)
    assert body["status"] == "done", body
    urls = {row["job_url"] for row in body["results"]}
    broader_urls = {row["job_url"] for row in body["low_confidence_results"]}
    assert "http://fresh/ai" in urls
    assert "http://old/ai" not in urls
    assert "http://old/ai" not in broader_urls


def test_linkedin_easy_apply_jobs_excluded_from_main_and_broader_results(monkeypatch):
    jobs = pd.DataFrame([
        {
            "title": "Applied AI Engineer",
            "company": "LinkedOnly",
            "location": "Remote",
            "date_posted": date.today(),
            "job_url": "https://www.linkedin.com/jobs/view/1",
            "apply_url": "https://www.linkedin.com/jobs/view/1",
            "source": "linkedin",
            "source_type": "linkedin",
            "is_linkedin_easy_apply": True,
            "description": "Build LLM RAG systems with MLOps.",
        },
        {
            "title": "Applied AI Engineer",
            "company": "DirectCo",
            "location": "Remote",
            "date_posted": date.today(),
            "job_url": "https://direct.co/jobs/ai",
            "apply_url": "https://direct.co/jobs/ai",
            "source": "greenhouse",
            "source_type": "greenhouse",
            "is_linkedin_easy_apply": False,
            "description": "Build LLM RAG systems with MLOps.",
        },
    ])

    body = _run_with_cached_jobs(monkeypatch, jobs)
    urls = {row["job_url"] for row in body["results"]}
    broader_urls = {row["job_url"] for row in body["low_confidence_results"]}
    assert "https://direct.co/jobs/ai" in urls
    assert "https://www.linkedin.com/jobs/view/1" not in urls
    assert "https://www.linkedin.com/jobs/view/1" not in broader_urls
    assert "LinkedIn Easy Apply jobs excluded: 1" in body["message"]


def test_unknown_date_jobs_need_80_plus_for_main_results(monkeypatch):
    jobs = pd.DataFrame([
        {
            "title": "Applied AI Engineer",
            "company": "UnknownOkay",
            "location": "Remote",
            "date_posted": None,
            "job_url": "http://unknown/strong",
            "description": "Build LLM RAG systems with MLOps.",
        },
        {
            "title": "Applied AI Engineer",
            "company": "UnknownBroad",
            "location": "Remote",
            "date_posted": None,
            "job_url": "http://unknown/broad",
            "description": "Build LLM RAG systems with MLOps.",
        },
    ])

    body = _run_with_cached_jobs(
        monkeypatch,
        jobs,
        {"http://unknown/strong": 95, "http://unknown/broad": 60},
    )
    urls = {row["job_url"] for row in body["results"]}
    assert "http://unknown/strong" in urls
    assert "http://unknown/broad" not in urls
    assert all(row["freshness_bucket"] == "unknown" for row in body["results"])


def test_internship_jobs_are_excluded_by_default(monkeypatch):
    jobs = pd.DataFrame([
        {
            "title": "AI Engineer Internship",
            "company": "InternCo",
            "location": "Remote",
            "date_posted": date.today(),
            "job_url": "http://intern/ai",
            "description": "Build LLM RAG systems with MLOps.",
        },
        {
            "title": "GenAI Engineer",
            "company": "GenCo",
            "location": "Remote",
            "date_posted": date.today(),
            "job_url": "http://genai/ai",
            "description": "Build LLM RAG systems.",
        },
    ])

    body = _run_with_cached_jobs(monkeypatch, jobs)
    urls = {row["job_url"] for row in body["results"]}
    assert "http://genai/ai" in urls
    assert "http://intern/ai" not in urls


def test_mlops_and_ai_data_engineering_recent_jobs_can_appear(monkeypatch):
    jobs = pd.DataFrame([
        {
            "title": "MLOps Engineer",
            "company": "OpsAI",
            "location": "Remote",
            "date_posted": date.today(),
            "job_url": "http://mlops/ai",
            "description": "Deploy model serving, inference, and MLOps platforms.",
        },
        {
            "title": "Data Engineer",
            "company": "FeatureCo",
            "location": "Remote",
            "date_posted": date.today(),
            "job_url": "http://data/ml",
            "description": "Build ML pipelines, MLOps, RAG, feature engineering, vector search, and model deployment.",
        },
    ])

    body = _run_with_cached_jobs(monkeypatch, jobs, {"http://mlops/ai": 88, "http://data/ml": 82})
    urls = {row["job_url"] for row in body["results"]}
    assert "http://mlops/ai" in urls
    assert "http://data/ml" in urls


def test_does_not_fill_top_10_with_bad_jobs(monkeypatch):
    rows = [{
        "title": "Applied AI Engineer",
        "company": "StrongCo",
        "location": "Remote",
        "date_posted": date.today(),
        "job_url": "http://one/strong",
        "description": "Build LLM RAG systems with MLOps.",
    }]
    for i in range(12):
        rows.append({
            "title": f"Business Analyst {i}",
            "company": "BadCo",
            "location": "Remote",
            "date_posted": date.today(),
            "job_url": f"http://bad/{i}",
            "description": "Reporting dashboards and stakeholder analysis.",
        })
    body = _run_with_cached_jobs(monkeypatch, pd.DataFrame(rows))

    assert body["status"] == "done", body
    assert [row["job_url"] for row in body["results"]] == ["http://one/strong"]


def test_frontend_default_min_score_is_65():
    frontend_path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "ResultsTable.jsx"
    with open(frontend_path, encoding="utf-8") as f:
        text = f.read()
    assert "useState(65)" in text
    assert "ATS match" in text


def test_frontend_sends_selected_result_limit():
    root = Path(__file__).resolve().parents[2]
    with open(root / "frontend" / "src" / "api.js", encoding="utf-8") as f:
        api_text = f.read()
    with open(root / "frontend" / "src" / "components" / "UploadForm.jsx", encoding="utf-8") as f:
        upload_text = f.read()
    with open(root / "frontend" / "src" / "components" / "ReadyToSearch.jsx", encoding="utf-8") as f:
        ready_text = f.read()

    assert "resultLimit = 10" in api_text
    assert 'form.append("result_limit", resultLimit)' in api_text
    assert "useState(10)" in upload_text
    assert "useState(10)" in ready_text
    assert "20 jobs" in upload_text
    assert "30 jobs" in ready_text


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

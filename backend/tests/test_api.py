"""Exercises the real FastAPI lifecycle with mocked scraper, dedup, resume store,
and Claude scoring — no internet access or API keys needed.
Run with: pytest tests/test_api.py -v
"""
import time
from datetime import date, timedelta
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import main


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


def _fake_score_with_claude(df, resume_text, client):
    """Returns all rows with claude_score=80, sorted descending (no threshold, no slice)."""
    if df.empty:
        return df
    df = df.copy()
    df["claude_score"] = 80
    return df.sort_values("claude_score", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Base fixture used by existing tests
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.scraper, "scrape_all", _fake_scrape_all)
    monkeypatch.setattr(main.dedup, "filter_unseen", lambda df: df)
    monkeypatch.setattr(main.dedup, "mark_seen", lambda df: None)
    monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
    monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
    monkeypatch.setattr(main.resume_parser, "extract_keywords",
                        lambda text, client: ["AI Engineer", "ML Engineer"])
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
    assert len(results) == 2, f"expected 2 (sales role filtered out by prefilter), got {len(results)}"
    assert results[0]["title"] == "Senior AI Engineer"

    r = client.get(f"/api/analyze/{job_id}/export.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert len(r.content) > 1000


def test_unknown_job_returns_404(client):
    r = client.get("/api/analyze/doesnotexist")
    assert r.status_code == 404


def test_results_sorted_by_claude_score(monkeypatch):
    """Results must be sorted descending by claude_score even when scores are low
    (i.e. below what used to be the 75/50 thresholds that no longer exist)."""

    def low_score_claude(df, resume_text, client):
        if df.empty:
            return df
        df = df.copy()
        # Assign scores below the old 75/50 cutoffs — both should still appear
        scores = {"http://x/1": 45, "http://x/2": 30}
        df["claude_score"] = df["job_url"].map(scores).fillna(0).astype(int)
        return df.sort_values("claude_score", ascending=False).reset_index(drop=True)

    with TestClient(main.app) as c:
        monkeypatch.setattr(main.scraper, "scrape_all", _fake_scrape_all)
        monkeypatch.setattr(main.dedup, "filter_unseen", lambda df: df)
        monkeypatch.setattr(main.dedup, "mark_seen", lambda df: None)
        monkeypatch.setattr(main.resume_store, "load_resume", lambda: None)
        monkeypatch.setattr(main.resume_store, "save_resume", lambda **kw: None)
        monkeypatch.setattr(main.resume_parser, "extract_keywords",
                            lambda text, client: ["AI Engineer", "ML Engineer"])
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
        assert len(results) == 2, f"both jobs should appear with no score threshold, got {len(results)}"
        assert results[0]["claude_score"] == 45
        assert results[1]["claude_score"] == 30
        assert results[0]["title"] == "Senior AI Engineer"


def test_resume_reuse(monkeypatch):
    """Second upload with same filename must skip extract_keywords (use cached resume)."""
    extract_calls = []
    stored_resume: dict = {}

    def fake_extract_keywords(text, client):
        extract_calls.append(text)
        return ["AI Engineer", "ML Engineer"]

    def fake_save_resume(**kw):
        stored_resume.update(kw)

    def fake_load_resume():
        if stored_resume.get("filename"):
            return {
                "filename": stored_resume["filename"],
                "text": stored_resume["text"],
                "keywords": stored_resume["keywords"],
                "email": stored_resume.get("email"),
                "phone": stored_resume.get("phone"),
            }
        return None

    monkeypatch.setattr(main.scraper, "scrape_all", _fake_scrape_all)
    monkeypatch.setattr(main.dedup, "filter_unseen", lambda df: df)
    monkeypatch.setattr(main.dedup, "mark_seen", lambda df: None)
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

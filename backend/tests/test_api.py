"""Exercises the real FastAPI lifecycle with a mocked scraper (this sandbox
has no internet access to LinkedIn/Indeed), to verify upload -> background
task -> polling -> scoring -> xlsx export all work end to end.
Run with: pytest tests/test_api.py -v
"""
import time
from datetime import date, timedelta
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import main


def _fake_scrape_all(location, is_remote, hours_old, on_progress=None):
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


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.scraper, "scrape_all", _fake_scrape_all)
    with TestClient(main.app) as c:
        yield c


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
    assert len(results) == 2, f"expected 2 (sales role filtered out), got {len(results)}"
    assert results[0]["title"] == "Senior AI Engineer"

    r = client.get(f"/api/analyze/{job_id}/export.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert len(r.content) > 1000


def test_unknown_job_returns_404(client):
    r = client.get("/api/analyze/doesnotexist")
    assert r.status_code == 404


"""Arbeitnow public job board API — no API key required.

API: https://www.arbeitnow.com/api/job-board-api
Returns English-language, primarily remote tech jobs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import requests

from .. import config, freshness

_API_URL = "https://www.arbeitnow.com/api/job-board-api"

_AI_TERMS = [
    "machine learning", "ml engineer", " ai ", "ai engineer", "artificial intelligence",
    "llm", "large language model", "genai", "generative ai", " rag ",
    "mlops", "data scientist", "applied scientist", "deep learning",
    "nlp", "computer vision", "pytorch", "tensorflow", "langchain",
    "embeddings", "vector database", "model training", "model serving",
    "agentic", "fine-tuning", "fine tuning",
]


def _is_ai_relevant(title: str, description: str) -> bool:
    text = (" " + title + " " + description + " ").lower()
    return any(term in text for term in _AI_TERMS)


def normalize_arbeitnow_job(job: dict, scraped_at: datetime | None = None) -> dict | None:
    """Normalize one Arbeitnow API record to the shared job schema.
    Returns None if not AI/ML relevant."""
    scraped_at = scraped_at or datetime.now(timezone.utc)
    title = str(job.get("title") or "")
    description = str(job.get("description") or "")
    if not _is_ai_relevant(title, description):
        return None

    # created_at is a Unix timestamp integer
    created_ts = job.get("created_at")
    posted: dict = {}
    if created_ts:
        try:
            dt = datetime.fromtimestamp(int(created_ts), tz=timezone.utc)
            posted = freshness.normalize_posted_fields(
                {"date_posted": dt.isoformat()}, scraped_at
            )
        except (TypeError, ValueError, OSError):
            posted = {}

    job_url = str(job.get("url") or "")
    return {
        "source": "arbeitnow",
        "source_type": "arbeitnow",
        "title": title,
        "company": str(job.get("company_name") or ""),
        "location": str(job.get("location") or ""),
        "is_remote": bool(job.get("remote")),
        "date_posted": posted.get("posted_at_ts"),
        "posted_at_raw": posted.get("posted_at_raw"),
        "posted_at_ts": posted.get("posted_at_ts"),
        "posted_age_minutes": posted.get("posted_age_minutes"),
        "posted_age_label": posted.get("posted_age_label"),
        "posted_precision": posted.get("posted_precision"),
        "freshness_bucket": posted.get("freshness_bucket"),
        "job_url": job_url,
        "apply_url": job_url,
        "description": description,
        "raw_json": job,
    }


def fetch_arbeitnow_jobs(
    timeout: int = config.SOURCE_REQUEST_TIMEOUT_SECONDS,
) -> pd.DataFrame:
    if not config.ENABLE_ARBEITNOW:
        return pd.DataFrame()
    try:
        resp = requests.get(_API_URL, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return pd.DataFrame()

    rows: list[dict] = []
    scraped_at = datetime.now(timezone.utc)
    for job in data.get("data", []) or []:
        if not isinstance(job, dict):
            continue
        row = normalize_arbeitnow_job(job, scraped_at)
        if row is not None:
            row["scraped_at"] = scraped_at.isoformat()
            rows.append(row)
    return pd.DataFrame(rows)

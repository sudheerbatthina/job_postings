"""Adzuna job board API.

Requires ADZUNA_APP_ID and ADZUNA_APP_KEY env vars — skips gracefully if missing.
API docs: https://developer.adzuna.com/
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
import requests

from .. import config, freshness

_API_BASE = "https://api.adzuna.com/v1/api/jobs"

_AI_TERMS = [
    "machine learning", "ml engineer", "ai engineer", "llm", "large language model",
    "genai", "generative ai", " rag ", "mlops", "applied scientist", "deep learning",
    "nlp", "computer vision", "pytorch", "tensorflow", "langchain", "embeddings",
    "agentic", "fine-tuning", "fine tuning", "vector database",
]


def _is_ai_relevant(title: str, description: str) -> bool:
    text = (" " + title + " " + description + " ").lower()
    return any(term in text for term in _AI_TERMS)


def normalize_adzuna_job(job: dict, scraped_at: datetime | None = None) -> dict | None:
    """Normalize one Adzuna API result record to the shared job schema.
    Returns None if not AI/ML relevant."""
    scraped_at = scraped_at or datetime.now(timezone.utc)
    title = str(job.get("title") or "")
    description = str(job.get("description") or "")
    if not _is_ai_relevant(title, description):
        return None

    # Adzuna provides ISO 8601 string in `created`
    created = job.get("created")
    posted: dict = {}
    if created:
        try:
            posted = freshness.normalize_posted_fields(
                {"date_posted": str(created)}, scraped_at
            )
        except Exception:
            posted = {}

    company = job.get("company") or {}
    location = job.get("location") or {}
    company_name = str(company.get("display_name") or "") if isinstance(company, dict) else ""
    location_name = str(location.get("display_name") or "") if isinstance(location, dict) else ""
    job_url = str(job.get("redirect_url") or "")

    return {
        "source": "adzuna",
        "source_type": "adzuna",
        "title": title,
        "company": company_name,
        "location": location_name,
        "is_remote": "remote" in title.lower() or "remote" in location_name.lower(),
        "date_posted": posted.get("posted_at_ts") or created,
        "posted_at_raw": posted.get("posted_at_raw"),
        "posted_at_ts": posted.get("posted_at_ts"),
        "posted_age_minutes": posted.get("posted_age_minutes"),
        "posted_age_label": posted.get("posted_age_label"),
        "posted_precision": posted.get("posted_precision"),
        "freshness_bucket": posted.get("freshness_bucket"),
        "job_url": job_url,
        "apply_url": job_url,
        "description": description,
        "min_amount": job.get("salary_min"),
        "max_amount": job.get("salary_max"),
        "raw_json": job,
    }


def fetch_adzuna_jobs(
    search_terms: list[str] | None = None,
    country: str = "us",
    timeout: int = config.SOURCE_REQUEST_TIMEOUT_SECONDS,
) -> pd.DataFrame:
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        return pd.DataFrame()

    terms = (search_terms or config.DEFAULT_STEM_SEARCH_TITLES)[:5]
    rows: list[dict] = []
    scraped_at = datetime.now(timezone.utc)
    for term in terms:
        try:
            resp = requests.get(
                f"{_API_BASE}/{country}/search/1",
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "what": term,
                    "results_per_page": 20,
                    "content-type": "application/json",
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue
        for job in data.get("results", []) or []:
            if not isinstance(job, dict):
                continue
            row = normalize_adzuna_job(job, scraped_at)
            if row is not None:
                row["scraped_at"] = scraped_at.isoformat()
                rows.append(row)
    return pd.DataFrame(rows)

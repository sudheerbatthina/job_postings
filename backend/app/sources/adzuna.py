"""Adzuna job board API.

Requires ADZUNA_APP_ID and ADZUNA_APP_KEY env vars — skips gracefully if missing.
API docs: https://developer.adzuna.com/
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone

import pandas as pd
import requests

from .. import config, freshness

_API_BASE = "https://api.adzuna.com/v1/api/jobs"
logger = logging.getLogger(__name__)

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
    logger.info(
        "adzuna credentials present: app_id=%s app_key=%s",
        bool(app_id),
        bool(app_key),
    )
    if not app_id or not app_key:
        logger.info("adzuna skipped: missing credentials")
        return pd.DataFrame()

    terms = list(dict.fromkeys(config.ADZUNA_SEARCH_TITLES + (search_terms or [])))
    rows: list[dict] = []
    scraped_at = datetime.now(timezone.utc)
    for term in terms:
        term_raw = 0
        term_rows = 0
        for page in range(1, config.ADZUNA_PAGES_PER_QUERY + 1):
            try:
                resp = requests.get(
                    f"{_API_BASE}/{country}/search/{page}",
                    params={
                        "app_id": app_id,
                        "app_key": app_key,
                        "what": term,
                        "sort_by": "date",
                        "results_per_page": config.ADZUNA_RESULTS_PER_PAGE,
                        "content-type": "application/json",
                    },
                    timeout=timeout,
                )
                logger.info("adzuna query=%r page=%s status=%s", term, page, resp.status_code)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning("adzuna query=%r page=%s failed: %s", term, page, e)
                continue
            page_results = data.get("results", []) or []
            term_raw += len(page_results)
            logger.info("adzuna query=%r page=%s raw_count=%s", term, page, len(page_results))
            for job in page_results:
                if not isinstance(job, dict):
                    continue
                row = normalize_adzuna_job(job, scraped_at)
                if row is not None:
                    row["scraped_at"] = scraped_at.isoformat()
                    rows.append(row)
                    term_rows += 1
            if not page_results:
                break
        logger.info("adzuna query=%r raw_count=%s normalized_count=%s", term, term_raw, term_rows)
    logger.info("adzuna total_normalized_count=%s", len(rows))
    df = pd.DataFrame(rows)
    if not df.empty and "posted_at_ts" in df.columns:
        df["_posted_sort"] = pd.to_datetime(df["posted_at_ts"], errors="coerce", utc=True)
        df = df.sort_values("_posted_sort", ascending=False, na_position="last").drop(columns=["_posted_sort"])
    return df.reset_index(drop=True)

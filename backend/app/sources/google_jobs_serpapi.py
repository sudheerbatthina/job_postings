"""Google Jobs ingestion through SerpAPI."""

from __future__ import annotations
import os
from datetime import datetime, timezone

import pandas as pd
import requests

from .. import config


def _first_apply_link(job: dict) -> str | None:
    for key in ("apply_options", "related_links"):
        links = job.get(key) or []
        if isinstance(links, list):
            for item in links:
                if isinstance(item, dict) and item.get("link"):
                    return item["link"]
    return job.get("share_link")


def normalize_google_job(job: dict, search_term: str | None = None) -> dict:
    apply_url = _first_apply_link(job)
    return {
        "source": "google_jobs",
        "source_type": "google_jobs",
        "title": job.get("title"),
        "company": job.get("company_name"),
        "location": job.get("location"),
        "is_remote": "remote" in str(job.get("location") or "").lower(),
        "date_posted": job.get("detected_extensions", {}).get("posted_at"),
        "job_url": apply_url or job.get("share_link") or job.get("job_id"),
        "apply_url": apply_url,
        "description": job.get("description") or "",
        "search_term": search_term,
        "raw_json": job,
    }


def fetch_google_jobs(
    search_terms: list[str],
    location: str = "United States",
    api_key: str | None = None,
    timeout: int = config.SOURCE_REQUEST_TIMEOUT_SECONDS,
) -> pd.DataFrame:
    api_key = api_key or os.environ.get("SERPAPI_API_KEY")
    if not api_key or not config.ENABLE_SERPAPI_GOOGLE_JOBS:
        return pd.DataFrame()

    rows: list[dict] = []
    for term in search_terms:
        params = {
            "engine": "google_jobs",
            "q": term,
            "location": location,
            "api_key": api_key,
        }
        try:
            resp = requests.get("https://serpapi.com/search.json", params=params, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            continue
        for job in payload.get("jobs_results", []) or []:
            if isinstance(job, dict):
                row = normalize_google_job(job, term)
                row["scraped_at"] = datetime.now(timezone.utc).isoformat()
                rows.append(row)
    return pd.DataFrame(rows)

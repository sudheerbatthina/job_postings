"""Google Jobs ingestion through SerpAPI."""

from __future__ import annotations
import logging
import os
from datetime import datetime, timezone

import pandas as pd
import requests

from .. import config
from .. import freshness

logger = logging.getLogger(__name__)


def _first_apply_link(job: dict) -> str | None:
    links = job.get("apply_options") or []
    if isinstance(links, list):
        for item in links:
            if isinstance(item, dict) and item.get("link"):
                return item["link"]
    links = job.get("related_links") or []
    if isinstance(links, list):
        for item in links:
            link = item.get("link") if isinstance(item, dict) else None
            if link and "google.com" not in str(link).lower():
                return link
    return job.get("share_link")


def normalize_google_job(job: dict, search_term: str | None = None, scraped_at: datetime | None = None) -> dict:
    scraped_at = scraped_at or datetime.now(timezone.utc)
    apply_url = _first_apply_link(job)
    extensions = job.get("detected_extensions", {}) or {}
    posted_raw = extensions.get("posted_at")
    posted = freshness.parse_relative_posted_at(posted_raw, scraped_at)
    if posted["posted_at_ts"] is None and posted_raw:
        posted = freshness.normalize_posted_fields({"posted_at_raw": posted_raw}, scraped_at)
    applicants = freshness.extract_applicant_signal(job, job.get("description") or "", extensions)
    return {
        "source": "google_jobs",
        "source_type": "google_jobs",
        "title": job.get("title"),
        "company": job.get("company_name"),
        "location": job.get("location"),
        "is_remote": "remote" in str(job.get("location") or "").lower(),
        "date_posted": posted.get("posted_at_ts") or posted_raw,
        "posted_at_raw": posted.get("posted_at_raw") or posted_raw,
        "posted_at_ts": posted.get("posted_at_ts"),
        "posted_age_minutes": posted.get("posted_age_minutes"),
        "posted_age_label": freshness.build_posted_age_label(
            posted.get("posted_at_ts"), posted.get("posted_precision"), scraped_at
        ),
        "posted_precision": posted.get("posted_precision"),
        "freshness_bucket": freshness.freshness_bucket(posted.get("posted_at_ts")),
        "job_url": apply_url or job.get("share_link") or job.get("job_id"),
        "apply_url": apply_url,
        "description": job.get("description") or "",
        "search_term": search_term,
        **applicants,
        "raw_json": job,
    }


def fetch_google_jobs(
    search_terms: list[str],
    location: str = "United States",
    api_key: str | None = None,
    timeout: int = config.SOURCE_REQUEST_TIMEOUT_SECONDS,
    no_cache: bool | None = None,
    pages_per_query: int | None = None,
) -> pd.DataFrame:
    api_key = api_key or os.environ.get("SERPAPI_API_KEY")
    if not api_key or not config.ENABLE_SERPAPI_GOOGLE_JOBS:
        return pd.DataFrame()

    no_cache = config.LIVE_FRESH_SEARCH if no_cache is None else bool(no_cache)
    pages_per_query = pages_per_query or config.SERPAPI_PAGES_PER_QUERY
    rows: list[dict] = []
    for term in search_terms:
        next_page_token = None
        term_raw = 0
        term_rows = 0
        for page in range(1, pages_per_query + 1):
            params = {
                "engine": "google_jobs",
                "q": term,
                "location": location,
                "api_key": api_key,
            }
            if no_cache:
                params["no_cache"] = "true"
            if next_page_token:
                params["next_page_token"] = next_page_token
            try:
                resp = requests.get("https://serpapi.com/search.json", params=params, timeout=timeout)
                logger.info("serpapi google_jobs query=%r page=%s status=%s no_cache=%s", term, page, resp.status_code, no_cache)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as e:
                logger.warning("serpapi google_jobs query=%r page=%s failed: %s", term, page, e)
                continue
            page_results = payload.get("jobs_results", []) or []
            term_raw += len(page_results)
            scraped_at = datetime.now(timezone.utc)
            for job in page_results:
                if isinstance(job, dict):
                    row = normalize_google_job(job, term, scraped_at)
                    row["scraped_at"] = scraped_at.isoformat()
                    rows.append(row)
                    term_rows += 1
            next_page_token = (payload.get("serpapi_pagination") or {}).get("next_page_token")
            if not next_page_token:
                break
        logger.info("serpapi google_jobs query=%r raw_count=%s normalized_count=%s", term, term_raw, term_rows)
    return pd.DataFrame(rows)

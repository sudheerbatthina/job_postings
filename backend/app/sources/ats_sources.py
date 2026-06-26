"""Public ATS/company portal ingestion adapters."""

from __future__ import annotations
import re
from datetime import datetime, timezone

import pandas as pd
import requests

from .. import config
from ..company_registry import COMPANIES


_AI_TERMS = [
    "ai", "machine learning", "ml ", "genai", "generative ai", "llm", "rag",
    "mlops", "model", "applied scientist", "data scientist", "nlp",
    "computer vision", "deep learning",
]


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text))).strip()


def _is_relevant_title_or_desc(title: str | None, description: str | None) -> bool:
    text = f"{title or ''} {description or ''}".lower()
    return any(term in text for term in _AI_TERMS)


def _remote_from_location(location: str | None) -> bool:
    return "remote" in str(location or "").lower()


def normalize_greenhouse_job(job: dict, company: str, slug: str | None = None) -> dict:
    location = (job.get("location") or {}).get("name") if isinstance(job.get("location"), dict) else job.get("location")
    absolute_url = job.get("absolute_url")
    return {
        "source": "greenhouse",
        "source_type": "greenhouse",
        "title": job.get("title"),
        "company": company,
        "location": location,
        "is_remote": _remote_from_location(location),
        "date_posted": job.get("updated_at") or job.get("created_at"),
        "job_url": absolute_url or f"https://boards.greenhouse.io/{slug or company}",
        "apply_url": absolute_url,
        "description": _strip_html(job.get("content")),
        "raw_json": job,
    }


def normalize_lever_job(job: dict, company: str) -> dict:
    categories = job.get("categories") or {}
    location = categories.get("location") or job.get("workplaceType")
    apply_url = job.get("hostedUrl") or job.get("applyUrl")
    description = " ".join(
        _strip_html(section.get("content"))
        for section in (job.get("lists") or [])
        if isinstance(section, dict)
    )
    return {
        "source": "lever",
        "source_type": "lever",
        "title": job.get("text"),
        "company": company,
        "location": location,
        "is_remote": _remote_from_location(f"{location} {job.get('workplaceType')}"),
        "date_posted": None,
        "job_url": apply_url or job.get("id"),
        "apply_url": apply_url,
        "description": description or _strip_html(job.get("description")),
        "raw_json": job,
    }


def normalize_ashby_job(job: dict, company: str) -> dict:
    location = job.get("locationName") or job.get("location")
    apply_url = job.get("jobUrl") or job.get("hostedUrl") or job.get("applyUrl")
    return {
        "source": "ashby",
        "source_type": "ashby",
        "title": job.get("title"),
        "company": company,
        "location": location,
        "is_remote": _remote_from_location(location),
        "date_posted": job.get("publishedAt"),
        "job_url": apply_url or job.get("id"),
        "apply_url": apply_url,
        "description": _strip_html(job.get("descriptionHtml") or job.get("descriptionPlain")),
        "raw_json": job,
    }


def _company_rows(ats: str) -> list[dict]:
    return [company for company in COMPANIES if company.get("ats") == ats and company.get("slug")]


def _fetch_json(url: str, timeout: int) -> dict | list | None:
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def fetch_greenhouse_jobs(timeout: int = config.SOURCE_REQUEST_TIMEOUT_SECONDS) -> pd.DataFrame:
    rows = []
    for company in _company_rows("greenhouse"):
        slug = company["slug"]
        payload = _fetch_json(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
            timeout,
        )
        for job in (payload or {}).get("jobs", []) if isinstance(payload, dict) else []:
            row = normalize_greenhouse_job(job, company["company"], slug)
            if _is_relevant_title_or_desc(row["title"], row["description"]):
                rows.append(row)
    return pd.DataFrame(rows)


def fetch_lever_jobs(timeout: int = config.SOURCE_REQUEST_TIMEOUT_SECONDS) -> pd.DataFrame:
    rows = []
    for company in _company_rows("lever"):
        payload = _fetch_json(
            f"https://api.lever.co/v0/postings/{company['slug']}?mode=json",
            timeout,
        )
        jobs = payload if isinstance(payload, list) else []
        for job in jobs:
            row = normalize_lever_job(job, company["company"])
            if _is_relevant_title_or_desc(row["title"], row["description"]):
                rows.append(row)
    return pd.DataFrame(rows)


def fetch_ashby_jobs(timeout: int = config.SOURCE_REQUEST_TIMEOUT_SECONDS) -> pd.DataFrame:
    rows = []
    for company in _company_rows("ashby"):
        payload = _fetch_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{company['slug']}",
            timeout,
        )
        jobs = (payload or {}).get("jobs", []) if isinstance(payload, dict) else []
        for job in jobs:
            row = normalize_ashby_job(job, company["company"])
            if _is_relevant_title_or_desc(row["title"], row["description"]):
                rows.append(row)
    return pd.DataFrame(rows)


def fetch_company_portal_jobs() -> pd.DataFrame:
    """Placeholder for stable company-portal discovery.

    Workday/Workable/custom sites are intentionally not scraped with brittle HTML
    parsing here; Google Jobs and explicit ATS adapters provide those links when
    public JSON is unavailable.
    """
    return pd.DataFrame()

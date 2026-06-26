"""Wraps JobSpy's scrape_jobs. Runs synchronously per call (JobSpy itself is
blocking/sync) — callers should run this inside asyncio.to_thread so it
doesn't block the event loop while other requests are being served."""

from __future__ import annotations
from typing import Callable, Optional

import requests
import pandas as pd
from jobspy import scrape_jobs
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

from . import config

_TRANSIENT = (requests.exceptions.RequestException, ConnectionError)


def is_specific_location(location: str) -> bool:
    """Return True only if location looks like a real city (contains a comma)."""
    return "," in location


@retry(stop=stop_after_attempt(2), wait=wait_fixed(3), retry=retry_if_exception_type(_TRANSIENT))
def _scrape_one(term: str, location: str, is_remote: bool, hours_old: int, sites: list[str]) -> pd.DataFrame:
    return scrape_jobs(
        site_name=sites,
        search_term=term,
        google_search_term=f"{term} jobs in {location}",
        location=location,
        results_wanted=config.RESULTS_WANTED_PER_TERM,
        hours_old=hours_old,
        country_indeed=config.COUNTRY_INDEED,
        is_remote=is_remote,
        linkedin_fetch_description=True,
        description_format="markdown",
        verbose=0,
    )


def scrape_all(
    location: str,
    is_remote: bool,
    hours_old: int,
    on_progress: Optional[Callable[[str], None]] = None,
    search_terms: Optional[list[str]] = None,
) -> pd.DataFrame:
    terms = search_terms if search_terms is not None else []
    specific = is_specific_location(location)
    sites = []
    if config.ENABLE_JOBSPY_LINKEDIN:
        sites.append("linkedin")
    sites.append("google")
    if specific:
        sites.append("glassdoor")
    if config.ENABLE_INDEED_FALLBACK:
        sites.append("indeed")
    sites = [site for site in sites if site not in config.DISABLED_SOURCES]
    if not specific and on_progress:
        on_progress("Skipping Glassdoor — needs a specific city, not a broad location.")

    frames = []
    for term in terms:
        if on_progress:
            on_progress(f"Scraping: {term}")
        try:
            df = _scrape_one(term, location, is_remote, hours_old, sites)
            if df is None or df.empty:
                continue
            df["search_term"] = term
            frames.append(df)
        except requests.exceptions.RequestException as e:
            if on_progress:
                on_progress(f"{term}: network error, skipping ({e})")
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 429:
                if on_progress:
                    on_progress(f"{term}: rate-limited (429), skipping")
            else:
                if on_progress:
                    on_progress(f"{term}: failed, skipping ({e})")

    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    if combined.empty:
        return pd.DataFrame()
    return combined

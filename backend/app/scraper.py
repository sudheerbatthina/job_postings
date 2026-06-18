"""Wraps JobSpy's scrape_jobs. Runs synchronously per call (JobSpy itself is
blocking/sync) — callers should run this inside asyncio.to_thread so it
doesn't block the event loop while other requests are being served."""

from __future__ import annotations
from typing import Callable, Optional

import pandas as pd
from jobspy import scrape_jobs

from . import config


def scrape_all(
    location: str,
    is_remote: bool,
    hours_old: int,
    on_progress: Optional[Callable[[str], None]] = None,
) -> pd.DataFrame:
    frames = []
    for term in config.SEARCH_TERMS:
        if on_progress:
            on_progress(f"Scraping: {term}")
        try:
            df = scrape_jobs(
                site_name=config.SITES,
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
            if df is not None and len(df):
                df["search_term"] = term
                frames.append(df)
        except Exception as e:
            if on_progress:
                on_progress(f"{term} failed: {e}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)

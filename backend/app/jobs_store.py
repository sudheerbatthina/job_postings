"""In-memory job tracking — no database for v1. A single process-wide
semaphore serializes actual scraping so the app never hits LinkedIn/Indeed
with more than one concurrent run from this server's IP."""

from __future__ import annotations
import asyncio
import math
import time
import uuid
from datetime import date, datetime
from typing import Optional

import pandas as pd

from . import config

JOBS: dict[str, dict] = {}
SCRAPE_SEMAPHORE = asyncio.Semaphore(1)


def create_job() -> str:
    _prune_old_jobs()
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "status": "pending",
        "message": "Queued",
        "results": None,
        "low_confidence_results": None,
        "error": None,
        "created_at": time.time(),
        "_df": None,  # holds the ranked DataFrame for the xlsx export endpoint
    }
    return job_id


def update_job(job_id: str, **fields) -> None:
    if job_id in JOBS:
        JOBS[job_id].update(fields)


def get_job(job_id: str) -> Optional[dict]:
    return JOBS.get(job_id)


def _sanitize(val):
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    if isinstance(val, pd.Timestamp):
        return None if pd.isna(val) else val.isoformat()
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return val


def _sanitize_record(record: dict) -> dict:
    return {k: _sanitize(v) for k, v in record.items()}


def set_results(
    job_id: str,
    df: pd.DataFrame,
    message: str | None = None,
    low_confidence_df: pd.DataFrame | None = None,
) -> None:
    records = df.drop(columns=[c for c in ["kw_score", "resume_match"] if c in df.columns], errors="ignore")
    records = records.where(pd.notnull(records), None)
    low_df = low_confidence_df if low_confidence_df is not None else pd.DataFrame()
    low_records = low_df.drop(
        columns=[c for c in ["kw_score", "resume_match"] if c in low_df.columns],
        errors="ignore",
    )
    if not low_records.empty:
        low_records = low_records.where(pd.notnull(low_records), None)
    update_job(
        job_id,
        status="done",
        message=message or f"Found {len(df)} matching jobs",
        results=[_sanitize_record(r) for r in records.to_dict(orient="records")],
        low_confidence_results=[
            _sanitize_record(r) for r in low_records.to_dict(orient="records")
        ],
        _df=df,
    )


def _prune_old_jobs() -> None:
    cutoff = time.time() - config.JOB_TTL_SECONDS
    stale = [jid for jid, j in JOBS.items() if j.get("created_at", 0) < cutoff]
    for jid in stale:
        JOBS.pop(jid, None)

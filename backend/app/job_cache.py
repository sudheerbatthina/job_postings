"""Persistent hourly job cache used by user searches.

The cache decouples user search latency/reliability from live job-board scraping.
"""

from __future__ import annotations
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone

import pandas as pd

from . import config, scraper

_DB_PATH = os.environ.get("JOB_CACHE_DB_PATH", "/data/job_cache.db")
try:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    open(_DB_PATH, "a").close()
except OSError:
    _DB_PATH = "./job_cache.db"


@contextmanager
def _conn():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        _ensure_schema(con)
        yield con
    finally:
        con.close()


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS jobs_cache (
            job_url      TEXT PRIMARY KEY,
            title        TEXT,
            company      TEXT,
            location     TEXT,
            site         TEXT,
            description  TEXT,
            date_posted  TEXT,
            scraped_at   TEXT,
            last_seen_at TEXT,
            raw_json     TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS job_cache_runs (
            id             TEXT PRIMARY KEY,
            started_at     TEXT,
            finished_at    TEXT,
            status         TEXT,
            raw_count      INTEGER,
            inserted_count INTEGER,
            updated_count  INTEGER,
            message        TEXT
        )
    """)
    con.commit()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value):
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _row_value(row, key: str):
    value = row.get(key)
    return _json_safe(value)


def upsert_jobs(df: pd.DataFrame, scraped_at: str | None = None) -> dict:
    if df is None or df.empty or "job_url" not in df.columns:
        return {"raw_count": 0, "inserted_count": 0, "updated_count": 0}

    now = scraped_at or _iso_now()
    inserted = 0
    updated = 0

    with _conn() as con:
        for _, row in df.iterrows():
            job_url = _row_value(row, "job_url")
            if not job_url:
                continue
            exists = con.execute(
                "SELECT 1 FROM jobs_cache WHERE job_url = ?",
                (job_url,),
            ).fetchone()
            raw = {str(k): _json_safe(v) for k, v in row.to_dict().items()}
            values = (
                job_url,
                _row_value(row, "title"),
                _row_value(row, "company"),
                _row_value(row, "location"),
                _row_value(row, "site") or _row_value(row, "site_name"),
                _row_value(row, "description"),
                _row_value(row, "date_posted"),
                now,
                now,
                json.dumps(raw, default=str),
            )
            con.execute(
                """
                INSERT INTO jobs_cache (
                    job_url, title, company, location, site, description,
                    date_posted, scraped_at, last_seen_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_url) DO UPDATE SET
                    title = excluded.title,
                    company = excluded.company,
                    location = excluded.location,
                    site = excluded.site,
                    description = excluded.description,
                    date_posted = excluded.date_posted,
                    last_seen_at = excluded.last_seen_at,
                    raw_json = excluded.raw_json
                """,
                values,
            )
            if exists:
                updated += 1
            else:
                inserted += 1
        con.commit()

    return {
        "raw_count": int(len(df)),
        "inserted_count": inserted,
        "updated_count": updated,
    }


def refresh_job_cache(
    force: bool = False,
    location: str = "United States",
    is_remote: bool = False,
    hours_old: int = config.JOB_CACHE_REFRESH_HOURS,
    on_progress=None,
) -> dict:
    if not force and not is_cache_stale(config.JOB_CACHE_MAX_AGE_MINUTES) and count_jobs() > 0:
        return {
            "status": "skipped",
            "raw_count": 0,
            "inserted_count": 0,
            "updated_count": 0,
            "message": "cache fresh",
        }

    run_id = uuid.uuid4().hex
    started = _iso_now()
    with _conn() as con:
        con.execute(
            "INSERT INTO job_cache_runs (id, started_at, status, message) VALUES (?, ?, ?, ?)",
            (run_id, started, "running", "refreshing"),
        )
        con.commit()

    try:
        df = scraper.scrape_all(
            location=location,
            is_remote=is_remote,
            hours_old=hours_old,
            on_progress=on_progress,
            search_terms=config.DEFAULT_STEM_SEARCH_TITLES,
        )
        counts = upsert_jobs(df, scraped_at=_iso_now())
        status = "done"
        message = "ok"
    except Exception as e:
        counts = {"raw_count": 0, "inserted_count": 0, "updated_count": 0}
        status = "error"
        message = str(e)

    with _conn() as con:
        con.execute(
            """
            UPDATE job_cache_runs
            SET finished_at = ?, status = ?, raw_count = ?, inserted_count = ?,
                updated_count = ?, message = ?
            WHERE id = ?
            """,
            (
                _iso_now(), status, counts["raw_count"], counts["inserted_count"],
                counts["updated_count"], message, run_id,
            ),
        )
        con.commit()

    return {"id": run_id, "status": status, "message": message, **counts}


def get_cache_age_minutes() -> float | None:
    with _conn() as con:
        row = con.execute(
            "SELECT MAX(finished_at) AS finished_at FROM job_cache_runs WHERE status = 'done'"
        ).fetchone()
    finished_at = row["finished_at"] if row else None
    if not finished_at:
        return None
    try:
        dt = datetime.fromisoformat(finished_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


def is_cache_stale(max_age_minutes: int = config.JOB_CACHE_MAX_AGE_MINUTES) -> bool:
    age = get_cache_age_minutes()
    return age is None or age > max_age_minutes


def count_jobs() -> int:
    with _conn() as con:
        row = con.execute("SELECT COUNT(*) AS n FROM jobs_cache").fetchone()
    return int(row["n"] or 0)


def get_recent_jobs(hours_old: int) -> pd.DataFrame:
    with _conn() as con:
        rows = con.execute("SELECT * FROM jobs_cache").fetchall()
    if not rows:
        return pd.DataFrame()

    records = []
    for row in rows:
        raw = {}
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        record = {**raw, **dict(row)}
        records.append(record)

    df = pd.DataFrame(records)
    if df.empty:
        return df

    posted = pd.to_datetime(df.get("date_posted"), errors="coerce", utc=True)
    scraped = pd.to_datetime(df.get("scraped_at"), errors="coerce", utc=True)
    basis = posted.fillna(scraped)
    cutoff = datetime.now(timezone.utc) - pd.Timedelta(hours=hours_old)
    df = df[basis >= cutoff].copy()
    return df.reset_index(drop=True)

"""Persistent hourly job cache used by user searches.

The cache decouples user search latency/reliability from live job-board scraping.
"""

from __future__ import annotations
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from urllib.parse import urlparse

import pandas as pd

from . import config, scraper
from .sources import ats_sources, google_jobs_serpapi

SOURCE_COUNT_KEYS = [
    "linkedin_count",
    "indeed_count",
    "google_jobs_count",
    "greenhouse_count",
    "lever_count",
    "ashby_count",
    "workday_count",
    "company_portal_count",
    "total_cache_jobs",
]

SOURCE_PRIORITY = {
    "greenhouse": 1,
    "lever": 1,
    "ashby": 1,
    "workday": 1,
    "company_portal": 1,
    "linkedin": 2,
    "google_jobs": 3,
    "google": 3,
    "indeed": 4,
}

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
            source       TEXT,
            source_type  TEXT,
            site         TEXT,
            description  TEXT,
            date_posted  TEXT,
            apply_url    TEXT,
            scraped_at   TEXT,
            last_seen_at TEXT,
            raw_json     TEXT
        )
    """)
    for col, typedef in (
        ("source", "TEXT"),
        ("source_type", "TEXT"),
        ("apply_url", "TEXT"),
        ("is_linkedin_easy_apply", "INTEGER DEFAULT 0"),
        ("excluded_reason", "TEXT"),
    ):
        try:
            con.execute(f"ALTER TABLE jobs_cache ADD COLUMN {col} {typedef}")
            con.commit()
        except sqlite3.OperationalError:
            pass
    con.execute("""
        CREATE TABLE IF NOT EXISTS job_cache_runs (
            id             TEXT PRIMARY KEY,
            started_at     TEXT,
            finished_at    TEXT,
            status         TEXT,
            raw_count      INTEGER,
            inserted_count INTEGER,
            updated_count  INTEGER,
            source_counts  TEXT,
            message        TEXT
        )
    """)
    try:
        con.execute("ALTER TABLE job_cache_runs ADD COLUMN source_counts TEXT")
        con.commit()
    except sqlite3.OperationalError:
        pass
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


def _empty_source_counts() -> dict:
    return {key: 0 for key in SOURCE_COUNT_KEYS}


def _normal_text(value) -> str:
    return re.sub(r"\W+", " ", str(value or "").lower()).strip()


def _canonical_url(value) -> str:
    if not value:
        return ""
    parsed = urlparse(str(value))
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def _is_linkedin_url(value) -> bool:
    if not value:
        return False
    host = urlparse(str(value)).netloc.lower()
    return "linkedin.com" in host


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _raw_easy_apply_flag(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).lower()
            if name in {"easy_apply", "is_easy_apply"} and _truthy(item):
                return True
            if name in {"apply_method", "application_method"} and "easy" in str(item).lower():
                return True
            if _raw_easy_apply_flag(item):
                return True
    elif isinstance(value, list):
        return any(_raw_easy_apply_flag(item) for item in value)
    return False


def is_linkedin_easy_apply_job(row) -> bool:
    source = str(row.get("source_type") or row.get("source") or row.get("site") or "").lower()
    job_url = row.get("job_url")
    apply_url = row.get("apply_url")
    is_linkedin = source == "linkedin" or _is_linkedin_url(job_url) or _is_linkedin_url(apply_url)
    if not is_linkedin:
        return False
    has_external_apply = bool(apply_url) and not _is_linkedin_url(apply_url)
    if has_external_apply:
        return False
    return (
        _raw_easy_apply_flag(row.to_dict() if hasattr(row, "to_dict") else dict(row))
        or not apply_url
        or _is_linkedin_url(apply_url)
    )


def add_linkedin_easy_apply_metadata(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if "apply_url" not in df.columns:
        df["apply_url"] = df.get("job_url")
    flags = [is_linkedin_easy_apply_job(row) for _, row in df.iterrows()]
    df["is_linkedin_easy_apply"] = flags
    if "excluded_reason" not in df.columns:
        df["excluded_reason"] = ""
    if "exclude_reason" not in df.columns:
        df["exclude_reason"] = ""
    mask = pd.Series(flags, index=df.index)
    df.loc[mask, "excluded_reason"] = "linkedin_easy_apply"
    df.loc[mask, "exclude_reason"] = "linkedin_easy_apply"
    return df


def _source_priority(row) -> int:
    source_type = str(row.get("source_type") or row.get("source") or row.get("site") or "").lower()
    return SOURCE_PRIORITY.get(source_type, 9)


def normalize_jobspy_jobs(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    site = df.get("site") if "site" in df.columns else df.get("site_name")
    df["source"] = site.fillna("jobspy") if site is not None else "jobspy"
    df["source_type"] = df["source"]
    if "apply_url" not in df.columns:
        df["apply_url"] = df.get("job_url")
    return df


def dedupe_prefer_sources(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    for col in ("title", "company", "location", "apply_url", "job_url"):
        if col not in df.columns:
            df[col] = ""
    df["_priority"] = df.apply(_source_priority, axis=1)
    df["_canonical_apply"] = df["apply_url"].fillna(df["job_url"]).apply(_canonical_url)
    df["_display_key"] = (
        df["title"].apply(_normal_text) + "|"
        + df["company"].apply(_normal_text) + "|"
        + df["location"].apply(_normal_text)
    )
    df = df.sort_values("_priority", ascending=True)
    with_apply = df[df["_canonical_apply"] != ""].drop_duplicates("_canonical_apply")
    without_apply = df[df["_canonical_apply"] == ""]
    df = pd.concat([with_apply, without_apply], ignore_index=True)
    df = df.sort_values("_priority", ascending=True).drop_duplicates("_display_key")
    return df.drop(columns=["_priority", "_canonical_apply", "_display_key"]).reset_index(drop=True)


def source_counts_from_df(df: pd.DataFrame) -> dict:
    counts = _empty_source_counts()
    if df is None or df.empty:
        return counts
    source_type = df.get("source_type")
    source = df.get("source")
    values = (source_type if source_type is not None else source).fillna("").astype(str).str.lower()
    counts["linkedin_count"] = int((values == "linkedin").sum())
    counts["indeed_count"] = int((values == "indeed").sum())
    counts["google_jobs_count"] = int((values == "google_jobs").sum() + (values == "google").sum())
    counts["greenhouse_count"] = int((values == "greenhouse").sum())
    counts["lever_count"] = int((values == "lever").sum())
    counts["ashby_count"] = int((values == "ashby").sum())
    counts["workday_count"] = int((values == "workday").sum())
    counts["company_portal_count"] = int((values == "company_portal").sum())
    counts["total_cache_jobs"] = int(len(df))
    return counts


def format_source_counts(counts: dict) -> str:
    return (
        f"sources linkedin={counts.get('linkedin_count', 0)}, "
        f"indeed={counts.get('indeed_count', 0)}, "
        f"google_jobs={counts.get('google_jobs_count', 0)}, "
        f"greenhouse={counts.get('greenhouse_count', 0)}, "
        f"lever={counts.get('lever_count', 0)}, "
        f"ashby={counts.get('ashby_count', 0)}, "
        f"workday={counts.get('workday_count', 0)}, "
        f"company_portal={counts.get('company_portal_count', 0)}, "
        f"total={counts.get('total_cache_jobs', 0)}"
    )


def fetch_jobspy_jobs(location, is_remote, hours_old, on_progress=None, search_terms=None) -> pd.DataFrame:
    df = scraper.scrape_all(
        location=location,
        is_remote=is_remote,
        hours_old=hours_old,
        on_progress=on_progress,
        search_terms=search_terms or config.DEFAULT_STEM_SEARCH_TITLES,
    )
    return normalize_jobspy_jobs(df)


def fetch_google_jobs(search_terms: list[str], location: str) -> pd.DataFrame:
    return google_jobs_serpapi.fetch_google_jobs(search_terms, location)


def fetch_company_ats_jobs() -> pd.DataFrame:
    if not config.ENABLE_COMPANY_ATS_SOURCES:
        return pd.DataFrame()
    frames = []
    for fetcher in (
        ats_sources.fetch_greenhouse_jobs,
        ats_sources.fetch_lever_jobs,
        ats_sources.fetch_ashby_jobs,
        ats_sources.fetch_company_portal_jobs,
    ):
        try:
            df = fetcher()
            if df is not None and not df.empty:
                frames.append(df)
        except Exception:
            continue
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def upsert_jobs(df: pd.DataFrame, scraped_at: str | None = None) -> dict:
    if df is None or df.empty or "job_url" not in df.columns:
        return {"raw_count": 0, "inserted_count": 0, "updated_count": 0}

    df = add_linkedin_easy_apply_metadata(dedupe_prefer_sources(df))
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
                _row_value(row, "source") or _row_value(row, "site") or _row_value(row, "site_name"),
                _row_value(row, "source_type") or _row_value(row, "source"),
                _row_value(row, "site") or _row_value(row, "site_name"),
                _row_value(row, "description"),
                _row_value(row, "date_posted"),
                _row_value(row, "apply_url") or job_url,
                now,
                now,
                1 if bool(_row_value(row, "is_linkedin_easy_apply")) else 0,
                _row_value(row, "excluded_reason") or _row_value(row, "exclude_reason"),
                json.dumps(raw, default=str),
            )
            con.execute(
                """
                INSERT INTO jobs_cache (
                    job_url, title, company, location, source, source_type, site,
                    description, date_posted, apply_url, scraped_at, last_seen_at,
                    is_linkedin_easy_apply, excluded_reason, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_url) DO UPDATE SET
                    title = excluded.title,
                    company = excluded.company,
                    location = excluded.location,
                    source = excluded.source,
                    source_type = excluded.source_type,
                    site = excluded.site,
                    description = excluded.description,
                    date_posted = excluded.date_posted,
                    apply_url = excluded.apply_url,
                    last_seen_at = excluded.last_seen_at,
                    is_linkedin_easy_apply = excluded.is_linkedin_easy_apply,
                    excluded_reason = excluded.excluded_reason,
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


def prune_old_jobs(max_age_hours: int = config.MAX_JOB_AGE_HOURS) -> int:
    cutoff = datetime.now(timezone.utc) - pd.Timedelta(hours=max_age_hours)
    with _conn() as con:
        rows = con.execute("SELECT job_url, date_posted, scraped_at FROM jobs_cache").fetchall()
        old_urls = []
        for row in rows:
            posted = pd.to_datetime(row["date_posted"], errors="coerce", utc=True)
            scraped = pd.to_datetime(row["scraped_at"], errors="coerce", utc=True)
            basis = posted if not pd.isna(posted) else scraped
            if pd.isna(basis) or basis < cutoff:
                old_urls.append(row["job_url"])
        if old_urls:
            con.executemany("DELETE FROM jobs_cache WHERE job_url = ?", [(url,) for url in old_urls])
            con.commit()
    return len(old_urls)


def refresh_job_cache(
    force: bool = False,
    location: str = "United States",
    is_remote: bool = False,
    hours_old: int = config.JOB_CACHE_REFRESH_HOURS,
    on_progress=None,
    search_terms: list[str] | None = None,
) -> dict:
    empty_counts = _empty_source_counts()
    pruned_before = prune_old_jobs(config.MAX_JOB_AGE_HOURS)
    if not force and not is_cache_stale(config.JOB_CACHE_MAX_AGE_MINUTES) and count_jobs() > 0:
        return {
            "status": "skipped",
            "raw_count": 0,
            "inserted_count": 0,
            "updated_count": 0,
            "pruned_count": pruned_before,
            "source_counts": empty_counts,
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

    source_counts = empty_counts
    try:
        terms = search_terms or config.DEFAULT_STEM_SEARCH_TITLES
        frames = []
        for fetcher in (
            lambda: fetch_jobspy_jobs(location, is_remote, hours_old, on_progress, terms),
            lambda: fetch_google_jobs(terms, location),
            fetch_company_ats_jobs,
        ):
            try:
                frame = fetcher()
                if frame is not None and not frame.empty:
                    frames.append(frame)
            except Exception:
                continue
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        df = dedupe_prefer_sources(df)
        source_counts = source_counts_from_df(df)
        counts = upsert_jobs(df, scraped_at=_iso_now())
        status = "done"
        message = format_source_counts(source_counts)
    except Exception as e:
        counts = {"raw_count": 0, "inserted_count": 0, "updated_count": 0}
        status = "error"
        message = str(e)

    with _conn() as con:
        con.execute(
            """
            UPDATE job_cache_runs
            SET finished_at = ?, status = ?, raw_count = ?, inserted_count = ?,
                updated_count = ?, source_counts = ?, message = ?
            WHERE id = ?
            """,
            (
                _iso_now(), status, counts["raw_count"], counts["inserted_count"],
                counts["updated_count"], json.dumps(source_counts), message, run_id,
            ),
        )
        con.commit()

    pruned_after = prune_old_jobs(config.MAX_JOB_AGE_HOURS)
    return {
        "id": run_id,
        "status": status,
        "message": message,
        "source_counts": source_counts,
        "pruned_count": pruned_before + pruned_after,
        **counts,
    }


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


def get_latest_source_counts() -> dict:
    with _conn() as con:
        row = con.execute(
            "SELECT source_counts FROM job_cache_runs WHERE source_counts IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
    if not row or not row["source_counts"]:
        return _empty_source_counts()
    try:
        data = json.loads(row["source_counts"])
    except (TypeError, json.JSONDecodeError):
        return _empty_source_counts()
    counts = _empty_source_counts()
    counts.update({key: int(data.get(key, 0) or 0) for key in SOURCE_COUNT_KEYS})
    return counts


def is_cache_stale(max_age_minutes: int = config.JOB_CACHE_MAX_AGE_MINUTES) -> bool:
    age = get_cache_age_minutes()
    return age is None or age > max_age_minutes


def count_jobs() -> int:
    with _conn() as con:
        row = con.execute("SELECT COUNT(*) AS n FROM jobs_cache").fetchone()
    return int(row["n"] or 0)


def get_recent_jobs(hours_old: int) -> pd.DataFrame:
    hours_old = min(int(hours_old or config.DEFAULT_HOURS_OLD), config.MAX_JOB_AGE_HOURS)
    prune_old_jobs(config.MAX_JOB_AGE_HOURS)
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

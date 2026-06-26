"""Per-resume seen-job tracking.

Seen history is soft: it helps rank unseen jobs first, but must never make a
search empty when cached jobs exist.
"""

from __future__ import annotations
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

from . import config

_DB_PATH = os.environ.get("DEDUP_DB_PATH", "/data/seen_jobs.db")
try:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    open(_DB_PATH, "a").close()
except OSError:
    _DB_PATH = "./seen_jobs.db"


def resume_hash(resume_text: str, primary_track: str | None = None) -> str:
    basis = f"{primary_track or 'default'}\n{resume_text or ''}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


@contextmanager
def _conn():
    con = sqlite3.connect(_DB_PATH)
    try:
        _ensure_schema(con)
        yield con
    finally:
        con.close()


def _ensure_schema(con: sqlite3.Connection) -> None:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='seen_jobs'"
    ).fetchone()
    if row:
        cols = [r[1] for r in con.execute("PRAGMA table_info(seen_jobs)").fetchall()]
        if "resume_hash" not in cols:
            con.execute("ALTER TABLE seen_jobs RENAME TO seen_jobs_legacy")
            con.execute("""
                CREATE TABLE seen_jobs (
                    resume_hash TEXT,
                    job_url     TEXT,
                    seen_at     TEXT,
                    PRIMARY KEY (resume_hash, job_url)
                )
            """)
            con.execute("""
                INSERT OR IGNORE INTO seen_jobs (resume_hash, job_url, seen_at)
                SELECT '__legacy__', job_url, seen_at FROM seen_jobs_legacy
            """)
            con.execute("DROP TABLE seen_jobs_legacy")
            con.commit()
            return

    con.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            resume_hash TEXT,
            job_url     TEXT,
            seen_at     TEXT,
            PRIMARY KEY (resume_hash, job_url)
        )
    """)
    con.commit()


def _prune(con: sqlite3.Connection) -> None:
    con.execute(
        "DELETE FROM seen_jobs WHERE datetime(seen_at) < datetime('now', ?)",
        (f"-{config.SEEN_TTL_HOURS} hours",),
    )
    con.commit()


def _seen_urls(con: sqlite3.Connection, urls: tuple, resume_hash_value: str) -> set[str]:
    if not urls:
        return set()
    placeholders = ",".join("?" * len(urls))
    return {
        row[0]
        for row in con.execute(
            f"""
            SELECT job_url FROM seen_jobs
            WHERE resume_hash = ? AND job_url IN ({placeholders})
            """,
            (resume_hash_value, *urls),
        )
    }


def split_seen(df: pd.DataFrame, resume_hash_value: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df.empty or "job_url" not in df.columns:
        return df, df.iloc[0:0].copy()
    with _conn() as con:
        _prune(con)
        urls = tuple(df["job_url"].dropna().unique())
        seen = _seen_urls(con, urls, resume_hash_value)
    if not seen:
        return df.reset_index(drop=True), df.iloc[0:0].copy()
    mask = df["job_url"].isin(seen)
    unseen = df[~mask].reset_index(drop=True)
    seen_df = df[mask].copy().reset_index(drop=True)
    seen_df["seen_before"] = True
    return unseen, seen_df


def filter_unseen(df: pd.DataFrame, resume_hash_value: str | None = None) -> pd.DataFrame:
    if resume_hash_value is None:
        return df
    unseen, _ = split_seen(df, resume_hash_value)
    return unseen


def mark_seen(df: pd.DataFrame, resume_hash_value: str | None = None) -> None:
    if resume_hash_value is None or df.empty or "job_url" not in df.columns:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = [(resume_hash_value, url, now) for url in df["job_url"].dropna().unique()]
    if not rows:
        return
    with _conn() as con:
        con.executemany(
            "INSERT OR REPLACE INTO seen_jobs (resume_hash, job_url, seen_at) VALUES (?, ?, ?)",
            rows,
        )
        con.commit()

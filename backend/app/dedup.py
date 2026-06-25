"""Persistent deduplication via SQLite.

DEDUP_DB_PATH env var sets the DB location:
  - Production (Railway + volume): /data/seen_jobs.db
  - Local fallback: ./seen_jobs.db
"""

from __future__ import annotations
import os
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager

import pandas as pd

_DB_PATH = os.environ.get("DEDUP_DB_PATH", "/data/seen_jobs.db")
# Fall back to local path if the production path isn't writable
try:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    open(_DB_PATH, "a").close()
except OSError:
    _DB_PATH = "./seen_jobs.db"


@contextmanager
def _conn():
    con = sqlite3.connect(_DB_PATH)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS seen_jobs "
            "(job_url TEXT PRIMARY KEY, seen_at TEXT)"
        )
        con.commit()
        yield con
    finally:
        con.close()


def filter_unseen(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "job_url" not in df.columns:
        return df
    with _conn() as con:
        # Prune rows older than 24 hours before querying
        con.execute("DELETE FROM seen_jobs WHERE datetime(seen_at) < datetime('now', '-1 day')")
        con.commit()
        urls = tuple(df["job_url"].dropna().unique())
        if not urls:
            return df
        placeholders = ",".join("?" * len(urls))
        seen = {
            row[0]
            for row in con.execute(
                f"SELECT job_url FROM seen_jobs WHERE job_url IN ({placeholders})", urls
            )
        }
    return df[~df["job_url"].isin(seen)].reset_index(drop=True)


def mark_seen(df: pd.DataFrame) -> None:
    if df.empty or "job_url" not in df.columns:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = [(url, now) for url in df["job_url"].dropna().unique()]
    if not rows:
        return
    with _conn() as con:
        con.executemany(
            "INSERT OR IGNORE INTO seen_jobs (job_url, seen_at) VALUES (?, ?)", rows
        )
        con.commit()

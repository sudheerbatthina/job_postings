"""Persists the most-recently-uploaded resume so subsequent requests
can skip re-parsing and re-extracting keywords when the same file is
re-uploaded. Shares the SQLite DB file with dedup (DEDUP_DB_PATH env var)."""

from __future__ import annotations
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

_DB_PATH = os.environ.get("DEDUP_DB_PATH", "/data/seen_jobs.db")
try:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    open(_DB_PATH, "a").close()
except OSError:
    _DB_PATH = "./seen_jobs.db"


@contextmanager
def _conn():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS resume (
                id       INTEGER PRIMARY KEY,
                filename TEXT,
                text     TEXT,
                keywords TEXT,
                email    TEXT,
                phone    TEXT,
                stored_at TEXT
            )
        """)
        con.commit()
        yield con
    finally:
        con.close()


def save_resume(
    filename: str,
    text: str,
    keywords: str,
    email: str | None,
    phone: str | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("DELETE FROM resume")
        con.execute(
            "INSERT INTO resume (filename, text, keywords, email, phone, stored_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (filename, text, keywords, email, phone, now),
        )
        con.commit()


def load_resume() -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM resume LIMIT 1").fetchone()
    if row is None:
        return None
    return dict(row)


def has_resume() -> bool:
    return load_resume() is not None

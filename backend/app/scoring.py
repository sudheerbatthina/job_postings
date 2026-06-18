"""Keyword + resume + recency scoring. Logic ported from the validated
ai_engineer_jobs.py CLI script, with module-level config replaced by
function parameters so it's safe across concurrent requests."""

from __future__ import annotations
import re
import math
from datetime import date, datetime

import pandas as pd

from . import config


def keyword_score(title: str, desc: str) -> float:
    t, d = (title or "").lower(), (desc or "").lower()
    raw = 0
    for kw, w in config.AI_KEYWORDS.items():
        if kw in t:
            raw += w * 3
        elif kw in d:
            raw += w
    return min(1.0, raw / (config.KW_NORM * 3))


def resume_score(desc: str, resume_tokens: set[str]) -> float:
    if not resume_tokens or not desc:
        return 0.0
    job_toks = {t for t in re.findall(r"[a-zA-Z][a-zA-Z+#.\-]{2,}", desc.lower())
                if t not in config.STOPWORDS}
    if not job_toks:
        return 0.0
    return len(job_toks & resume_tokens) / len(job_toks)


def recency_score(d, window_hours: int) -> float:
    if d is None or (isinstance(d, float) and math.isnan(d)):
        return 0.3
    if isinstance(d, str):
        try:
            d = datetime.fromisoformat(d).date()
        except ValueError:
            return 0.3
    if isinstance(d, datetime):
        d = d.date()
    days_old = (date.today() - d).days
    window_days = max(1, window_hours / 24)
    return max(0.0, 1.0 - days_old / window_days)


def title_blocked(title: str) -> bool:
    t = (title or "").lower()
    return any(b in t for b in config.TITLE_BLOCKLIST)


def score_and_rank(df: pd.DataFrame, resume_tokens: set[str], hours_old: int, top_results: int = config.TOP_RESULTS) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.drop_duplicates(subset="job_url")
    df = df.drop_duplicates(subset=["title", "company"]).reset_index(drop=True)

    for col in ("title", "description"):
        if col not in df.columns:
            df[col] = ""
        else:
            df[col] = df[col].fillna("")
    if "date_posted" not in df.columns:
        df["date_posted"] = None

    df = df[~df["title"].apply(title_blocked)].copy()

    df["kw_score"] = df.apply(lambda r: keyword_score(r["title"], r["description"]), axis=1)
    df["resume_match"] = df.apply(lambda r: resume_score(r["description"], resume_tokens), axis=1)
    df["recency"] = df["date_posted"].apply(lambda d: recency_score(d, hours_old))

    df = df[df["kw_score"] >= config.MIN_KEYWORD_SCORE].copy()
    if df.empty:
        return df

    total_w = sum(config.WEIGHTS.values())
    df["score"] = (
        config.WEIGHTS["keyword"] * df["kw_score"]
        + config.WEIGHTS["resume"] * df["resume_match"]
        + config.WEIGHTS["recency"] * df["recency"]
    )
    df["score_100"] = (df["score"] / total_w * 100).round(0).astype(int)

    df = df.sort_values(["score_100", "recency"], ascending=False).reset_index(drop=True)
    df = df.head(top_results)
    df["rank"] = range(1, len(df) + 1)
    return df

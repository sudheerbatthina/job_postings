"""Two-stage scoring pipeline.

Stage 1 — prefilter(): fast, free token-overlap filter.
Stage 2 — score_with_claude(): Claude Haiku scores each shortlisted job.

The legacy score_and_rank() is kept for reference / CLI use.
"""

from __future__ import annotations
import json
import re
import math
from datetime import date, datetime
from typing import TYPE_CHECKING

import pandas as pd

from . import config

if TYPE_CHECKING:
    import anthropic as _anthropic


# ---------------------------------------------------------------------------
# Low-level scorers (shared by both pipelines)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Stage 1: fast free prefilter
# ---------------------------------------------------------------------------

def prefilter(df: pd.DataFrame, resume_tokens: set[str]) -> pd.DataFrame:
    """Keep rows with token overlap >= 0.15; return at most 50 sorted by recency."""
    if df.empty:
        return df
    df = df.copy()
    if "description" not in df.columns:
        df["description"] = ""
    else:
        df["description"] = df["description"].fillna("")

    df["_overlap"] = df["description"].apply(lambda d: resume_score(d, resume_tokens))
    df = df[df["_overlap"] >= 0.15].drop(columns=["_overlap"])

    if df.empty:
        return df.reset_index(drop=True)

    if "date_posted" not in df.columns:
        df["date_posted"] = None

    df = df.sort_values("date_posted", ascending=False, na_position="last").head(50)
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Stage 2: Claude scoring
# ---------------------------------------------------------------------------

def claude_score(job_description: str, resume_text: str, client: "_anthropic.Anthropic | None") -> int:
    """Ask Claude Haiku to score resume↔job fit on 0-100. Returns 0 on any failure."""
    if client is None:
        return 0
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=50,
            messages=[{
                "role": "user",
                "content": (
                    'Score how well this resume matches this job description. '
                    'Return only a JSON object: {"score": <0-100>}.\n'
                    f"Resume: {resume_text[:2000]}\n"
                    f"Job: {job_description[:1500]}"
                ),
            }],
        )
        data = json.loads(msg.content[0].text)
        return int(data["score"])
    except Exception:
        return 0


def score_with_claude(
    df: pd.DataFrame,
    resume_text: str,
    client: "_anthropic.Anthropic | None",
) -> pd.DataFrame:
    """Apply claude_score to each row. Filter by threshold; return top 10."""
    if df.empty:
        return df
    df = df.copy()
    df["claude_score"] = df["description"].fillna("").apply(
        lambda desc: claude_score(desc, resume_text, client)
    )
    df = df.sort_values("claude_score", ascending=False).reset_index(drop=True)

    result = df[df["claude_score"] >= config.MIN_SCORE_THRESHOLD]
    if len(result) < 10:
        result = df[df["claude_score"] >= config.MIN_SCORE_FALLBACK]

    return result.head(10).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Legacy pipeline (kept for CLI / reference; not called from the API)
# ---------------------------------------------------------------------------

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
    if df.empty:
        return df

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

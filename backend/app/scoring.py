"""Two-stage scoring pipeline.

Stage 1 — prefilter(): fast, free token-overlap filter.
Stage 2 — score_with_claude(): Claude Haiku scores each shortlisted job.

The legacy score_and_rank() is kept for reference / CLI use.
"""

from __future__ import annotations
import json
import logging
import re
import math
from datetime import date, datetime
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

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

_SCORE_FAILED: dict = {"ats_score": None, "missing_keywords": [], "used_fallback_score": True}


def fallback_score_row(row, resume_tokens: set[str] | None, window_hours: int) -> int:
    """Cheap non-AI score so Claude/API failures still return ranked jobs."""
    title = row.get("title", "") if hasattr(row, "get") else ""
    desc = row.get("description", "") if hasattr(row, "get") else ""
    date_posted = row.get("date_posted") if hasattr(row, "get") else None
    kw = keyword_score(title, desc)
    resume = resume_score(desc, resume_tokens or set())
    recency = recency_score(date_posted, window_hours)
    score = 100 * (0.45 * kw + 0.35 * resume + 0.20 * recency)
    return max(0, min(100, int(round(score))))


def sort_scored(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    if "date_posted" not in df.columns:
        df["date_posted"] = None
    df["_sort_date"] = pd.to_datetime(df["date_posted"], errors="coerce")
    df = df.sort_values(
        ["ats_score", "_sort_date"],
        ascending=[False, False],
        na_position="last",
    )
    return df.drop(columns=["_sort_date"]).reset_index(drop=True)


def fallback_score_dataframe(
    df: pd.DataFrame,
    resume_tokens: set[str] | None,
    window_hours: int,
) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["ats_score"] = df.apply(
        lambda row: fallback_score_row(row, resume_tokens, window_hours), axis=1
    )
    df["missing_keywords"] = [[] for _ in range(len(df))]
    df["used_fallback_score"] = True
    return sort_scored(df)


def claude_score(
    job_description: str,
    resume_text: str,
    skill_signals: list[str],
    total_yoe: int,
    client: "_anthropic.Anthropic | None",
) -> dict:
    """Ask Claude Haiku to ATS-score resume↔job fit.
    Returns {"ats_score": 0-100, "missing_keywords": [...]}; all-zero on any failure."""
    if client is None:
        return _SCORE_FAILED
    yoe_note = (
        f"The candidate has approximately {total_yoe} year{'s' if total_yoe != 1 else ''} "
        "of professional experience. "
        if total_yoe
        else ""
    )
    signals_note = (
        f"Candidate's key differentiating skills: {', '.join(skill_signals)}.\n"
        if skill_signals
        else ""
    )
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "You are an ATS. Score this resume against the job description (0-100) "
                    "and list must-have skills from the JD that are absent from the resume.\n\n"
                    "Scoring factors:\n"
                    "- Skill/keyword coverage: does the resume mention the JD's required technologies?\n"
                    "- Must-have requirements: are the JD's explicit required qualifications present?\n"
                    "- Title alignment: does the candidate's background match this role's level and focus?\n"
                    f"- Seniority fit: {yoe_note}does the experience level match the role?\n"
                    + signals_note
                    + "\nReturn ONLY valid JSON:\n"
                    '{"ats_score": <0-100>, "missing_keywords": [<JD must-have skills absent from resume>]}\n\n'
                    f"Resume:\n{resume_text[:2000]}\n\n"
                    f"Job description:\n{job_description[:1500]}"
                ),
            }],
        )
        data = json.loads(msg.content[0].text)
        return {
            "ats_score": int(data["ats_score"]),
            "missing_keywords": [str(k) for k in data.get("missing_keywords", [])],
        }
    except Exception as e:
        logger.warning("claude_score failed: %s", e)
        return _SCORE_FAILED


def score_with_claude(
    df: pd.DataFrame,
    resume_text: str,
    skill_signals: list[str],
    total_yoe: int,
    client: "_anthropic.Anthropic | None",
    resume_tokens: set[str] | None = None,
    window_hours: int = config.DEFAULT_HOURS_OLD,
) -> pd.DataFrame:
    """Apply claude_score to each row; return all rows sorted by ats_score descending.
    Slicing to TOP_RESULTS happens in the caller after accumulation across windows."""
    if df.empty:
        return df
    df = df.copy()
    scores = []
    missing_keywords = []
    fallback_flags = []
    for _, row in df.iterrows():
        result = claude_score(
            row.get("description") or "",
            resume_text,
            skill_signals,
            total_yoe,
            client,
        )
        if result.get("ats_score") is None:
            scores.append(fallback_score_row(row, resume_tokens, window_hours))
            missing_keywords.append([])
            fallback_flags.append(True)
        else:
            scores.append(max(0, min(100, int(result["ats_score"]))))
            missing_keywords.append([str(k) for k in result.get("missing_keywords", [])])
            fallback_flags.append(False)
    df["ats_score"] = scores
    df["missing_keywords"] = missing_keywords
    df["used_fallback_score"] = fallback_flags
    return sort_scored(df)


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

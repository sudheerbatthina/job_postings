"""Two-stage scoring pipeline.

Stage 1 — prefilter(): fast, free token-overlap filter.
Stage 2 — score_with_claude(): Claude Haiku scores each shortlisted job.

The legacy score_and_rank() is kept for reference / CLI use.
"""

from __future__ import annotations
import logging
import os
import re
import math
from datetime import date, datetime
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

import pandas as pd

from . import config, freshness

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

_AI_ROLE_TITLE_TERMS = [
    "applied ai", "ai engineer", "ai/ml", "machine learning engineer", "ml engineer",
    "genai", "generative ai", "llm", "rag engineer", "mlops", "ai platform",
    "applied scientist", "machine learning scientist",
]
_AI_SIGNAL_TERMS = [
    "machine learning", "deep learning", "llm", "large language model", "generative ai",
    "genai", "rag", "retrieval augmented", "agentic", "ai agent", "mlops",
    "model deployment", "model serving", "inference", "fine tuning", "fine-tuning",
    "embeddings", "vector search", "vector database", "feature engineering",
    "ml pipeline", "ml pipelines", "ai platform", "nlp", "computer vision",
    "pytorch", "tensorflow", "langchain", "langgraph", "hugging face",
]
_DATA_ENGINEERING_TITLE_TERMS = ["data engineer", "etl engineer", "analytics engineer"]
_SOFTWARE_TITLE_TERMS = ["software engineer", "backend engineer", "platform engineer"]
_CONSULTING_TERMS = ["consultant", "consulting", "manager", "pharma technology", "advisory"]
_INFRA_ADMIN_TERMS = ["splunk", "observability", "administrator", "admin", "monitoring"]
_ANALYTICS_TERMS = ["business intelligence", "bi ", "tableau", "power bi", "reporting", "dashboard"]


def _parse_posted_date(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return pd.to_datetime(text, errors="coerce", utc=True).date()
    except Exception:
        return None


def freshness_for_date(value) -> dict:
    posted = _parse_posted_date(value)
    if posted is None:
        return {
            "posted_age_days": None,
            "posted_age_label": None,
            "freshness_bucket": "unknown",
            "freshness_score": 0.25,
        }
    days = max(0, (date.today() - posted).days)
    if days == 0:
        label = "Posted today"
        bucket = "24h"
        freshness = 1.0
    elif days == 1:
        label = "Posted yesterday"
        bucket = "72h"
        freshness = 0.85
    elif days <= 3:
        label = f"Posted {days} days ago"
        bucket = "72h"
        freshness = 0.72
    else:
        label = f"Posted {days} days ago"
        bucket = "old"
        freshness = 0.10
    return {
        "posted_age_days": days,
        "posted_age_label": label,
        "freshness_bucket": bucket,
        "freshness_score": freshness,
    }


def add_freshness_metadata(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    records = []
    for _, row in df.iterrows():
        if row.get("posted_at_ts") is not None or row.get("posted_at_raw") is not None or row.get("date_posted") is not None:
            meta = freshness.normalize_posted_fields(row.to_dict())
            bucket = meta["freshness_bucket"]
            freshness_score = 1.0 if bucket == "24h" else 0.72 if bucket == "72h" else 0.10 if bucket == "old" else 0.25
            age_days = None if meta["posted_age_minutes"] is None else int(meta["posted_age_minutes"] // 1440)
            meta["posted_age_days"] = age_days
            meta["freshness_score"] = freshness_score
            records.append(meta)
        else:
            meta = freshness_for_date(row.get("date_posted"))
            records.append(meta)
    for key in (
        "posted_at_raw", "posted_at_ts", "posted_age_minutes", "posted_age_label",
        "posted_precision", "freshness_bucket", "posted_age_days", "freshness_score",
    ):
        df[key] = [item.get(key) for item in records]
    return df


def match_band(score: int | float | None, excluded: bool = False) -> str:
    if excluded or score is None:
        return "hidden"
    score = float(score)
    if score >= 80:
        return "strong"
    if score >= config.DEFAULT_MIN_ATS_SCORE:
        return "good"
    if score >= config.BROADER_MIN_ATS_SCORE:
        return "broader"
    return "hidden"


def _text_for_job(job) -> tuple[str, str, str]:
    title = str(job.get("title", "") if hasattr(job, "get") else "")
    company = str(job.get("company", "") if hasattr(job, "get") else "")
    desc = str(job.get("description", "") if hasattr(job, "get") else "")
    return title.lower(), company.lower(), desc.lower()


def _has_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def _relevance_result(job_family: str, role_relevance: int, exclude: bool, reason: str) -> dict:
    return {
        "job_family": job_family,
        "role_relevance": max(0, min(100, int(role_relevance))),
        "exclude_by_default": bool(exclude),
        "exclude_reason": reason,
    }


def _boolish(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def classify_job_relevance(job, target_profile: dict | None = None) -> dict:
    """Classify whether a job belongs in the candidate's target role family."""
    if _boolish(job.get("is_linkedin_easy_apply")):
        return _relevance_result("other", 0, True, "linkedin_easy_apply")

    profile = target_profile or config.DEFAULT_TARGET_PROFILE
    primary_track = profile.get("primary_track", "applied_ai_ml")
    title, _, desc = _text_for_job(job)
    text = f"{title} {desc}"
    has_ai_title = _has_any(title, _AI_ROLE_TITLE_TERMS)
    has_ai_signal = _has_any(text, _AI_SIGNAL_TERMS)
    hands_on = _has_any(text, ["build", "develop", "deploy", "production", "engineer", "model"])

    if primary_track != "applied_ai_ml":
        return _relevance_result("other", 50, False, "")

    for term in ("internship", "intern", "new grad", "student", "university graduate"):
        if term in title:
            return _relevance_result(
                "other", 20, True,
                f"excluded role term: {term}",
            )

    if _has_any(title, _CONSULTING_TERMS) or "pharma technology" in text:
        if has_ai_signal and hands_on:
            return _relevance_result("applied_ai_ml", 62, False, "")
        return _relevance_result(
            "consulting", 25, True,
            "consulting or manager role without clear hands-on AI/ML engineering",
        )

    if _has_any(title, _INFRA_ADMIN_TERMS):
        if has_ai_signal and hands_on:
            return _relevance_result("applied_ai_ml", 60, False, "")
        return _relevance_result(
            "infra_admin", 25, True,
            "Splunk/admin/observability role without explicit AI/ML engineering",
        )

    for term in config.DEFAULT_EXCLUDED_ROLE_TERMS:
        if term in title:
            return _relevance_result(
                "other", 25, True,
                f"excluded role term: {term}",
            )

    if _has_any(title, _DATA_ENGINEERING_TITLE_TERMS):
        if _has_any(text, [
            "ml pipeline", "ml pipelines", "feature engineering", "feature store",
            "mlops", "model deployment", "model serving", "ai/ml platform",
            "ai platform", "embeddings", "vector search", "llm", "rag",
        ]):
            return _relevance_result("data_engineering", 68, False, "")
        return _relevance_result(
            "data_engineering", 42, True,
            "data engineering role lacks AI/ML, MLOps, embeddings, RAG, or model deployment signals",
        )

    if "data scientist" in title:
        if has_ai_signal or _has_any(text, ["modeling", "predictive", "statistical model"]):
            return _relevance_result("applied_ai_ml", 72, False, "")
        return _relevance_result(
            "analytics", 45, True,
            "data science role lacks ML/AI/NLP/modeling signals",
        )

    if _has_any(title, _ANALYTICS_TERMS):
        if has_ai_signal:
            return _relevance_result("applied_ai_ml", 60, False, "")
        return _relevance_result("analytics", 35, True, "BI/reporting-only role")

    if _has_any(title, _SOFTWARE_TITLE_TERMS):
        if has_ai_signal:
            return _relevance_result("software", 65, False, "")
        return _relevance_result(
            "software", 45, True,
            "software/backend role lacks explicit AI/ML/LLM platform focus",
        )

    if has_ai_title:
        return _relevance_result("applied_ai_ml", 86 if has_ai_signal else 76, False, "")

    if has_ai_signal and hands_on:
        return _relevance_result("applied_ai_ml", 70, False, "")

    return _relevance_result("other", 35, True, "role is outside the Applied AI/ML target family")


def add_role_relevance(df: pd.DataFrame, target_profile: dict | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    df = add_freshness_metadata(df.copy())
    classifications = [classify_job_relevance(row, target_profile) for _, row in df.iterrows()]
    for key in ("job_family", "role_relevance", "exclude_by_default", "exclude_reason"):
        df[key] = [item[key] for item in classifications]
    df["excluded_reason"] = df["exclude_reason"]
    return df


def apply_final_score_rules(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = add_freshness_metadata(df.copy())
    final_scores = []
    bands = []
    exclude_flags = []
    exclude_reasons = []
    for _, row in df.iterrows():
        base = float(row.get("ats_score") or 0)
        relevance = float(row.get("role_relevance") or 0)
        freshness = float(row.get("freshness_score") or 0)
        score = (0.55 * base) + (0.30 * relevance) + (0.15 * freshness * 100)
        excluded = bool(row.get("exclude_by_default"))
        reason = str(row.get("exclude_reason") or "")
        bucket = row.get("freshness_bucket")
        if _boolish(row.get("is_linkedin_easy_apply")):
            score = min(score, 49)
            excluded = True
            reason = "linkedin_easy_apply"
        if bucket == "old":
            score = min(score, 49)
            excluded = True
            reason = reason or "job is older than 30 hours"
        elif bucket == "unknown" and score < 80:
            score = min(score, 64)
            reason = reason or "unknown posting date"
        if excluded:
            score = min(score, 49)
        rounded = max(0, min(100, int(round(score))))
        final_scores.append(rounded)
        exclude_flags.append(excluded)
        exclude_reasons.append(reason)
        bands.append(match_band(rounded, excluded))
    df["ats_score"] = final_scores
    df["exclude_by_default"] = exclude_flags
    df["exclude_reason"] = exclude_reasons
    df["excluded_reason"] = exclude_reasons
    df["match_band"] = bands
    return df


def dedupe_display_jobs(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    for col in ("title", "company", "location"):
        if col not in df.columns:
            df[col] = ""
    normalized = (
        df["title"].fillna("").astype(str).str.lower().str.replace(r"\W+", " ", regex=True).str.strip()
        + "|"
        + df["company"].fillna("").astype(str).str.lower().str.replace(r"\W+", " ", regex=True).str.strip()
        + "|"
        + df["location"].fillna("").astype(str).str.lower().str.replace(r"\W+", " ", regex=True).str.strip()
    )
    df["_display_key"] = normalized
    df = df.drop_duplicates(subset=["_display_key"]).drop(columns=["_display_key"])
    return df.reset_index(drop=True)


def fallback_score_row(row, resume_tokens: set[str] | None, window_hours: int) -> int:
    """Cheap non-AI score so Claude/API failures still return ranked jobs."""
    title = row.get("title", "") if hasattr(row, "get") else ""
    desc = row.get("description", "") if hasattr(row, "get") else ""
    date_posted = row.get("posted_at_ts") or row.get("date_posted") if hasattr(row, "get") else None
    kw = keyword_score(title, desc)
    resume = resume_score(desc, resume_tokens or set())
    recency = recency_score(date_posted, window_hours)
    score = 100 * (0.45 * kw + 0.35 * resume + 0.20 * recency)
    if hasattr(row, "get") and row.get("role_relevance") is not None:
        score = (0.65 * float(row.get("role_relevance") or 0)) + (0.35 * score)
        if row.get("exclude_by_default"):
            score = min(score, float(row.get("role_relevance") or 30))
    return max(0, min(100, int(round(score))))


def sort_scored(df: pd.DataFrame) -> pd.DataFrame:
    """Sort results by match_band (strong first), then recency (newest first), then ats_score desc."""
    if df.empty:
        return df
    df = df.copy()
    if "posted_age_minutes" not in df.columns:
        df["posted_age_minutes"] = None
    if "match_band" not in df.columns:
        df["match_band"] = ""
    _BAND_ORDER = {"strong": 0, "good": 1, "broader": 2, "hidden": 3}
    df["_band_priority"] = df["match_band"].fillna("").str.lower().map(_BAND_ORDER).fillna(9)
    df["_sort_age"] = pd.to_numeric(df["posted_age_minutes"], errors="coerce")
    df = df.sort_values(
        ["_band_priority", "_sort_age", "ats_score"],
        ascending=[True, True, False],
        na_position="last",
    )
    return df.drop(columns=["_band_priority", "_sort_age"]).reset_index(drop=True)


def fallback_score_dataframe(
    df: pd.DataFrame,
    resume_tokens: set[str] | None,
    window_hours: int,
    target_profile: dict | None = None,
) -> pd.DataFrame:
    if df.empty:
        return df
    df = add_role_relevance(df.copy(), target_profile)
    df["ats_score"] = df.apply(
        lambda row: fallback_score_row(row, resume_tokens, window_hours), axis=1
    )
    df["missing_keywords"] = [[] for _ in range(len(df))]
    df["matched_keywords"] = [[] for _ in range(len(df))]
    df["confidence"] = df["role_relevance"]
    df["used_fallback_score"] = True
    df = apply_final_score_rules(df)
    return sort_scored(df)


def claude_score(
    job_description: str,
    resume_text: str,
    skill_signals: list[str],
    total_yoe: int,
    client: "_anthropic.Anthropic | None",
    target_profile: dict | None = None,
    job_title: str = "",
) -> dict:
    """Ask Claude Haiku to ATS-score resume↔job fit.
    Returns {"ats_score": 0-100, "missing_keywords": [...]}; all-zero on any failure.
    Uses the same JSON extraction helpers as resume_parser to handle markdown fences,
    empty responses, and extra prose without crashing."""
    if client is None:
        return _SCORE_FAILED

    # Shared helpers from resume_parser — handles markdown fences and prose preambles.
    from .resume_parser import _message_text, _first_json_object

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
    profile = target_profile or config.DEFAULT_TARGET_PROFILE
    target_note = (
        f"Candidate target profile: primary_track={profile.get('primary_track')}; "
        f"target_titles={', '.join(profile.get('target_titles', [])[:12])}; "
        f"must_have_signals={', '.join(profile.get('must_have_signals', [])[:12])}.\n"
    )
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "You are an ATS. Score this resume against the job description (0-100) "
                    "for the candidate's target profile, not generic STEM. Also classify role relevance.\n\n"
                    "Scoring factors:\n"
                    "- Skill/keyword coverage: does the resume mention the JD's required technologies?\n"
                    "- Must-have requirements: are the JD's explicit required qualifications present?\n"
                    "- Title alignment: does the candidate's background match this role's level and focus?\n"
                    f"- Seniority fit: {yoe_note}does the experience level match the role?\n"
                    "- A job outside the target role family must not score above 40.\n"
                    "- Pure data engineering without AI/ML/LLM must not score above 45.\n"
                    "- Consulting manager roles must not score above 30 unless hands-on AI/ML engineering is explicit.\n"
                    "- Splunk/admin roles must not score above 30 unless AI/ML model engineering is explicit.\n"
                    "- Strong Applied AI / LLM / RAG / Agentic AI matches should usually score 70+; great matches 80+.\n"
                    "- Do not give high scores just because Python/SQL/cloud appear.\n"
                    + target_note
                    + signals_note
                    + "\nReturn ONLY valid JSON:\n"
                    '{"ats_score": <0-100>, "role_relevance": <0-100>, "job_family": "applied_ai_ml|data_engineering|software|analytics|consulting|infra_admin|other", '
                    '"matched_keywords": [], "missing_keywords": [<JD must-have skills absent from resume>], '
                    '"exclude_by_default": true|false, "exclude_reason": "", "confidence": <0-100>}\n\n'
                    f"Resume:\n{resume_text[:2000]}\n\n"
                    f"Job title:\n{job_title[:200]}\n\n"
                    f"Job description:\n{job_description[:1500]}"
                ),
            }],
        )
        raw_text = _message_text(msg)
        data = _first_json_object(raw_text)
        if data is None:
            logger.warning(
                "claude_score: no JSON object in response; response_len=%s job_title=%r",
                len(raw_text), (job_title or "")[:80],
            )
            return _SCORE_FAILED
        return {
            "ats_score": int(data["ats_score"]),
            "role_relevance": int(data.get("role_relevance", data["ats_score"])),
            "job_family": str(data.get("job_family", "")),
            "matched_keywords": [str(k) for k in data.get("matched_keywords", [])],
            "missing_keywords": [str(k) for k in data.get("missing_keywords", [])],
            "exclude_by_default": bool(data.get("exclude_by_default", False)),
            "exclude_reason": str(data.get("exclude_reason", "")),
            "confidence": int(data.get("confidence", data.get("role_relevance", data["ats_score"]))),
        }
    except Exception as e:
        http_status = getattr(e, "status_code", None)
        api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
        logger.warning(
            "claude_score failed: exc_type=%s http_status=%s api_key_present=%s job_title=%r",
            type(e).__name__, http_status, api_key_present, (job_title or "")[:80],
        )
        return _SCORE_FAILED


def score_with_claude(
    df: pd.DataFrame,
    resume_text: str,
    skill_signals: list[str],
    total_yoe: int,
    client: "_anthropic.Anthropic | None",
    resume_tokens: set[str] | None = None,
    window_hours: int = config.DEFAULT_HOURS_OLD,
    target_profile: dict | None = None,
) -> pd.DataFrame:
    """Apply claude_score to each row; return all rows sorted by ats_score descending.
    Slicing to TOP_RESULTS happens in the caller after accumulation across windows."""
    if df.empty:
        return df
    df = add_role_relevance(df.copy(), target_profile)
    scores = []
    role_relevance = []
    job_families = []
    exclude_flags = []
    exclude_reasons = []
    matched_keywords = []
    missing_keywords = []
    confidence = []
    fallback_flags = []
    for _, row in df.iterrows():
        result = claude_score(
            row.get("description") or "",
            resume_text,
            skill_signals,
            total_yoe,
            client,
            target_profile,
            row.get("title") or "",
        )
        if result.get("ats_score") is None:
            scores.append(fallback_score_row(row, resume_tokens, window_hours))
            role_relevance.append(int(row.get("role_relevance") or 0))
            job_families.append(str(row.get("job_family") or "other"))
            exclude_flags.append(bool(row.get("exclude_by_default")))
            exclude_reasons.append(str(row.get("exclude_reason") or ""))
            matched_keywords.append([])
            missing_keywords.append([])
            confidence.append(int(row.get("role_relevance") or 0))
            fallback_flags.append(True)
        else:
            gate = classify_job_relevance(row, target_profile)
            excluded = bool(result.get("exclude_by_default", gate["exclude_by_default"]))
            relevance = max(0, min(100, int(result.get("role_relevance", gate["role_relevance"]))))
            score = max(0, min(100, int(result["ats_score"])))
            if excluded:
                score = min(score, 40, relevance)
            elif relevance < 55:
                score = min(score, 45)
            scores.append(score)
            role_relevance.append(relevance)
            job_families.append(str(result.get("job_family") or gate["job_family"]))
            exclude_flags.append(excluded)
            exclude_reasons.append(str(result.get("exclude_reason") or gate["exclude_reason"]))
            matched_keywords.append([str(k) for k in result.get("matched_keywords", [])])
            missing_keywords.append([str(k) for k in result.get("missing_keywords", [])])
            confidence.append(max(0, min(100, int(result.get("confidence", relevance)))))
            fallback_flags.append(False)
    df["ats_score"] = scores
    df["role_relevance"] = role_relevance
    df["job_family"] = job_families
    df["matched_keywords"] = matched_keywords
    df["missing_keywords"] = missing_keywords
    df["exclude_by_default"] = exclude_flags
    df["exclude_reason"] = exclude_reasons
    df["confidence"] = confidence
    df["used_fallback_score"] = fallback_flags
    df = apply_final_score_rules(df)
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

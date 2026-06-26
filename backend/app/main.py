"""FastAPI backend for the AI Engineer resume-matcher.

Endpoints:
  POST /api/analyze              upload resume + params -> {job_id}
  GET  /api/analyze/{job_id}     poll status / get results
  GET  /api/analyze/{job_id}/export.xlsx   download ranked results
  GET  /api/resume               stored resume summary
  GET  /api/health               healthcheck
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

import anthropic
import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import config, jobs_store, resume_parser, resume_store, scoring, export, dedup, job_cache

# ---------------------------------------------------------------------------
# App + rate limiter
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="AI Engineer Job Matcher")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_origins = os.environ.get("FRONTEND_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Anthropic client (None if key not set — functions fall back gracefully)
# ---------------------------------------------------------------------------

ai_client: anthropic.Anthropic | None = None
if os.environ.get("ANTHROPIC_API_KEY"):
    ai_client = anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        timeout=config.CLAUDE_TIMEOUT_SECONDS,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/resume")
def get_resume():
    stored = resume_store.load_resume()
    if not stored:
        return {"stored": False}
    analysis = resume_parser.normalize_resume_analysis({
        "search_titles": json.loads(stored.get("search_titles") or "[]"),
        "skill_signals": json.loads(stored.get("skill_signals") or "[]"),
        "target_profile": _stored_target_profile(stored),
        "total_yoe": stored.get("total_yoe") or 0,
    })
    return {
        "filename": stored.get("filename"),
        "search_titles": analysis["search_titles"],
        "skill_signals": analysis["skill_signals"],
        "target_profile": analysis["target_profile"],
        "total_yoe": analysis["total_yoe"],
        "email": stored.get("email"),
        "stored_at": stored.get("stored_at"),
    }


@app.post("/api/analyze")
@limiter.limit("4/day")
async def analyze(
    request: Request,
    resume: UploadFile | None = File(None),
    location: str = Form("United States"),
    is_remote: bool = Form(False),
    hours_old: int = Form(config.DEFAULT_HOURS_OLD),
    top_results: int = Form(config.TOP_RESULTS),
):
    if resume is not None:
        content: bytes | None = await resume.read()
        filename = resume.filename or ""
        if not content:
            raise HTTPException(400, "Empty resume file")
    else:
        # No file — use stored resume (ReadyToSearch path)
        _stored = resume_store.load_resume()
        if not _stored:
            raise HTTPException(400, "No resume provided and no stored resume found")
        filename = _stored["filename"]
        content = None  # signals _run_analysis to use stored resume directly

    job_id = jobs_store.create_job()
    asyncio.create_task(
        _run_analysis(job_id, filename, content, location, is_remote, hours_old, top_results)
    )
    return {"job_id": job_id}


@app.get("/api/analyze/{job_id}")
def get_status(job_id: str):
    job = jobs_store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found (it may have expired)")
    return {
        "status": job["status"],
        "message": job["message"],
        "results": job["results"],
        "low_confidence_results": job.get("low_confidence_results") or [],
        "error": job["error"],
    }


@app.get("/api/analyze/{job_id}/export.xlsx")
def export_xlsx(job_id: str):
    job = jobs_store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found (it may have expired)")
    if job["status"] != "done" or job.get("_df") is None:
        raise HTTPException(409, "Job not finished yet")
    data = export.dataframe_to_xlsx_bytes(job["_df"])
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=ai_engineer_jobs_{job_id[:8]}.xlsx"},
    )


# ---------------------------------------------------------------------------
# Background pipeline
# ---------------------------------------------------------------------------

def _resume_tokens_from_text(resume_text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-zA-Z][a-zA-Z+#.\-]{2,}", resume_text.lower())
        if t not in config.STOPWORDS
    }


def _cache_analysis_version(stored: dict | None) -> int:
    if not stored:
        return 0
    try:
        return int(stored.get("analysis_version") or 0)
    except (TypeError, ValueError):
        return 0


def _cache_analysis_current(stored: dict | None) -> bool:
    return _cache_analysis_version(stored) >= config.RESUME_ANALYSIS_VERSION


def _stored_search_titles(stored: dict | None) -> list[str]:
    if not stored:
        return []
    try:
        return json.loads(stored.get("search_titles") or "[]")
    except (TypeError, json.JSONDecodeError):
        return []


def _cache_analysis_valid(stored: dict | None) -> bool:
    return len(resume_parser.sanitize_search_titles(_stored_search_titles(stored))) >= 3


def _stored_target_profile(stored: dict | None) -> dict | None:
    if not stored:
        return None
    try:
        return json.loads(stored.get("target_profile") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None


def _recent_shortlist(df: pd.DataFrame, limit: int) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    if "date_posted" not in df.columns:
        df["date_posted"] = None
    return df.sort_values("date_posted", ascending=False, na_position="last").head(limit).reset_index(drop=True)


def _update_trail(job_id: str, trail: list[str]) -> None:
    jobs_store.update_job(job_id, message=" → ".join(trail))


async def _run_cached_search(
    job_id: str,
    resume_text: str,
    resume_tokens: set[str],
    skill_signals: list[str],
    target_profile: dict,
    total_yoe: int | None,
    location: str,
    is_remote: bool,
    hours_old: int,
    top_results: int,
    trail: list[str],
) -> None:
    windows = [hours_old] + [h for h in config.FALLBACK_HOURS if h > hours_old]
    resume_hash_value = dedup.resume_hash(
        resume_text, target_profile.get("primary_track", "applied_ai_ml")
    )
    used_seen_fallback = False
    used_prefilter_bypass = False
    used_score_fallback = False
    cache_refreshed = False

    cache_age = await asyncio.to_thread(job_cache.get_cache_age_minutes)
    cache_count = await asyncio.to_thread(job_cache.count_jobs)
    source_counts = await asyncio.to_thread(job_cache.get_latest_source_counts)
    age_label = "unknown" if cache_age is None else f"{cache_age:.1f}m"
    trail.append(f"Job cache age: {age_label}; cached jobs: {cache_count}")
    trail.append(job_cache.format_source_counts(source_counts))
    _update_trail(job_id, trail)

    cache_stale = await asyncio.to_thread(
        job_cache.is_cache_stale, config.JOB_CACHE_MAX_AGE_MINUTES
    )
    if cache_count == 0 or cache_stale:
        cache_refreshed = True
        trail.append("Refreshing job cache...")
        _update_trail(job_id, trail)
        refresh = await asyncio.to_thread(
            job_cache.refresh_job_cache,
            True,
            location,
            is_remote,
            config.JOB_CACHE_REFRESH_HOURS,
        )
        trail.append(
            f"Cache refresh {refresh.get('status')}: raw {refresh.get('raw_count', 0)}, "
            f"inserted {refresh.get('inserted_count', 0)}, updated {refresh.get('updated_count', 0)}"
        )
        trail.append(job_cache.format_source_counts(refresh.get("source_counts", {})))
        _update_trail(job_id, trail)

    raw_df = pd.DataFrame()
    selected_window = windows[-1]
    for window in windows:
        candidate_df = await asyncio.to_thread(job_cache.get_recent_jobs, window)
        trail.append(f"{window}h cached jobs selected: {len(candidate_df)}")
        _update_trail(job_id, trail)
        raw_df = candidate_df
        selected_window = window
        if len(candidate_df) >= top_results:
            break

    if raw_df.empty:
        jobs_store.set_results(
            job_id,
            pd.DataFrame(),
            message="No strong AI/ML matches found right now because the job cache is empty.",
            low_confidence_df=pd.DataFrame(),
        )
        return

    raw_df = await asyncio.to_thread(scoring.dedupe_display_jobs, raw_df)
    relevant_pool = await asyncio.to_thread(scoring.add_role_relevance, raw_df, target_profile)
    allowed_count = int((~relevant_pool["exclude_by_default"].fillna(True)).sum())
    trail.append(f"AI/ML relevant cached jobs: {allowed_count}")
    _update_trail(job_id, trail)

    if allowed_count < top_results:
        cache_refreshed = True
        target_titles = target_profile.get("target_titles") or config.DEFAULT_SEARCH_TITLES
        trail.append("Refreshing job cache with target AI/ML titles")
        _update_trail(job_id, trail)
        refresh = await asyncio.to_thread(
            job_cache.refresh_job_cache,
            True,
            location,
            is_remote,
            config.JOB_CACHE_REFRESH_HOURS,
            None,
            target_titles,
        )
        trail.append(
            f"Targeted refresh {refresh.get('status')}: raw {refresh.get('raw_count', 0)}, "
            f"inserted {refresh.get('inserted_count', 0)}, updated {refresh.get('updated_count', 0)}"
        )
        trail.append(job_cache.format_source_counts(refresh.get("source_counts", {})))
        _update_trail(job_id, trail)
        refreshed_df = await asyncio.to_thread(job_cache.get_recent_jobs, selected_window)
        if not refreshed_df.empty:
            raw_df = await asyncio.to_thread(scoring.dedupe_display_jobs, refreshed_df)
            relevant_pool = await asyncio.to_thread(scoring.add_role_relevance, raw_df, target_profile)
            allowed_count = int((~relevant_pool["exclude_by_default"].fillna(True)).sum())
            trail.append(f"AI/ML relevant jobs after target refresh: {allowed_count}")
            _update_trail(job_id, trail)

    excluded_pool = relevant_pool[relevant_pool["exclude_by_default"].fillna(True)].copy()
    allowed_pool = relevant_pool[~relevant_pool["exclude_by_default"].fillna(True)].reset_index(drop=True)

    if allowed_pool.empty:
        low_confidence = scoring.fallback_score_dataframe(
            excluded_pool, resume_tokens, selected_window, target_profile
        )
        low_confidence = scoring.dedupe_display_jobs(scoring.sort_scored(low_confidence)).head(top_results)
        jobs_store.set_results(
            job_id,
            pd.DataFrame(),
            message="No strong AI/ML matches found right now. Lower the score filter to see broader roles.",
            low_confidence_df=low_confidence,
        )
        return

    unseen_df, seen_df = await asyncio.to_thread(dedup.split_seen, allowed_pool, resume_hash_value)
    if not unseen_df.empty:
        unseen_df = unseen_df.copy()
        unseen_df["seen_before"] = False
    trail.append(f"Unseen cached jobs: {len(unseen_df)}")

    if len(unseen_df) < top_results and not seen_df.empty:
        needed = top_results - len(unseen_df)
        candidate_df = pd.concat([unseen_df, seen_df.head(needed)], ignore_index=True)
        used_seen_fallback = True
        trail.append(f"Seen-fill jobs: {min(needed, len(seen_df))}")
    else:
        candidate_df = unseen_df
        trail.append("Seen-fill jobs: 0")
    _update_trail(job_id, trail)

    if candidate_df.empty:
        candidate_df = allowed_pool.copy()
        candidate_df["seen_before"] = True
        used_seen_fallback = True
        trail.append(f"All relevant cached jobs were seen; scoring relevant pool: {len(candidate_df)}")
        _update_trail(job_id, trail)

    filtered = await asyncio.to_thread(scoring.prefilter, candidate_df, resume_tokens)
    if filtered.empty and not candidate_df.empty:
        filtered = _recent_shortlist(candidate_df, config.PREFILTER_BYPASS_LIMIT)
        used_prefilter_bypass = True
        trail.append(f"Jobs after prefilter: 0; scoring recent cached shortlist: {len(filtered)}")
    else:
        trail.append(f"Jobs after prefilter: {len(filtered)}")
    _update_trail(job_id, trail)

    if filtered.empty:
        filtered = _recent_shortlist(allowed_pool, config.PREFILTER_BYPASS_LIMIT)
        used_prefilter_bypass = True
        trail.append(f"Showing best recent matches from cached jobs: {len(filtered)}")
        _update_trail(job_id, trail)

    try:
        scored = await asyncio.wait_for(
            asyncio.to_thread(
                scoring.score_with_claude,
                filtered,
                resume_text,
                skill_signals,
                total_yoe,
                ai_client,
                resume_tokens,
                selected_window,
                target_profile,
            ),
            timeout=config.SCRAPE_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.warning("score_with_claude failed, using fallback scores: %s", e)
        scored = await asyncio.to_thread(
            scoring.fallback_score_dataframe, filtered, resume_tokens, selected_window, target_profile
        )

    fallback_count = (
        int(scored["used_fallback_score"].fillna(False).sum())
        if "used_fallback_score" in scored.columns
        else 0
    )
    if fallback_count:
        used_score_fallback = True
    trail.append(f"Jobs scored: {len(scored)}")
    if fallback_count:
        trail.append(f"Fallback scores used: {fallback_count}")
    _update_trail(job_id, trail)

    if scored.empty:
        jobs_store.set_results(
            job_id,
            pd.DataFrame(),
            message="No strong AI/ML matches found right now. Lower the score filter to see broader roles.",
            low_confidence_df=pd.DataFrame(),
        )
        return

    strong = scored[
        (~scored["exclude_by_default"].fillna(False))
        & (scored["ats_score"].fillna(0) >= config.DEFAULT_MIN_ATS_SCORE)
    ].copy()
    scored_low = scored.drop(strong.index, errors="ignore")
    excluded_low = scoring.fallback_score_dataframe(
        excluded_pool, resume_tokens, selected_window, target_profile
    )
    low_parts = [part for part in (scored_low, excluded_low) if not part.empty]
    low_confidence = (
        scoring.dedupe_display_jobs(scoring.sort_scored(pd.concat(low_parts, ignore_index=True)))
        if low_parts
        else pd.DataFrame()
    ).head(top_results).reset_index(drop=True)

    final = scoring.sort_scored(strong).head(top_results).reset_index(drop=True)
    final.insert(0, "rank", range(1, len(final) + 1))
    if not low_confidence.empty:
        low_confidence.insert(0, "rank", range(1, len(low_confidence) + 1))
    trail.append(f"Final jobs returned: {len(final)}")
    trail.append(f"Low-confidence jobs separated: {len(low_confidence)}")
    _update_trail(job_id, trail)

    await asyncio.to_thread(dedup.mark_seen, final, resume_hash_value)

    notes = []
    if cache_refreshed:
        notes.append("job cache refreshed")
    if used_seen_fallback:
        notes.append("No brand-new jobs found, showing best recent matches including previously seen jobs.")
    if used_prefilter_bypass:
        notes.append("keyword prefilter was relaxed; showing best recent matches from cached jobs")
    if used_score_fallback:
        notes.append("fallback scoring used for some jobs")

    n = len(final)
    if n == 0:
        done_msg = "No strong AI/ML matches found right now. Lower the score filter to see broader roles."
    elif n < top_results:
        done_msg = f"Found {n} strong AI/ML match{'' if n == 1 else 'es'}. Lower the score filter to see broader roles."
    else:
        done_msg = f"Found {n} strong AI/ML match{'' if n == 1 else 'es'}"
    if notes:
        done_msg += " (" + "; ".join(notes) + ")"
    done_msg += " | " + " → ".join(trail)
    jobs_store.set_results(job_id, final, message=done_msg, low_confidence_df=low_confidence)


async def _run_analysis(
    job_id: str,
    filename: str,
    content: bytes | None,
    location: str,
    is_remote: bool,
    hours_old: int,
    top_results: int = config.TOP_RESULTS,
) -> None:
    try:
        # Step 1: Resume — use cache if same filename, else parse + extract keywords
        jobs_store.update_job(job_id, status="running", message="Reading your resume")

        stored = resume_store.load_resume()
        if content is None or (stored and stored.get("filename") == filename):
            # Use stored resume text. Re-run analysis if its cached schema/prompt is stale.
            if not stored:
                raise ValueError("No stored resume found")
            resume_text = stored["text"]
            resume_tokens = _resume_tokens_from_text(resume_text)
            if _cache_analysis_current(stored) and _cache_analysis_valid(stored):
                kw_dict = resume_parser.normalize_resume_analysis({
                    "search_titles": _stored_search_titles(stored),
                    "skill_signals": json.loads(stored.get("skill_signals") or "[]"),
                    "target_profile": _stored_target_profile(stored),
                    "total_yoe": stored.get("total_yoe") or 0,
                })
            else:
                kw_dict = await asyncio.wait_for(
                    asyncio.to_thread(resume_parser.extract_keywords, resume_text, ai_client),
                    timeout=config.CLAUDE_TIMEOUT_SECONDS,
                )
                kw_dict = resume_parser.normalize_resume_analysis(kw_dict)
                resume_store.save_resume(
                    filename=stored.get("filename") or filename,
                    text=resume_text,
                    search_titles=json.dumps(kw_dict["search_titles"]),
                    skill_signals=json.dumps(kw_dict["skill_signals"]),
                    target_profile=json.dumps(kw_dict["target_profile"]),
                    total_yoe=kw_dict["total_yoe"],
                    email=stored.get("email"),
                    phone=stored.get("phone"),
                    analysis_version=config.RESUME_ANALYSIS_VERSION,
                )
            search_titles = kw_dict["search_titles"]
            skill_signals = kw_dict["skill_signals"]
            target_profile = kw_dict["target_profile"]
            total_yoe = kw_dict["total_yoe"]
        else:
            parsed = await asyncio.to_thread(resume_parser.parse_resume, filename, content)
            resume_text = parsed["text"]
            resume_tokens = parsed["tokens"]
            kw_dict = await asyncio.wait_for(
                asyncio.to_thread(resume_parser.extract_keywords, resume_text, ai_client),
                timeout=config.CLAUDE_TIMEOUT_SECONDS,
            )
            kw_dict = resume_parser.normalize_resume_analysis(kw_dict)
            search_titles = kw_dict.get("search_titles", [])
            skill_signals = kw_dict.get("skill_signals", [])
            target_profile = kw_dict.get("target_profile", config.DEFAULT_TARGET_PROFILE)
            total_yoe = kw_dict.get("total_yoe")
            resume_store.save_resume(
                filename=filename,
                text=resume_text,
                search_titles=json.dumps(search_titles),
                skill_signals=json.dumps(skill_signals),
                target_profile=json.dumps(target_profile),
                total_yoe=total_yoe,
                email=parsed.get("email"),
                phone=parsed.get("phone"),
                analysis_version=config.RESUME_ANALYSIS_VERSION,
            )

        normalized_analysis = resume_parser.normalize_resume_analysis({
            "search_titles": search_titles,
            "skill_signals": skill_signals,
            "target_profile": target_profile,
            "total_yoe": total_yoe,
        })
        search_titles = normalized_analysis["search_titles"]
        skill_signals = normalized_analysis["skill_signals"]
        target_profile = normalized_analysis["target_profile"]
        total_yoe = normalized_analysis["total_yoe"]

        # Accumulated stage trail — appended after each pipeline step and pushed
        # to the job message so the full path is visible in the frontend without
        # needing Railway logs or DevTools.
        trail: list[str] = [f"Search titles used: {search_titles}"]
        jobs_store.update_job(job_id, message=" → ".join(trail))
        logger.info("search_titles=%s skill_signals=%s", search_titles, skill_signals)

        await _run_cached_search(
            job_id=job_id,
            resume_text=resume_text,
            resume_tokens=resume_tokens,
            skill_signals=skill_signals,
            target_profile=target_profile,
            total_yoe=total_yoe,
            location=location,
            is_remote=is_remote,
            hours_old=hours_old,
            top_results=top_results,
            trail=trail,
        )
        return

    except Exception as e:
        jobs_store.update_job(job_id, status="error", message="Something went wrong", error=str(e))


# StreamingResponse needs io
import io  # noqa: E402 — kept at bottom to avoid circular-looking import at top

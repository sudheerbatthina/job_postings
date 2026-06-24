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
import os
import re

import anthropic
import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import config, jobs_store, resume_parser, resume_store, scraper, scoring, export, dedup

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
    return {
        "filename": stored.get("filename"),
        "search_titles": json.loads(stored.get("search_titles") or "[]"),
        "skill_signals": json.loads(stored.get("skill_signals") or "[]"),
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

async def _run_analysis(
    job_id: str,
    filename: str,
    content: bytes,
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
            # Use cached resume (no file uploaded, or same file re-uploaded)
            if not stored:
                raise ValueError("No stored resume found")
            resume_text = stored["text"]
            search_titles = json.loads(stored.get("search_titles") or "[]")
            skill_signals = json.loads(stored.get("skill_signals") or "[]")
            resume_tokens = {
                t for t in re.findall(r"[a-zA-Z][a-zA-Z+#.\-]{2,}", resume_text.lower())
                if t not in config.STOPWORDS
            }
        else:
            parsed = await asyncio.to_thread(resume_parser.parse_resume, filename, content)
            resume_text = parsed["text"]
            resume_tokens = parsed["tokens"]
            kw_dict = await asyncio.wait_for(
                asyncio.to_thread(resume_parser.extract_keywords, resume_text, ai_client),
                timeout=config.CLAUDE_TIMEOUT_SECONDS,
            )
            search_titles = kw_dict.get("search_titles", [])
            skill_signals = kw_dict.get("skill_signals", [])
            resume_store.save_resume(
                filename=filename,
                text=resume_text,
                search_titles=json.dumps(search_titles),
                skill_signals=json.dumps(skill_signals),
                email=parsed.get("email"),
                phone=parsed.get("phone"),
            )

        if not search_titles:
            search_titles = ["AI Engineer", "Machine Learning Engineer"]

        # Steps 2-6: window retry loop — widen search until we have top_results
        windows = [hours_old] + [h for h in config.FALLBACK_HOURS if h > hours_old]
        # Dict keyed by job_url prevents re-scoring the same job across windows
        all_scored: dict[str, dict] = {}
        timed_out = False

        def progress(msg: str) -> None:
            jobs_store.update_job(job_id, message=msg)

        for window in windows:
            if len(all_scored) >= top_results:
                break

            try:
                # Step 2: Scrape — timeout inside the semaphore so a hang still releases it
                async with jobs_store.SCRAPE_SEMAPHORE:
                    jobs_store.update_job(job_id, message=f"Searching jobs from last {window}h…")
                    df = await asyncio.wait_for(
                        asyncio.to_thread(
                            scraper.scrape_all, location, is_remote, window, progress,
                            search_terms=search_titles,
                        ),
                        timeout=config.SCRAPE_TIMEOUT_SECONDS,
                    )

                if df.empty:
                    continue

                # Step 3: Dedup — drop jobs already returned in previous runs
                df = await asyncio.to_thread(dedup.filter_unseen, df)

                # Drop URLs already accumulated in earlier windows of this run
                if all_scored and "job_url" in df.columns:
                    df = df[~df["job_url"].isin(all_scored.keys())].reset_index(drop=True)

                if df.empty:
                    continue

                # Step 4: Prefilter — fast token-overlap stage
                jobs_store.update_job(job_id, message="Filtering matches…")
                filtered = await asyncio.to_thread(scoring.prefilter, df, resume_tokens)

                if filtered.empty:
                    continue

                # Step 5: Claude scoring — returns all rows sorted by score, no threshold
                jobs_store.update_job(job_id, message="Scoring with AI…")
                scored = await asyncio.wait_for(
                    asyncio.to_thread(
                        scoring.score_with_claude, filtered, resume_text, skill_signals, ai_client
                    ),
                    timeout=config.SCRAPE_TIMEOUT_SECONDS,
                )

                for _, row in scored.iterrows():
                    url = row.get("job_url")
                    if url and url not in all_scored:
                        all_scored[url] = row.to_dict()

            except asyncio.TimeoutError:
                jobs_store.update_job(job_id, message=f"Timed out on {window}h window — using results so far…")
                timed_out = True
                break

        if not all_scored:
            msg = (
                "Job boards were too slow to respond — try again in a few minutes"
                if timed_out
                else "No matching jobs found — try checking back later"
            )
            jobs_store.update_job(job_id, status="done", message=msg, results=[])
            return

        # Step 6: Assemble final top-N across all windows, re-rank
        rows = sorted(all_scored.values(), key=lambda r: r.get("claude_score", 0), reverse=True)
        final = pd.DataFrame(rows[:top_results]).reset_index(drop=True)
        final.insert(0, "rank", range(1, len(final) + 1))

        # Step 7: Mark only the returned jobs as seen
        await asyncio.to_thread(dedup.mark_seen, final)

        n = len(final)
        done_msg = (
            f"Found {n} job{'' if n == 1 else 's'} (search cut short — a job board was slow)"
            if timed_out
            else f"Found {n} matching job{'' if n == 1 else 's'}"
        )
        jobs_store.set_results(job_id, final, message=done_msg)

    except Exception as e:
        jobs_store.update_job(job_id, status="error", message="Something went wrong", error=str(e))


# StreamingResponse needs io
import io  # noqa: E402 — kept at bottom to avoid circular-looking import at top

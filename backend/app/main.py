"""FastAPI backend for the AI Engineer resume-matcher.

Endpoints:
  POST /api/analyze              upload resume + params -> {job_id}
  GET  /api/analyze/{job_id}     poll status / get results
  GET  /api/analyze/{job_id}/export.xlsx   download ranked results
  GET  /api/health               healthcheck
"""

from __future__ import annotations
import asyncio
import os

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import io

from . import config, jobs_store, resume_parser, scraper, scoring, export

app = FastAPI(title="AI Engineer Job Matcher")

_origins = os.environ.get("FRONTEND_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/analyze")
async def analyze(
    resume: UploadFile = File(...),
    location: str = Form("United States"),
    is_remote: bool = Form(False),
    hours_old: int = Form(config.DEFAULT_HOURS_OLD),
    top_results: int = Form(config.TOP_RESULTS),
):
    content = await resume.read()
    if not content:
        raise HTTPException(400, "Empty resume file")

    job_id = jobs_store.create_job()
    asyncio.create_task(
        _run_analysis(job_id, resume.filename, content, location, is_remote, hours_old, top_results)
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


async def _run_analysis(job_id, filename, content, location, is_remote, hours_old, top_results=config.TOP_RESULTS):
    try:
        jobs_store.update_job(job_id, status="running", message="Reading your resume")
        parsed = await asyncio.to_thread(resume_parser.parse_resume, filename, content)

        async with jobs_store.SCRAPE_SEMAPHORE:
            jobs_store.update_job(job_id, message="Scraping job boards (this can take a minute)")

            def progress(msg: str):
                jobs_store.update_job(job_id, message=msg)

            df = await asyncio.to_thread(
                scraper.scrape_all, location, is_remote, hours_old, progress
            )

        if df.empty:
            jobs_store.update_job(
                job_id, status="done",
                message="No jobs found — try widening the date range or check back later",
                results=[],
            )
            return

        jobs_store.update_job(job_id, message="Scoring matches against your resume")
        ranked = await asyncio.to_thread(scoring.score_and_rank, df, parsed["tokens"], hours_old, top_results)

        if ranked.empty:
            jobs_store.update_job(
                job_id, status="done", message="No matching jobs found — try widening the date range", results=[]
            )
        else:
            jobs_store.set_results(job_id, ranked)
    except Exception as e:
        jobs_store.update_job(job_id, status="error", message="Something went wrong", error=str(e))

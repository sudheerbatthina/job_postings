# Job Matcher

Upload a resume, get back recently posted roles ranked by how well they match
your background, with apply links.

- `/backend` — FastAPI + JobSpy. Deploy to Railway (root directory: `backend`).
- `/frontend` — Vite + React + Tailwind. Deploy to Vercel (root directory: `frontend`).

## Local development

Backend:
```bash
cd backend
pip install -r requirements.txt
pip install python-jobspy --no-deps
uvicorn app.main:app --reload
```

Set `ANTHROPIC_API_KEY` in the environment (or in `backend/.env`). Without it,
keyword extraction falls back to bigrams and all jobs score 0.

Frontend (separate terminal):
```bash
cd frontend
npm install
npm run dev
```
Frontend reads the backend URL from `VITE_API_URL` (see `.env.example`).

## Deploying

**Railway** (backend): new project from this repo, set root directory to
`backend`. It auto-detects Python via `.python-version` + `railway.json`.
Set env var `FRONTEND_ORIGIN` to your Vercel URL once it exists (comma-separate
multiple origins). Set `ANTHROPIC_API_KEY` and optionally `DEDUP_DB_PATH`
(defaults to `/data/seen_jobs.db` on the persistent volume). Healthcheck is
`/api/health`.

**Vercel** (frontend): new project from this repo, set root directory to
`frontend`, framework preset "Vite". Set env var `VITE_API_URL` to your
Railway backend URL (e.g. `https://your-app.up.railway.app`).

## How it works
Upload triggers a background scrape of LinkedIn, Indeed, Google, ZipRecruiter,
and optionally Glassdoor (city-level locations only) via JobSpy. Claude Haiku
first extracts target role titles and differentiating skills from the resume; those
titles drive the job board searches. Each shortlisted job is then scored 0–100 by
Claude Haiku based on resume fit, and the top 10 are returned ranked.

Resumes are stored between sessions in SQLite so returning users can search again
without re-uploading. Results live in memory for about an hour. Scraping is
serialized behind a single semaphore so the app never hits job boards with more
than one concurrent request from this server's IP.

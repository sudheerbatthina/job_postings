# AI Engineer Job Matcher

Upload a resume, get back recently posted AI-engineer roles ranked by how
well they match it, with apply links.

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

Frontend (separate terminal):
```bash
cd frontend
npm install
npm run dev
```
Frontend reads the backend URL from `VITE_API_URL` (see `.env.example`).

## Deploying

**Railway** (backend): new project from this repo, set root directory to
`backend`. It auto-detects Python via `nixpacks.toml` + `railway.json`.
Set env var `FRONTEND_ORIGIN` to your Vercel URL once it exists (comma-separate
multiple origins). Healthcheck is `/api/health`.

**Vercel** (frontend): new project from this repo, set root directory to
`frontend`, framework preset "Vite". Set env var `VITE_API_URL` to your
Railway backend URL (e.g. `https://your-app.up.railway.app`).

## How it works
Upload triggers a background scrape (LinkedIn, Indeed, Google, ZipRecruiter
via JobSpy) across a fixed set of AI-engineer search terms. Each result is
scored 0–100 from keyword relevance + resume-token overlap + recency, then
returned ranked. No database — results live in memory for about an hour,
resumes are parsed and discarded, never stored. Scraping is serialized
behind a single semaphore so the app never hits job boards with more than
one concurrent request from this server's IP.

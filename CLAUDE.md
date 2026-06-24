# CLAUDE.md — Job Matcher Codebase Reference

Dense reference for future Claude Code sessions. Everything here is derived from
reading the actual code, not planning notes. Discrepancies vs planning notes are
flagged explicitly at the bottom.

---

## What this is

Resume-driven job matcher. User uploads a resume once; Claude extracts the candidate's
target role titles and differentiating tech skills; those drive live JobSpy scrapes of
five job boards; Claude scores each shortlisted job 0-100 against the full resume;
top 10 are returned ranked, with apply links. **Not AI-engineering-specific** — works
for any resume/field (though legacy config constants reference AI/ML).

---

## Architecture

```
Browser (React SPA on Vercel)
│
│  GET /api/resume  ──────────────────────────────────────────────────────────┐
│  POST /api/analyze (multipart: resume?, location, is_remote)                │
│  GET /api/analyze/{job_id}  (poll, 2 s interval, 90 poll max = 3 min)      │
│  GET /api/analyze/{job_id}/export.xlsx                                      │
│                                                                              │
▼                                                                              │
FastAPI (Python 3.11, Railway)                                                 │
│                                                                              │
│  POST /api/analyze                                                           │
│    └─ asyncio.create_task(_run_analysis(...))  ← returns job_id immediately │
│                                                                              │
│  _run_analysis() pipeline:                                                   │
│                                                                              │
│  [STEP 1 — outside semaphore]                                                │
│    load_resume() ─────────────────────────────────────────► SQLite           │
│    if content=None or filename matches stored:                               │
│      use cached search_titles, skill_signals, re-derive resume_tokens       │
│    else:                                                                     │
│      parse_resume() ── to_thread                                            │
│      extract_keywords() ── wait_for(30s) ──────────────────► Claude Haiku   │
│        returns {"search_titles": [...], "skill_signals": [...]}              │
│      save_resume() ───────────────────────────────────────► SQLite           │
│                                                                              │
│  [STEP 2-5 — window loop: [24h] then optionally [72h]]                      │
│    for each window (break if all_scored ≥ 10 or TimeoutError):              │
│                                                                              │
│    async with SCRAPE_SEMAPHORE (global, capacity=1):                        │
│      wait_for(120s): scrape_all() ── to_thread                              │
│        is_specific_location(location)?  ─── comma check                     │
│        sites = SITES  or  SITES minus "glassdoor"                           │
│        for each search_title (SEQUENTIAL):                                  │
│          _scrape_one() ── tenacity retry(2, wait=3s)                        │
│            ── LinkedIn / Indeed / Google / ZipRecruiter [/ Glassdoor]       │
│        returns combined DataFrame                                            │
│    ← semaphore released here                                                 │
│                                                                              │
│    filter_unseen() ── to_thread ─────────────────────────► SQLite           │
│    drop URLs already in all_scored (cross-window dedup)                     │
│    prefilter() ── to_thread  (token overlap ≥ 0.15, keep ≤ 50)             │
│    wait_for(120s): score_with_claude() ── to_thread                         │
│      for each job (SEQUENTIAL, one API call per job):                       │
│        claude_score(desc, resume_text, skill_signals) ──► Claude Haiku      │
│      returns DataFrame sorted by claude_score desc (no threshold)           │
│    accumulate into all_scored dict keyed by job_url                         │
│    on asyncio.TimeoutError: timed_out=True, break                           │
│                                                                              │
│  [STEP 6 — assembly]                                                         │
│    sort all_scored by claude_score desc, slice to top_results (10)          │
│    mark_seen() ── to_thread ─────────────────────────────► SQLite           │
│       (ONLY the returned jobs are marked, not everything scraped)            │
│    jobs_store.set_results() ─────────────────────────────► JOBS dict        │
│                                                                              │
└───────────────────────────────────────────────────────────────────────────── │
                                                                               │
SQLite (/data/seen_jobs.db in prod, ./seen_jobs.db locally)                   │
  table seen_jobs: (job_url PK, seen_at TEXT)  — 24h auto-expiry              │
  table resume:    (id, filename, text, search_titles, skill_signals,          │
                    email, phone, stored_at) — 1 row max                      │
                                                                               │
JOBS dict (in-memory, pruned on create_job(), 1h TTL)                        │
  keys: job_id (uuid hex)                                                      │
  values: {status, message, results, error, created_at, _df}                  │
  _df is the raw DataFrame kept only for xlsx export                           │
```

---

## Frontend state machine

```
mount
  └─ getStoredResume()
       ├─ data.filename truthy → stage="ready"
       └─ else            → stage="upload"

"upload"   UploadForm        → submit  → stage="polling" → POST /api/analyze (with file)
"ready"    ReadyToSearch     → submit  → stage="polling" → POST /api/analyze (no file)
                             → replace → stage="upload", storedResume=null
"polling"  ProgressView      → poll GET /api/analyze/{id} every 2s
               done  → stage="done",  storedResume refreshed via getStoredResume()
               error → stage="error"
               >90 polls (3 min) → stage="error"  (rate-limit message)
"done"     ResultsTable      → "New search" → stage="ready" if storedResume, else "upload"
"error"    error text        → "Try again"  → same reset logic
```

---

## Tech stack

| Layer | Tech |
|-------|------|
| Frontend | React 19, Vite 8, Tailwind 4, react-dropzone, lucide-react |
| Backend | FastAPI 0.110+, Python **3.11** (pinned), uvicorn |
| Scraping | python-jobspy ≥1.1.80, tenacity (retry) |
| AI | anthropic ≥0.25, claude-haiku-4-5 (keyword extraction + scoring) |
| Persistence | SQLite (stdlib), two tables in one file |
| Rate limiting | slowapi (4 /api/analyze per IP per day) |
| PDF/DOCX | pdfplumber, python-docx |
| Deploy | Railway (backend, Nixpacks), Vercel (frontend) |

---

## Directory layout

```
job_postings/
├── CLAUDE.md              ← this file
├── DECISIONS.md           ← architectural choices log
├── README.md              ← user-facing overview
├── README (1).md          ← legacy CLI script docs (ai_engineer_jobs.py), ignore
├── ai_engineer_jobs.py    ← original CLI script, not part of the web app
│
├── backend/
│   ├── .python-version    ← "3.11" — Railway reads this
│   ├── .railwayignore     ← excludes tests/, *.md, .env from Railway deploy
│   ├── railway.json       ← start command + healthcheck path
│   ├── requirements.txt   ← pip deps (NOT including jobspy — see below)
│   ├── INSTALL.md         ← numpy/jobspy install gotcha docs
│   └── app/
│       ├── config.py      ← all constants (sites, timeouts, top_results, etc.)
│       ├── main.py        ← FastAPI app, endpoints, _run_analysis pipeline
│       ├── scraper.py     ← JobSpy wrapper, Glassdoor gating, tenacity retry
│       ├── scoring.py     ← prefilter() + score_with_claude() + legacy score_and_rank()
│       ├── resume_parser.py  ← PDF/DOCX/TXT parse + Claude keyword extraction
│       ├── resume_store.py   ← SQLite resume cache (1-row table)
│       ├── dedup.py          ← SQLite seen-jobs dedup (24h expiry)
│       ├── jobs_store.py     ← in-memory job tracking + SCRAPE_SEMAPHORE
│       └── export.py         ← xlsx builder (NOTE: see gotchas re: score column)
│
└── frontend/
    ├── vite.config.js
    ├── package.json
    └── src/
        ├── App.jsx           ← stage machine, polling, storedResume state
        ├── api.js            ← fetch wrappers for all 4 endpoints
        └── components/
            ├── UploadForm.jsx     ← file dropzone + location + remote checkbox
            ├── ReadyToSearch.jsx  ← uses stored resume; shows keyword pills (see gotchas)
            ├── ProgressView.jsx   ← spinner + backend message
            └── ResultsTable.jsx   ← ranked list, min-score slider, remote filter, xlsx link
```

---

## Configuration constants (backend/app/config.py)

```python
SITES = ["linkedin", "indeed", "google", "zip_recruiter", "glassdoor"]
RESULTS_WANTED_PER_TERM = 25
DEFAULT_HOURS_OLD = 24
FALLBACK_HOURS = [72]          # Only one fallback window, and only if < TOP_RESULTS found
TOP_RESULTS = 10
SCRAPE_TIMEOUT_SECONDS = 120
CLAUDE_TIMEOUT_SECONDS = 30    # also set on the Anthropic client at init
JOB_TTL_SECONDS = 3600         # 1 hour, jobs pruned from JOBS dict on next create_job()
```

The `AI_KEYWORDS`, `TITLE_BLOCKLIST`, `KW_NORM`, `WEIGHTS` etc. are still in config.py
but only used by `score_and_rank()` which is **not called from the API pipeline**
(it's legacy/CLI only). The live pipeline uses Claude scores, not these weights.

---

## How to run tests

```bash
cd backend
python -m pytest tests/test_api.py -v
```

All tests mock: `scraper.scrape_all`, `dedup.filter_unseen/mark_seen`,
`resume_store.load_resume/save_resume`, `resume_parser.extract_keywords`,
`scoring.score_with_claude`. No network calls, no API keys needed.

An `autouse` fixture resets slowapi's in-memory rate-limit storage before each
test to prevent 429s from bleeding across tests.

---

## How to run locally

**Backend** (Python 3.11 recommended — see INSTALL.md for 3.13 workaround):
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Required env vars:
- `ANTHROPIC_API_KEY` — if missing, Claude calls are skipped; keyword extraction
  falls back to top bigrams, scoring returns 0 for all jobs
- `FRONTEND_ORIGIN` — CORS allow-list (default `*`)
- `DEDUP_DB_PATH` — SQLite path (default `/data/seen_jobs.db`, falls back to `./seen_jobs.db`)

**Frontend**:
```bash
cd frontend
npm install
npm run dev      # http://localhost:5173
```

Create `frontend/.env.local`:
```
VITE_API_URL=http://localhost:8000
```

---

## Known gotchas

### Python version (critical)
`python-jobspy` pins `numpy==1.26.3`. No Python 3.13 wheel exists for this on
Windows/macOS. **Use Python 3.11** (matches Railway). On 3.13, do
`pip install python-jobspy --no-deps` to bypass the pin.

### ZipRecruiter 403s (permanent, unfixable)
ZipRecruiter blocks scraping via Cloudflare WAF. Expect 403s on every run.
Tenacity retries won't help (not a transient error — it's a WAF block). Results
will simply have no ZipRecruiter listings. Not fixable without paid proxies.

### Glassdoor requires a specific city
`is_specific_location(location)` checks `"," in location`. A comma means specific
enough. "United States", "Remote", "San Francisco Bay Area" → Glassdoor excluded.
"New York, NY", "Austin, TX" → Glassdoor included. The comma check is a heuristic;
it isn't bulletproof (e.g. "Bay Area, California" passes but may still 400 on
Glassdoor). On_progress logs "Skipping Glassdoor — needs a specific city, not a
broad location." when excluded.

### Semaphore + wait_for design (production incident prevention)
A global `asyncio.Semaphore(1)` in `jobs_store.py` serializes all scraping so the
server never hits job boards concurrently from one IP. Previously, a hung scrape or
Claude call could hold this semaphore forever, wedging all subsequent requests.
`asyncio.wait_for` timeouts are placed:
- INSIDE the semaphore block for scrape_all (120s) — timeout releases semaphore
- OUTSIDE the semaphore block for score_with_claude (120s)
- OUTSIDE the semaphore, before the loop, for extract_keywords (30s)
The Anthropic client is also initialized with `timeout=30` as a second layer.

### Sequential scraping (not concurrent)
`scrape_all()` loops over search_titles sequentially. With 4-6 titles × 5 boards
× 25 results_wanted, a full run can take 60-120 seconds. Parallel scraping was
considered; not implemented.

### Sequential Claude scoring (not batched)
`score_with_claude()` calls `claude_score()` once per job via pandas `.apply()`.
With up to 50 jobs after prefilter, this is 50 sequential Claude API calls.
Batching was considered; not implemented.

### hours_old param still accepted but frontend doesn't send it
The `POST /api/analyze` endpoint accepts `hours_old: int = Form(DEFAULT_HOURS_OLD)`.
The frontend removed this field, so it always defaults to 24h. You can still hit
the API directly with a different value (e.g. 168 for 7 days), which will override
the fallback windows accordingly.

### Resume cache key is filename only
Cache hit is determined by `stored.get("filename") == filename`. If a user uploads
a file with the same name but different contents, the cached version is used. To
force a re-parse, they must "Replace resume" (which clears the stored resume).

### Export.py score column gap
`export.py` has `COLUMN_ORDER = ["rank", "score_100", ...]` but the live pipeline
produces `claude_score`, not `score_100`. The xlsx export silently omits the score
column (the `[c for c in COLUMN_ORDER if c in df.columns]` filter drops it). The
frontend table correctly shows `claude_score`; the xlsx doesn't. This is a known
bug, not yet fixed.

### ReadyToSearch keyword pills broken
`ReadyToSearch.jsx` reads `storedResume?.keywords` for the pill display. But
`GET /api/resume` returns `search_titles` and `skill_signals`, not `keywords`.
Pills are always empty (the field is undefined). The underlying functionality
(searching with cached resume) works; only the visual display is broken.

### In-memory job store is per-process
`JOBS` dict in `jobs_store.py` is process-local. Railway runs one process. If
Railway restarts (deploy, crash), all in-flight jobs are lost. Jobs are pruned
from the dict 1 hour after creation.

### "AI Engineer Job Matcher" label is stale
`FastAPI(title="AI Engineer Job Matcher")` in main.py and the module docstring
still say "AI Engineer" but the pipeline is now resume-driven for any field.

---

## Discrepancies found

Comparing the planning summary against actual code:

| # | Summary claim | Actual code | Notes |
|---|---------------|-------------|-------|
| 1 | "parallel scraping across search terms was proposed but uncertain" | **Sequential** `for` loop in `scrape_all()` | Never implemented |
| 2 | "batched Claude scoring was proposed but uncertain" | **One call per job** via pandas `.apply()` | Never implemented |
| 3 | "Glassdoor 400s on broad locations" | Code checks `"," in location`; the actual HTTP error code is not validated/logged in code | The comma heuristic may also pass some broad locations |
| 4 | "resume-driven, not limited to AI engineering" | Partially true: prefilter is generic (token overlap). But `config.AI_KEYWORDS`, `TITLE_BLOCKLIST` still exist and are used by the **legacy** `score_and_rank()`. The live pipeline (prefilter + Claude) is generic. | |
| 5 | "widens to 72h only if too few candidates" | True, but "too few" = `< TOP_RESULTS (10)`, not a score threshold. The 72h window runs if and only if `len(all_scored) < 10` after the 24h window. | |
| 6 | "never to 7 days" | True as a default. But `hours_old` Form param is still accepted, so 7 days is possible via direct API. Frontend removed the UI for it. | |
| 7 | Not mentioned | **Frontend keyword pills are broken**: `ReadyToSearch.jsx` reads `.keywords` but API returns `.search_titles` + `.skill_signals` | Introduced when API schema was updated |
| 8 | Not mentioned | **Export xlsx omits score**: `export.py` uses `score_100` column name, pipeline produces `claude_score` | Score column silently dropped from xlsx |
| 9 | Not mentioned | Semaphore wraps only the scrape step; dedup, prefilter, and scoring are OUTSIDE the semaphore | |

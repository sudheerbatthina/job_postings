# DECISIONS.md — Architectural choices and why

Deliberate decisions that would look wrong without context. Ordered roughly by
how surprising each one is to a new reader.

---

## On-demand pipeline (not pre-indexed)

**What:** Every search triggers a live scrape. There is no background indexer,
no job database, no scheduler.

**Why:** Freshness. Pre-indexing would require re-scraping every few hours across
all possible search terms and locations, which would make the bot the most
aggressive scraper on these boards. On-demand scraping also means we only scrape
for locations/terms the user actually searched — far fewer total requests.

**Trade-off:** Each search takes 1-2 minutes instead of being instant. Mitigated
by the progress messages in the UI.

---

## JobSpy, not a dedicated scraping service

**What:** Uses the `python-jobspy` PyPI package (open source, wraps Playwright
and requests against job board HTML/APIs).

**Why:** Zero per-query cost, no vendor dependency, no API key needed. All
scraping logic is pip-installable.

**Trade-off:** ZipRecruiter blocks JobSpy via Cloudflare WAF (returns 403 on every
call). LinkedIn rate-limits after ~10 pages. These are platform-side limits that
proxies could fix but would cost money and operational complexity.

---

## Global semaphore serializing all scrapes

**What:** `jobs_store.SCRAPE_SEMAPHORE = asyncio.Semaphore(1)`. At most one
`scrape_all()` call runs at any moment across all requests.

**Why:** The server shares one outbound IP with the job boards. Concurrent scraping
from one IP triggers much faster rate-limiting. Serializing scrapes means we look
like a slower human browser, not a bot.

**Trade-off:** If two users submit simultaneously, the second waits for the first's
scrape to finish or time out. With `SCRAPE_TIMEOUT_SECONDS=120`, worst-case wait
is 2 minutes. Acceptable for a low-traffic tool.

---

## asyncio.wait_for wrapping synchronous calls

**What:** `await asyncio.wait_for(asyncio.to_thread(scraper.scrape_all, ...), timeout=120)`

**Why:** JobSpy and Claude are both blocking/sync. Running them in
`asyncio.to_thread()` keeps the event loop unblocked so health checks and poll
requests still respond during a scrape. `wait_for` is added so a hung network call
doesn't hold the semaphore indefinitely. The semaphore is inside the `try` so a
timeout exits the context manager cleanly and releases it.

**Note:** `score_with_claude` also gets a 120s `wait_for` (same constant
`SCRAPE_TIMEOUT_SECONDS`) even though it's a Claude call, not a scrape. The
Anthropic client is separately initialized with a 30s per-request timeout
(`CLAUDE_TIMEOUT_SECONDS`); together these form a two-layer guard. The 120s outer
timeout is generous: it's 50 jobs × ~2s per Claude call.

---

## Mid-loop timeout returns partial results, not an error

**What:** If the 72h fallback window times out, the pipeline returns whatever it
already scored in the 24h window (which succeeded) and marks the job as "done"
with a "(search cut short)" note. The status is "done", not "error".

**Why:** From the user's perspective, getting 3-5 good matches is far better than
an error page. The pipeline result is still useful. "Error" implies nothing usable
came back.

---

## Two-stage scoring: prefilter then Claude

**What:** Stage 1 (`prefilter`) keeps only jobs where the token overlap between
the job description and the resume is ≥ 0.15, and caps at 50 rows (sorted newest
first). Stage 2 (`score_with_claude`) calls Claude Haiku once per remaining job
to score 0–100.

**Why:** Claude calls cost money and time. Prefilter is free and fast; it removes
obviously irrelevant jobs (no shared vocabulary). With `RESULTS_WANTED_PER_TERM=25`
and 4-6 search titles, a full scrape can return 100-150 jobs; prefilter typically
cuts this to 10-30 before Claude sees them.

---

## No score threshold

**What:** All jobs that pass prefilter and get scored by Claude are accumulated,
sorted, and the top 10 are returned. There is no minimum `claude_score` cutoff.

**Why:** The pipeline is already resume-targeted. With resume-derived search titles
+ skill_signals in the prompt, Claude naturally scores relevant jobs high and
irrelevant ones low. An arbitrary threshold (e.g. ≥50) would silently return zero
results on niche roles or thin markets, with no explanation. Better to show
lower-confidence matches than nothing.

---

## Sequential scraping across search terms

**What:** `scrape_all()` loops over `search_terms` sequentially, one `_scrape_one`
call after the next.

**Why:** IP rate-limit risk. Firing 4-6 parallel requests at LinkedIn from the
same IP simultaneously would trigger faster bans. Sequential requests look more
human. Parallel scraping was considered and explicitly not implemented.

---

## One Claude call per job, not batched

**What:** `scoring.score_with_claude()` uses `df["description"].apply(lambda: claude_score(...))`.

**Why:** Batching job descriptions into one prompt requires fitting many
job descriptions plus the resume into one message, which pushes context size up and
makes the output parsing brittle. One-per-job is simpler, failure-isolated (one
bad JSON response from Claude returns 0 for that job only, not all), and Haiku is
cheap enough to make it practical.

---

## Dedup marks only returned jobs, not all scraped jobs

**What:** `dedup.filter_unseen(df)` filters out previously-shown job URLs.
`dedup.mark_seen(final)` marks only the 10 jobs actually returned to the user.

**Why:** If we marked all 100+ scraped jobs as seen, subsequent searches would find
nothing — every job seen by the scraper would be dedup'd out, even ones the user
never saw ranked. By marking only the shown results, the user can search again
tomorrow and see new jobs, plus any from the prior scrape that didn't make the
top 10.

---

## 24h dedup expiry

**What:** `seen_jobs` rows older than 24 hours are pruned before each
`filter_unseen()` call.

**Why:** Job postings update constantly. A job that was shown yesterday may have
new details or still be the best match; blocking it forever would permanently hide
it. 24h is short enough that the next day's search shows fresh content, long enough
to prevent the same job appearing twice in one day's searches.

---

## Resume stored in SQLite, not in memory or a temp file

**What:** After the first upload, `resume_store.save_resume()` writes the full
resume text, `search_titles`, `skill_signals`, email, and phone into a single-row
SQLite table. Subsequent requests (including the `ReadyToSearch` path with no file)
read this row.

**Why:** Railway processes can restart (deploy, OOM). Storing in-process memory
would lose the resume on restart. SQLite on the Railway `/data` persistent volume
survives restarts. The same SQLite file is shared with dedup (single file, two
tables), simplifying operations.

**Cache key:** Filename only (not content hash). If a user uploads a different
file with the same name, the cached version is used. "Replace resume" in the UI
clears the stored row and forces a re-parse.

---

## Glassdoor gated on `"," in location`

**What:** `is_specific_location(location)` returns `"," in location`.
Glassdoor is excluded from the site list when this returns False.

**Why:** Glassdoor's API requires a specific city and returns HTTP errors on broad
locations like "United States" or "Remote". The comma is a simple, fast heuristic:
"New York, NY" passes; "United States" does not. It occasionally admits broad
locations that happen to contain a comma (e.g. "Bay Area, California"), but in
practice those work well enough.

---

## Python 3.11 pin

**What:** `backend/.python-version` contains `3.11`.

**Why:** `python-jobspy` pins `numpy==1.26.3`, which has no prebuilt wheel for
Python 3.13 on Windows/macOS. Python 3.11 has the wheel and matches Railway's
Nixpacks provisioning. Fixes the only practical install problem developers encounter.

---

## FALLBACK_HOURS = [72] — one fallback, not a progressive ladder

**What:** `config.FALLBACK_HOURS = [72]`. The window progression is `[hours_old,
72]` (typically `[24, 72]`).

**Why:** One fallback is usually enough. A 3-day window catches postings that
slipped through the 24h window due to scraper lag. More fallback levels (168h,
336h) would return increasingly stale jobs and make the pipeline run longer. Seven
days was removed from the defaults because most job postings older than 3 days are
either filled or at application saturation.

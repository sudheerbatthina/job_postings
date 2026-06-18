# AI Engineer Job Finder — v1

Scrapes LinkedIn / Indeed / Google / ZipRecruiter, scores each posting by
keyword relevance + resume match + recency, and writes a ranked spreadsheet.

## Setup
```bash
pip install -r requirements.txt
```

## Run
```bash
python ai_engineer_jobs.py
```
Output: `ai_engineer_jobs_YYYYMMDD.xlsx` (and a `.csv`), ranked best-first.

## Tune (edit CONFIG block at top of ai_engineer_jobs.py)
- `SEARCH_TERMS` — titles to search
- `LOCATION` / `IS_REMOTE` — where
- `HOURS_OLD` — recency window (168 = 7 days)
- `RESUME_PATH` — point at a .pdf/.txt resume for personalized scoring (optional)
- `WEIGHTS` — relative weight of keyword vs resume vs recency
- `SORT_MODE` — "composite" (default), "recency", or "keyword"

## Notes / known limits
- v1 does NOT filter by applicant count (skipped by design; ranks by recency).
- LinkedIn rate-limits scraping after ~10 pages from one IP. If you hit 429s,
  lower RESULTS_WANTED, or add `proxies=[...]` inside scrape_all().
- Runs free, no API keys. To make a "data" or "SDE" variant, just copy this
  file and swap SEARCH_TERMS + AI_KEYWORDS.

# Agency Lead-Enrichment Pipeline

## Setup

Using `uv` (recommended):
```
uv add aiohttp pandas openpyxl beautifulsoup4 lxml playwright
uv run playwright install chromium
```

Using `pip`:
```
pip install -r requirements.txt
playwright install chromium
```

## Run

Place `websites.xlsx` (with a `Website` column) in this folder, then:
```
uv run main.py
```
(or `python3 main.py` if not using `uv`)

To force a fresh run and ignore/delete any existing checkpoint:
```
uv run main.py --cleanstart
```

You can also point at a different input file:
```
uv run main.py --input other_file.xlsx
```

## Files
- `config.py` — all tunable settings (concurrency, timeouts, thresholds)
- `main.py` — entry point: reads Excel, runs crawl, writes output
- `pipeline.py` — per-domain orchestration (fetch -> extract -> score)
- `crawler.py` — aiohttp/Playwright fetching, throttling, retries
- `business_extraction.py` — name/phone/address/socials/CTA extraction
- `email_extraction.py` — email discovery, dedupe, primary-email selection
- `tech_detection.py` — CMS/framework detection
- `signals.py` — agency-opportunity heuristics
- `scoring.py` — industry classification + lead scoring + recommendations
- `checkpoint.py` — crash-safe resume support (checkpoint.jsonl)
- `domain_cache.py` — avoids re-crawling duplicate domains

## Resume behavior
If the run crashes or is interrupted (Ctrl+C), progress is saved to
`checkpoint.jsonl` every 50 rows (configurable), plus an immediate flush
the moment you hit Ctrl+C (in-flight tasks are cancelled first so nothing
half-written gets recorded). `agency_leads.xlsx` / `hot_leads.xlsx` are
still written from whatever was completed so far.

Just re-run `uv run main.py` — it detects `checkpoint.jsonl` and skips
already-processed rows automatically, continuing where it left off.

To ignore the checkpoint and start completely from scratch:
```
uv run main.py --cleanstart
```
This deletes the existing `checkpoint.jsonl` before the crawl begins.

## Output
- `agency_leads.xlsx` — full enriched dataset
- `hot_leads.xlsx` — LeadScore >= 60, sorted descending
- `scraper_debug.log` — detailed per-site debug log

## Safety features added for 20k-scale runs
- Hard per-domain timeout (90s default) so one bad site can never stall the run
- Outer timeout around every Playwright call, independent of Playwright's own timeout
- Subpages fetched concurrently (bounded) instead of sequentially — faster
- All exceptions caught at every layer; nothing propagates to kill the batch
- Browser-launch failure falls back to aiohttp-only crawling
- Checkpoint flushed in a `finally` block on crash, interrupt, or normal exit
- Domain-level caching + locking: duplicate domains are crawled exactly once

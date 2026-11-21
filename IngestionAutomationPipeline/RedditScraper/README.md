# RedditScraper

A production-quality Python microservice that monitors Reddit subreddits for new posts and forwards them to the lead ingestion API — designed to be **extremely conservative with Reddit's RSS rate limits**.

Managed with [**uv**](https://docs.astral.sh/uv/) — no manual venv or `pip install` needed.

## Architecture

```
Reddit RSS (.rss)
    ↓
RSS cache (SQLite, ETag/Last-Modified)
    ↓
feedparser + BeautifulSoup
    ↓
SQLite post cache (deduplication)
    ↓
Identify unseen posts
    ↓
Persist posts locally
    ↓
POST /ingest
    ↓
Mark successful
    ↓
Update feed cursor
```

## Project layout

```
RedditScraper/
├── main.py          Entry point, main loop, graceful shutdown
├── config.py        Environment configuration
├── scheduler.py     Smart priority-queue scheduler (adaptive intervals)
├── reddit.py        RSS fetching, caching, parsing, extraction
├── ingestion.py     POST to /ingest with retry/backoff
├── state.py         SQLite persistence (posts, feed state, RSS cache)
├── subreddits.csv   List of subreddits to monitor
├── pyproject.toml   uv project manifest + dependencies
├── uv.lock          Locked dependency tree
├── .env.example     Configuration template
└── README.md        This file
```

## Quick start

```bash
# 1. Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Configure
cp .env.example .env
# Edit .env — set LEAD_INGESTION_SERVER_URL at minimum

# 3. Edit subreddits.csv to list the subreddits you want to monitor

# 4. Run (uv resolves deps and the venv automatically)
uv run python main.py
```

That's it — `uv run` handles the virtual environment and dependency installation from `uv.lock` on first run.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `LEAD_INGESTION_SERVER_URL` | *(required)* | Base URL of ingestion API |
| `REDDIT_SUBREDDITS_CSV` | `subreddits.csv` | CSV of subreddits to monitor |
| `REDDIT_MIN_REQUEST_INTERVAL` | `60` | Minimum seconds between any two Reddit requests |
| `REDDIT_RSS_CACHE_TTL` | `60` | Seconds a cached RSS response stays fresh |
| `REDDIT_MIN_FEED_INTERVAL` | `300` | Minimum seconds before re-polling a feed |
| `REDDIT_MAX_FEED_INTERVAL` | `3600` | Maximum seconds before a feed must be re-polled |
| `REDDIT_BOOTSTRAP_MODE` | `latest` | `latest` skips existing posts; `ingest` processes them |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `DB_PATH` | `reddit_monitor.db` | SQLite database path |

## Scheduler design

The scheduler maintains a **min-heap** of feeds ordered by `next_poll_at`.

- It never makes two Reddit requests simultaneously.
- A **global minimum interval** (`REDDIT_MIN_REQUEST_INTERVAL`) is enforced between all requests.
- Per-feed intervals adapt between `REDDIT_MIN_FEED_INTERVAL` and `REDDIT_MAX_FEED_INTERVAL` based on an **exponential moving average** of posts-per-poll (activity score).
- High-activity feeds are polled more frequently; idle feeds approach `REDDIT_MAX_FEED_INTERVAL`.
- HTTP 429 responses trigger **exponential backoff × 4**, plus honouring `Retry-After`.
- Network errors and 5xx responses trigger standard exponential backoff.

## Combined feeds

Subreddits are grouped into **combined RSS feeds** (up to 10 per request):

```
https://www.reddit.com/r/sub1+sub2+sub3/new/.rss
```

This reduces total Reddit requests. The originating subreddit for each post is extracted from the post's link URL.

## Caching

- RSS responses are cached in SQLite with `ETag` and `Last-Modified` headers.
- Conditional requests (`If-None-Match`, `If-Modified-Since`) are used where supported.
- `304 Not Modified` responses are handled correctly.
- Cache TTL is configurable via `REDDIT_RSS_CACHE_TTL`.

## Deduplication

Posts are deduplicated by **Reddit post ID** (never by title). The `last_seen_post_id` cursor advances only after posts are safely persisted.

## Ingestion retry

| Status | Behaviour |
|---|---|
| 2xx | Mark ingested |
| 429 | Respect `Retry-After`, exponential backoff, retry |
| 5xx / network | Exponential backoff, retry up to 8 times |
| Other 4xx | Log and mark failed (no infinite retry) |

Pending posts survive restarts — on startup the service drains any posts left in `pending`/`retry` state before beginning normal operation.

## Graceful shutdown

Send `SIGINT` (Ctrl-C) or `SIGTERM`. The service finishes its current task and exits cleanly.

## Rate-limit philosophy

> The objective is NOT to scrape Reddit as aggressively as possible. The objective is to obtain fresh posts from all configured subreddits with the **fewest reasonable Reddit requests**, using intelligent rotation, caching, deduplication, activity-based scheduling, and adaptive backoff.

The service never uses proxies, rotating IPs, multiple accounts, or any technique intended to bypass Reddit's rate limits.

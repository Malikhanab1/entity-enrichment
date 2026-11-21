"""SQLite persistence: posts, feed state, and RSS cache."""

import sqlite3
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional, Generator

import config

log = logging.getLogger("state")

# ── Schema ─────────────────────────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS posts (
    reddit_post_id      TEXT PRIMARY KEY,
    subreddit           TEXT NOT NULL,
    title               TEXT NOT NULL,
    text                TEXT,
    url                 TEXT,
    posted_by           TEXT,
    posted_at           TEXT,
    discovered_at       TEXT NOT NULL,
    ingestion_status    TEXT NOT NULL DEFAULT 'pending',
    ingestion_attempts  INTEGER NOT NULL DEFAULT 0,
    last_attempt_at     TEXT,
    ingested_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_status
    ON posts(ingestion_status);

CREATE TABLE IF NOT EXISTS feed_state (
    feed_key            TEXT PRIMARY KEY,
    last_seen_post_id   TEXT,
    last_seen_posted_at TEXT,
    last_polled_at      REAL,
    next_poll_at        REAL,
    consecutive_errors  INTEGER NOT NULL DEFAULT 0,
    consecutive_429s    INTEGER NOT NULL DEFAULT 0,
    current_backoff     REAL NOT NULL DEFAULT 0,
    activity_score      REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS rss_cache (
    feed_key        TEXT PRIMARY KEY,
    fetched_at      REAL NOT NULL,
    etag            TEXT,
    last_modified   TEXT,
    content         BLOB
);
"""


# ── Connection factory ─────────────────────────────────────────────────────────


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create schema if it doesn't exist."""
    with get_conn() as conn:
        conn.executescript(_DDL)
    log.info("Database initialised at %s", config.DB_PATH)


# ── Feed state ─────────────────────────────────────────────────────────────────


def load_feed_state(feed_key: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM feed_state WHERE feed_key = ?", (feed_key,)
        ).fetchone()
    if row:
        return dict(row)
    return {
        "feed_key": feed_key,
        "last_seen_post_id": None,
        "last_seen_posted_at": None,
        "last_polled_at": None,
        "next_poll_at": 0.0,
        "consecutive_errors": 0,
        "consecutive_429s": 0,
        "current_backoff": 0.0,
        "activity_score": 1.0,
    }


def save_feed_state(state: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO feed_state
                (feed_key, last_seen_post_id, last_seen_posted_at,
                 last_polled_at, next_poll_at,
                 consecutive_errors, consecutive_429s,
                 current_backoff, activity_score)
            VALUES
                (:feed_key, :last_seen_post_id, :last_seen_posted_at,
                 :last_polled_at, :next_poll_at,
                 :consecutive_errors, :consecutive_429s,
                 :current_backoff, :activity_score)
            ON CONFLICT(feed_key) DO UPDATE SET
                last_seen_post_id   = excluded.last_seen_post_id,
                last_seen_posted_at = excluded.last_seen_posted_at,
                last_polled_at      = excluded.last_polled_at,
                next_poll_at        = excluded.next_poll_at,
                consecutive_errors  = excluded.consecutive_errors,
                consecutive_429s    = excluded.consecutive_429s,
                current_backoff     = excluded.current_backoff,
                activity_score      = excluded.activity_score
            """,
            state,
        )


def load_all_feed_states() -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM feed_state").fetchall()
    return {row["feed_key"]: dict(row) for row in rows}


# ── Post cache ─────────────────────────────────────────────────────────────────


def post_exists(reddit_post_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM posts WHERE reddit_post_id = ?", (reddit_post_id,)
        ).fetchone()
    return row is not None


def insert_post(post: dict) -> None:
    """Insert a new post; silently ignore if already present."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO posts
                (reddit_post_id, subreddit, title, text, url,
                 posted_by, posted_at, discovered_at,
                 ingestion_status, ingestion_attempts)
            VALUES
                (:reddit_post_id, :subreddit, :title, :text, :url,
                 :posted_by, :posted_at, :discovered_at,
                 'pending', 0)
            """,
            {**post, "discovered_at": now},
        )


def get_pending_posts(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM posts
            WHERE ingestion_status IN ('pending', 'retry')
            ORDER BY posted_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_post_ingested(reddit_post_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE posts
            SET ingestion_status = 'ingested',
                ingested_at = ?
            WHERE reddit_post_id = ?
            """,
            (now, reddit_post_id),
        )


def mark_post_retry(reddit_post_id: str, attempts: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE posts
            SET ingestion_status  = 'retry',
                ingestion_attempts = ?,
                last_attempt_at    = ?
            WHERE reddit_post_id = ?
            """,
            (attempts, now, reddit_post_id),
        )


def mark_post_failed(reddit_post_id: str, attempts: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE posts
            SET ingestion_status  = 'failed',
                ingestion_attempts = ?,
                last_attempt_at    = ?
            WHERE reddit_post_id = ?
            """,
            (attempts, now, reddit_post_id),
        )


# ── RSS cache ──────────────────────────────────────────────────────────────────


def get_rss_cache(feed_key: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM rss_cache WHERE feed_key = ?", (feed_key,)
        ).fetchone()
    if not row:
        return None
    entry = dict(row)
    age = time.time() - entry["fetched_at"]
    if age > config.REDDIT_RSS_CACHE_TTL:
        return None  # expired — caller will re-fetch
    return entry


def save_rss_cache(
    feed_key: str, etag: Optional[str], last_modified: Optional[str], content: bytes
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO rss_cache (feed_key, fetched_at, etag, last_modified, content)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(feed_key) DO UPDATE SET
                fetched_at    = excluded.fetched_at,
                etag          = excluded.etag,
                last_modified = excluded.last_modified,
                content       = excluded.content
            """,
            (feed_key, time.time(), etag, last_modified, content),
        )


def get_rss_validators(feed_key: str) -> tuple[Optional[str], Optional[str]]:
    """Return (etag, last_modified) for conditional requests."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT etag, last_modified FROM rss_cache WHERE feed_key = ?",
            (feed_key,),
        ).fetchone()
    if row:
        return row["etag"], row["last_modified"]
    return None, None


def get_rss_content_raw(feed_key: str) -> Optional[bytes]:
    """
    Return the raw cached RSS content for feed_key regardless of TTL.
    Used when a 304 response confirms the content is still current even
    though the warm-cache TTL has expired.
    Returns None if no cache row exists at all.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT content FROM rss_cache WHERE feed_key = ?", (feed_key,)
        ).fetchone()
    return row["content"] if row else None


def posts_exist_batch(post_ids: list[str]) -> set[str]:
    """
    Return the subset of post_ids that already exist in the DB.
    Uses a single parameterised query instead of N round-trips.
    """
    if not post_ids:
        return set()
    placeholders = ",".join("?" * len(post_ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT reddit_post_id FROM posts WHERE reddit_post_id IN ({placeholders})",
            post_ids,
        ).fetchall()
    return {row["reddit_post_id"] for row in rows}

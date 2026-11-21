"""
Ingestion client: POST posts to the lead ingestion API.

Retry strategy
--------------
2xx        → success
429        → respect Retry-After, exponential backoff
5xx/net    → exponential backoff, limited retries
4xx other  → log and mark failed (no infinite retry)

Architecture
------------
drain_pending() now runs in a background daemon thread so that ingestion
retries (which can sleep up to _MAX_BACKOFF seconds) never block the main
Reddit polling loop.  The queue is bounded to prevent unbounded memory growth.
"""

import logging
import queue
import threading
import time
from typing import Optional

import requests

import config
import state as st

log = logging.getLogger("ingestion")

_INGEST_URL: str = f"{config.LEAD_INGESTION_SERVER_URL.rstrip('/')}/ingest"
_MAX_RETRIES: int = 8
_BASE_BACKOFF: float = 5.0
_MAX_BACKOFF: float = 600.0

# ── Background worker ──────────────────────────────────────────────────────────

# Sentinel used to stop the worker thread cleanly.
_STOP = object()

# Bounded queue: if ingestion falls far behind, we rely on the DB for
# persistence and don't grow the queue forever.
_queue: queue.Queue = queue.Queue(maxsize=500)

_worker_thread: Optional[threading.Thread] = None


def start_worker() -> None:
    """Start the background ingestion thread.  Call once from main."""
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_thread = threading.Thread(
        target=_worker_loop, name="ingestion-worker", daemon=True
    )
    _worker_thread.start()
    log.info("[ingestion] background worker started")


def stop_worker(timeout: float = 10.0) -> None:
    """Signal the worker to stop and wait for it."""
    if _worker_thread and _worker_thread.is_alive():
        _queue.put(_STOP)
        _worker_thread.join(timeout=timeout)
        log.info("[ingestion] background worker stopped")


def _worker_loop() -> None:
    """Drain the in-memory queue, then drain the DB, then wait for more work."""
    while True:
        try:
            item = _queue.get(timeout=5)
        except queue.Empty:
            # Nothing queued — drain any DB-persisted pending posts.
            _drain_db_pending()
            continue

        if item is _STOP:
            _queue.task_done()
            break

        try:
            ingest_post(item)
        except Exception:
            log.exception(
                "[ingestion] unexpected error processing post %s",
                item.get("reddit_post_id"),
            )
        finally:
            _queue.task_done()


def _drain_db_pending() -> None:
    """Pull pending/retry posts from the DB and ingest them."""
    pending = st.get_pending_posts(limit=50)
    for post in pending:
        ingest_post(post)


def enqueue_post(post: dict) -> None:
    """
    Enqueue a post for background ingestion.  Non-blocking: if the queue is
    full the post is already persisted in the DB and will be picked up by
    the next _drain_db_pending sweep.
    """
    try:
        _queue.put_nowait(post)
    except queue.Full:
        log.warning(
            "[ingestion] queue full; post %s will be ingested from DB on next sweep",
            post.get("reddit_post_id"),
        )


def drain_pending(limit: int = 50) -> int:
    """
    Non-blocking: load pending posts from DB and enqueue them for the worker.
    Returns the number of posts enqueued (not necessarily ingested yet).
    """
    pending = st.get_pending_posts(limit)
    if not pending:
        return 0
    log.info(
        "[ingestion] enqueuing %d pending post(s) for background ingestion",
        len(pending),
    )
    for post in pending:
        enqueue_post(post)
    return len(pending)


# ── HTTP session ───────────────────────────────────────────────────────────────


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# Each thread gets its own session (requests.Session is not thread-safe).
_thread_local = threading.local()


def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = _make_session()
    return _thread_local.session


# ── Core ingestion logic ───────────────────────────────────────────────────────


def _build_payload(post: dict) -> dict:
    return {
        "title": post.get("title", ""),
        "text": post.get("text", ""),
        "source": "reddit",
        "posted_by": post.get("posted_by", ""),
        "user": post.get("posted_by", ""),
        "group": post.get("subreddit", ""),
        "posted_at": post.get("posted_at", ""),
    }


def _ingest_once(payload: dict) -> tuple[int, Optional[float]]:
    """
    Attempt a single POST.
    Returns (status_code, retry_after_seconds_or_None).
    Returns (-1, None) on network error.
    """
    try:
        resp = _get_session().post(_INGEST_URL, json=payload, timeout=30)
    except requests.RequestException as exc:
        log.warning("[ingestion] network error: %s", exc)
        return -1, None

    retry_after: Optional[float] = None
    if resp.status_code == 429:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                retry_after = float(ra)
            except ValueError:
                pass

    return resp.status_code, retry_after


def ingest_post(post: dict) -> bool:
    """
    Try to ingest a single post, retrying on transient failures.
    Updates DB status on success or permanent failure.
    Returns True if ingested successfully.

    Note: this function *does* sleep on retries, but it is always called
    from the background worker thread — never from the main polling loop.
    """
    pid = post["reddit_post_id"]
    payload = _build_payload(post)
    attempts = post.get("ingestion_attempts", 0)

    for attempt in range(1, _MAX_RETRIES + 1):
        attempts += 1
        status, retry_after = _ingest_once(payload)

        log.info("[ingestion] POST /ingest → %s (attempt %d)", status, attempt)

        if 200 <= status < 300:
            st.mark_post_ingested(pid)
            return True

        if status == 429:
            wait = retry_after if retry_after else _BASE_BACKOFF * (2**attempt)
            wait = min(wait, _MAX_BACKOFF)
            log.warning("[ingestion] rate limited; sleeping %.0fs", wait)
            st.mark_post_retry(pid, attempts)
            time.sleep(wait)
            continue

        if status in (-1, 500, 502, 503, 504):
            wait = min(_BASE_BACKOFF * (2**attempt), _MAX_BACKOFF)
            log.warning(
                "[ingestion] transient error %s; retrying in %.0fs", status, wait
            )
            st.mark_post_retry(pid, attempts)
            time.sleep(wait)
            continue

        if 400 <= status < 500:
            log.error(
                "[ingestion] permanent client error %d for post %s; marking failed",
                status,
                pid,
            )
            st.mark_post_failed(pid, attempts)
            return False

    # Exhausted retries
    log.error("[ingestion] exhausted retries for post %s", pid)
    st.mark_post_failed(pid, attempts)
    return False

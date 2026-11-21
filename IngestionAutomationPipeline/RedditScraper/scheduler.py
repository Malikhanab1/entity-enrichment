"""
Smart rotating scheduler for Reddit RSS feeds.

Key design decisions
--------------------
* A single priority queue (heap) holds all feed entries.
* Priority = next_poll_at (monotonic timestamp) — the feed due soonest wins.
* The scheduler never makes two Reddit requests simultaneously and enforces
  a global minimum interval (REDDIT_MIN_REQUEST_INTERVAL) between requests.
* Per-feed intervals adapt between REDDIT_MIN_FEED_INTERVAL and
  REDDIT_MAX_FEED_INTERVAL based on observed activity and errors.
* Activity score is an exponential moving average of new-posts-per-poll.
"""

import heapq
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

import config
import state as st

log = logging.getLogger("scheduler")

# Set to True by main.py signal handler to unblock tick()
_SHUTDOWN: bool = False

# Exponential-backoff constants
_BASE_BACKOFF: float = 60.0  # seconds for first error
_MAX_BACKOFF: float = 3600.0  # hard ceiling per-feed
_429_MULTIPLIER: float = 4.0  # extra penalty for rate-limit hits
_ACTIVITY_EMA_ALPHA: float = 0.3  # smoothing factor for activity score


@dataclass(order=True)
class FeedEntry:
    """Heap entry: (next_poll_at, feed_key, subreddits)."""

    next_poll_at: float
    feed_key: str = field(compare=False)
    subreddits: list[str] = field(compare=False)


class Scheduler:
    """
    Maintains a min-heap of feeds ordered by next_poll_at.

    Usage:
        scheduler.tick() → blocks until the best feed is ready, returns it.
        scheduler.report_success(feed_key, new_post_count)
        scheduler.report_error(feed_key, status_code, retry_after)
    """

    def __init__(self, feeds: dict[str, list[str]]) -> None:
        """
        feeds: mapping of feed_key → [subreddit, ...].
        """
        self._feeds: dict[str, list[str]] = feeds  # feed_key → subreddits
        self._states: dict[str, dict] = {}  # feed_key → db state
        self._heap: list[FeedEntry] = []
        self._last_request_at: float = 0.0

        # Load persisted state; initialise missing entries.
        # Always start eligible immediately — next_poll_at is only relevant
        # for rescheduling *after* a successful poll within a single run.
        persisted = st.load_all_feed_states()
        for feed_key, subreddits in feeds.items():
            s = persisted.get(feed_key, st.load_feed_state(feed_key))
            s["next_poll_at"] = 0.0  # always poll on startup
            self._states[feed_key] = s
            heapq.heappush(self._heap, FeedEntry(0.0, feed_key, subreddits))

        log.info("Scheduler initialised with %d feed(s)", len(feeds))

    # ── Public API ─────────────────────────────────────────────────────────────

    def tick(self) -> FeedEntry:
        """
        Block until the highest-priority eligible feed is ready and the
        global request cooldown has elapsed.  Returns the feed to poll.
        """
        while True:
            if not self._heap:
                time.sleep(5)
                continue

            entry = self._heap[0]  # peek

            # How long until this feed is eligible?
            now = time.time()
            feed_wait = max(0.0, entry.next_poll_at - now)

            # How long until the global cooldown clears?
            global_wait = max(
                0.0,
                (self._last_request_at + config.REDDIT_MIN_REQUEST_INTERVAL) - now,
            )

            wait = max(feed_wait, global_wait)
            if wait > 0:
                reason = (
                    "(global cooldown)" if global_wait >= feed_wait else entry.feed_key
                )
                log.info("[reddit] waiting %.0fs for %s", wait, reason)
                # Sleep in small increments so Ctrl-C / SIGTERM is noticed
                # promptly, but only log once per wait — not every 10 seconds.
                deadline = time.time() + wait
                while time.time() < deadline:
                    if _SHUTDOWN:
                        raise SystemExit(0)
                    time.sleep(min(0.25, deadline - time.time()))
                continue

            # Pop and return
            entry = heapq.heappop(self._heap)
            self._last_request_at = time.time()
            log.info("[reddit] selecting %s", entry.feed_key)
            return entry

    def report_success(
        self,
        feed_key: str,
        new_post_count: int,
        newest_post_id: Optional[str] = None,
        newest_posted_at: Optional[str] = None,
    ) -> None:
        """Update state and reschedule after a successful fetch."""
        s = self._states[feed_key]
        now = time.time()

        # Reset error counters
        s["consecutive_errors"] = 0
        s["consecutive_429s"] = 0
        s["current_backoff"] = 0.0
        s["last_polled_at"] = now

        if newest_post_id:
            s["last_seen_post_id"] = newest_post_id
        if newest_posted_at:
            s["last_seen_posted_at"] = newest_posted_at

        # Update activity score (EMA of posts-per-poll)
        old_score: float = s.get("activity_score", 1.0)
        s["activity_score"] = (
            _ACTIVITY_EMA_ALPHA * new_post_count + (1 - _ACTIVITY_EMA_ALPHA) * old_score
        )

        interval = self._compute_interval(s)
        s["next_poll_at"] = now + interval
        st.save_feed_state(s)

        heapq.heappush(
            self._heap,
            FeedEntry(s["next_poll_at"], feed_key, self._feeds[feed_key]),
        )

        if new_post_count:
            log.info(
                "[reddit] %s activity_score=%.2f → next poll in %.0fs",
                feed_key,
                s["activity_score"],
                interval,
            )

    def report_error(
        self,
        feed_key: str,
        status_code: Optional[int],
        retry_after: Optional[float] = None,
    ) -> None:
        """Update state and reschedule after a failed fetch."""
        s = self._states[feed_key]
        now = time.time()

        is_429 = status_code == 429

        s["consecutive_errors"] = s.get("consecutive_errors", 0) + 1
        if is_429:
            s["consecutive_429s"] = s.get("consecutive_429s", 0) + 1
        s["last_polled_at"] = now

        backoff = self._compute_backoff(s, is_429, retry_after)
        s["current_backoff"] = backoff
        s["next_poll_at"] = now + backoff

        st.save_feed_state(s)

        heapq.heappush(
            self._heap,
            FeedEntry(s["next_poll_at"], feed_key, self._feeds[feed_key]),
        )

        log.warning(
            "[reddit] %s backed off for %.0fs after %s",
            feed_key,
            backoff,
            f"HTTP {status_code}" if status_code else "network error",
        )

    def get_state(self, feed_key: str) -> dict:
        return self._states[feed_key]

    # ── Internals ──────────────────────────────────────────────────────────────

    def _compute_interval(self, s: dict) -> float:
        """
        Adaptive interval based on activity score.

        High activity (score > 5)  → MIN_FEED_INTERVAL
        Zero activity (score = 0)  → MAX_FEED_INTERVAL
        Graduated in between using inverse log scale.
        """
        score: float = s.get("activity_score", 1.0)
        mn = float(config.REDDIT_MIN_FEED_INTERVAL)
        mx = float(config.REDDIT_MAX_FEED_INTERVAL)

        if score <= 0:
            return mx

        # Map activity score logarithmically to [mn, mx]
        # score=0 → mx, score≥10 → mn
        ratio = math.log1p(score) / math.log1p(10.0)
        interval = mx - ratio * (mx - mn)
        return max(mn, min(mx, interval))

    def _compute_backoff(
        self, s: dict, is_429: bool, retry_after: Optional[float]
    ) -> float:
        errors: int = s.get("consecutive_errors", 1)
        backoff = _BASE_BACKOFF * (2 ** (errors - 1))

        if is_429:
            backoff *= _429_MULTIPLIER
            if retry_after and retry_after > backoff:
                backoff = retry_after + 10  # honour Retry-After with margin

        return min(backoff, _MAX_BACKOFF)

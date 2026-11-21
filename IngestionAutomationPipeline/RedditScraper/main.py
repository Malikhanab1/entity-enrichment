"""
Reddit Monitor — main entry point.

Run with:  python main.py
"""

import logging
import signal
import sys
import time

import config

config.configure_logging()

log = logging.getLogger("main")

import state as st
import reddit as rd
import ingestion as ing
import scheduler as sched
from scheduler import Scheduler


def _handle_signal(sig, frame):
    log.info("Shutdown signal received (%s); stopping…", sig)
    sched._SHUTDOWN = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def run() -> None:
    log.info(
        "Starting Reddit monitor (bootstrap_mode=%s)", config.REDDIT_BOOTSTRAP_MODE
    )

    # ── Initialise DB ──────────────────────────────────────────────────────────
    st.init_db()

    # ── Start background ingestion worker ──────────────────────────────────────
    ing.start_worker()

    # ── Load subreddits → build feed groups ────────────────────────────────────
    subreddits = rd.load_subreddits()
    if not subreddits:
        log.error("No subreddits configured. Exiting.")
        ing.stop_worker()
        sys.exit(1)

    feed_groups = rd.build_feed_groups(subreddits)
    log.info(
        "Monitoring %d subreddit(s) across %d feed(s)",
        len(subreddits),
        len(feed_groups),
    )

    # ── Drain any posts left pending from a previous run ──────────────────────
    log.info("Enqueuing any pending posts from previous run…")
    ing.drain_pending()

    # ── Initialise scheduler ───────────────────────────────────────────────────
    scheduler = Scheduler(feed_groups)

    # ── Main loop ──────────────────────────────────────────────────────────────
    try:
        while not sched._SHUTDOWN:
            try:
                entry = scheduler.tick()
                if sched._SHUTDOWN:
                    break

                feed_key = entry.feed_key
                feed_state = scheduler.get_state(feed_key)

                try:
                    new_count, newest_id, newest_at = rd.process_feed(
                        feed_key, entry.subreddits, feed_state
                    )
                except rd.RedditRateLimitError as exc:
                    scheduler.report_error(feed_key, 429, exc.retry_after)
                    continue
                except rd.RedditHTTPError as exc:
                    scheduler.report_error(feed_key, exc.status_code)
                    continue
                except rd.RedditNetworkError:
                    scheduler.report_error(feed_key, None)
                    continue

                if new_count:
                    log.info("[reddit] found %d new post(s) in %s", new_count, feed_key)
                    # Enqueue for background ingestion — does NOT block.
                    ing.drain_pending()

                scheduler.report_success(feed_key, new_count, newest_id, newest_at)

            except KeyboardInterrupt:
                break
            except Exception as exc:
                log.exception("Unexpected error in main loop: %s", exc)
                time.sleep(10)
    finally:
        log.info("Shutting down ingestion worker…")
        ing.stop_worker(timeout=30)
        log.info("Reddit monitor stopped cleanly.")


if __name__ == "__main__":
    run()

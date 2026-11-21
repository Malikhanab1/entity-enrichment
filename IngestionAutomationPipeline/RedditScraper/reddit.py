"""
Reddit RSS fetcher and post parser.

Responsibilities
----------------
* Fetch RSS feeds for one or more subreddits (combined or single).
* Cache responses (ETag / Last-Modified / TTL).
* Parse entries into structured post dicts.
* Determine originating subreddit for combined feeds.
* Identify genuinely new posts (not seen since last cursor).
"""

import csv
import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

import config
import state as st

log = logging.getLogger("reddit")

_RSS_URL = "https://www.reddit.com/r/{subs}/new/.rss"
_MAX_COMBINED = 10  # Reddit's multi-subreddit URL limit
_POST_ID_RE = re.compile(r"t3_([a-z0-9]+)", re.IGNORECASE)
_LINK_RE = re.compile(
    r"https?://www\.reddit\.com/r/([^/]+)/comments/([a-z0-9]+)/", re.IGNORECASE
)


# ── Session ────────────────────────────────────────────────────────────────────


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": config.USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml",
        }
    )
    return s


_SESSION: requests.Session = _make_session()


# ── CSV loading ────────────────────────────────────────────────────────────────


def load_subreddits() -> list[str]:
    """Load subreddit names from CSV (first column, skip header if present)."""
    path = Path(config.REDDIT_SUBREDDITS_CSV)
    if not path.exists():
        log.warning("Subreddits CSV not found: %s", path)
        return []

    subs = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            name = row[0].strip().lstrip("r/").strip()
            if name.lower() == "subreddit":
                continue  # header row
            if name:
                subs.append(name)

    log.info("Loaded %d subreddits from %s", len(subs), path)
    return subs


def build_feed_groups(subreddits: list[str]) -> dict[str, list[str]]:
    """
    Group subreddits into combined feeds (up to _MAX_COMBINED each).
    Returns {feed_key: [sub1, sub2, ...]}.
    """
    groups: dict[str, list[str]] = {}
    for i in range(0, len(subreddits), _MAX_COMBINED):
        chunk = subreddits[i : i + _MAX_COMBINED]
        key = "r/" + "+".join(chunk)
        groups[key] = chunk
    return groups


def feed_url(feed_key: str) -> str:
    # feed_key is like "r/sub1+sub2"
    subs = feed_key.lstrip("r/")
    return _RSS_URL.format(subs=subs)


# ── HTTP fetch with caching ────────────────────────────────────────────────────


class FetchResult:
    __slots__ = ("status", "content", "etag", "last_modified", "retry_after")

    def __init__(
        self,
        status: int,
        content: Optional[bytes] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        self.status = status
        self.content = content
        self.etag = etag
        self.last_modified = last_modified
        self.retry_after = retry_after


def fetch_feed(feed_key: str) -> FetchResult:
    """
    Fetch the RSS feed for feed_key with caching and conditional requests.
    Returns FetchResult.  Never raises; returns status=-1 on network errors.
    """
    # 1. Check warm cache (content still fresh — skip the network entirely)
    cached = st.get_rss_cache(feed_key)
    if cached:
        log.debug("[reddit] %s served from warm cache", feed_key)
        return FetchResult(200, cached["content"])

    url = feed_url(feed_key)
    headers: dict[str, str] = {}

    # 2. Add conditional-request headers from stored validators.
    #    These live in the cache row independently of the TTL, so we can
    #    send If-None-Match / If-Modified-Since even after the warm cache
    #    expires to save bandwidth.
    etag, last_modified = st.get_rss_validators(feed_key)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        resp = _SESSION.get(url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        log.warning("[reddit] network error fetching %s: %s", feed_key, exc)
        return FetchResult(-1)

    log.info("[reddit] fetched %s → %d", feed_key, resp.status_code)

    if resp.status_code == 304:
        # Server says nothing changed.  Re-use the last cached content if
        # we still have it, otherwise treat as empty (no new posts).
        # IMPORTANT: do NOT use get_rss_cache() here — the TTL has already
        # expired (that's why we went to the network), so get_rss_cache()
        # will return None.  Use get_rss_content_raw() instead which skips
        # the TTL check.
        content = st.get_rss_content_raw(feed_key)
        if content:
            log.debug("[reddit] %s 304 — replaying cached content", feed_key)
        else:
            log.debug(
                "[reddit] %s 304 but no cached content — treating as empty", feed_key
            )
        return FetchResult(304, content or b"")

    if resp.status_code == 429:
        retry_after: Optional[float] = None
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                retry_after = float(ra)
            except ValueError:
                pass
        return FetchResult(429, retry_after=retry_after)

    if resp.status_code != 200:
        return FetchResult(resp.status_code)

    content = resp.content
    new_etag = resp.headers.get("ETag")
    new_last_modified = resp.headers.get("Last-Modified")

    st.save_rss_cache(feed_key, new_etag, new_last_modified, content)
    return FetchResult(200, content, new_etag, new_last_modified)


# ── Parsing ────────────────────────────────────────────────────────────────────


def _extract_post_id(entry) -> Optional[str]:
    """Extract the base-36 post ID from a feedparser entry."""
    # Try entry.id directly (e.g. "t3_abc123")
    eid = getattr(entry, "id", "") or ""
    m = _POST_ID_RE.search(eid)
    if m:
        return m.group(1)
    # Fall back to link URL
    link = getattr(entry, "link", "") or ""
    m = _LINK_RE.search(link)
    if m:
        return m.group(2)
    return None


def _extract_subreddit(entry, default_subreddits: list[str]) -> str:
    """Determine originating subreddit from the entry."""
    link = getattr(entry, "link", "") or ""
    m = _LINK_RE.search(link)
    if m:
        return m.group(1).lower()
    # Tags sometimes contain subreddit info
    for tag in getattr(entry, "tags", []) or []:
        term = getattr(tag, "term", "") or ""
        if term:
            return term.lower()
    # For single-sub feeds
    if len(default_subreddits) == 1:
        return default_subreddits[0].lower()
    return (default_subreddits[0] if default_subreddits else "unknown").lower()


def _extract_body(summary_html: str) -> str:
    """
    Strip Reddit's wrapper HTML and return only the post body text.
    Returns empty string for link-only posts.
    """
    if not summary_html:
        return ""

    soup = BeautifulSoup(summary_html, "html.parser")

    # Remove "submitted by" paragraph and [link]/[comments] spans
    for tag in soup.find_all(["p", "span", "a"]):
        text_lower = tag.get_text(strip=True).lower()
        if text_lower in ("[link]", "[comments]"):
            tag.decompose()
            continue
        if text_lower.startswith("submitted by"):
            tag.decompose()

    # Target the markdown div if present
    md_div = soup.find("div", class_="md")
    target = md_div if md_div else soup

    return target.get_text(separator="\n", strip=True)


def _parse_timestamp(entry) -> str:
    """Return ISO-8601 UTC timestamp from entry.published."""
    published = getattr(entry, "published", None)
    if published:
        try:
            dt = parsedate_to_datetime(published)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    # feedparser also exposes published_parsed (time.struct_time, UTC)
    tp = getattr(entry, "published_parsed", None)
    if tp:
        dt = datetime(*tp[:6], tzinfo=timezone.utc)
        return dt.isoformat()
    return datetime.now(timezone.utc).isoformat()


def _parse_author(entry) -> str:
    author = getattr(entry, "author", "") or ""
    # RSS often gives "/u/username"
    return author.lstrip("/u").lstrip("/").strip() or "unknown"


def parse_entries(content: bytes, subreddits: list[str]) -> list[dict]:
    """Parse RSS content and return a list of post dicts."""
    feed = feedparser.parse(content)
    posts = []

    for entry in feed.entries:
        post_id = _extract_post_id(entry)
        if not post_id:
            log.debug(
                "Could not extract post ID from entry: %s", getattr(entry, "id", "")
            )
            continue

        subreddit = _extract_subreddit(entry, subreddits)
        title = (getattr(entry, "title", "") or "").strip()
        summary = getattr(entry, "summary", "") or ""
        body = _extract_body(summary)
        link = getattr(entry, "link", "") or ""
        author = _parse_author(entry)
        posted_at = _parse_timestamp(entry)

        posts.append(
            {
                "reddit_post_id": post_id,
                "subreddit": subreddit,
                "title": title,
                "text": body,
                "url": link,
                "posted_by": author,
                "posted_at": posted_at,
            }
        )

    return posts


# ── New-post identification ────────────────────────────────────────────────────


def find_new_posts(
    posts: list[dict], feed_state: dict, bootstrap_mode: str
) -> list[dict]:
    """
    Return only posts not yet seen, respecting bootstrap mode.

    bootstrap_mode='latest': skip everything on first run; just record cursor.
    bootstrap_mode='ingest': process everything currently visible.

    Uses a single batched DB query instead of one query per post to avoid
    connection-open/close overhead at scale.
    """
    if not posts:
        return []

    last_seen_id = feed_state.get("last_seen_post_id")

    # Batch existence check — one DB round-trip for all post IDs.
    post_ids = [p["reddit_post_id"] for p in posts]
    existing_ids = st.posts_exist_batch(post_ids)

    new = []
    for post in posts:
        pid = post["reddit_post_id"]
        if pid in existing_ids:
            continue
        if bootstrap_mode == "latest" and last_seen_id is None:
            # First run in latest mode: skip existing posts, just note them
            continue
        new.append(post)

    return new


def process_feed(
    feed_key: str, subreddits: list[str], feed_state: dict
) -> tuple[int, Optional[str], Optional[str]]:
    """
    Full pipeline for one feed: fetch → parse → persist → return new count.

    Returns (new_post_count, newest_post_id, newest_posted_at).
    Raises on HTTP errors so the scheduler can handle backoff.
    """
    result = fetch_feed(feed_key)

    if result.status == 304:
        log.debug("[reddit] %s not modified (304)", feed_key)
        return 0, None, None

    if result.status == 429:
        raise RedditRateLimitError(result.retry_after)

    if result.status in (403, 503, 500, 502, 504):
        raise RedditHTTPError(result.status)

    if result.status == -1:
        raise RedditNetworkError()

    if result.status != 200:
        log.warning("[reddit] unexpected status %d for %s", result.status, feed_key)
        return 0, None, None

    if not result.content:
        return 0, None, None

    posts = parse_entries(result.content, subreddits)

    bootstrap_mode = config.REDDIT_BOOTSTRAP_MODE
    new_posts = find_new_posts(posts, feed_state, bootstrap_mode)

    # On first bootstrap in 'latest' mode, record cursor without ingesting
    if not feed_state.get("last_seen_post_id") and bootstrap_mode == "latest" and posts:
        newest = posts[0]  # feedparser returns newest first
        log.info(
            "[reddit] %s bootstrap: establishing cursor at post %s",
            feed_key,
            newest["reddit_post_id"],
        )
        return 0, newest["reddit_post_id"], newest["posted_at"]

    if not new_posts:
        return 0, None, None

    # Persist all new posts BEFORE updating cursor
    for post in new_posts:
        st.insert_post(post)
        log.info("[reddit] new post: %s — %s", post["subreddit"], post["title"][:80])

    newest = new_posts[0]
    return len(new_posts), newest["reddit_post_id"], newest["posted_at"]


# ── Custom exceptions ──────────────────────────────────────────────────────────


class RedditRateLimitError(Exception):
    def __init__(self, retry_after: Optional[float] = None) -> None:
        self.retry_after = retry_after


class RedditHTTPError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class RedditNetworkError(Exception):
    pass

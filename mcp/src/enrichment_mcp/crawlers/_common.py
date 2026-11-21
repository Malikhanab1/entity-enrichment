"""Shared crawling primitives — token-efficient, agent-driven.

PATCH v2:
  - extract_links_intelligently no longer skips /blog and /news —
    many business owner stories and About pages live there.
  - max_links default raised 15→20 to give multi-page crawl more candidates.
  - sanitize_text handles surrogate characters that cause JSON errors.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from crawlee import ConcurrencySettings
from crawlee.crawlers import PlaywrightCrawler


@dataclass(frozen=True)
class CrawlSettings:
    concurrency: int = int(os.getenv("ENRICHMENT_CONCURRENCY", "1"))
    min_delay_seconds: float = float(os.getenv("ENRICHMENT_MIN_DELAY_SECONDS", "0.5"))
    max_delay_seconds: float = float(os.getenv("ENRICHMENT_MAX_DELAY_SECONDS", "1.5"))
    max_retries: int = int(os.getenv("ENRICHMENT_MAX_RETRIES", "1"))


SETTINGS = CrawlSettings()

BLOCK_MARKERS = (
    "captcha",
    "unusual traffic",
    "access denied",
    "temporarily blocked",
    "automated queries",
    "verify you are human",
    "i'm not a robot",
    "before you continue to google",
    "enable javascript and cookies",
    "your connection is not private",
)

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_SANITIZE_RE = re.compile(r"[\xa0\u00ad\u200b\ufeff]")
_ELLIPSIS_RE = re.compile(r"\u2026")
# PATCH: also strip surrogate characters that break JSON serialization
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def sanitize_text(text: str) -> str:
    """
    Replace problematic Unicode characters that break LLM JSON tool-call parsing.

    PATCH: also removes surrogate characters (lone surrogates) which cause
    json.dumps() to raise UnicodeEncodeError in some Python versions.
    """
    if not text:
        return text
    text = _SANITIZE_RE.sub(" ", text)
    text = _ELLIPSIS_RE.sub("...", text)
    text = _SURROGATE_RE.sub("", text)  # PATCH: remove lone surrogates
    text = re.sub(r"  +", " ", text)
    return text.strip()


def conservative_crawler(
    *,
    max_requests: int,
    ignore_errors: list[int] | None = None,
    wait_until: str = "domcontentloaded",
) -> PlaywrightCrawler:
    """Single-worker Playwright crawler — token-efficient."""
    kwargs: dict = {}
    if ignore_errors:
        kwargs["ignore_http_error_status_codes"] = ignore_errors

    return PlaywrightCrawler(
        headless=True,
        max_requests_per_crawl=max_requests,
        max_request_retries=SETTINGS.max_retries,
        max_session_rotations=0,
        retry_on_blocked=False,
        respect_robots_txt_file=False,
        navigation_timeout=timedelta(seconds=15),
        goto_options={"wait_until": wait_until},
        concurrency_settings=ConcurrencySettings(
            min_concurrency=1,
            desired_concurrency=1,
            max_concurrency=max(1, SETTINGS.concurrency),
            max_tasks_per_minute=5,
        ),
        browser_launch_options={
            "args": ["--no-sandbox", "--disable-setuid-sandbox"],
        },
        browser_new_context_options={
            "user_agent": _CHROME_UA,
            "locale": "en-US",
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "DNT": "1",
            },
        },
        **kwargs,
    )


async def polite_pause() -> None:
    await asyncio.sleep(
        random.uniform(SETTINGS.min_delay_seconds, SETTINGS.max_delay_seconds)
    )


def extract_meaningful_text(text: str, max_chars: int = 2000) -> str:
    """
    Extract meaningful text from page body, trimming aggressively.
    """
    text = sanitize_text(text)

    noise_patterns = [
        r"(?i)(privacy policy|terms of service|cookie|accept all).{0,100}",
        r"(?i)(follow us on|social media|subscribe).{0,80}",
        r"(?i)(copyright|all rights reserved).{0,50}",
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, "", text)

    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars].strip()


async def page_evidence(page, url: str, source_type: str) -> dict:
    title = sanitize_text((await page.title()).strip())
    try:
        body_text = await page.inner_text("body")
        text = extract_meaningful_text(body_text, max_chars=2000)
    except Exception:
        text = ""

    blocked = is_blocked_response(text[:1000], title)

    return {
        "source_url": url,
        "source_type": source_type,
        "title": title,
        "text": text,
        "status": "blocked" if blocked else "ok",
    }


def is_blocked_response(text: str, title: str) -> bool:
    combined = f"{title} {text}".lower()
    return any(marker in combined for marker in BLOCK_MARKERS)


def same_site(candidate: str, root_url: str) -> bool:
    return urlparse(candidate).netloc == urlparse(root_url).netloc


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        return f"https://{url.strip()}"
    return url.strip()


def extract_links_intelligently(
    raw_links: list[dict],
    max_links: int = 20,  # PATCH: was 15
) -> list[dict]:
    """
    Keep only the most useful links, discard noise.

    PATCH: removed /blog and /news from skip_patterns — many businesses
    publish their About/founder story on their blog, and news pages often
    contain contact information. Only pure pagination and social links are skipped.
    """
    skip_patterns = [
        r"(?i)(facebook\.com|twitter\.com|linkedin\.com|instagram\.com|youtube\.com|tiktok\.com|pinterest\.com)",
        r"(?i)(page=|p=|\?page|\?p=|/page/\d|/p/\d)",  # pagination only
        r"(?i)(privacy|terms|cookie|legal|disclaimer)",
        r"(?i)(/cart|/checkout|/shop/cart|/store/cart)",
        r"(?i)(/wp-login|/wp-admin|/login|/signin|/register)",
    ]

    filtered = []
    for link in raw_links:
        href = link.get("href", "").lower()
        if any(re.search(pattern, href) for pattern in skip_patterns):
            continue
        if len(href) > 200:
            continue
        filtered.append(link)

    return filtered[:max_links]

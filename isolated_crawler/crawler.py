"""
Core fetching layer. Preserves the original hybrid aiohttp + Playwright
architecture: fast aiohttp first, Playwright fallback only for JS-shell
pages that render app content client-side.

Adds: per-domain delay/locking (respectful crawling), retry count, and
response-time tracking used by the opportunity-signal heuristics.
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from collections import defaultdict

import aiohttp
from bs4 import BeautifulSoup

from config import CONFIG
from logging_setup import logger

CONTACT_PAGE_KEYWORDS = [
    "contact",
    "about",
    "team",
    "support",
    "help",
    "info",
    "reach",
    "us",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


class DomainThrottle:
    """Ensures we don't hammer a single domain with concurrent/rapid requests."""

    def __init__(self, delay: float):
        self.delay = delay
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_hit: dict[str, float] = {}

    async def wait(self, domain: str) -> None:
        lock = self._locks[domain]
        async with lock:
            last = self._last_hit.get(domain, 0.0)
            elapsed = time.monotonic() - last
            if elapsed < self.delay:
                await asyncio.sleep(self.delay - elapsed)
            self._last_hit[domain] = time.monotonic()


def is_js_shell(soup: BeautifulSoup) -> bool:
    """Detect if the page is likely a React/Vue/Angular SPA missing rendered content."""
    text = soup.get_text(separator=" ", strip=True)

    if len(text) < 200 and soup.find("script", src=True):
        return True

    root_elements = soup.find(id=re.compile(r"^(root|app|__next)$", re.I))
    if root_elements and len(text) < 500:
        return True

    return False


def get_crawlable_links(
    soup: BeautifulSoup, base_url: str, base_domain: str
) -> dict[str, str]:
    subpage_urls: dict[str, str] = {}

    def add_links(elements, reason) -> bool:
        for a_tag in elements:
            href = a_tag.get("href")
            if not href or href.startswith(("javascript:", "tel:", "mailto:")):
                continue
            full_url = urllib.parse.urljoin(base_url, href)
            parsed_full = urllib.parse.urlparse(full_url)
            if parsed_full.netloc == base_domain:
                clean_url = full_url.split("#")[0].split("?")[0]
                if clean_url != base_url and clean_url not in subpage_urls:
                    subpage_urls[clean_url] = reason
                if len(subpage_urls) >= CONFIG.max_pages_per_site:
                    return True
        return False

    keyword_links = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].lower()
        text = a_tag.get_text(separator=" ", strip=True).lower()
        if any(kw in href for kw in CONTACT_PAGE_KEYWORDS) or any(
            kw in text for kw in CONTACT_PAGE_KEYWORDS
        ):
            keyword_links.append(a_tag)

    if add_links(keyword_links, "keyword match"):
        return subpage_urls
    for nav in soup.find_all(["nav", "header"]):
        if add_links(nav.find_all("a", href=True), "navbar"):
            return subpage_urls
    for footer in soup.find_all(["footer", "div"], class_=re.compile("footer", re.I)):
        if add_links(footer.find_all("a", href=True), "footer"):
            return subpage_urls

    return subpage_urls


async def fetch_with_playwright(
    context, pw_semaphore: asyncio.Semaphore, url: str
) -> tuple[int, str]:
    """Fallback fetcher using a shared browser context (no new browser per site)."""
    # Timeout the semaphore acquire itself: if all browser slots are occupied by
    # hung pages, we must not block here forever (the outer site_timeout can't
    # cancel a bare semaphore.acquire() that never returns).
    try:
        await asyncio.wait_for(
            pw_semaphore.acquire(), timeout=CONFIG.playwright_hard_timeout
        )
    except asyncio.TimeoutError:
        logger.debug(
            f"[{url}] (Playwright) Timed out waiting for a browser slot. Skipping."
        )
        return 0, ""
    try:
        return await asyncio.wait_for(
            _fetch_with_playwright_inner(context, url),
            timeout=CONFIG.playwright_hard_timeout,
        )
    except asyncio.TimeoutError:
        logger.debug(f"[{url}] (Playwright) Hard timeout exceeded, aborting.")
        return 0, ""
    except Exception as e:
        logger.debug(f"[{url}] (Playwright) Unexpected error: {e}")
        return 0, ""
    finally:
        pw_semaphore.release()


async def _fetch_with_playwright_inner(context, url: str) -> tuple[int, str]:
    page = None
    try:
        page = await context.new_page()

        async def intercept_route(route):
            try:
                if route.request.resource_type in (
                    "image",
                    "stylesheet",
                    "font",
                    "media",
                ):
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                # Route may already be handled/closed; never let this crash the page.
                pass

        await page.route("**/*", intercept_route)
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=CONFIG.playwright_timeout
        )
        status = response.status if response else 200
        if status in (403, 401):
            return status, ""

        await page.wait_for_timeout(1000)
        html = await page.content()
        return status, html
    except Exception as e:
        logger.debug(f"[{url}] (Playwright) Error fetching page: {e}")
        return 0, ""
    finally:
        if page:
            try:
                await asyncio.wait_for(page.close(), timeout=5)
            except BaseException:
                pass


async def fetch_page(
    session: aiohttp.ClientSession,
    context,
    pw_semaphore: asyncio.Semaphore,
    throttle: DomainThrottle,
    url: str,
) -> tuple[int, str, float]:
    """Two-stage fetcher with retries and per-domain throttling.

    Returns (status, html, response_time_seconds).
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    domain = urllib.parse.urlparse(url).netloc

    status, html, elapsed = 0, "", 0.0
    attempt = 0
    while attempt <= CONFIG.retry_count:
        attempt += 1
        await throttle.wait(domain)
        start = time.monotonic()
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=CONFIG.timeout,
                ssl=False,
                allow_redirects=True,
            ) as response:
                status = response.status
                elapsed = time.monotonic() - start
                if status in (403, 401):
                    return status, "", elapsed
                if "text/html" in response.headers.get("Content-Type", ""):
                    html = await response.text(errors="ignore")
                    break
                return status, "", elapsed
        except asyncio.TimeoutError:
            status, elapsed = 408, time.monotonic() - start
        except Exception:
            status, elapsed = 0, time.monotonic() - start
        # retry loop continues unless attempts exhausted

    if status == 200 and html:
        soup = BeautifulSoup(html, "lxml")
        if is_js_shell(soup):
            logger.debug(
                f"[{url}] Detected JS-rendered shell. Falling back to Playwright..."
            )
            pw_status, pw_html = await fetch_with_playwright(context, pw_semaphore, url)
            return pw_status, pw_html, elapsed

    return status, html, elapsed


async def _head_check(session: aiohttp.ClientSession, link: str) -> bool:
    """Return True if the link appears dead (error, timeout, or bad status)."""
    try:
        async with session.head(
            link, timeout=6, ssl=False, allow_redirects=True
        ) as resp:
            return resp.status >= 400
    except Exception:
        return True


async def check_links_alive(
    session: aiohttp.ClientSession, links: list[str], sample_size: int = 5
) -> float:
    """Cheaply HEAD-check a sample of internal links (in parallel); return dead-link ratio."""
    if not links:
        return 0.0
    sample = links[:sample_size]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(_head_check(session, link) for link in sample), return_exceptions=True
            ),
            timeout=CONFIG.link_check_timeout,
        )
    except asyncio.TimeoutError:
        return 0.0  # inconclusive -- don't penalize on a timeout of our own check

    dead = sum(1 for r in results if r is True)
    return dead / len(sample)

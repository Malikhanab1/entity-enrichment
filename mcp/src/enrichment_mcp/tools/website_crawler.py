"""
MCP Tool: crawl_website_for_contacts
=====================================
Progressive multi-page website crawler that extracts emails, phone numbers,
social profiles, Google Maps links, and WhatsApp numbers.

This tool is integrated into the MCP server and is callable by the agent.
It replaces the isolated_crawler's pipeline.py crawl logic for single-domain
on-demand enrichment.

Design principles:
  - Progressive: caller can request 1–10 pages; each call extends what's known.
  - Respectful: per-domain delay, no parallel hammering of one host.
  - Robust: aiohttp primary, httpx fallback for server-rendered pages.
  - Smart: contact/about/team pages are prioritised automatically.
  - Signal-rich: social + Maps + WhatsApp links surface for outreach.
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ..extractors.contacts import (
    extract_contacts_from_pages,
    _SOCIAL_PATTERNS,
    _GMAPS_RE,
    _TEL_HREF_RE,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_CONTACT_PRIORITY_TERMS = [
    "contact", "about", "team", "people", "staff", "reach", "info",
    "leadership", "management", "support", "help", "get-in-touch",
]

# Social link URLs that should be collected from the page
_SOCIAL_HREF_PATTERNS = {
    "facebook":  re.compile(r"facebook\.com/(?!share|sharer|tr\b|plugins|dialog)", re.I),
    "instagram": re.compile(r"instagram\.com/[a-zA-Z0-9_.]", re.I),
    "linkedin":  re.compile(r"linkedin\.com/(?:company|in|school)/", re.I),
    "twitter":   re.compile(r"(?:twitter|x)\.com/[a-zA-Z0-9_]", re.I),
    "youtube":   re.compile(r"(?:youtube\.com/(?:channel|@|c/|user/)|youtu\.be/)", re.I),
    "tiktok":    re.compile(r"tiktok\.com/@", re.I),
    "whatsapp":  re.compile(r"(?:wa\.me|whatsapp\.com/send)", re.I),
    "telegram":  re.compile(r"(?:t\.me|telegram\.me)/[a-zA-Z0-9_]", re.I),
    "pinterest": re.compile(r"pinterest\.com/[a-zA-Z0-9_.]", re.I),
    "snapchat":  re.compile(r"snapchat\.com/add/", re.I),
    "threads":   re.compile(r"threads\.net/@", re.I),
}

_GMAPS_HREF_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:google\.com/maps|maps\.google\.com|goo\.gl/maps|maps\.app\.goo\.gl)",
    re.I,
)


# ---------------------------------------------------------------------------
# Crawl helpers
# ---------------------------------------------------------------------------

def _normalise_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url.rstrip("/")


def _same_domain(a: str, b: str) -> bool:
    return urllib.parse.urlparse(a).netloc == urllib.parse.urlparse(b).netloc


def _score_url_for_contacts(url: str, anchor: str = "") -> int:
    """Higher score = fetch this page first."""
    evidence = f"{url.lower()} {anchor.lower()}"
    for i, term in enumerate(_CONTACT_PRIORITY_TERMS):
        if term in evidence:
            return 100 - i * 5
    return 0


def _extract_page_data(html: str, source_url: str) -> dict:
    """Parse HTML and return a page dict compatible with extract_contacts_from_pages."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove script/style noise
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()

    title = ""
    if soup.title:
        title = soup.title.get_text(" ", strip=True)[:120]

    # Collect contact_links (mailto: + tel: + social hrefs)
    contact_links: list[str] = []
    internal_links: list[tuple[str, int, str]] = []  # (url, score, anchor)

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        anchor = a.get_text(" ", strip=True)[:80]
        if not href:
            continue
        lower = href.lower()
        if lower.startswith(("mailto:", "tel:")):
            contact_links.append(href)
        elif href.startswith("http") and _same_domain(href, source_url):
            clean = href.split("#")[0].rstrip("/")
            score = _score_url_for_contacts(clean, anchor)
            if clean and clean != source_url:
                internal_links.append((clean, score, anchor))
        # Collect social hrefs regardless of domain
        for platform, pat in _SOCIAL_HREF_PATTERNS.items():
            if pat.search(href):
                contact_links.append(href)
                break
        # Google Maps hrefs
        if _GMAPS_HREF_RE.search(href):
            contact_links.append(href)

    # Deduplicate internal links, keep highest-score per URL
    seen_internal: dict[str, tuple[int, str]] = {}
    for url_c, score, anchor in internal_links:
        if url_c not in seen_internal or score > seen_internal[url_c][0]:
            seen_internal[url_c] = (score, anchor)
    sorted_internal = sorted(seen_internal.items(), key=lambda x: -x[1][0])

    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()[:5000]

    return {
        "source_url": source_url,
        "source_type": "website",
        "title": title,
        "text": text,
        "contact_links": list(dict.fromkeys(contact_links))[:30],
        "status": "ok",
        "_internal_links": [(u, s, a) for u, (s, a) in sorted_internal[:20]],
    }


async def _fetch_one(client: httpx.AsyncClient, url: str) -> tuple[str, str | None]:
    """Fetch a single URL. Returns (url_fetched, html_or_None)."""
    try:
        response = await client.get(url, follow_redirects=True, timeout=15.0)
        if response.status_code in (403, 401, 429):
            return url, None
        ct = response.headers.get("content-type", "")
        if "html" not in ct:
            return url, None
        final_url = str(response.url).split("#")[0].rstrip("/")
        return final_url, response.text
    except Exception:
        return url, None


# ---------------------------------------------------------------------------
# Public MCP tool
# ---------------------------------------------------------------------------

async def crawl_website_for_contacts(
    website_url: str,
    max_pages: int = 5,
    extra_pages: list[str] | None = None,
) -> dict[str, Any]:
    """
    Progressively crawl a business website to extract contact information.

    Visits the homepage and automatically discovers/prioritises contact,
    about, team, and support pages. Extracts emails, phone numbers, social
    media profiles (Facebook, Instagram, LinkedIn, WhatsApp, etc.), and
    Google Maps links.

    Args:
        website_url:  Root URL of the business website (e.g. "https://acme.com").
        max_pages:    Maximum pages to fetch in this call (1–10, default 5).
        extra_pages:  Additional specific sub-page URLs to include in this crawl.

    Returns:
        {
          "website": str,
          "pages_crawled": int,
          "emails_found": list[str],
          "phones_found": list[str],
          "social_profiles": dict[str, str],
          "google_maps_url": str | None,
          "email_pattern": str | None,
          "pages_visited": list[str],
          "recommended_pages": list[dict],  # ranked unvisited pages for next call
          "errors": list[str],
        }
    """
    max_pages = max(1, min(int(max_pages), 10))
    root_url = _normalise_url(website_url)
    errors: list[str] = []
    pages_data: list[dict] = []
    visited: set[str] = set()

    # Build initial queue: homepage first, then caller-supplied extras
    queue: list[tuple[str, int, str]] = [(root_url, 10_000, "homepage")]
    for extra in (extra_pages or []):
        eu = _normalise_url(extra).split("#")[0]
        if _same_domain(eu, root_url) and eu != root_url:
            queue.append((eu, 9_000, "caller-supplied"))

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=15.0) as client:
        while queue and len(pages_data) < max_pages:
            # Sort queue by priority descending
            queue.sort(key=lambda item: (-item[1], item[0]))
            url_to_fetch, _, reason = queue.pop(0)

            if url_to_fetch in visited:
                continue
            visited.add(url_to_fetch)

            # Polite delay between requests (not for first page)
            if pages_data:
                await asyncio.sleep(0.5)

            final_url, html = await _fetch_one(client, url_to_fetch)
            if not html:
                errors.append(f"Failed to fetch: {url_to_fetch}")
                continue

            # Update visited with final (post-redirect) URL
            visited.add(final_url)

            page_data = _extract_page_data(html, final_url)
            pages_data.append(page_data)

            # Enqueue discovered internal pages not yet visited
            for link_url, score, anchor in page_data.pop("_internal_links", []):
                if link_url not in visited and not any(q[0] == link_url for q in queue):
                    queue.append((link_url, score, anchor))

    if not pages_data:
        errors.append(f"Could not retrieve any pages from {root_url}.")
        return {
            "website": root_url,
            "pages_crawled": 0,
            "emails_found": [],
            "phones_found": [],
            "social_profiles": {},
            "google_maps_url": None,
            "email_pattern": None,
            "pages_visited": [],
            "recommended_pages": [],
            "errors": errors,
        }

    # Build recommended_pages from remaining queue entries
    recommended: list[dict] = []
    seen_rec: set[str] = set()
    for q_url, q_score, q_anchor in sorted(queue, key=lambda x: -x[1]):
        if q_url not in visited and q_url not in seen_rec and q_score > 0:
            recommended.append({
                "url": q_url,
                "relevance": q_score,
                "reason": q_anchor or q_url,
            })
            seen_rec.add(q_url)
        if len(recommended) >= 5:
            break

    # Extract structured contacts from all pages collected
    contact_info = extract_contacts_from_pages(pages_data, root_url)

    return {
        "website": root_url,
        "pages_crawled": len(pages_data),
        "emails_found": contact_info["emails_found"],
        "phones_found": contact_info["phones_found"],
        "social_profiles": contact_info["social_profiles"],
        "google_maps_url": contact_info["google_maps_url"],
        "email_pattern": contact_info["email_pattern"],
        "pages_visited": [p["source_url"] for p in pages_data],
        "recommended_pages": recommended,
        "errors": errors,
    }

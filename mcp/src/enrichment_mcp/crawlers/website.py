"""
Business website crawler with atomic, agent-selected page fetches.

Each request fetches one page unless a caller explicitly opts into a bounded
multi-page crawl. Browser rendering is preferred, with an HTTP/BeautifulSoup
fallback for ordinary server-rendered sites and fresh environments.
"""

from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from ._common import (
    conservative_crawler,
    normalize_url,
    is_blocked_response,
    polite_pause,
    same_site,
    extract_meaningful_text,
    extract_links_intelligently,
    sanitize_text,
)

PAGE_INTENT_WEIGHTS = {
    "founder": 100,
    "owner": 100,
    "leadership": 95,
    "management": 90,
    "director": 90,
    "principal": 90,
    "headmaster": 90,
    "head teacher": 90,
    "team": 85,
    "staff": 85,
    "people": 80,
    "about": 70,
    "who-we-are": 70,
    "our-story": 65,
    "contact": 60,
    "admissions": 35,
    "services": 30,
    "company": 25,
}


def rank_internal_page(url: str, anchor_text: str = "") -> int:
    evidence = f"{url.lower()} {anchor_text.lower()}"
    return max(
        (weight for term, weight in PAGE_INTENT_WEIGHTS.items() if term in evidence),
        default=0,
    )


async def crawl_business_website(
    url: str,
    max_pages: int = 1,
    selected_pages: list[str] | None = None,
    follow_relevant_links: bool = False,
) -> dict:
    """
    Crawl a business website with a bounded, agent-selected scope.

    PATCH v2:
    - follow_relevant_links defaults to True so About/Contact pages are crawled.
    - max_pages defaults to 5.
    - DNS resolution errors (ERR_NAME_NOT_RESOLVED) are caught per-URL and
      returned in errors[] instead of crashing.

    Args:
        url:                   Root business website URL.
        max_pages:             Maximum pages to crawl (5 by default).
        selected_pages:        Specific URLs agent wants to inspect.
        follow_relevant_links: If True, crawl ranked internal links automatically.
    """
    root_url = normalize_url(url)
    pages: list[dict] = []
    errors: list[str] = []
    visited: set[str] = set()

    # An explicit selection replaces the homepage rather than adding to it.
    # This is what makes each tool call atomic and credit/prompt predictable.
    to_crawl: list[tuple[str, int, str]] = []
    for page in selected_pages or []:
        page_url = normalize_url(page).rstrip("/")
        if same_site(page_url, root_url):
            to_crawl.append((page_url, 9_000, "selected by agent"))
    if not to_crawl:
        to_crawl.append((root_url, 10_000, "homepage"))

    recommended: dict[str, dict] = {}

    while to_crawl and len(pages) < max_pages:
        to_crawl.sort(key=lambda item: (-item[1], item[0]))

        batch = to_crawl[:1]
        to_crawl = to_crawl[1:]

        batch_pages, batch_errors, discovered = await _crawl_batch(
            batch, root_url, visited
        )
        pages.extend(batch_pages)
        errors.extend(batch_errors)
        visited.update(p["source_url"] for p in batch_pages)

        for link, score, reason in discovered:
            recommended.setdefault(
                link, {"url": link, "relevance": score, "reason": reason}
            )

        # PATCH: only break early when NOT following links
        if not follow_relevant_links:
            break

        queued_urls = {item[0] for item in to_crawl}
        for link, score, reason in discovered:
            if link not in visited and link not in queued_urls and score > 0:
                to_crawl.append((link, score, reason))

    if not pages:
        errors.append(f"No pages collected from {root_url}.")

    return {
        "source_url": root_url,
        "source_type": "website",
        "pages": pages,
        "recommended_pages": sorted(
            recommended.values(), key=lambda item: (-item["relevance"], item["url"])
        )[:5],
        "crawl_mode": "agent_directed",
        "errors": errors,
    }


async def _crawl_batch(
    queued: list[tuple[str, int, str]], root_url: str, visited: set[str]
) -> tuple[list[dict], list[str], list[tuple[str, int, str]]]:
    pages: list[dict] = []
    errors: list[str] = []
    discovered: list[tuple[str, int, str]] = []

    crawler = conservative_crawler(
        max_requests=len(queued),
        ignore_errors=[403, 404, 429, 503],
    )

    @crawler.router.default_handler
    async def handle(context) -> None:
        await polite_pause()
        current_url = context.request.url

        if current_url in visited:
            return

        raw_title = await context.page.title()
        title = sanitize_text(raw_title.strip())

        try:
            body_text = await context.page.inner_text("body")
            # PATCH: raised 1500→2000 chars for richer contact/bio extraction
            text = extract_meaningful_text(body_text, max_chars=2000)
        except Exception:
            text = ""

        blocked = is_blocked_response(text[:500], title)

        try:
            contact_links = await context.page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href).filter(h => /^(mailto:|tel:)/i.test(h))"""
            )
        except Exception:
            contact_links = []

        pages.append(
            {
                "source_url": current_url,
                "source_type": "website",
                "title": title,
                "text": text,
                "contact_links": contact_links[:20],
                "status": "blocked" if blocked else "ok",
            }
        )

        if blocked:
            errors.append(f"Blocked: {current_url}")
            return

        try:
            raw_links = await context.page.evaluate(
                """
            () => Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({href: a.href, text: (a.innerText || a.textContent || '').trim()}))
                .filter(link => link.href.startsWith('http'))
            """
            )
        except Exception:
            raw_links = []

        raw_links = extract_links_intelligently(raw_links, max_links=15)

        for raw_link in raw_links:
            href = raw_link.get("href", "").split("#")[0].rstrip("/")
            anchor_text = raw_link.get("text", "")
            score = rank_internal_page(href, anchor_text)

            if (
                same_site(href, root_url)
                and href not in visited
                and score > 0
                and not any(item[0] == href for item in discovered)
            ):
                discovered.append((href, score, anchor_text[:80] or href))

    try:
        await crawler.run([item[0] for item in queued])
    except Exception as exc:
        err_str = str(exc)
        # PATCH: surface DNS errors clearly instead of generic "Crawl error"
        if "ERR_NAME_NOT_RESOLVED" in err_str:
            errors.append(
                f"DNS resolution failed for {queued[0][0] if queued else 'URL'} — "
                "the domain does not exist. Website URL may be incorrect."
            )
        elif "ERR_CONNECTION_REFUSED" in err_str:
            errors.append(f"Connection refused: {queued[0][0] if queued else 'URL'}")
        elif "timeout" in err_str.lower():
            errors.append(f"Timeout crawling: {queued[0][0] if queued else 'URL'}")
        else:
            errors.append(f"Crawl error: {type(exc).__name__}: {err_str[:200]}")

    # Playwright is excellent for JavaScript-heavy sites, but its browser
    # binary may not be installed in a fresh environment and some ordinary
    # sites reject headless browsing.  A conventional HTTP fetch means the
    # crawler works immediately for server-rendered business sites.
    if not pages:
        for target, _, _ in queued:
            fallback_page, fallback_links, fallback_error = await _http_fallback(target)
            if fallback_page:
                pages.append(fallback_page)
                discovered.extend(fallback_links)
            elif fallback_error:
                errors.append(fallback_error)

    return pages, errors, discovered


async def _http_fallback(url: str) -> tuple[dict | None, list[tuple[str, int, str]], str | None]:
    """Fetch one server-rendered page when browser crawling is unavailable."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; CognivelleResearch/1.0)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return None, [], f"HTTP fallback failed for {url}: {type(exc).__name__}: {str(exc)[:160]}"

    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type:
        return None, [], f"HTTP fallback skipped non-HTML response: {response.url}"
    soup = BeautifulSoup(response.text, "html.parser")
    title = sanitize_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    text = extract_meaningful_text(soup.get_text(" ", strip=True), max_chars=4000)
    source_url = str(response.url).split("#", 1)[0].rstrip("/")
    links: list[tuple[str, int, str]] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href:
            continue
        absolute = str(httpx.URL(source_url).join(href)).split("#", 1)[0].rstrip("/")
        label = sanitize_text(anchor.get_text(" ", strip=True))[:80]
        score = rank_internal_page(absolute, label)
        if same_site(absolute, source_url) and score > 0 and not any(item[0] == absolute for item in links):
            links.append((absolute, score, label or absolute))
    contact_links = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if href.lower().startswith(("mailto:", "tel:")):
            contact_links.append(href)
    return (
        {
            "source_url": source_url,
            "source_type": "website",
            "title": title,
            "text": text,
            "status": "blocked" if is_blocked_response(text[:500], title) else "ok",
            "crawl_method": "http_fallback",
            "contact_links": contact_links[:20],
        },
        links,
        None,
    )

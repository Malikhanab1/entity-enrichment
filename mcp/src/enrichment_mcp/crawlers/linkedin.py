"""
LinkedIn: public data via the configured web-search provider only.

LinkedIn requires login for almost everything meaningful. Attempting to
scrape linkedin.com directly with Playwright just wastes 8-10 seconds on
a login redirect. Instead we:
  1. Search the configured providers for company-page URLs and people profiles
  2. Parse the result snippets (Google caches a lot of LinkedIn metadata)
  3. Return whatever we can extract without ever touching linkedin.com

This approach is fast (httpx, ~1s) and never hits a block wall.
"""

from __future__ import annotations

from .google import search_google


async def crawl_linkedin(company_name: str, linkedin_url: str | None = None) -> dict:
    """
    Find LinkedIn data for a company via web search (no direct LinkedIn crawl).

    Returns company snippet, any people found in results, and the profile URL.
    """
    errors: list[str] = []
    people: list[dict] = []
    company_data: dict | None = None

    # ── 1. Find company page ─────────────────────────────────────────────────
    if not linkedin_url:
        disc = await search_google(f'site:linkedin.com/company "{company_name}"')
        errors.extend(disc.get("errors", []))
        for item in disc.get("results", []):
            if "/company/" in item.get("url", ""):
                linkedin_url = item["url"]
                company_data = {
                    "source_url": linkedin_url,
                    "source_type": "linkedin_snippet",
                    "title": item.get("title", ""),
                    "text": item.get("snippet", ""),
                    "status": "ok",
                }
                break

    # ── 2. Find people profiles ──────────────────────────────────────────────
    people_disc = await search_google(f'site:linkedin.com/in "{company_name}"')
    errors.extend(people_disc.get("errors", []))
    for item in people_disc.get("results", []):
        url = item.get("url", "")
        if "linkedin.com/in/" not in url:
            continue
        people.append({
            "linkedin_url": url,
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
        })

    if not linkedin_url:
        errors.append(
            "No public LinkedIn company page found via search. "
            "The company may not have a LinkedIn presence, or their page may be unlisted."
        )

    return {
        "company_name": company_name,
        "source_url": linkedin_url,
        "source_type": "linkedin",
        "company": company_data,
        "people": people[:5],
        "errors": errors,
    }

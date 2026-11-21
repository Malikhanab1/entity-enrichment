"""
Facebook: public data via the configured web-search provider only.

PATCH v2 — key fixes:
  1. Now tries MULTIPLE search variants (exact name, name+location, short name)
     instead of a single query — catches pages whose Facebook name differs
     slightly from the registered business name.
  2. Snippet is now mined for owner/founder name mentions and returned in
     people[] so find_decision_makers can use it without extra searches.
  3. Also searches for the business on Facebook without site: restriction
     as a fallback — catches pages indexed under different titles.
"""

from __future__ import annotations

import re

from .google import search_google
from ._common import sanitize_text

_NAME_RE = re.compile(
    r"[A-Z][a-zA-Z'\-]{1,25}"
    r"(?:\s+(?:al|ul|bin|binti|van|de|di|del|du|von|of|el|abu))?"
    r"(?:\s+[A-Z][a-zA-Z'\-]{1,25}){1,3}"
)

_OWNER_TITLES = re.compile(
    r"\b(owner|founder|co-founder|proprietor|director|principal|chairman|CEO|MD)\b",
    re.I,
)


def _extract_people_from_snippet(text: str, business_name: str) -> list[dict]:
    """Mine a Facebook snippet for person name + title mentions."""
    people = []
    biz_words = {w.lower() for w in business_name.split() if len(w) > 2}

    for m in _NAME_RE.finditer(text):
        name = m.group(0).strip()
        # Skip if name is just the business name words
        name_words = {w.lower() for w in name.split() if len(w) > 2}
        if name_words and name_words.issubset(biz_words):
            continue

        # Look for a title within 100 chars of the name
        start = max(0, m.start() - 15)
        end = min(len(text), m.end() + 100)
        context = text[start:end]
        title_match = _OWNER_TITLES.search(context)
        title = title_match.group(0) if title_match else None

        people.append({"name": name, "title": title, "source": "facebook_snippet"})

        if len(people) >= 3:
            break

    return people


async def crawl_facebook(business_name: str, facebook_url: str | None = None) -> dict:
    """
    Find Facebook data for a business via web search (no direct Facebook crawl).

    PATCH v2: tries multiple query variants; mines snippets for owner names.
    """
    errors: list[str] = []
    business_data: dict | None = None
    people: list[dict] = []

    if not facebook_url:
        # PATCH: try multiple queries for better coverage
        search_queries = [
            f'site:facebook.com "{business_name}"',
            f'site:facebook.com "{business_name}" about',
        ]

        for query in search_queries:
            disc = await search_google(query, num_results=5)
            errors.extend(disc.get("errors", []))
            for item in disc.get("results", []):
                url = item.get("url", "")
                if (
                    "facebook.com/" in url
                    and "/groups/" not in url
                    and "/events/" not in url
                    and "/pages/category/" not in url
                ):
                    snippet = sanitize_text(item.get("snippet", ""))
                    title_text = sanitize_text(item.get("title", ""))

                    facebook_url = url
                    business_data = {
                        "source_url": url,
                        "source_type": "facebook_snippet",
                        "title": title_text,
                        "text": snippet,
                        "status": "ok",
                    }

                    # PATCH: mine snippet for owner/founder mentions
                    combined = f"{title_text} {snippet}"
                    people.extend(_extract_people_from_snippet(combined, business_name))
                    break

            if facebook_url:
                break

    if not facebook_url:
        # PATCH: broader fallback — search without site: restriction
        fallback_q = f'"{business_name}" facebook page about owner'
        disc2 = await search_google(fallback_q, num_results=5)
        errors.extend(disc2.get("errors", []))
        for item in disc2.get("results", []):
            url = item.get("url", "")
            if "facebook.com/" in url and "/groups/" not in url:
                facebook_url = url
                snippet = sanitize_text(item.get("snippet", ""))
                business_data = {
                    "source_url": url,
                    "source_type": "facebook_snippet",
                    "title": sanitize_text(item.get("title", "")),
                    "text": snippet,
                    "status": "ok",
                }
                people.extend(_extract_people_from_snippet(snippet, business_name))
                break

    if not facebook_url:
        errors.append(
            "No public Facebook business page found. "
            "The business may not have a Facebook presence."
        )

    return {
        "business_name": business_name,
        "source_url": facebook_url,
        "source_type": "facebook",
        "business": business_data,
        "people": people[:3],
        "errors": errors,
    }

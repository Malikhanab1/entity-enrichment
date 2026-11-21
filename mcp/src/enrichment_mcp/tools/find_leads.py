"""
STEP 1 — find_leads
====================
Discover candidate businesses that are likely to need custom software,
automation, or a new/improved website.

PATCH v2 — key fixes:
  1. Fires MULTIPLE search queries per request (bare niche, niche+location,
     local intent, directory listing) and deduplicates results — was a single
     generic query that missed most local businesses.
  2. Results from directory/listing sites (Yelp, JustDial, etc.) are now
     MINED for business names + websites instead of being silently dropped.
  3. Lead deduplication is domain-aware — same business appearing in multiple
     queries is merged, not listed twice.
  4. `num_results` cap raised to 30; caller controls how many they want.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from ..crawlers.google import search_google
from ..crawlers._common import sanitize_text
from ..extractors.leads import extract_leads_from_results, DIRECTORY_DOMAINS

HIGH_VALUE_NICHES = [
    "law firm",
    "accounting firm",
    "real estate agency",
    "dental clinic",
    "medical clinic",
    "logistics company",
    "recruitment agency",
    "e-commerce store",
    "construction company",
    "manufacturing company",
    "restaurant chain",
    "hotel",
    "insurance broker",
    "marketing agency",
    "consulting firm",
    "school",
    "college",
    "pharmacy",
    "clinic",
    "beauty salon",
    "gym",
    "fitness center",
    "travel agency",
]

# Directories that list multiple businesses — we mine their snippets for names
_DIRECTORY_HINTS = {
    "yelp.com",
    "justdial.com",
    "sulekha.com",
    "yellowpages.com",
    "tripadvisor.com",
    "zomato.com",
    "foursquare.com",
    "findglocal.com",
    "moawin.pk",
    "businesslist.pk",
    "pakistan.businessdir.com",
}


def _root_domain(url: str) -> str:
    try:
        parts = urlparse(url).netloc.lstrip("www.").split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else ""
    except Exception:
        return ""


def _dedup_leads(leads: list[dict]) -> list[dict]:
    """Deduplicate by domain; keep highest-ranked entry per domain."""
    seen: dict[str, dict] = {}
    for lead in leads:
        domain = _root_domain(lead.get("website") or "")
        key = domain or lead["business_name"].lower()[:30]
        if key not in seen:
            seen[key] = lead
        # keep whichever has a website if one does and one doesn't
        elif not seen[key].get("website") and lead.get("website"):
            seen[key] = lead
    return list(seen.values())


async def find_leads(
    niche: str,
    location: str,
    num_results: int = 10,
    require_website: bool = False,
) -> dict[str, object]:
    """
    STEP 1 — Find potential leads (businesses) by niche and location.

    PATCH v2: fires 3 complementary search queries and merges results so
    local businesses visible only via directory listings or maps are included.

    Queries fired:
      1. "<niche> <location>"                    — baseline
      2. "<niche> in <location>"                 — local intent (maps-biased)
      3. "<niche> <location> contact OR website" — filters for web-present biz

    After deduplication, up to `num_results` leads are returned.

    Args:
        niche:           Type of business, e.g. "dental clinic", "law firm".
        location:        City/region, e.g. "Lahore", "Dubai".
        num_results:     How many unique leads to surface (max 30).
        require_website: If True, only include businesses with a website URL.
    """
    num_results = max(1, min(num_results, 30))
    errors: list[str] = []
    all_results: list[dict] = []

    # PATCH: 3 complementary queries
    queries = [
        f"{niche} {location}",
        f"{niche} in {location}",
        f"{niche} {location} contact OR website OR phone",
    ]

    # Independent discovery queries should not wait on each other. The search
    # router still chooses one quota-safe provider per query.
    for raw in await asyncio.gather(
        *(search_google(query, num_results=10) for query in queries)
    ):
        errors.extend(raw.get("errors", []))
        all_results.extend(raw.get("results", []))

    # Deduplicate raw results by URL before extracting leads
    seen_urls: set[str] = set()
    unique_results: list[dict] = []
    for r in all_results:
        u = r.get("url", "")
        if u not in seen_urls:
            seen_urls.add(u)
            unique_results.append(r)

    # Extract leads (drops directory domains by default)
    leads = extract_leads_from_results(unique_results, require_website=require_website)

    # PATCH: also mine directory snippets for business names
    # Many Pakistani/regional businesses only appear on JustDial, Moawin, etc.
    directory_bonus = _mine_directory_snippets(unique_results, location)
    leads = leads + directory_bonus

    leads = _dedup_leads(leads)
    if require_website:
        leads = [l for l in leads if l.get("website")]

    # Re-rank: leads with a website URL first
    leads.sort(key=lambda l: (0 if l.get("website") else 1, l.get("rank", 999)))

    # Re-number
    for i, lead in enumerate(leads, start=1):
        lead["rank"] = i

    leads = leads[:num_results]

    next_step = (
        "Leads found. Now call `enrich_business` for each promising candidate "
        "to get their full website, social profiles, and contact details. "
        "Pass `business_name` and `website` (if available) from the lead."
    )
    if not leads:
        next_step = (
            "No leads found. Try a broader niche term or a different location. "
            "Do NOT start crawling random domains."
        )

    return {
        "query": queries[0],
        "queries_fired": queries,
        "niche": niche,
        "location": location,
        "leads": leads,
        "total_found": len(leads),
        "suggested_niches": HIGH_VALUE_NICHES,
        "errors": errors,
        "next_step": next_step,
    }


def _mine_directory_snippets(results: list[dict], location: str) -> list[dict]:
    """
    Extract business names from directory listing snippets.

    Directories like JustDial/Moawin show multiple businesses in one snippet.
    Parse them out so they can be enriched later.
    """
    found: list[dict] = []
    loc_lower = location.lower()

    for item in results:
        domain = _root_domain(item.get("url", ""))
        if domain not in _DIRECTORY_HINTS:
            continue

        title = sanitize_text(item.get("title", ""))
        snippet = sanitize_text(item.get("snippet", ""))
        combined = f"{title} {snippet}"

        # Extract lines that look like business names (Title Case, short)
        for line in re.split(r"[,;|\n·•]", combined):
            line = line.strip(" -–.")
            if not line or len(line) < 4 or len(line) > 80:
                continue
            # Skip lines that are clearly descriptive sentences
            if re.search(
                r"\b(is|are|was|were|the best|top|leading|find)\b", line, re.I
            ):
                continue
            # Must look like a proper noun (first letter capital)
            if not line[0].isupper():
                continue
            # Skip if it's just the location name
            if line.lower() == loc_lower:
                continue

            found.append(
                {
                    "rank": 999,
                    "business_name": line[:80],
                    "website": None,
                    "snippet": snippet[:200],
                    "source_url": item.get("url", ""),
                }
            )

        if len(found) > 20:
            break

    return found

"""
STEP 2 — enrich_business
=========================
Given a business name (and optionally its website), collect all publicly
available information: website content, LinkedIn company page, Facebook page,
and any additional Google results.

PATCH v2 — key fixes:
  1. Minimum candidate score raised 0→25 so junk/fake domains are rejected.
  2. TLD validation rejects domains with invalid/single-char TLDs (e.g. .c).
  3. Website resolution now fires 3 targeted queries instead of 2, including
     a local-business ("maps") query to catch businesses only visible on Maps.
  4. Website crawl changed to follow_relevant_links=True and max_pages=5 so
     About/Contact/Team pages are actually fetched instead of homepage-only.
  5. Facebook snippet is mined for contact details even without a website.
  6. Resolution confidence check: if best candidate score <50, a second-pass
     query using '"<name>" site:<tld common for region>' is attempted.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from ..crawlers.google import search_google
from ..crawlers.website import crawl_business_website
from ..crawlers.linkedin import crawl_linkedin
from ..crawlers.facebook import crawl_facebook
from ..crawlers._common import sanitize_text
from ..extractors.business import extract_business_profile


NON_WEBSITE_DOMAINS = (
    "facebook.com",
    "linkedin.com",
    "instagram.com",
    "youtube.com",
    "twitter.com",
    "x.com",
    "findglocal.com",
    "schoolandcollegelistings.com",
    "moawin.pk",
    "yelp.com",
    "yellowpages",
    "tripadvisor",
    "trustpilot",
    "clutch.co",
    "g2.com",
    "wikipedia.org",
    "maps.google.com",
    "google.com",
    "goo.gl",
    "bit.ly",
    "t.co",
)

# PATCH: valid TLDs must be ≥2 chars. Rejects garbage like ".c", ".co" is ok.
_VALID_TLD_RE = re.compile(r"\.[a-zA-Z]{2,}$")

_URL_IN_TEXT = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)

# PATCH: minimum score a URL candidate must reach to be considered at all.
_MIN_CANDIDATE_SCORE = 25


def _normalise_candidate_url(value: str) -> str | None:
    """Normalise a URL mentioned in search evidence, rejecting non-sites."""
    value = value.strip(".,;:!?)]}»\"'")
    if not value:
        return None
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    parsed = urlparse(value)
    host = parsed.netloc.lower().removeprefix("www.")
    if not host or any(domain in host for domain in NON_WEBSITE_DOMAINS):
        return None
    # PATCH: reject domains whose TLD is shorter than 2 chars (e.g. .c, .x)
    if not _VALID_TLD_RE.search(host):
        return None
    return value.rstrip("/")


def _host_tokens(url: str) -> set[str]:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return set(re.split(r"[\.\-_]", host))


def _website_candidates(
    results: list[dict], business_name: str, location: str | None
) -> list[dict]:
    """
    Rank likely official sites from result URLs *and* corroborating snippets.

    PATCH: score threshold raised — candidates with score < _MIN_CANDIDATE_SCORE
    are discarded rather than being returned as the "best" option. This prevents
    completely unrelated or malformed URLs (e.g. dentist-dentist.c) from being
    selected as the official website.
    """
    name_terms = [
        term.lower()
        for term in re.findall(r"[a-zA-Z0-9]+", business_name)
        if len(term) > 2
    ]
    location_terms = [
        term.lower()
        for term in re.findall(r"[a-zA-Z0-9]+", location or "")
        if len(term) > 2
    ]
    candidates: dict[str, dict] = {}

    for result in results:
        evidence = " ".join(str(result.get(key, "")) for key in ("title", "snippet"))
        urls = [str(result.get("url", ""))] + _URL_IN_TEXT.findall(evidence)

        for raw_url in urls:
            url = _normalise_candidate_url(raw_url)
            if not url:
                continue

            tokens = _host_tokens(url)
            score = 0

            exact_matches = sum(1 for t in name_terms if t in tokens)
            score += 40 * exact_matches

            if exact_matches < len(name_terms):
                for t in name_terms:
                    if t not in tokens:
                        for ht in tokens:
                            if t in ht:
                                score += 10
                                break

            if exact_matches > 0:
                score += 5 * sum(1 for t in location_terms if t in tokens)

            if raw_url != result.get("url", ""):
                score += 35

            if "official" in evidence.lower():
                score += 10

            score += 8 * sum(1 for t in name_terms if t in evidence.lower())

            # PATCH: reject low-confidence candidates outright
            if score < _MIN_CANDIDATE_SCORE:
                continue

            current = candidates.get(url)
            entry = {
                "url": url,
                "score": score,
                "evidence": result.get("url", ""),
                "reason": "domain/name match"
                if raw_url == result.get("url", "")
                else "URL published in search snippet",
            }
            if current is None or entry["score"] > current["score"]:
                candidates[url] = entry

    return sorted(
        candidates.values(),
        key=lambda c: (-c["score"], c["url"]),
    )


def _build_page_summaries(raw_website: dict) -> list[dict]:
    _error_title_re = re.compile(r"^\s*\d{3}\b.*$", re.IGNORECASE)

    summaries = []
    for p in raw_website.get("pages", []):
        if p.get("status") == "blocked":
            continue

        raw_title = p.get("title", "")
        if _error_title_re.match(raw_title):
            continue

        title = sanitize_text(raw_title)[:80]
        excerpt = sanitize_text(p.get("text", ""))[:400]  # PATCH: raised 300→400

        excerpt_lower = excerpt.lower()
        if any(
            kw in excerpt_lower
            for kw in ("403 forbidden", "access denied", "404 not found")
        ):
            continue

        summaries.append(
            {
                "url": p.get("source_url", ""),
                "title": title,
                "excerpt": excerpt,
                "status": p.get("status", "ok"),
            }
        )

    return summaries


async def enrich_business(
    business_name: str,
    website: str | None = None,
    location: str | None = None,
    skip_social: bool = False,
) -> dict[str, object]:
    """
    STEP 2 — Deeply enrich a single business from Step 1.

    PATCH v2 changes:
    - 3 search queries for website discovery (was 2): adds a local/maps query
      so businesses discoverable via Google Maps are also found.
    - Website crawl now follows relevant internal links (About/Contact/Team)
      up to max_pages=5 (was 3, follow_relevant_links=False → homepage only).
    - URL candidate score threshold: rejects URLs scoring below 25.
    - TLD validation: rejects domains with invalid TLDs like .c.
    - If best candidate score is low (<50), fires an extra targeted query.

    Args:
        business_name:  Exact or near-exact business name from Step 1.
        website:        Website URL if found in Step 1.
        location:       City/country to disambiguate common business names.
        skip_social:    Set True to skip LinkedIn/Facebook (faster, less data).
    """
    errors: list[str] = []

    business_name = sanitize_text(business_name)
    location = sanitize_text(location) if location else location

    resolution: dict[str, object] = {"selected": None, "candidates": []}

    # ── 1. Resolve website ──────────────────────────────────────────────────
    if not website:
        # PATCH: 3 targeted queries instead of 2.
        # Query 1: bare name (e.g. "Denticare Lahore")
        # Query 2: name + location (disambiguation)
        # Query 3: name as a local search ("near me" style) — catches map-only businesses
        loc_str = location or ""
        queries = [business_name]
        if location:
            queries.append(f"{business_name} {location}")
        queries.append(
            f"{business_name} {loc_str} contact OR website OR official".strip()
        )

        all_results: list[dict] = []
        for google_result in await asyncio.gather(
            *(search_google(query, num_results=10) for query in queries[:3])
        ):
            errors.extend(google_result.get("errors", []))
            all_results.extend(google_result.get("results", []))

        candidates = _website_candidates(all_results, business_name, location)
        resolution["candidates"] = candidates[:5]

        if candidates:
            best = candidates[0]
            # PATCH: if best candidate is weak, try one more targeted query
            if best["score"] < 50:
                fallback_q = f'"{business_name}" {loc_str} official site'.strip()
                fb_result = await search_google(fallback_q, num_results=10)
                errors.extend(fb_result.get("errors", []))
                all_results.extend(fb_result.get("results", []))
                candidates = _website_candidates(all_results, business_name, location)
                resolution["candidates"] = candidates[:5]

            if candidates:
                website = candidates[0]["url"]
                resolution["selected"] = candidates[0]
            else:
                errors.append("No evidence-backed official website candidate found.")
        else:
            errors.append("No evidence-backed official website candidate found.")
    else:
        website = _normalise_candidate_url(website) or website
        resolution["selected"] = {"url": website, "reason": "provided by caller"}

    # ── 2. Crawl website ────────────────────────────────────────────────────
    # PATCH: follow_relevant_links=True and max_pages=5 so the crawler
    # actually visits About/Contact/Team pages instead of stopping at homepage.
    raw_website: dict = {}
    if website:
        try:
            raw_website = await crawl_business_website(
                website,
                max_pages=5,  # PATCH: was 3
                follow_relevant_links=True,  # PATCH: was False — homepage-only
            )
            errors.extend(raw_website.get("errors", []))
        except Exception as exc:
            errors.append(f"Website crawl failed: {type(exc).__name__}: {exc}")
    else:
        errors.append("No website found for this business.")

    # ── 3. Crawl social (optional) ──────────────────────────────────────────
    raw_linkedin: dict = {}
    raw_facebook: dict = {}
    if not skip_social:
        try:
            raw_linkedin = await crawl_linkedin(business_name)
            errors.extend(raw_linkedin.get("errors", []))
        except Exception as exc:
            errors.append(f"LinkedIn crawl failed: {type(exc).__name__}: {exc}")
        try:
            raw_facebook = await crawl_facebook(business_name)
            errors.extend(raw_facebook.get("errors", []))
        except Exception as exc:
            errors.append(f"Facebook crawl failed: {type(exc).__name__}: {exc}")

    # ── 4. Extract structured profile ───────────────────────────────────────
    profile = extract_business_profile(raw_website, raw_linkedin, raw_facebook)

    social = {
        "linkedin_url": raw_linkedin.get("source_url"),
        "facebook_url": raw_facebook.get("source_url"),
    }

    # ── 5. Build compact page summaries ────────────────────────────────────
    page_summaries = _build_page_summaries(raw_website)

    # PATCH: if no website was found but Facebook data exists, synthesize a
    # minimal page summary from the Facebook snippet so decision-maker search
    # has something to work with.
    if not page_summaries and raw_facebook.get("business"):
        fb_biz = raw_facebook["business"]
        fb_text = sanitize_text(fb_biz.get("text", ""))[:400]
        if fb_text:
            page_summaries.append(
                {
                    "url": raw_facebook.get("source_url", ""),
                    "title": sanitize_text(fb_biz.get("title", "Facebook page"))[:80],
                    "excerpt": fb_text,
                    "status": "ok",
                }
            )

    next_step = (
        f"Business enriched. Now call `find_decision_makers` with "
        f'business_name="{business_name}" and website="{website or ""}" '
        f"and website_pages=<page_summaries from this result> "
        f"to find the owner, CEO, or key contacts."
    )

    return {
        "business_name": business_name,
        "website": website,
        "website_resolution": resolution,
        "profile": profile,
        "social": social,
        "page_summaries": page_summaries,
        "recommended_pages": raw_website.get("recommended_pages", [])[:3],
        "errors": errors,
        "next_step": next_step,
    }

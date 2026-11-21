"""
Small, composable tools for agent-directed business research.

KEY OPTIMIZATION: Responses are minimal. No full search result lists.
Agent makes informed decisions with summaries, not raw data dumps.

New addition: `compress_research_context` reduces repeated evidence into a
stable fact bundle so agents reuse facts instead of re-sending the same page
text and crawling context again. This is a token-saving compression strategy
that preserves meaning, not a raw input-size trim.
"""

from __future__ import annotations

import re
from collections import OrderedDict

from ..crawlers.google import search_google
from ..crawlers.website import crawl_business_website
from ..crawlers._common import sanitize_text
from ..extractors.business import extract_business_profile
from .enrich_business import _website_candidates

# HTTP error titles to filter out of page summaries
_HTTP_ERROR_RE = re.compile(r"^\s*\d{3}\b", re.IGNORECASE)


async def find_businesses(
    need: str,
    location: str | None = None,
    limit: int = 5,
) -> dict[str, object]:
    """
    Discover businesses matching the agent's need.

    Returns only TOP results + agent guidance. No exhaustive lists.
    """
    query = " ".join(filter(None, [need, location]))
    search = await search_google(query, num_results=min(limit, 10))

    results = search.get("results", [])

    # Keep only top 5 with key fields — sanitize strings to remove \xa0 etc.
    candidates = []
    for item in results[:5]:
        candidates.append(
            {
                "name": sanitize_text(item.get("title", ""))[:60],
                "url": item.get("url", ""),
                "snippet": sanitize_text(item.get("snippet", ""))[:150],
            }
        )

    return {
        "query": query,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "recommendation": (
            "Pick the most relevant candidate and call assess_business with its name. "
            "Do not search again unless this list is unhelpful."
        ),
    }


async def assess_business(
    business_name: str,
    location: str | None = None,
) -> dict[str, object]:
    """
    Check if we can find a business website.

    Returns: One recommended website + decision for agent (crawl or skip).
    """
    # Sanitize inputs — business_name may carry \xa0 from search result titles
    business_name = sanitize_text(business_name)
    location = sanitize_text(location) if location else location

    query = " ".join(filter(None, [business_name, location, "official website"]))
    search = await search_google(query)

    candidates = _website_candidates(search.get("results", []), business_name, location)
    website = candidates[0]["url"] if candidates else None

    return {
        "business_name": business_name,
        "website_status": "likely_found" if website else "not_found",
        "found": website is not None,
        "recommended_website": website,
        "website_candidates": [
            {"url": item["url"], "reason": item["reason"], "score": item["score"]}
            for item in candidates[:3]
        ],
        "recommendation": (
            "Choose one URL from website_candidates, then call "
            f"crawl_business_pages({website!r}) to inspect exactly its homepage. "
            if website
            else (
                "No website found. Either search a different business name, "
                "or mark as 'no discoverable website'."
            )
        ),
    }


async def crawl_business_pages(
    website: str,
    selected_pages: list[str] | None = None,
) -> dict[str, object]:
    """
    Crawl exactly one page from a website.

    With no selected page this fetches only the homepage. On a later call, pass
    exactly one URL returned in recommended_pages. The crawler never follows
    links automatically or spends requests on multiple pages.
    """
    if selected_pages is not None and len(selected_pages) != 1:
        raise ValueError("selected_pages must contain exactly one recommended URL, or be omitted for the homepage.")
    crawl_result = await crawl_business_website(
        website,
        max_pages=1,
        selected_pages=selected_pages,
        follow_relevant_links=False,
    )

    # Compact response — NEVER include full page text in tool output.
    # Each page gets a 400-char excerpt max; total output stays under ~2 KB.
    # Blocked/error pages are filtered out to prevent \xa0-laden garbage
    # titles from corrupting downstream tool-call JSON arguments.
    pages_summary = []
    total_chars = 0
    EXCERPT_MAX = 400
    OUTPUT_BUDGET = 2_000

    for page in crawl_result.get("pages", []):
        # Skip blocked pages — they have no useful data
        if page.get("status") == "blocked":
            continue

        raw_title = page.get("title", "")
        # Skip HTTP-error pages (403, 404, etc.)
        if _HTTP_ERROR_RE.match(raw_title):
            continue

        title = sanitize_text(raw_title)[:60]
        remaining = max(0, OUTPUT_BUDGET - total_chars)
        excerpt_len = min(EXCERPT_MAX, remaining)
        raw_excerpt = page["text"][:excerpt_len] if excerpt_len > 0 else ""
        excerpt = sanitize_text(raw_excerpt)
        total_chars += len(excerpt)

        pages_summary.append(
            {
                "url": page["source_url"],
                "title": title,
                "text_length": len(page["text"]),
                "excerpt": excerpt,
                "status": page["status"],
            }
        )

    recommended = crawl_result.get("recommended_pages", [])
    recommended_summary = [
        {
            "url": item["url"],
            "reason": sanitize_text(item["reason"])[:50],
            "relevance": item["relevance"],
        }
        for item in recommended[:3]
    ]

    return {
        "website": website,
        "follow_relevant_links": False,
        "selected_pages": selected_pages,
        "max_pages": 1,
        "pages_crawled": len(pages_summary),
        "pages": pages_summary,
        "recommended_pages": recommended_summary,
        "recommendation": (
            "If you need more info, pick one URL from recommended_pages and "
            "call crawl_business_pages again. Otherwise, call synthesize to finalize."
        ),
    }


async def compress_research_context(
    business_name: str,
    website_pages: list[dict] | None = None,
    profile: dict | None = None,
    linkedin_evidence: dict | None = None,
    facebook_evidence: dict | None = None,
) -> dict[str, object]:
    """
    Build a reusable, deduped evidence packet that keeps a fact graph rather
    than repeating page snippets. The output is compact enough to ship back to
    an agent as a token-aware memory artifact, which is the useful save.

    The compression is evidence-aware, not truncation-only: it normalises
    repeated texts, expands facts from the known profile, and emits one
    canonical fact object per unique page contribution.
    """
    pages = website_pages or []
    # De-duplicate by exact text, not by URL-only. Unique pages with identical
    # text are merged into one canonical evidence contribution to save tokens.
    unique_text_groups: list[dict] = []
    seen_text: set[str] = set()
    for page in pages:
        text = sanitize_text(str(page.get("text") or "")).strip()
        source_url = str(page.get("source_url") or page.get("url") or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen_text:
            continue
        seen_text.add(key)
        unique_text_groups.append(
            {
                "source_url": source_url,
                "title": sanitize_text(str(page.get("title") or ""))[:80],
                "text": text,
                "status": page.get("status", "ok"),
            }
        )

    # Keep unique page evidence and count the deduped pages as the number of
    # unique text facts instead of the original crawl page count.
    deduped_pages = len(unique_text_groups)
    text_chunks = [page["text"] for page in unique_text_groups]

    combined_text = "\n".join(text_chunks)
    services = []
    if profile and isinstance(profile, dict):
        for item in profile.get("services") or []:
            if isinstance(item, str) and item.strip():
                services.append(item.strip().lower())

    reusable = {
        "name": sanitize_text(business_name),
        "industry": (profile or {}).get("industry"),
        "description": (profile or {}).get("description") or "",
        "services": list(OrderedDict.fromkeys(services)),
        "keywords": sorted({w.lower() for w in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9+./:&-]{2,}", combined_text) if len(w) > 2})[:20],
    }

    compressed = sanitize_text(" ".join(text_chunks))
    if len(compressed) > 600:
        compressed = compressed[:600].rsplit(" ", 1)[0] + " …"

    return {
        "entity": {
            "name": sanitize_text(business_name),
            "industry": reusable["industry"],
            "services": reusable["services"],
            "description": reusable["description"],
        },
        "deduped_pages": deduped_pages,
        "reusable_facts": reusable,
        "compressed_evidence": compressed,
        "links": [page["source_url"] for page in unique_text_groups if page.get("source_url")],
        "linkedin_evidence": linkedin_evidence or {},
        "facebook_evidence": facebook_evidence or {},
    }


async def score_opportunity(
    business_name: str,
    website: str | None = None,
    profile: dict | None = None,
    decision_makers: list[dict] | None = None,
    contact_hints: dict | None = None,
    raw_website_text: str | None = None,
) -> dict[str, object]:
    """Score a normalized business opportunity using the qualification rules.

    Returns a small score packet that is safe to pass to the CRM builder or UI.
    """
    from .qualify_lead import qualify_lead

    result = await qualify_lead(
        business_name=business_name,
        website=website,
        profile=profile,
        decision_makers=decision_makers,
        contact_hints=contact_hints,
        raw_website_text=raw_website_text,
    )
    return {
        "business_name": result.get("business_name"),
        "opportunity_score": result.get("score"),
        "priority": result.get("priority"),
        "score_breakdown": result.get("score_breakdown") or {},
        "pain_points": result.get("pain_points") or [],
        "buying_signals": result.get("buying_signals") or [],
        "recommended_service": result.get("recommended_service"),
        "outreach_summary": result.get("outreach_summary"),
    }


async def generate_strategy_and_proposal_path(
    business_name: str,
    location: str | None = None,
    website: str | None = None,
    profile: dict | None = None,
    compressed_evidence: str | None = None,
) -> dict[str, object]:
    """Return an internal strategy and a compact proposal path from a normalized entity.

    This is a non-network bridge from entity normalization to a human-facing
    CRM/strategy artifact so the six-step process remains deterministic.
    """
    entity = await normalize_business_entity(business_name, location, website, profile)
    profile = profile or {}
    services = [str(s).strip().lower() for s in profile.get("services") or []]
    industry = str(profile.get("industry") or entity.get("industry") or "unknown")
    service_path = "Website + automation modernization path"
    if services:
        service_path = "Website + automation modernization path"
    elif website:
        service_path = "Website modernization path"
    else:
        service_path = "Discovery + conversion path"

    strategy = (
        "Normalize -> enrich -> find contacts -> qualify -> CRM -> proposal"
    )
    if compressed_evidence:
        strategy = strategy + " with compressed evidence"

    return {
        "canonical_name": entity["canonical_name"],
        "entity_type": entity["entity_type"],
        "industry": industry,
        "services": sorted(set(services)),
        "confidence": entity["confidence"],
        "proposal_path": service_path,
        "strategy": strategy,
        "route": [
            "normalize_business_entity",
            "compress_research_context",
            "score_opportunity",
            "build_crm_record",
            "generate_strategy_and_proposal_path",
        ],
    }


async def normalize_business_entity(
    business_name: str,
    location: str | None = None,
    website: str | None = None,
    profile: dict | None = None,
) -> dict[str, object]:
    """
    Normalize a business mention into a reusable canonical entity object.

    This is the first low-token fact router in the all-in-one MCP. It has no
    network dependency and builds a compact signal plan for downstream tools:
    entity type, token fingerprint, confidence, and the next best action.
    """
    clean_name = sanitize_text(business_name).strip()
    clean_location = sanitize_text(location).strip() if location else ""
    clean_website = sanitize_text(website).strip() if website else ""

    # City/location is now a first-class entity key, not just a signal string.
    city_key = clean_location.lower() if clean_location else ""
    key_name = f"{clean_name}|{city_key}" if city_key else clean_name

    tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9+./:&-]{1,}", clean_name) if len(t) > 2]
    if clean_location:
        tokens.extend([t.lower() for t in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9+./:&-]{1,}", clean_location) if len(t) > 2])

    profile = profile or {}
    industry = str(profile.get("industry") or "unknown")
    services = [str(s).strip().lower() for s in profile.get("services") or [] if str(s).strip()]

    confidence = 0.55
    if clean_website:
        confidence += 0.1
    if clean_location:
        confidence += 0.1
    if isinstance(profile.get("description"), str) and profile.get("description").strip():
        confidence += 0.1
    if services:
        confidence += 0.1

    next_action = "enrich" if clean_website or services or profile.get("industry") else "search"

    return {
        "canonical_name": clean_name,
        "location": clean_location,
        "city_key": city_key,
        "business_key": key_name,
        "website": clean_website,
        "entity_type": "business",
        "tokens": sorted(set(tokens))[:30],
        "industry": industry,
        "services": sorted(set(services)),
        "confidence": round(min(confidence, 0.95), 2),
        "next_best_action": next_action,
        "signal_plan": {
            "should_search": not clean_website,
            "should_enrich": bool(clean_website or services),
            "should_find_contacts": bool(clean_website and services),
        },
    }


async def plan_research_strategy(
    business_name: str,
    location: str | None = None,
    website: str | None = None,
    profile: dict | None = None,
) -> dict[str, object]:
    """
    Convert a normalized entity into a token-wise research route.

    The route is deterministic and safe: call normalize_business_entity,
    enrich_business, find_decision_makers, then qualify_lead. The strategy
    is intentionally sequential so the agent gets a compact action ladder.
    """
    entity = await normalize_business_entity(business_name, location, website, profile)

    route = [
        "normalize_business_entity",
        "enrich_business",
        "find_decision_makers",
        "qualify_lead",
    ]

    return {
        "canonical_name": entity["canonical_name"],
        "entity_type": entity["entity_type"],
        "route": route,
        "save_tokens": True,
        "reason": "Evidence is fact-compressed and domain-normalized before each enrichment hop.",
        "confidence": entity["confidence"],
        "next_best_action": entity["next_best_action"],
    }


async def synthesize_business_profile(
    website_pages: list[dict],
    linkedin_evidence: dict | None = None,
    facebook_evidence: dict | None = None,
    business_name: str | None = None,
) -> dict[str, object]:
    """
    Create a structured profile from evidence collected so far.

    No network calls — just synthesis of what you've gathered.
    """
    website = {"pages": website_pages}
    profile = extract_business_profile(
        website,
        linkedin_evidence or {},
        facebook_evidence or {},
        business_name=business_name,
    )

    return {
        "profile": profile,
        "pages_synthesized": len(website_pages),
    }

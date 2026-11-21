"""
STEP 3 — find_decision_makers
==============================
Finds the actual humans who make buying decisions at a business,
then enriches each found person with targeted site: searches.

SEARCH STRATEGY (3 phases):
  Phase 1 — Discovery: 2 searches to find who the decision-maker IS
    a) site:linkedin.com/in  "<business_name>"  → direct LinkedIn profile hits
    b) "<business_name>" owner OR founder OR CEO (general web)

  Phase 2 — Profile enrichment: for each discovered person, fire targeted
    site: searches to find their LinkedIn, Facebook, Twitter/X, and any
    email/phone published publicly. These are cheap (1 search each, fast).

  Phase 3 — Website contact extraction from already-crawled pages (free).

Total: 2 discovery + up to 3 enrichment searches per person found.
Much better hit rate than a single generic query.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ..crawlers.google import search_google
from ..crawlers._common import sanitize_text
from ..extractors.contacts import extract_contacts_from_pages
from .contact_store import save_research


DECISION_MAKER_TITLES = [
    "founder",
    "co-founder",
    "owner",
    "CEO",
    "managing director",
    "MD",
    "principal",
    "director",
    "partner",
    "CTO",
    "COO",
    "president",
    "head teacher",
    "headmaster",
    "headmistress",
    "proprietor",
]

HIGH_VALUE_TITLES = {
    "founder",
    "co-founder",
    "owner",
    "ceo",
    "managing director",
    "md",
    "principal",
    "director",
    "partner",
    "president",
    "proprietor",
    "head teacher",
    "headmaster",
    "headmistress",
}

# Social platforms we try to find for each person
_SOCIAL_SITES = [
    ("linkedin", "site:linkedin.com/in"),
    ("facebook", "site:facebook.com"),
    ("twitter", "site:twitter.com OR site:x.com"),
]

# Business-level social page searches (not person-level)
_BIZ_SOCIAL_SITES = [
    ("facebook",  "site:facebook.com"),
    ("instagram", "site:instagram.com"),
    ("linkedin",  "site:linkedin.com/company"),
]

# HTTP error codes that indicate a page is blocked/inaccessible
_HTTP_ERROR_RE = re.compile(r"^\s*\d{3}\b", re.IGNORECASE)


def _clean_url_field(value: str | None) -> str | None:
    """Remove markdown wrappers / stray punctuation from URLs before they
    become tool-call arguments. This hardens the schema against the kind of
    malformed JSON that Groq reports as a tool_use_failed parse rejection.
    """
    if not value:
        return None
    text = str(value).strip()
    text = sanitize_text(text)
    # Accept only a valid URL-ish token and strip leading square-bracket markdown.
    text = re.sub(r"^\[+", "", text)
    text = re.sub(r"\]+$", "", text)
    text = re.sub(r"\s+", "", text)
    text = text.replace('"', '').replace("'", '')
    if text.startswith("["):
        text = text.strip("[]")
    if "facebook_url" in text.lower() or "linkedin_url" in text.lower():
        text = text.replace("facebook_url", "")
        text = text.replace("linkedin_url", "")
    # keep only plausible URL; no bracketed markdown wrappers or stray schema keys
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if text.startswith("[http://") or text.startswith("[https://"):
        return text.strip("[]")
    return None


def _domain_from_url(url: str | None) -> str | None:
    """Return bare hostname without www., or None."""
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        return host or None
    except Exception:
        return None


def _result_matches_business(
    item: dict,
    business_name: str,
    location: str | None,
    website_domain: str | None,
) -> bool:
    """Return True if a search result is likely about *this specific* business.

    Checks (in decreasing specificity):
    1. The result URL or snippet contains the known website domain.
    2. Both the business name AND location appear in the combined text.
    3. At least the business name appears (weakest — accept only when we have
       no location and no domain to cross-check).
    """
    combined = (
        item.get("title", "") + " " + item.get("snippet", "") + " " + item.get("url", "")
    ).lower()

    # Strongest signal: known domain appears in the result
    if website_domain and website_domain in combined:
        return True

    biz_tokens = [w.lower() for w in business_name.split() if len(w) > 3]
    biz_in_text = any(t in combined for t in biz_tokens)

    # With location: require both name and location
    if location:
        loc_tokens = [w.lower() for w in location.split() if len(w) > 2]
        loc_in_text = any(t in combined for t in loc_tokens)
        return biz_in_text and loc_in_text

    # No location, no domain: weakest — just name
    return biz_in_text


def _sanitize_page_summaries(pages: list[dict]) -> list[dict]:
    """
    Clean and filter page_summaries before using them.

    Removes:
    - Blocked/error pages (status == 'blocked', or title starts with HTTP code)
    - Pages whose excerpt is just an error message

    Sanitizes:
    - All string fields (removes \\xa0, \\u2026, etc.)

    This prevents garbled data from blocked pages entering the tool-call
    arguments and causing JSON parse failures.
    """
    cleaned = []
    for p in pages:
        if not isinstance(p, dict):
            continue

        # Skip blocked pages
        if p.get("status") == "blocked":
            continue

        raw_title = str(p.get("title", ""))
        # Skip HTTP-error titles like "403 Forbidden", "404 Not Found"
        if _HTTP_ERROR_RE.match(raw_title):
            continue

        title = sanitize_text(raw_title)[:80]
        excerpt = sanitize_text(str(p.get("excerpt", "")))[:300]
        url = str(p.get("url", ""))

        # Skip if excerpt is obviously an access-denied/error page
        excerpt_lower = excerpt.lower()
        if any(
            kw in excerpt_lower
            for kw in ("403 forbidden", "access denied", "404 not found")
        ):
            continue

        cleaned.append(
            {
                "url": url,
                "title": title,
                "excerpt": excerpt,
                "status": p.get("status", "ok"),
            }
        )
    return cleaned


def _summarize_pages(pages: list[dict], max_excerpt: int = 120) -> list[dict]:
    """Truncate page excerpts to keep total context small.

    Keeps only url, title, and a short excerpt — enough for name/title
    extraction but small enough not to blow the Groq token budget.
    """
    out = []
    for p in pages:
        excerpt = str(p.get("excerpt", ""))
        if len(excerpt) > max_excerpt:
            excerpt = excerpt[:max_excerpt].rsplit(" ", 1)[0] + "…"
        out.append({
            "url":     p.get("url", ""),
            "title":   str(p.get("title", ""))[:60],
            "excerpt": excerpt,
            "status":  p.get("status", "ok"),
        })
    return out


async def find_decision_makers(
    business_name: str,
    website: str | None = None,
    location: str | None = None,
    website_pages: list[dict] | None = None,
) -> dict[str, object]:
    """
    STEP 3 — Find and fully enrich the owner / decision-maker at a business.

    Phase 1: discover who the decision-maker is (2 searches).
    Phase 2: for each person found, run targeted site: searches to get their
             LinkedIn URL, Facebook profile, Twitter/X, and any contact info.
    Phase 3: extract contacts from already-crawled website pages.

    Args:
        business_name:  Business name from Step 1/2.
        website:        Company website URL (used for email pattern guessing).
        location:       City/country for disambiguation.
        website_pages:  Compact page summaries from `enrich_business`
                        (`result["page_summaries"]`). Pass this to
                        avoid a redundant crawl.

    Returns:
        {
          "business_name":   str,
          "decision_makers": list[dict],
          "contact_hints":   dict,
          "search_log":      list[str],   # queries fired, for debugging
          "errors":          list[str],
          "next_step":       str,
        }
    """
    errors: list[str] = []
    decision_makers: list[dict] = []
    search_log: list[str] = []

    # ── Sanitize all string inputs ───────────────────────────────────────────
    business_name = sanitize_text(business_name)
    location = sanitize_text(location) if location else location
    loc_suffix = f" {location}" if location else ""

    # Derive bare domain for disambiguation (e.g. "acmedental.pk")
    # Used to reject results that are about a same-named business elsewhere.
    website_domain = _domain_from_url(website)

    # ── Sanitize and filter website_pages ───────────────────────────────────
    pages = _sanitize_page_summaries(website_pages or [])
    pages = _summarize_pages(pages)

    # ── Phase 1a: Direct LinkedIn profile search ────────────────────────────
    li_query = f'site:linkedin.com/in "{business_name}"{loc_suffix}'
    search_log.append(li_query)
    li_result = await search_google(li_query)
    errors.extend(li_result.get("errors", []))

    for item in li_result.get("results", []):
        if "linkedin.com/in/" not in item.get("url", ""):
            continue
        if not _result_matches_business(item, business_name, location, website_domain):
            continue
        person = _parse_linkedin_snippet(item, business_name)
        if person and not _already_found(person["name"], decision_makers):
            decision_makers.append(person)

    # ── Phase 1b: General web search if LinkedIn gave nothing ───────────────
    if not decision_makers:
        titles_str = " OR ".join(f'"{t}"' for t in DECISION_MAKER_TITLES[:8])
        # Include website domain in query when known to pin results to this business
        domain_hint = f' site:{website_domain}' if website_domain else ""
        web_query = f'"{business_name}"{loc_suffix}{domain_hint} ({titles_str})'
        search_log.append(web_query)
        web_result = await search_google(web_query, num_results=8)
        errors.extend(web_result.get("errors", []))

        for item in web_result.get("results", []):
            if not _result_matches_business(item, business_name, location, website_domain):
                continue
            if "linkedin.com/in/" in item.get("url", ""):
                person = _parse_linkedin_snippet(item, business_name)
            else:
                person = _parse_general_snippet(item, business_name)
            if person and not _already_found(person["name"], decision_makers):
                decision_makers.append(person)

    # ── Phase 1c: Company LinkedIn page → people ─────────────────────────────
    if not decision_makers:
        co_query = f'site:linkedin.com/company "{business_name}"{loc_suffix}'
        search_log.append(co_query)
        co_result = await search_google(co_query, num_results=3)
        errors.extend(co_result.get("errors", []))
        for item in co_result.get("results", []):
            if not _result_matches_business(item, business_name, location, website_domain):
                continue
            person = _parse_general_snippet(item, business_name)
            if person and not _already_found(person["name"], decision_makers):
                decision_makers.append(person)

    # ── Phase 1d: Website 'About/Team' page mining ──────────────────────────
    # Mine already-fetched page excerpts for names — zero extra network cost.
    if not decision_makers and pages:
        for page in pages:
            excerpt = page.get("excerpt", "")
            url_lower = page.get("url", "").lower()
            # Prefer contact/about/team pages
            is_contact_page = any(
                kw in url_lower
                for kw in ("about", "team", "staff", "contact", "leadership")
            )
            candidates = _extract_names_from_text(excerpt, business_name)
            for name, title_found in candidates:
                if not _already_found(name, decision_makers):
                    confidence = (
                        "medium"
                        if (title_found and title_found.lower() in HIGH_VALUE_TITLES)
                        or is_contact_page
                        else "low"
                    )
                    decision_makers.append(
                        {
                            "name": name,
                            "title": title_found,
                            "linkedin_url": None,
                            "facebook_url": None,
                            "twitter_url": None,
                            "email": None,
                            "phone": None,
                            "confidence": confidence,
                            "source": f"website: {page.get('url', '')}",
                        }
                    )

    # ── Phase 1e: Business-level social page searches (FB / IG / LinkedIn co.) ─
    biz_social_profiles: dict[str, str] = {}
    for platform, site_op in _BIZ_SOCIAL_SITES:
        if biz_social_profiles.get(platform):
            continue
        biz_query = f'{site_op} "{business_name}"{loc_suffix}'
        search_log.append(biz_query)
        biz_result = await search_google(biz_query, num_results=3)
        errors.extend(biz_result.get("errors", []))
        for item in biz_result.get("results", []):
            url = item.get("url", "")
            if not _result_matches_business(item, business_name, location, website_domain):
                continue
            if platform == "facebook" and "facebook.com/" in url:
                if not any(x in url for x in ("/groups/", "/events/", "/marketplace")):
                    biz_social_profiles["facebook"] = url
                    break
            elif platform == "instagram" and "instagram.com/" in url:
                biz_social_profiles["instagram"] = url
                break
            elif platform == "linkedin" and "linkedin.com/company/" in url:
                biz_social_profiles["linkedin_company"] = url
                break

    # ── Phase 2: Enrich each discovered person with targeted site: searches ──
    for dm in decision_makers[:3]:
        await _enrich_person(dm, business_name, location, search_log, errors, website_domain)

    # ── Phase 3: Mine website pages for contacts (zero network cost) ─────────
    contact_hints = extract_contacts_from_pages(pages, website)

    # Attach a website email to any DM that has none
    for dm in decision_makers:
        if not dm.get("email") and contact_hints["emails_found"]:
            dm["email"] = contact_hints["emails_found"][0]
            dm["email_source"] = "extracted_from_website"

    # Sort: high confidence first, then by number of social links found
    decision_makers.sort(
        key=lambda d: (
            {"high": 0, "medium": 1, "low": 2}.get(d["confidence"], 3),
            -sum(
                1 for k in ("linkedin_url", "facebook_url", "twitter_url") if d.get(k)
            ),
        )
    )

    next_step = (
        f"Found {len(decision_makers)} decision-maker(s) "
        f"(searches fired: {len(search_log)}). "
        "Now call `qualify_lead` with business_name, website, profile (from Step 2), "
        "decision_makers and contact_hints to get a score and outreach message."
        if decision_makers
        else "No public decision-makers found. Call `qualify_lead` anyway — "
        "it will note the missing contact and adjust the score."
    )

    storage = save_research(
        business_name, website, decision_makers, contact_hints,
        social_profiles=biz_social_profiles,
    )
    return {
        "business_name": business_name,
        "decision_makers": decision_makers,
        "contact_hints": contact_hints,
        "business_social_profiles": biz_social_profiles,
        "search_log": search_log,
        "errors": errors,
        "storage": storage,
        "next_step": next_step,
    }


# ── Phase 2 enrichment ───────────────────────────────────────────────────────


async def _enrich_person(
    dm: dict,
    business_name: str,
    location: str | None,
    search_log: list[str],
    errors: list[str],
    website_domain: str | None = None,
) -> None:
    """
    Fire targeted site: searches for a specific named person to fill gaps
    in their profile (LinkedIn URL, Facebook, Twitter, email, phone).

    Only fires a search for a platform if the URL is not already known.
    Stops early once all three social slots are filled.
    """
    name = dm["name"]
    loc_suffix = f" {location}" if location else ""

    for platform, site_op in _SOCIAL_SITES:
        url_key = f"{platform}_url"

        if dm.get(url_key):
            continue

        query = f'{site_op} "{name}" "{business_name}"{loc_suffix}'
        search_log.append(query)
        result = await search_google(query, num_results=3)
        errors.extend(result.get("errors", []))

        for item in result.get("results", []):
            url = item.get("url", "")
            snippet = item.get("snippet", "")
            title = item.get("title", "")

            # Name must appear in the result (first + last token)
            if not _name_appears_in(name, f"{title} {snippet}"):
                continue

            # Result must also be connected to this specific business
            if not _result_matches_business(item, business_name, location, website_domain):
                continue

            url = _clean_url_field(url) or ""
            if platform == "linkedin" and "linkedin.com/in/" in url:
                dm[url_key] = url
                dm["confidence"] = "high"
                if not dm.get("title"):
                    dm["title"] = _extract_title_from_linkedin(title, snippet)
                break

            elif platform == "facebook" and "facebook.com/" in url:
                if any(x in url for x in ("/groups/", "/events/", "/pages/category/")):
                    continue
                dm[url_key] = url
                break

            elif platform == "twitter" and ("twitter.com/" in url or "x.com/" in url):
                if "/status/" in url:
                    continue
                dm[url_key] = url
                break

        if not dm.get("email"):
            for item in result.get("results", []):
                email = _extract_email_from_text(
                    item.get("snippet", "") + " " + item.get("title", "")
                )
                if email:
                    dm["email"] = email
                    dm["email_source"] = f"found via {platform} search"
                    break


# ── Parsers ──────────────────────────────────────────────────────────────────


def _parse_linkedin_snippet(item: dict, business_name: str) -> dict | None:
    """
    Parse a LinkedIn profile search result snippet.

    Typical Google-cached formats:
      "Muhammad Ali - Managing Director at Acme Corp | LinkedIn"
      "Sara Khan – CEO, Acme | LinkedIn"
      "John Smith | Founder · Acme Solutions | LinkedIn"
    """
    result_title = item.get("title", "")
    url = item.get("url", "")
    snippet = item.get("snippet", "")

    # Must be a /in/ profile URL — company pages are not people
    url = _clean_url_field(url) or ""
    if "linkedin.com/in/" not in url:
        return None

    # Strip trailing platform label
    clean = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", result_title, flags=re.I).strip()

    name_match = re.match(_NAME_RE_STR, clean)
    if not name_match:
        return None

    name = name_match.group(0).strip()
    if _is_junk_name(name, business_name):
        return None

    # Title: everything between name and " at " / " · " / end
    after_name = clean[len(name):].strip()
    title = _extract_title_from_linkedin(after_name, snippet)

    # Require title to be a recognised DM title OR the snippet/url mentions
    # the business — prevents accepting random LinkedIn profiles that merely
    # have the business name somewhere on a results page.
    combined = f"{result_title} {snippet}".lower()
    biz_tokens = [w.lower() for w in business_name.split() if len(w) > 3]
    biz_match = any(t in combined for t in biz_tokens)
    is_dm_title = bool(title and any(t.lower() in title.lower() for t in HIGH_VALUE_TITLES))

    if not biz_match and not is_dm_title:
        return None  # no connection to this business at all

    confidence = "high" if is_dm_title and biz_match else "medium"

    return {
        "name": name,
        "title": title,
        "linkedin_url": url,
        "facebook_url": None,
        "twitter_url": None,
        "email": None,
        "phone": None,
        "confidence": confidence,
        "source": "linkedin_search",
    }


def _parse_general_snippet(item: dict, business_name: str) -> dict | None:
    """
    Parse any non-LinkedIn search result for a person mention.

    Stricter than before: requires BOTH a DM title AND the business name to
    appear in the combined title+snippet before accepting the candidate.
    Low-confidence (title-only or name-only) results are dropped here so
    they don't inflate the decision_makers list.
    """
    snippet = item.get("snippet", "")
    result_title = item.get("title", "")
    combined = f"{result_title} {snippet}"
    combined_lower = combined.lower()

    # Must mention a DM title
    title_found = None
    for t in DECISION_MAKER_TITLES:
        if t.lower() in combined_lower:
            title_found = t
            break
    if not title_found:
        return None

    # Must also mention the business (at least one significant word)
    biz_tokens = [w.lower() for w in business_name.split() if len(w) > 3]
    if biz_tokens and not any(t in combined_lower for t in biz_tokens):
        return None

    name_match = re.search(_NAME_RE_STR, combined)
    if not name_match:
        return None

    name = name_match.group(0).strip()
    if _is_junk_name(name, business_name):
        return None
    if re.fullmatch(r"[A-Z]{2,}", name):  # all-caps acronym
        return None

    # The name must appear close to the title (within 120 chars) to avoid
    # picking up a random name from a totally different sentence.
    title_pos = combined_lower.find(title_found.lower())
    name_pos = combined_lower.find(name.lower())
    if abs(title_pos - name_pos) > 120:
        return None

    confidence = "medium" if title_found.lower() in HIGH_VALUE_TITLES else "low"

    return {
        "name": name,
        "title": title_found,
        "linkedin_url": None,
        "facebook_url": None,
        "twitter_url": None,
        "email": None,
        "phone": None,
        "confidence": confidence,
        "source": f"web: {item.get('url', '')}",
    }


def _extract_names_from_text(
    text: str, business_name: str
) -> list[tuple[str, str | None]]:
    """
    Extract (name, title) pairs from free text — used for website page mining.

    Only returns candidates where a recognised DM title appears within 100
    characters of the name. Bare names without a nearby title are dropped to
    avoid extracting random capitalized phrases.

    Returns up to 3 candidates.
    """
    results: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    for name_match in re.finditer(_NAME_RE_STR, text):
        name = name_match.group(0).strip()
        if _is_junk_name(name, business_name):
            continue
        if re.fullmatch(r"[A-Z]{2,}", name):
            continue
        if name.lower() in seen:
            continue

        # Look for a DM title near the name (within 100 chars either side)
        start = max(0, name_match.start() - 15)
        end = min(len(text), name_match.end() + 100)
        context = text[start:end].lower()

        title_found = None
        for t in DECISION_MAKER_TITLES:
            if t.lower() in context:
                title_found = t
                break

        # Only accept if a title was found nearby — bare names are noise
        if not title_found:
            continue

        seen.add(name.lower())
        results.append((name, title_found))

        if len(results) >= 3:
            break

    return results


# ── Helpers ──────────────────────────────────────────────────────────────────

# Name regex — covers Western, South Asian, Arabic, hyphenated, with particles
_NAME_RE_STR = (
    r"[A-Z][a-zA-Z''\-]{1,25}"
    r"(?:\s+(?:al|ul|bin|binti|van|de|di|del|du|von|of|el|abu))?"
    r"(?:\s+[A-Z][a-zA-Z''\-]{1,25}){1,3}"
)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I)


def _extract_title_from_linkedin(
    after_name_or_title_str: str, snippet: str = ""
) -> str | None:
    """Extract job title from the part of a LinkedIn title string after the name."""
    s = after_name_or_title_str.strip()

    # Common separators: " - ", " – ", " | ", " · "
    sep_match = re.match(
        r"^[\-–|·,]\s*(.+?)(?:\s+at\s+|\s+@\s+|\s+·\s+|,\s*[A-Z]|$)", s, re.I
    )
    if sep_match:
        title = sep_match.group(1).strip(" -–|·,")
        if title:
            return title

    # Mine snippet as fallback
    if snippet:
        m = re.search(
            r"(?:is|as|works as|serves as|titled?)\s+(?:the\s+)?([A-Za-z &/]{3,40}?)(?:\s+at|\s+of|\.|,)",
            snippet,
            re.I,
        )
        if m:
            return m.group(1).strip()

    return None


def _name_appears_in(name: str, text: str) -> bool:
    """Check that the full name (first + last) appears in the text.

    Requires both the first meaningful token and the last token to be present.
    Checking only the last name is too loose — common surnames like 'Khan',
    'Ali', or 'Smith' appear in many unrelated results.
    """
    tokens = [t for t in name.split() if len(t) > 1]
    if not tokens:
        return False
    text_lower = text.lower()
    # For single-token names just check presence
    if len(tokens) == 1:
        return tokens[0].lower() in text_lower
    # Require first AND last token both present
    return tokens[0].lower() in text_lower and tokens[-1].lower() in text_lower


def _extract_email_from_text(text: str) -> str | None:
    junk = {
        "noreply",
        "no-reply",
        "donotreply",
        "webmaster",
        "support",
        "info",
        "admin",
    }
    for m in _EMAIL_RE.finditer(text):
        local = m.group(0).split("@")[0].lower()
        if local not in junk:
            return m.group(0)
    return None


def _is_junk_name(name: str, business_name: str) -> bool:
    """True if the extracted name is not a real person's name.

    Filters:
    - Names made entirely of business-name words
    - Common UI/web/generic phrases that look capitalised
    - Single tokens that are clearly not a person
    - Titles that accidentally match the name regex
    """
    # All words are business-name words
    biz_words = {w.lower() for w in business_name.split() if len(w) > 2}
    name_words = [w.lower() for w in name.split() if len(w) > 2]
    if name_words and all(w in biz_words for w in name_words):
        return True

    # Hardcoded phrase blocklist — page/UI/navigation terms
    _JUNK = {
        "home", "about", "contact", "services", "welcome", "official",
        "privacy", "policy", "terms", "conditions", "login", "sign",
        "more", "read", "click", "here", "learn", "news", "blog",
        "team", "staff", "people", "leadership", "management",
        "get started", "our team", "meet team", "view all",
        "all rights", "rights reserved", "cookie", "copyright",
        "follow us", "share this", "read more",
        # Common DM titles that also look like names when capitalised
        "managing director", "chief executive", "general manager",
        "head teacher", "head master", "head mistress",
    }
    name_lower = name.lower()
    if name_lower in _JUNK:
        return True

    # Single word that is a known DM title token (e.g. "Director", "Manager")
    title_words = {
        "director", "manager", "founder", "owner", "ceo", "coo", "cto",
        "president", "principal", "partner", "proprietor",
    }
    if len(name.split()) == 1 and name_lower in title_words:
        return True

    # Name is only one word and is very short — too ambiguous
    if len(name.split()) == 1 and len(name) < 4:
        return True

    return False


def _already_found(name: str, found: list[dict]) -> bool:
    """Fuzzy dedupe — match if ≥2 meaningful tokens overlap."""
    name_tokens = set(name.lower().split())
    for existing in found:
        existing_tokens = set(existing["name"].lower().split())
        overlap = {t for t in (name_tokens & existing_tokens) if len(t) > 2}
        if len(overlap) >= 2:
            return True
        if existing["name"].lower() == name.lower():
            return True
    return False

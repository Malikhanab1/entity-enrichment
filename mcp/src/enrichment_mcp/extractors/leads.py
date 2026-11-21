"""Parse Google search results into structured lead candidates."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Domains that are directories, not business websites
DIRECTORY_DOMAINS = {
    "yelp.com",
    "yellowpages.com",
    "tripadvisor.com",
    "trustpilot.com",
    "clutch.co",
    "g2.com",
    "facebook.com",
    "linkedin.com",
    "instagram.com",
    "twitter.com",
    "wikipedia.org",
    "google.com",
    "maps.google.com",
    "foursquare.com",
    "zomato.com",
    "justdial.com",
    "sulekha.com",
}

# Words that are page/section labels, not part of a business name
_GENERIC_LABELS = {
    "home",
    "about",
    "about us",
    "welcome",
    "contact",
    "contact us",
    "services",
    "official",
    "official site",
    "official website",
    "index",
    "main",
    "homepage",
    "page",
    "site",
    "website",
    "login",
    "sign in",
    "register",
}

# Strip these suffixes from any candidate name (case-insensitive)
_NOISE_SUFFIXES = re.compile(
    r"\s*[\|•·–—]\s*(linkedin|facebook|instagram|twitter|youtube"
    r"|home|official|official\s+site|official\s+website|homepage"
    r"|contact|about|services|welcome)\s*$",
    re.I,
)

# Characters that signal a title-case proper noun run has ended
_NAME_BREAKERS = re.compile(r"[,;:()\[\]|•·–—»«]")


def extract_leads_from_results(
    results: list[dict],
    require_website: bool = False,
) -> list[dict]:
    """Convert raw search result items into structured lead candidates."""
    leads = []
    seen_domains: set[str] = set()

    for rank, item in enumerate(results, start=1):
        url = item.get("url", "")
        title = item.get("title", "")
        snippet = item.get("snippet", "")

        domain = _root_domain(url)
        if domain in DIRECTORY_DOMAINS:
            continue
        if domain in seen_domains:
            continue
        seen_domains.add(domain)

        website = url if (url and url.startswith("http")) else None

        if require_website and not website:
            continue

        business_name = _extract_business_name(title, snippet, domain)

        leads.append(
            {
                "rank": rank,
                "business_name": business_name or title[:80],
                "website": website,
                "snippet": snippet[:200],
                "source_url": item.get("source_url", url),
            }
        )

    return leads


def _root_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        parts = parsed.netloc.lstrip("www.").split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else parsed.netloc
    except Exception:
        return ""


def _extract_business_name(title: str, snippet: str, domain: str = "") -> str | None:
    """
    Robustly extract a business name from a search result title.

    Strategy (in priority order):
    1. Split on separators ( - | – — : ) and score each part.
    2. Fall back to the domain stem if the title yields nothing useful.
    3. Never return a generic label.
    """
    # Strip trailing noise like "| LinkedIn", "- Official Site"
    cleaned = _NOISE_SUFFIXES.sub("", title).strip()

    candidates: list[str] = []

    for sep in (" - ", " | ", " – ", " — ", " · ", ": "):
        if sep in cleaned:
            parts = [p.strip() for p in cleaned.split(sep)]
            candidates.extend(parts)
            break

    # Also consider the whole cleaned title as a candidate
    candidates.append(cleaned)

    # Score and pick the best candidate
    best: str | None = None
    best_score = -1

    for candidate in candidates:
        if not candidate:
            continue
        score = _name_score(candidate)
        if score > best_score:
            best_score = score
            best = candidate

    # Reject if the winner is a generic label
    if best and best.lower() in _GENERIC_LABELS:
        best = None

    # Last resort: derive from domain stem (e.g. "acmelawfirm" → "Acmelawfirm")
    if not best and domain:
        stem = domain.split(".")[0]
        if stem and stem not in {"www", "mail", "app"}:
            best = stem.replace("-", " ").replace("_", " ").title()

    return best[:80].strip() if best else None


def _name_score(candidate: str) -> int:
    """
    Heuristic score for how likely a string is a business name.

    Higher = more likely to be the real name.
    """
    low = candidate.lower()

    # Hard disqualifiers
    if low in _GENERIC_LABELS:
        return -100
    if len(candidate) < 2:
        return -50

    score = 0

    # Prefer title-cased words (proper nouns)
    words = candidate.split()
    titled = sum(1 for w in words if w and w[0].isupper())
    score += titled * 3

    # Prefer moderate length (business names are rarely 1 word or 10+ words)
    if 2 <= len(words) <= 6:
        score += 10
    elif len(words) == 1:
        score += 3

    # Penalise generic labels appearing inside the string
    for label in _GENERIC_LABELS:
        if low == label:
            score -= 20
        elif low.startswith(label + " ") or low.endswith(" " + label):
            score -= 5

    # Penalise if it reads like a page description (long, has verb-like words)
    if len(candidate) > 60:
        score -= 15
    if re.search(r"\b(is|are|was|were|we|our|your|the best|leading|top)\b", low):
        score -= 8

    return score

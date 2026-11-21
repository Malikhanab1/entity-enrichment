"""Merge website + LinkedIn + Facebook raw data into a unified business profile."""

from __future__ import annotations

import re


def extract_business_profile(
    raw_website: dict,
    raw_linkedin: dict,
    raw_facebook: dict,
    business_name: str | None = None,
) -> dict:
    """Build a structured profile dict from three raw crawler outputs.

    Adds an optional business-name hint that allows the output entity to be
    normalised without pretending the crawler invented a separate identity.
    """
    pages: list[dict] = raw_website.get("pages", [])
    all_text = " ".join(str(p.get("text", "")) for p in pages)

    li_company = raw_linkedin.get("company") or {}
    li_text = li_company.get("text", "") if isinstance(li_company, dict) else ""

    fb_business = raw_facebook.get("business") or {}
    fb_text = fb_business.get("text", "") if isinstance(fb_business, dict) else ""

    combined = f"{all_text} {li_text} {fb_text}"
    description = _extract_description(all_text, li_text, fb_text)
    services = _extract_services(combined)
    address = _extract_address(combined)
    phone = _extract_first(combined, _PHONE_RE, validator=_valid_phone)
    email = _extract_first(combined, _EMAIL_RE, validator=_valid_email)
    employee_count = _extract_first(combined, _EMPLOYEE_RE)
    founded = _extract_founded(combined)
    industry = _extract_industry(combined)

    # Keep the profile clean: anchor the canonical business name only when
    # we are able to observe it as string evidence; avoid inventing names.
    name = _clean_entity_name(business_name) if business_name else None

    return {
        "name": name,
        "description": description,
        "services": services,
        "address": address,
        "phone": phone,
        "email": email,
        "employee_count": employee_count,
        "founded": founded,
        "industry": industry,
    }


# ── Regex patterns ─────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I)
_PHONE_RE = re.compile(
    r"(\+?[\d][\d\s\-().]{6,18}[\d])",
    re.I,
)
_EMPLOYEE_RE = re.compile(
    r"(\d[\d,]*)\s*(?:employees|staff|team members|people)",
    re.I,
)
_FOUNDED_RE = re.compile(
    r"(?:founded|established|est\.?\s*|since\s*)[^\d]{0,10}(\d{4})",
    re.I,
)

# Address: number + street, broad coverage for US/UK/PK/AE formats
_ADDRESS_RE = re.compile(
    r"\d{1,5}[A-Za-z]?\s+[A-Za-z0-9\s,.\-]{4,60}"
    r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|"
    r"Lane|Ln\.?|Drive|Dr\.?|Court|Ct\.?|Floor|Fl\.?|Block|Plot|"
    r"Sector|Phase|DHA|Gulberg|Blue Area|Clifton)",
    re.I,
)

_SERVICE_KEYWORDS = [
    "web design",
    "website development",
    "website",
    "software development",
    "software",
    "app development",
    "mobile app",
    "automation",
    "digital marketing",
    "seo",
    "e-commerce",
    "shopify",
    "crm",
    "erp",
    "accounting",
    "bookkeeping",
    "legal",
    "consulting",
    "logistics",
    "shipping",
    "real estate",
    "property",
    "dental",
    "medical",
    "recruitment",
    "staffing",
    "insurance",
    "construction",
    "manufacturing",
]

# Industry: ordered by specificity — more specific entries first to avoid
# a generic match overriding a precise one (e.g. "medical" before "health")
_INDUSTRY_MAP = [
    ({"solicitor", "barrister", "advocate"}, "Legal"),
    ({"law firm", "law office", "legal services", "attorneys"}, "Legal"),
    ({"attorney", "lawyer"}, "Legal"),
    ({"dental", "dentist", "orthodontic"}, "Healthcare"),
    ({"medical clinic", "health clinic", "hospital"}, "Healthcare"),
    ({"medical", "clinic", "healthcare", "physiotherapy"}, "Healthcare"),
    ({"real estate agency", "property dealer"}, "Real Estate"),
    ({"real estate", "property management", "estate agent"}, "Real Estate"),
    ({"freight forwarding", "3pl", "third-party logistics"}, "Logistics"),
    ({"logistics", "freight", "shipping", "courier"}, "Logistics"),
    ({"recruitment agency", "staffing agency", "headhunting"}, "Staffing"),
    ({"recruitment", "staffing", "hr consultancy"}, "Staffing"),
    ({"restaurant", "cafe", "bakery", "catering"}, "F&B"),
    ({"food", "beverage", "eatery"}, "F&B"),
    ({"hotel", "resort", "hospitality", "travel agency"}, "Hospitality"),
    ({"travel", "tourism", "tours"}, "Hospitality"),
    ({"insurance broker", "insurance agency"}, "Insurance"),
    ({"insurance"}, "Insurance"),
    ({"general contractor", "civil contractor"}, "Construction"),
    ({"construction", "builder", "contractor", "architecture"}, "Construction"),
    ({"manufacturing", "factory", "industrial"}, "Manufacturing"),
    ({"digital marketing agency", "advertising agency"}, "Marketing"),
    ({"marketing", "advertising", "branding", "pr agency"}, "Marketing"),
    ({"chartered accountant", "ca firm", "audit firm"}, "Accounting"),
    ({"accounting", "bookkeeping", "tax consultant", "cpa"}, "Accounting"),
    ({"e-commerce", "online store", "ecommerce"}, "E-Commerce"),
    ({"retail", "supermarket", "departmental store"}, "Retail"),
    (
        {"school", "college", "university", "academy", "institute", "education"},
        "Education",
    ),
    ({"software house", "it company", "tech company", "saas"}, "Technology"),
]

# Sentence-ending noise to skip in description extraction
_SKIP_SENTENCE_RE = re.compile(
    r"^(cookie|we use cookie|©|copyright|\d{1,2}[:/]\d{2}|"
    r"all rights reserved|privacy|terms|subscribe|follow us|"
    r"javascript|enable|loading|skip to|menu|navigation)",
    re.I,
)


# ── Public helpers ─────────────────────────────────────────────────────────


def _extract_description(
    website_text: str, linkedin_text: str, facebook_text: str = ""
) -> str:
    """
    Pick the best single-sentence description from available text sources.

    Preference order: website → LinkedIn → Facebook.
    Skips boilerplate sentences (cookies, copyright, nav labels, etc.)
    """
    candidates = []
    for text in (website_text, linkedin_text, facebook_text):
        if not text:
            continue
        # Split on sentence boundaries — handle both punctuated and unpunctuated text
        sentences = re.split(r"(?<=[.!?])\s+|\n{2,}", text.strip())
        for sent in sentences:
            sent = sent.strip()
            # Length guard: too short = nav label; too long = paragraph blob
            if not (25 <= len(sent) <= 250):
                continue
            if _SKIP_SENTENCE_RE.match(sent):
                continue
            # Must contain at least one real word (not just numbers/symbols)
            if not re.search(r"[A-Za-z]{4,}", sent):
                continue
            # Aggressively remove generic title/noise such as "business" labels.
            sent = re.sub(r"\b(?:business|website|company|organization)\b", "", sent, flags=re.I)
            sent = re.sub(r"\s+", " ", sent).strip(" -–—")
            if sent:
                candidates.append(sent)

    if not candidates:
        return ""

    # Prefer the longest clean one, then dedupe identical sentences.
    return sorted(set(candidates), key=len, reverse=True)[0]


def _clean_entity_name(name: str | None) -> str | None:
    """Normalise a business/entity hint without inventing or overfitting.

    Avoids adding punctuation, malformed entities, or repeated whitespace.
    """
    if not name:
        return None
    clean = re.sub(r"\s+", " ", str(name).strip())
    if len(clean) < 2:
        return None
    return clean.title() if clean.isupper() else clean


def _extract_address(text: str) -> str | None:
    match = _ADDRESS_RE.search(text)
    if match:
        addr = match.group(0).strip()
        # Try to extend to include city/country on the same line
        end = match.end()
        tail = text[end : end + 40]
        city_match = re.match(r"[,\s]+([A-Za-z\s]{3,25})", tail)
        if city_match:
            addr += city_match.group(0)
        return addr.strip()
    return None


def _extract_first(text: str, pattern: re.Pattern, validator=None) -> str | None:
    for match in pattern.finditer(text):
        value = match.group(0).strip()
        if validator and not validator(value):
            continue
        return value
    return None


def _extract_founded(text: str) -> str | None:
    match = _FOUNDED_RE.search(text)
    if match:
        year = match.group(1)
        # Sanity check: year between 1800 and current decade
        if 1800 <= int(year) <= 2030:
            return year
    return None


def _extract_services(text: str) -> list[str]:
    text_lower = text.lower()
    # Longer/more specific keywords score over shorter ones — return deduplicated
    found = []
    seen: set[str] = set()
    for kw in sorted(_SERVICE_KEYWORDS, key=len, reverse=True):
        if kw in text_lower and kw not in seen:
            found.append(kw)
            # Mark sub-strings as seen to avoid e.g. "website" AND "website development"
            for other in _SERVICE_KEYWORDS:
                if other != kw and other in kw:
                    seen.add(other)
            seen.add(kw)
    return found


def _extract_industry(text: str) -> str | None:
    text_lower = text.lower()
    for keywords, industry in _INDUSTRY_MAP:
        if any(kw in text_lower for kw in keywords):
            return industry
    return None


def _valid_email(email: str) -> bool:
    junk_prefixes = {
        "noreply",
        "no-reply",
        "donotreply",
        "webmaster",
        "postmaster",
        "abuse",
        "spam",
        "unsubscribe",
        "support",
    }
    junk_domains = {
        "example.com",
        "sentry.io",
        "w3.org",
        "schema.org",
        "googleapis.com",
        "cloudflare.com",
    }
    local, _, domain = email.partition("@")
    return local.lower() not in junk_prefixes and domain.lower() not in junk_domains


def _valid_phone(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone)
    return 7 <= len(digits) <= 15

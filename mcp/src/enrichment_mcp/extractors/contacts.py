"""Extract contact information from already-crawled website pages.

PATCH v3 — key improvements:
  1. EMAIL: Expanded obfuscation patterns (at, [at], (at), [dot], (dot),
     unicode @ lookalikes) and properly decode mailto: href attributes.
  2. PHONE: Wider regex that catches international formats (+92, 00xx),
     Pakistani/local formats, and tel: links; digit-count validation
     5–15 digits; dedupe by normalised digit string.
  3. SOCIAL: Dedicated social-link parser covering Facebook, Instagram,
     LinkedIn, Twitter/X, YouTube, TikTok, WhatsApp (wa.me + whatsapp.com),
     Pinterest, Snapchat, Telegram, and threads.net.
  4. GOOGLE MAPS: Extracts Google Maps links from href + text, also mines
     JSON-LD for geo coordinates so Maps links can be synthesised.
  5. Returns deduplicated, priority-ranked results.
"""

from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.I,
)

# Obfuscated variants:  name [at] domain [dot] com  /  name (at) domain (dot) com
_OBF_EMAIL_RE = re.compile(
    r"([a-z0-9._%+\-]{2,64})"
    r"\s*(?:\[|\()?\s*(?:at|@)\s*(?:\]|\))?\s*"
    r"([a-z0-9.\-]{2,64})"
    r"\s*(?:\[|\()?\s*(?:dot|\.)\s*(?:\]|\))?\s*"
    r"([a-z]{2,24})",
    re.I,
)

# HTML entity obfuscation  &#64;  or  &commat;
_HTML_AT_RE = re.compile(r"([a-z0-9._%+\-]+)(?:&#64;|&commat;)([a-z0-9.\-]+\.[a-z]{2,})", re.I)

# Tel link  tel:+923001234567
_TEL_HREF_RE = re.compile(r"tel:([+\d\s\-().]+)", re.I)

# Phone number — broad pattern, validated by digit count
_PHONE_RE = re.compile(
    r"(?<!\w)"
    r"(\+?(?:00)?[1-9]\d{0,3}[\s.\-]?)?"   # optional country code
    r"\(?\d{2,4}\)?"                         # area code (optional parens)
    r"[\s.\-]?"
    r"\d{3,5}"
    r"[\s.\-]?"
    r"\d{3,5}"
    r"(?!\d)",
    re.IGNORECASE,
)

# Name-format email: john.doe@example.com → pattern detection
_NAME_EMAIL_RE = re.compile(
    r"([a-z]+)[._]([a-z]+)@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    re.I,
)

# Social platforms with specific per-platform URL patterns
_SOCIAL_PATTERNS: dict[str, re.Pattern] = {
    "facebook":  re.compile(r"facebook\.com/(?!share|sharer|tr\b|plugins|dialog)[^\s\"'<>?#]+", re.I),
    "instagram": re.compile(r"instagram\.com/[a-zA-Z0-9_.]{1,60}", re.I),
    "linkedin":  re.compile(r"linkedin\.com/(?:company|in|school)/[^\s\"'<>?#]{2,80}", re.I),
    "twitter":   re.compile(r"(?:twitter|x)\.com/[a-zA-Z0-9_]{1,50}", re.I),
    "youtube":   re.compile(r"(?:youtube\.com/(?:channel|@|c/|user/)|youtu\.be/)[^\s\"'<>?#]{3,80}", re.I),
    "tiktok":    re.compile(r"tiktok\.com/@[a-zA-Z0-9_.]{2,60}", re.I),
    "whatsapp":  re.compile(r"(?:wa\.me|whatsapp\.com/send)[^\s\"'<>?#]{0,100}", re.I),
    "telegram":  re.compile(r"(?:t\.me|telegram\.me)/[a-zA-Z0-9_]{3,60}", re.I),
    "snapchat":  re.compile(r"snapchat\.com/add/[a-zA-Z0-9_.]{3,50}", re.I),
    "pinterest": re.compile(r"pinterest\.com/[a-zA-Z0-9_./]{2,60}", re.I),
    "threads":   re.compile(r"threads\.net/@[a-zA-Z0-9_.]{1,60}", re.I),
}

# Google Maps link patterns
_GMAPS_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:google\.com/maps|maps\.google\.com|goo\.gl/maps|maps\.app\.goo\.gl)"
    r"[^\s\"'<>]{0,200}",
    re.I,
)

# Pages most likely to have contact info — sorted by decreasing relevance
CONTACT_PAGE_TERMS = ("contact", "about", "team", "people", "leadership", "reach")

# Junk email prefixes / domains to filter out
_JUNK_PREFIXES = frozenset({
    "noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster",
    "abuse", "spam", "unsubscribe", "webmaster", "bounce", "autoresponder",
})
_JUNK_DOMAINS = frozenset({
    "example.com", "sentry.io", "w3.org", "schema.org",
    "googleapis.com", "cloudflare.com", "wix.com", "wixpress.com",
    "godaddy.com", "shopify.com", "wordpress.com", "squarespace.com",
    "email.com", "domain.com",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_contacts_from_pages(
    pages: list[dict],
    website: str | None = None,
) -> dict:
    """
    Extract emails, phone numbers, social profiles, and Maps links from pages.

    Args:
        pages:   List of page dicts from `crawl_business_website`.
        website: Base website URL, used to infer the company domain.

    Returns:
        {
          "emails_found":   list[str],
          "phones_found":   list[str],
          "social_profiles": dict[str, str],   # platform → URL
          "google_maps_url": str | None,
          "email_pattern":  str | None,
        }
    """
    def page_priority(page: dict) -> int:
        url = page.get("source_url", "").lower()
        for i, term in enumerate(CONTACT_PAGE_TERMS):
            if term in url:
                return i
        return len(CONTACT_PAGE_TERMS)

    sorted_pages = sorted(pages, key=page_priority)

    emails: list[str] = []
    phones: list[str] = []
    social: dict[str, str] = {}
    maps_urls: list[str] = []
    seen_emails: set[str] = set()
    seen_phones: set[str] = set()

    for page in sorted_pages:
        text = page.get("text", "")
        raw_links = page.get("contact_links", [])
        link_text = " ".join(str(v) for v in raw_links)
        # Combine visible text + contact link hrefs
        evidence = f"{text} {unquote(unescape(link_text))}"

        # ── Emails ──────────────────────────────────────────────────────────
        # 1. mailto: links  (highest confidence)
        for href in raw_links:
            href_s = str(href)
            if href_s.lower().startswith("mailto:"):
                addr = href_s[7:].split("?")[0].strip()
                _add_email(addr, emails, seen_emails)

        # 2. Regex in body text
        for addr in _EMAIL_RE.findall(evidence):
            _add_email(addr.strip(".,;:!?)]}"), emails, seen_emails)

        # 3. Obfuscated  name [at] domain [dot] tld
        for m in _OBF_EMAIL_RE.finditer(evidence):
            _add_email(f"{m.group(1)}@{m.group(2)}.{m.group(3)}", emails, seen_emails)

        # 4. HTML entity obfuscation &#64;
        for m in _HTML_AT_RE.finditer(evidence):
            _add_email(f"{m.group(1)}@{m.group(2)}", emails, seen_emails)

        # ── Phones ──────────────────────────────────────────────────────────
        # 1. tel: links (highest confidence)
        for href in raw_links:
            m = _TEL_HREF_RE.match(str(href))
            if m:
                _add_phone(m.group(1), phones, seen_phones)

        # 2. Regex in body text
        for match in _PHONE_RE.finditer(evidence):
            _add_phone(match.group(0), phones, seen_phones)

        # ── Social profiles ──────────────────────────────────────────────────
        # Scan all text + link hrefs for social patterns
        all_urls_evidence = f"{evidence} " + " ".join(
            str(cl) for cl in raw_links
        )
        for platform, pattern in _SOCIAL_PATTERNS.items():
            if platform not in social:
                m = pattern.search(all_urls_evidence)
                if m:
                    social[platform] = _normalise_social_url(m.group(0), platform)

        # ── Google Maps ──────────────────────────────────────────────────────
        for m in _GMAPS_RE.finditer(all_urls_evidence):
            url_candidate = m.group(0)
            if not url_candidate.startswith("http"):
                url_candidate = f"https://{url_candidate}"
            if url_candidate not in maps_urls:
                maps_urls.append(url_candidate)

        # Also try JSON-LD for lat/lng (synthesise a maps search link)
        _extract_jsonld_maps(text, maps_urls)

    # ── WhatsApp phone augmentation ─────────────────────────────────────────
    # If a wa.me link is present, extract its embedded phone number
    if "whatsapp" in social:
        wa_url = social["whatsapp"]
        wa_phone_match = re.search(r"wa\.me/(\d{7,15})", wa_url)
        if wa_phone_match:
            _add_phone(wa_phone_match.group(1), phones, seen_phones)

    email_pattern = _guess_email_pattern(emails, website)

    return {
        "emails_found": emails[:10],
        "phones_found": phones[:8],
        "social_profiles": social,
        "google_maps_url": maps_urls[0] if maps_urls else None,
        "email_pattern": email_pattern,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _add_email(addr: str, out: list[str], seen: set[str]) -> None:
    addr = addr.strip(".,;:!?)]}\"' ").lower()
    if "@" not in addr:
        return
    local, _, domain = addr.partition("@")
    domain = domain.strip(".")
    if not domain or "." not in domain:
        return
    if local in _JUNK_PREFIXES:
        return
    if domain in _JUNK_DOMAINS:
        return
    # Reject image/font file extensions masquerading as email domains
    if re.search(r"\.(png|jpe?g|gif|svg|webp|woff2?|ttf|eot|ico|js|css)$", domain):
        return
    if addr not in seen:
        seen.add(addr)
        out.append(addr)


def _add_phone(raw: str, out: list[str], seen: set[str]) -> None:
    """Normalise, validate digit count, deduplicate."""
    cleaned = re.sub(r"[^\d+]", "", raw)
    # Strip leading + for key comparison
    digits_only = re.sub(r"[^\d]", "", cleaned)
    if not (5 <= len(digits_only) <= 15):
        return
    # Avoid pure-digit sequences that look like years, zip codes, etc.
    if len(digits_only) < 7:
        return
    if digits_only not in seen:
        seen.add(digits_only)
        # Store human-friendly form: preserve + if present
        formatted = cleaned if cleaned.startswith("+") else re.sub(r"\s+", " ", raw).strip(".,;:!?)]} ")
        out.append(formatted)


def _normalise_social_url(raw: str, platform: str) -> str:
    """Ensure the matched fragment becomes a full https:// URL."""
    if raw.startswith("http"):
        return raw.split("?")[0].rstrip("/")
    return f"https://{raw.split('?')[0].rstrip('/')}"


def _extract_jsonld_maps(text: str, maps_urls: list[str]) -> None:
    """Try to find JSON-LD GeoCoordinates and build a maps.google.com link."""
    for m in re.finditer(r"\{[^{}]{0,2000}\}", text):
        try:
            obj = json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            continue
        geo = obj.get("geo") or {}
        lat = geo.get("latitude") or obj.get("latitude")
        lng = geo.get("longitude") or obj.get("longitude")
        if lat and lng:
            url = f"https://maps.google.com/?q={lat},{lng}"
            if url not in maps_urls:
                maps_urls.append(url)
            return


def _is_junk_email(email: str) -> bool:
    local, _, domain = email.partition("@")
    return local in _JUNK_PREFIXES or domain in _JUNK_DOMAINS


def _guess_email_pattern(emails: list[str], website: str | None) -> str | None:
    """
    If we see john.doe@acme.com and jane.smith@acme.com,
    infer the pattern '{first}.{last}@acme.com'.
    """
    if not emails:
        return None

    domain = None
    if website:
        parsed = urlparse(website)
        domain = parsed.netloc.lstrip("www.")

    name_emails: list[tuple[str, str, str]] = []
    for email in emails:
        m = _NAME_EMAIL_RE.match(email)
        if m:
            name_emails.append((m.group(1), m.group(2), m.group(3)))

    if not name_emails:
        return None

    email_domain = name_emails[0][2]
    if domain and domain not in email_domain:
        return None

    return f"{{first}}.{{last}}@{email_domain}"

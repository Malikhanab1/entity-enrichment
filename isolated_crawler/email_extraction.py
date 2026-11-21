"""
Email discovery, deduplication, type classification, and primary-email
selection. Extends the original single-email extractor to keep *all*
plausible business emails and rank them.
"""
from __future__ import annotations

import re
import urllib.parse

from bs4 import BeautifulSoup

from config import GENERIC_EMAIL_LOCALPARTS, IGNORED_EMAIL_DOMAINS, PERSONAL_EMAIL_PROVIDERS
from utils import deobfuscate_text

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", re.IGNORECASE
)

BAD_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".js", ".css", ".woff",
    ".woff2", ".ttf", ".ico",
)


def extract_emails(html_text: str, soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Return list of (email, source) tuples found on a single page."""
    valid_emails: list[tuple[str, str]] = []

    for a_tag in soup.find_all("a", href=True):
        href = urllib.parse.unquote(a_tag["href"].lower())
        if href.startswith("mailto:"):
            email = href.replace("mailto:", "").split("?")[0].strip()
            if "@" in email:
                valid_emails.append((email, "mailto: attribute"))

    clean = deobfuscate_text(html_text)
    for email in EMAIL_REGEX.findall(clean):
        valid_emails.append((email.lower(), "text regex"))

    final_emails: list[tuple[str, str]] = []
    seen = set()
    for email, source in valid_emails:
        if "@" not in email:
            continue
        domain = email.split("@")[-1].strip(".")
        if not domain or "." not in domain:
            continue
        if domain in IGNORED_EMAIL_DOMAINS:
            continue
        if email.endswith(BAD_EXTENSIONS):
            continue
        if email in seen:
            continue
        seen.add(email)
        final_emails.append((email, source))

    return final_emails


def dedupe_all_emails(all_found: list[tuple[str, str]]) -> list[str]:
    """Merge emails found across homepage + subpages into a clean unique list."""
    seen = set()
    result = []
    for email, _source in all_found:
        if email not in seen:
            seen.add(email)
            result.append(email)
    return result


def classify_email_type(email: str, site_domain: str) -> str:
    """Classify an email as business / generic / personal / unknown."""
    if not email or "@" not in email:
        return "unknown"
    local, _, domain = email.partition("@")
    domain = domain.lower()

    if domain in PERSONAL_EMAIL_PROVIDERS:
        return "personal"
    if site_domain and domain == site_domain:
        return "business" if local.lower() not in GENERIC_EMAIL_LOCALPARTS else "generic"
    if local.lower() in GENERIC_EMAIL_LOCALPARTS:
        return "generic"
    return "unknown"


def select_primary_email(emails: list[str], site_domain: str) -> tuple[str, str]:
    """
    Choose the best single email using priority:
    1. business-domain, non-generic local part
    2. contact@domain
    3. info@domain
    4. sales@domain
    5. hello@domain
    6. any other valid business-domain email
    Falls back to the first email overall if nothing matches the domain.
    Returns (email, email_type).
    """
    if not emails:
        return "", "unknown"

    same_domain = [e for e in emails if e.split("@")[-1].lower() == site_domain]

    def find_local(prefix: str) -> str | None:
        for e in same_domain:
            if e.split("@")[0].lower() == prefix:
                return e
        return None

    # Priority 1: non-generic business-domain email
    for e in same_domain:
        local = e.split("@")[0].lower()
        if local not in GENERIC_EMAIL_LOCALPARTS:
            return e, classify_email_type(e, site_domain)

    for prefix in ("contact", "info", "sales", "hello"):
        found = find_local(prefix)
        if found:
            return found, classify_email_type(found, site_domain)

    if same_domain:
        e = same_domain[0]
        return e, classify_email_type(e, site_domain)

    # No business-domain email at all: fall back to first valid email found
    e = emails[0]
    return e, classify_email_type(e, site_domain)

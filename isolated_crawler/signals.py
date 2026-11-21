"""
Deterministic, cheap heuristics that flag agency-opportunity signals.
Everything here is derived from data we already fetched (HTML, headers,
response time) -- no extra network calls per signal.
"""
from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup

OUTDATED_MARKERS = [
    "<marquee", "<font ", "table cellpadding", "flash", ".swf",
    "frameset", "<center>",
]

SLOW_THRESHOLD_SECONDS = 3.0


@dataclass
class SiteSignals:
    poor_website: bool = False
    outdated_design: bool = False
    no_booking: bool = False
    no_whatsapp: bool = False
    no_contact_form: bool = False
    weak_cta: bool = False
    no_social_links: bool = False
    missing_phone: bool = False
    missing_email: bool = False
    mobile_issues: bool = False
    slow_website: bool = False
    broken_links: bool = False
    https_missing: bool = False


def detect_website_signals(
    *,
    soup: BeautifulSoup,
    html_text: str,
    url: str,
    response_time: float,
    business_info,
    has_emails: bool,
    booking_relevant: bool,
    whatsapp_relevant: bool,
    broken_link_ratio: float = 0.0,
) -> SiteSignals:
    """Compute boolean opportunity signals from already-available data."""
    sig = SiteSignals()

    sig.https_missing = not url.lower().startswith("https://")

    # Mobile-friendliness: cheap check for a responsive viewport meta tag
    viewport = soup.find("meta", attrs={"name": "viewport"})
    sig.mobile_issues = viewport is None or "width=device-width" not in (
        viewport.get("content", "") if viewport else ""
    )

    html_lower = html_text.lower()
    sig.outdated_design = any(marker in html_lower for marker in OUTDATED_MARKERS)

    sig.no_contact_form = not business_info.has_contact_form
    sig.no_booking = booking_relevant and not business_info.has_booking
    sig.no_whatsapp = whatsapp_relevant and not business_info.has_whatsapp_cta
    sig.no_social_links = len(business_info.social_links) == 0
    sig.missing_phone = not business_info.phone
    sig.missing_email = not has_emails

    sig.weak_cta = (
        not business_info.has_phone_cta
        and not business_info.has_email_cta
        and not business_info.has_whatsapp_cta
        and not business_info.has_booking
    )

    sig.slow_website = response_time > SLOW_THRESHOLD_SECONDS
    sig.broken_links = broken_link_ratio > 0.2  # more than 20% of sampled links dead

    # PoorWebsite: composite heuristic, not a single measurement
    poor_signal_count = sum([
        sig.outdated_design,
        sig.mobile_issues,
        sig.weak_cta,
        sig.no_contact_form,
        sig.https_missing,
    ])
    sig.poor_website = poor_signal_count >= 3

    return sig

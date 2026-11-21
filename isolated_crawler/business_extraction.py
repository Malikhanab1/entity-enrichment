"""
Extracts structured business information from a page: name, description,
phone, address, social links, JSON-LD/schema.org data, and CTA presence
(contact form, booking, WhatsApp, phone, email).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from utils import clean_text

PHONE_REGEX = re.compile(
    r"(\+?\d{1,3}[\s.-]?)?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}"
)

SOCIAL_PATTERNS = {
    "Facebook": re.compile(r"facebook\.com", re.I),
    "Instagram": re.compile(r"instagram\.com", re.I),
    "LinkedIn": re.compile(r"linkedin\.com", re.I),
    "Twitter": re.compile(r"(twitter\.com|x\.com)", re.I),
    "YouTube": re.compile(r"youtube\.com|youtu\.be", re.I),
    "WhatsApp": re.compile(r"wa\.me|whatsapp\.com", re.I),
    "GoogleMaps": re.compile(r"google\.com/maps|goo\.gl/maps", re.I),
}

BOOKING_KEYWORDS = [
    "book now", "book an appointment", "schedule", "reservation",
    "book online", "make a booking", "appointment", "reserve a table",
]

CONTACT_FORM_HINTS = ["contact-form", "wpforms", "gform", "hs-form", "contact_form"]


@dataclass
class BusinessInfo:
    page_title: str = ""
    business_name: str = ""
    description: str = ""
    phone: str = ""
    address: str = ""
    city: str = ""
    country: str = ""
    social_links: dict = field(default_factory=dict)
    has_contact_form: bool = False
    has_booking: bool = False
    has_whatsapp_cta: bool = False
    has_phone_cta: bool = False
    has_email_cta: bool = False
    raw_text: str = ""


def _parse_json_ld(soup: BeautifulSoup) -> dict:
    """Pull the first useful LocalBusiness/Organization schema.org block."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("@type", ""))
            if any(t in item_type for t in ("LocalBusiness", "Organization", "Restaurant", "Store")):
                return item
    return {}


def extract_social_links(soup: BeautifulSoup) -> dict:
    """Scan all hrefs for known social/messaging/maps platforms."""
    links = {}
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        for platform, pattern in SOCIAL_PATTERNS.items():
            if platform not in links and pattern.search(href):
                links[platform] = href
    return links


def detect_ctas(soup: BeautifulSoup, html_text: str, social_links: dict) -> dict:
    """Detect presence of contact form, booking, WhatsApp/phone/email CTAs."""
    text_lower = html_text.lower()

    has_contact_form = bool(soup.find("form")) and any(
        hint in html_text.lower() for hint in CONTACT_FORM_HINTS
    ) or bool(soup.find("form", attrs={"id": re.compile("contact", re.I)})) \
        or bool(soup.find("form", attrs={"class": re.compile("contact", re.I)}))
    # Fallback: any form containing an email/message field counts too
    if not has_contact_form:
        for form in soup.find_all("form"):
            if form.find("input", attrs={"type": "email"}) or form.find("textarea"):
                has_contact_form = True
                break

    has_booking = any(kw in text_lower for kw in BOOKING_KEYWORDS)

    has_whatsapp_cta = "WhatsApp" in social_links or "wa.me" in text_lower
    has_phone_cta = bool(soup.find("a", href=re.compile(r"^tel:", re.I)))
    has_email_cta = bool(soup.find("a", href=re.compile(r"^mailto:", re.I)))

    return {
        "has_contact_form": has_contact_form,
        "has_booking": has_booking,
        "has_whatsapp_cta": has_whatsapp_cta,
        "has_phone_cta": has_phone_cta,
        "has_email_cta": has_email_cta,
    }


def extract_business_info(soup: BeautifulSoup, html_text: str, url: str) -> BusinessInfo:
    """Aggregate all business-identity extraction into one structured object."""
    info = BusinessInfo()

    # Page title
    if soup.title and soup.title.string:
        info.page_title = clean_text(soup.title.string, 200)

    ld = _parse_json_ld(soup)

    # Business name: JSON-LD > og:site_name > <title>
    og_site_name = soup.find("meta", property="og:site_name")
    if ld.get("name"):
        info.business_name = clean_text(str(ld["name"]), 150)
    elif og_site_name and og_site_name.get("content"):
        info.business_name = clean_text(og_site_name["content"], 150)
    elif info.page_title:
        # Strip common separators like " | " or " - " to isolate the brand
        info.business_name = clean_text(re.split(r"[|\-–—]", info.page_title)[0], 150)

    # Description: JSON-LD > meta description > og:description
    meta_desc = soup.find("meta", attrs={"name": "description"})
    og_desc = soup.find("meta", property="og:description")
    if ld.get("description"):
        info.description = clean_text(str(ld["description"]), 300)
    elif meta_desc and meta_desc.get("content"):
        info.description = clean_text(meta_desc["content"], 300)
    elif og_desc and og_desc.get("content"):
        info.description = clean_text(og_desc["content"], 300)

    # Phone: JSON-LD telephone > tel: link > regex in text
    tel_link = soup.find("a", href=re.compile(r"^tel:", re.I))
    if ld.get("telephone"):
        info.phone = clean_text(str(ld["telephone"]), 40)
    elif tel_link:
        info.phone = clean_text(tel_link["href"].replace("tel:", ""), 40)
    else:
        match = PHONE_REGEX.search(html_text)
        if match and len(re.sub(r"\D", "", match.group())) >= 7:
            info.phone = clean_text(match.group(), 40)

    # Address: JSON-LD PostalAddress preferred
    address_obj = ld.get("address")
    if isinstance(address_obj, dict):
        parts = [
            address_obj.get("streetAddress", ""),
            address_obj.get("addressLocality", ""),
            address_obj.get("addressRegion", ""),
            address_obj.get("postalCode", ""),
            address_obj.get("addressCountry", ""),
        ]
        info.address = clean_text(", ".join(p for p in parts if p), 300)
        info.city = clean_text(str(address_obj.get("addressLocality", "")), 100)
        info.country = clean_text(str(address_obj.get("addressCountry", "")), 100)
    elif isinstance(address_obj, str):
        info.address = clean_text(address_obj, 300)

    info.social_links = extract_social_links(soup)
    ctas = detect_ctas(soup, html_text, info.social_links)
    info.has_contact_form = ctas["has_contact_form"]
    info.has_booking = ctas["has_booking"]
    info.has_whatsapp_cta = ctas["has_whatsapp_cta"]
    info.has_phone_cta = ctas["has_phone_cta"]
    info.has_email_cta = ctas["has_email_cta"]

    info.raw_text = clean_text(soup.get_text(separator=" ", strip=True), 5000)

    return info

"""
Orchestrates a single website through the full enrichment pipeline:
fetch -> extract business info/emails/tech -> compute signals -> score ->
recommend. Domain-level caching means a domain is only ever crawled once,
regardless of how many input rows reference it.
"""

from __future__ import annotations

import asyncio
import urllib.parse

import aiohttp
from bs4 import BeautifulSoup

from business_extraction import extract_business_info
from config import CONFIG
from crawler import DomainThrottle, check_links_alive, fetch_page, get_crawlable_links
from domain_cache import DomainCache
from email_extraction import (
    classify_email_type,
    dedupe_all_emails,
    extract_emails,
    select_primary_email,
)
from logging_setup import logger
from scoring import (
    calculate_lead_score,
    classify_industry,
    generate_opportunity_reasons,
    is_booking_relevant,
    is_whatsapp_relevant,
    lead_tier,
    personalization_hook,
    recommend_service,
)
from signals import detect_website_signals
from tech_detection import detect_technology
from utils import get_domain, normalize_url

EMPTY_RESULT_FIELDS = [
    "NormalizedWebsite",
    "Domain",
    "HTTPStatus",
    "HTTPS",
    "WebsiteAccessible",
    "PageTitle",
    "BusinessName",
    "Description",
    "Industry",
    "IndustryConfidence",
    "Phone",
    "Address",
    "City",
    "Country",
    "Emails",
    "PrimaryEmail",
    "EmailType",
    "ContactPage",
    "AboutPage",
    "Facebook",
    "Instagram",
    "LinkedIn",
    "Twitter",
    "YouTube",
    "WhatsApp",
    "GoogleMaps",
    "HasContactForm",
    "HasBooking",
    "HasWhatsAppCTA",
    "HasPhoneCTA",
    "HasEmailCTA",
    "Technology",
    "CMS",
    "PoorWebsite",
    "OutdatedDesign",
    "NoBooking",
    "NoWhatsApp",
    "NoContactForm",
    "WeakCTA",
    "NoSocialLinks",
    "MissingPhone",
    "MissingEmail",
    "MobileIssues",
    "SlowWebsite",
    "BrokenLinks",
    "HTTPSMissing",
    "LeadScore",
    "LeadTier",
    "OpportunityReasons",
    "RecommendedService",
    "PersonalizationHook",
]


def _empty_result(raw_url: str, status: int = 0) -> dict:
    result = {field: "" for field in EMPTY_RESULT_FIELDS}
    result["NormalizedWebsite"] = normalize_url(raw_url)
    result["Domain"] = get_domain(result["NormalizedWebsite"])
    result["HTTPStatus"] = status
    result["HTTPS"] = result["NormalizedWebsite"].startswith("https://")
    result["WebsiteAccessible"] = False
    return result


async def crawl_and_enrich_domain(
    url: str,
    session: aiohttp.ClientSession,
    context,
    pw_semaphore,
    throttle: DomainThrottle,
) -> dict:
    """Crawl a single (already-deduplicated) domain and build its full result dict."""
    domain = get_domain(url)
    status, homepage_html, response_time = await fetch_page(
        session, context, pw_semaphore, throttle, url
    )

    if status in (403, 401):
        logger.warning(f"[{url}] Blocked by bot protection (Status: {status}).")
        return _empty_result(url, status)
    if not homepage_html:
        logger.debug(f"[{url}] Failed to fetch homepage (Status: {status}).")
        return _empty_result(url, status)

    soup = BeautifulSoup(homepage_html, "lxml")
    business_info = extract_business_info(soup, homepage_html, url)

    # Emails from homepage
    all_emails_found = extract_emails(homepage_html, soup)

    # Subpage discovery + crawl (contact/about/team/support pages)
    subpages_dict = get_crawlable_links(soup, url, domain)
    contact_page, about_page = "", ""
    for sub_url, reason in subpages_dict.items():
        lower = sub_url.lower()
        if not contact_page and "contact" in lower:
            contact_page = sub_url
        if not about_page and "about" in lower:
            about_page = sub_url

    subpage_semaphore = asyncio.Semaphore(CONFIG.subpage_fetch_concurrency)

    async def fetch_one_subpage(sub_url: str):
        async with subpage_semaphore:
            try:
                return await fetch_page(
                    session, context, pw_semaphore, throttle, sub_url
                )
            except Exception as e:
                logger.debug(f"[{url}] Subpage fetch error ({sub_url}): {e}")
                return 0, "", 0.0

    subpage_results = await asyncio.gather(
        *(fetch_one_subpage(sub_url) for sub_url in subpages_dict.keys()),
        return_exceptions=True,
    )

    for sub_result in subpage_results:
        if isinstance(sub_result, Exception) or sub_result is None:
            continue
        sub_status, sub_html, _ = sub_result
        if sub_status != 200 or not sub_html:
            continue
        try:
            sub_soup = BeautifulSoup(sub_html, "lxml")
            all_emails_found.extend(extract_emails(sub_html, sub_soup))
            # Enrich business_info with subpage social/CTA data if homepage lacked it
            sub_info = extract_business_info(sub_soup, sub_html, "")
            for platform, link in sub_info.social_links.items():
                business_info.social_links.setdefault(platform, link)
            business_info.has_contact_form = (
                business_info.has_contact_form or sub_info.has_contact_form
            )
            business_info.has_booking = (
                business_info.has_booking or sub_info.has_booking
            )
            business_info.has_whatsapp_cta = (
                business_info.has_whatsapp_cta or sub_info.has_whatsapp_cta
            )
            business_info.has_phone_cta = (
                business_info.has_phone_cta or sub_info.has_phone_cta
            )
            business_info.has_email_cta = (
                business_info.has_email_cta or sub_info.has_email_cta
            )
            if not business_info.phone and sub_info.phone:
                business_info.phone = sub_info.phone
            if not business_info.address and sub_info.address:
                business_info.address = sub_info.address
        except Exception as e:
            logger.debug(f"[{url}] Error parsing subpage content: {e}")
            continue

    unique_emails = dedupe_all_emails(all_emails_found)
    primary_email, email_type = select_primary_email(unique_emails, domain)

    # Technology detection (homepage only -- cheap, no extra fetches)
    technology, cms = detect_technology(homepage_html, soup)

    # Industry classification from title + description + visible text
    classification_text = " ".join(
        [
            business_info.page_title,
            business_info.description,
            business_info.raw_text,
        ]
    )
    industry, industry_confidence = classify_industry(classification_text)

    booking_relevant = is_booking_relevant(industry)
    whatsapp_relevant = is_whatsapp_relevant(industry)

    # Broken-link check: cheap sample of discovered internal links
    broken_ratio = await check_links_alive(session, list(subpages_dict.keys()))

    signals = detect_website_signals(
        soup=soup,
        html_text=homepage_html,
        url=url,
        response_time=response_time,
        business_info=business_info,
        has_emails=bool(unique_emails),
        booking_relevant=booking_relevant,
        whatsapp_relevant=whatsapp_relevant,
        broken_link_ratio=broken_ratio,
    )

    has_social_presence = len(business_info.social_links) >= 2
    score, reasons = calculate_lead_score(signals, industry, has_social_presence)
    tier = lead_tier(score)

    result = {
        "NormalizedWebsite": url,
        "Domain": domain,
        "HTTPStatus": status,
        "HTTPS": url.startswith("https://"),
        "WebsiteAccessible": True,
        "PageTitle": business_info.page_title,
        "BusinessName": business_info.business_name,
        "Description": business_info.description,
        "Industry": industry,
        "IndustryConfidence": industry_confidence,
        "Phone": business_info.phone,
        "Address": business_info.address,
        "City": business_info.city,
        "Country": business_info.country,
        "Emails": "; ".join(unique_emails),
        "PrimaryEmail": primary_email,
        "EmailType": email_type,
        "ContactPage": contact_page,
        "AboutPage": about_page,
        "Facebook": business_info.social_links.get("Facebook", ""),
        "Instagram": business_info.social_links.get("Instagram", ""),
        "LinkedIn": business_info.social_links.get("LinkedIn", ""),
        "Twitter": business_info.social_links.get("Twitter", ""),
        "YouTube": business_info.social_links.get("YouTube", ""),
        "WhatsApp": business_info.social_links.get("WhatsApp", ""),
        "GoogleMaps": business_info.social_links.get("GoogleMaps", ""),
        "HasContactForm": business_info.has_contact_form,
        "HasBooking": business_info.has_booking,
        "HasWhatsAppCTA": business_info.has_whatsapp_cta,
        "HasPhoneCTA": business_info.has_phone_cta,
        "HasEmailCTA": business_info.has_email_cta,
        "Technology": technology,
        "CMS": cms,
        "PoorWebsite": signals.poor_website,
        "OutdatedDesign": signals.outdated_design,
        "NoBooking": signals.no_booking,
        "NoWhatsApp": signals.no_whatsapp,
        "NoContactForm": signals.no_contact_form,
        "WeakCTA": signals.weak_cta,
        "NoSocialLinks": signals.no_social_links,
        "MissingPhone": signals.missing_phone,
        "MissingEmail": signals.missing_email,
        "MobileIssues": signals.mobile_issues,
        "SlowWebsite": signals.slow_website,
        "BrokenLinks": signals.broken_links,
        "HTTPSMissing": signals.https_missing,
        "LeadScore": score,
        "LeadTier": tier,
        "OpportunityReasons": generate_opportunity_reasons(reasons),
        "RecommendedService": recommend_service(industry),
        "PersonalizationHook": personalization_hook(signals, industry),
    }
    return result


async def process_website(
    row_index: int,
    raw_url: str,
    session: aiohttp.ClientSession,
    context,
    pw_semaphore,
    throttle: DomainThrottle,
    domain_cache: DomainCache,
) -> dict:
    """
    Process a single input row. Guaranteed not to raise. Uses the domain
    cache so a domain seen in an earlier row is never re-crawled.
    """
    result = {"row_index": row_index}
    try:
        url = normalize_url(raw_url)
        if not url:
            result.update(_empty_result(raw_url))
            return result

        domain = get_domain(url)
        if not domain:
            result.update(_empty_result(raw_url))
            return result

        lock = await domain_cache.get_lock(domain)
        async with lock:
            cached = domain_cache.get(domain)
            if cached is not None:
                logger.debug(f"[{url}] Using cached result for domain {domain}.")
                result.update(cached)
                return result

            try:
                enrichment = await asyncio.wait_for(
                    crawl_and_enrich_domain(
                        url, session, context, pw_semaphore, throttle
                    ),
                    timeout=CONFIG.site_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[{url}] Exceeded hard site timeout of {CONFIG.site_timeout}s. Skipping."
                )
                enrichment = _empty_result(raw_url)
            except asyncio.CancelledError:
                # CancelledError here means either:
                #   (a) wait_for's internal timeout cancelled the coroutine, or
                #   (b) the task itself was cancelled externally (Ctrl+C).
                # Check which: if our task is marked cancelled, propagate.
                # Otherwise treat it like a timeout and return an empty result.
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning(
                    f"[{url}] Site crawl cancelled (likely internal timeout). Skipping."
                )
                enrichment = _empty_result(raw_url)

            domain_cache.set(domain, enrichment)
            result.update(enrichment)
            return result

    except asyncio.CancelledError:
        raise  # propagate Ctrl+C to the shutdown handler in run_crawl
    except Exception as top_level_err:
        logger.error(f"Error processing row {row_index} ({raw_url}): {top_level_err}")
        result.update(_empty_result(raw_url))
        return result

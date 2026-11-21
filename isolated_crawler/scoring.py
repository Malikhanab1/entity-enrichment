"""
Business classification and transparent, deterministic lead scoring.
Every point awarded in calculate_lead_score() is traceable to a specific
signal so scores are explainable rather than a black box.
"""
from __future__ import annotations

from config import (
    BOOKING_RELEVANT_INDUSTRIES,
    HIGH_VALUE_INDUSTRIES,
    INDUSTRY_KEYWORDS,
    WHATSAPP_RELEVANT_INDUSTRIES,
    CONFIG,
)
from signals import SiteSignals

RECOMMENDED_SERVICES = {
    "Dental / Medical": "Website + WhatsApp booking + appointment automation",
    "Real Estate": "Lead capture + WhatsApp AI agent + CRM automation",
    "Restaurant": "Website + WhatsApp ordering/reservation automation",
    "Gym / Fitness": "Landing page + lead capture + automated WhatsApp follow-up",
    "Legal": "Lead capture funnel + intake automation + CRM integration",
    "Beauty / Salon": "Online booking system + WhatsApp appointment reminders",
    "Hotel / Hospitality": "Booking engine + WhatsApp concierge automation",
    "Automotive": "Service booking system + lead capture automation",
    "Construction": "Lead capture landing page + quote-request automation",
    "Education": "Enrollment landing page + lead nurture automation",
    "E-commerce": "Conversion-focused storefront + abandoned-cart automation",
    "SaaS / Software": "Landing page optimization + lead capture automation",
    "Professional Services": "Website modernization + CRM/automation",
    "Other": "Website modernization + CRM/automation",
}


def classify_industry(text: str) -> tuple[str, float]:
    """
    Classify business category from visible text/title/metadata using
    keyword frequency. Returns (industry, confidence 0-1).
    """
    if not text:
        return "Other", 0.0

    text_lower = text.lower()
    scores: dict[str, int] = {}
    for industry, keywords in INDUSTRY_KEYWORDS.items():
        count = sum(text_lower.count(kw) for kw in keywords)
        if count:
            scores[industry] = count

    if not scores:
        return "Other", 0.0

    best_industry = max(scores, key=scores.get)
    best_score = scores[best_industry]
    total_hits = sum(scores.values())

    # Confidence: relative dominance of the winning category, capped at 0.95
    confidence = min(0.95, round(best_score / max(total_hits, 1) * (0.5 + min(best_score, 5) / 10), 2))
    confidence = max(confidence, 0.3) if best_score >= 2 else min(confidence, 0.4)

    return best_industry, confidence


def calculate_lead_score(
    signals: SiteSignals,
    industry: str,
    has_social_presence: bool,
) -> tuple[int, list[str]]:
    """
    Deterministic 0-100 lead score. Returns (score, reasons) where reasons
    is a list of human-readable strings for each point awarded/withheld.
    Points are only awarded when the underlying signal is actually relevant
    to the business (e.g. booking points only apply to booking-relevant
    industries).
    """
    score = 0
    reasons: list[str] = []

    if signals.poor_website or signals.outdated_design:
        score += 20
        reasons.append("Outdated or poor-quality website")

    if signals.no_booking:
        score += 15
        reasons.append("No online booking despite being relevant to this industry")

    if signals.no_whatsapp:
        score += 15
        reasons.append("No WhatsApp CTA despite being relevant to this industry")

    if signals.weak_cta:
        score += 10
        reasons.append("Weak or missing calls-to-action")

    if signals.no_contact_form:
        score += 10
        reasons.append("No contact form")

    if signals.mobile_issues:
        score += 10
        reasons.append("Poor mobile experience (no responsive viewport)")

    if signals.https_missing:
        score += 5
        reasons.append("Website is not served over HTTPS")

    if signals.broken_links:
        score += 5
        reasons.append("Broken links detected")

    if industry in HIGH_VALUE_INDUSTRIES:
        score += 5
        reasons.append(f"High-value service industry ({industry})")

    if has_social_presence:
        score += 5
        reasons.append("Active social/business presence (upsell opportunity)")

    score = min(score, 100)
    return score, reasons


def lead_tier(score: int) -> str:
    if score >= CONFIG.hot_threshold:
        return "HOT"
    if score >= CONFIG.warm_threshold:
        return "WARM"
    if score >= CONFIG.qualified_threshold:
        return "QUALIFIED"
    return "LOW"


def generate_opportunity_reasons(reasons: list[str]) -> str:
    return "; ".join(reasons) if reasons else "No significant gaps detected"


def recommend_service(industry: str) -> str:
    return RECOMMENDED_SERVICES.get(industry, RECOMMENDED_SERVICES["Other"])


def personalization_hook(signals: SiteSignals, industry: str) -> str:
    """Short, factual, non-fabricated sentence explaining the opportunity."""
    if signals.no_booking:
        return "Your website currently has no visible online booking or WhatsApp booking option."
    if signals.no_whatsapp:
        return "Your website does not currently have a visible WhatsApp contact option."
    if signals.no_contact_form:
        return "Your website does not appear to have a contact form for capturing leads."
    if signals.mobile_issues:
        return "Your website does not appear to be optimized for mobile visitors."
    if signals.https_missing:
        return "Your website is not currently served securely over HTTPS."
    if signals.outdated_design:
        return "Your website's design appears outdated compared to current standards."
    return f"Your website could benefit from stronger digital lead capture for a {industry.lower()} business."


def is_booking_relevant(industry: str) -> bool:
    return industry in BOOKING_RELEVANT_INDUSTRIES


def is_whatsapp_relevant(industry: str) -> bool:
    return industry in WHATSAPP_RELEVANT_INDUSTRIES

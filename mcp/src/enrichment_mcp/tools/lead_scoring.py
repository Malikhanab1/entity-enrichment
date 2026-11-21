"""Comprehensive lead scoring pipeline and MCP tool.

Combines signals from website health, tech stack, booking availability,
WhatsApp CTA presence, local Google Maps authority, and industry niche
to generate an explainable 0-100 opportunity score and tier (HOT/WARM/COLD).
"""

from __future__ import annotations

import json
import re
from typing import Any
from pathlib import Path

try:
    from .contact_store import _connection, upsert_business
except ImportError:
    from enrichment_mcp.tools.contact_store import _connection, upsert_business

BOOKING_INDUSTRIES = {
    "dental", "medical", "clinic", "health", "doctor", "salon", "spa", "barber",
    "gym", "fitness", "yoga", "hospitality", "hotel", "restaurant", "dining",
    "legal", "lawyer", "attorney", "automotive", "car repair", "mechanic",
    "consulting", "coaching", "photography", "veterinary", "vet",
}

WHATSAPP_INDUSTRIES = {
    "real estate", "property", "realtor", "ecommerce", "retail", "shop", "store",
    "coaching", "academy", "training", "agency", "travel", "tourism", "logistics",
    "salon", "clinic", "restaurant", "hospitality", "construction", "contractor",
}

HIGH_VALUE_INDUSTRIES = {
    "legal", "law", "medical", "dental", "plastic surgery", "real estate",
    "wealth management", "financial", "saas", "software", "b2b", "agency",
}

RECOMMENDED_SERVICES = {
    "dental / medical": "Modern HIPAA-compliant Website + WhatsApp Appointment Automation",
    "real estate": "Lead Capture Landing Pages + WhatsApp AI Ingestion Agent",
    "salon / spa": "Visual Booking Engine + Automated WhatsApp Reminders",
    "gym / fitness": "High-Converting Membership Funnel + WhatsApp Lead Nurture",
    "hospitality / hotel": "Direct Booking Engine + WhatsApp Concierge Assistant",
    "restaurant": "Digital Menu & WhatsApp Reservation/Ordering Pipeline",
    "automotive": "Service Booking Portal + Immediate Lead Follow-up Automation",
    "legal": "Client Intake Automation + Consultation Scheduling Funnel",
    "ecommerce": "Storefront Speed Optimization + Abandoned Cart WhatsApp Recovery",
    "general": "Responsive Website Modernization + Digital Lead Capture Pipeline",
}


def infer_industry(text: str) -> str:
    """Infer the business industry category from text keywords."""
    t = text.lower()
    for kw, ind in [
        ("dentist", "dental / medical"),
        ("clinic", "dental / medical"),
        ("doctor", "dental / medical"),
        ("medical", "dental / medical"),
        ("realt", "real estate"),
        ("estate", "real estate"),
        ("property", "real estate"),
        ("salon", "salon / spa"),
        ("spa", "salon / spa"),
        ("barber", "salon / spa"),
        ("gym", "gym / fitness"),
        ("fitness", "gym / fitness"),
        ("hotel", "hospitality / hotel"),
        ("resort", "hospitality / hotel"),
        ("restaurant", "restaurant"),
        ("cafe", "restaurant"),
        ("auto", "automotive"),
        ("car", "automotive"),
        ("law", "legal"),
        ("attorney", "legal"),
        ("legal", "legal"),
        ("coaching", "general"),
        ("school", "general"),
        ("store", "ecommerce"),
        ("shop", "ecommerce"),
    ]:
        if kw in t:
            return ind
    return "general"


def calculate_lead_score(business: dict[str, Any], signals: dict[str, Any] | None = None) -> dict[str, Any]:
    """Calculate transparent 0-100 opportunity score with detailed breakdown."""
    score = 0
    reasons: list[dict[str, Any]] = []

    website = str(business.get("website") or "").strip()
    notes = str(business.get("notes") or "")
    phone = str(business.get("phone") or "")
    email = str(business.get("email") or "")
    social_profiles = business.get("social_profiles") or {}
    if isinstance(social_profiles, str):
        try:
            social_profiles = json.loads(social_profiles)
        except Exception:
            social_profiles = {}

    sig = signals or {}
    name = str(business.get("name") or "Business")
    industry = str(business.get("industry") or infer_industry(f"{name} {notes}")).lower()

    # 1. Website Existence and Modernity (up to 25 pts)
    if not website:
        score += 25
        reasons.append({"gap": "No Website", "points": 25, "detail": "Missing online presence; ripe for high-ticket new website build."})
    else:
        if sig.get("poor_website") or sig.get("outdated_design") or "outdated" in notes.lower():
            score += 20
            reasons.append({"gap": "Outdated Website", "points": 20, "detail": "Website appears legacy/outdated; modern redesign candidate."})
        if sig.get("mobile_issues"):
            score += 10
            reasons.append({"gap": "Mobile Optimization Issue", "points": 10, "detail": "Poor mobile responsiveness detected."})
        if website.startswith("http://") and not website.startswith("https://"):
            score += 5
            reasons.append({"gap": "Insecure HTTP", "points": 5, "detail": "Site lacks SSL/HTTPS encryption."})

    # 2. Conversion & Booking Automation (up to 25 pts)
    is_booking = any(k in industry for k in BOOKING_INDUSTRIES) or any(k in name.lower() for k in BOOKING_INDUSTRIES)
    if is_booking:
        if sig.get("no_booking", True) and "booking" not in notes.lower():
            score += 15
            reasons.append({"gap": "No Online Booking", "points": 15, "detail": f"High value in {industry}, but no self-serve booking found."})

    is_wa = any(k in industry for k in WHATSAPP_INDUSTRIES) or is_booking
    has_wa_link = "whatsapp" in social_profiles or "wa.me" in str(social_profiles) or "wa.me" in notes
    if is_wa and not has_wa_link:
        score += 15
        reasons.append({"gap": "No WhatsApp CTA", "points": 15, "detail": "No instant WhatsApp CTA button to capture immediate inquiries."})

    # 3. Contact Completeness & Funnels (up to 20 pts)
    if not email:
        score += 10
        reasons.append({"gap": "No Public Email", "points": 10, "detail": "Opportunity for automated contact/inquiry capture."})
    if sig.get("no_contact_form", False):
        score += 10
        reasons.append({"gap": "Missing Contact Form", "points": 10, "detail": "No structured lead intake form on site."})

    # 4. Authority & Up-sell Viability (up to 20 pts)
    if any(k in industry for k in HIGH_VALUE_INDUSTRIES):
        score += 10
        reasons.append({"gap": "High-Value Niche", "points": 10, "detail": f"Industry '{industry}' has high customer LTV and budget for automation."})

    if social_profiles:
        score += 5
        reasons.append({"gap": "Active Social Presence", "points": 5, "detail": "Business is marketing online and open to modernization."})

    # Local Maps Authority
    if "rating" in notes.lower() or business.get("source") == "maps":
        score += 5
        reasons.append({"gap": "Google Maps Presence", "points": 5, "detail": "Verified local business with active search foot-traffic."})

    final_score = min(100, max(0, score))
    if final_score >= 70:
        tier = "HOT"
    elif final_score >= 40:
        tier = "WARM"
    else:
        tier = "COLD"

    rec_service = RECOMMENDED_SERVICES.get(industry, RECOMMENDED_SERVICES["general"])
    hook = (
        f"We noticed {name} currently has no direct WhatsApp booking integration, "
        f"which typically leaves 30–40% of mobile inquiries uncaptured."
        if not has_wa_link
        else f"Modernizing {name}'s digital infrastructure with streamlined booking and lead nurturing."
    )

    return {
        "business_name": name,
        "opportunity_score": final_score,
        "lead_tier": tier,
        "industry": industry,
        "score_breakdown": reasons,
        "recommended_service": rec_service,
        "personalization_hook": hook,
        "proposal_path": f"{rec_service} -> WhatsApp/Email Outreach -> Demo Call",
    }


async def score_lead(
    business_name: str,
    website: str | None = None,
    city: str | None = None,
    industry: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Score a business opportunity and persist the score and tier to the CRM database."""
    clean_name = str(business_name or "").strip()
    if not clean_name:
        raise ValueError("business_name is required")

    # Fetch existing data from CRM if present
    record: dict[str, Any] = {
        "name": clean_name,
        "website": website,
        "city": city,
        "industry": industry,
        "notes": notes or "",
    }
    with _connection() as conn:
        row = conn.execute(
            "SELECT id, name, website, city, phone, email, notes, social_profiles, detected_tech, source "
            "FROM businesses WHERE name=?",
            (clean_name,),
        ).fetchone()
        if row:
            record = dict(row)
            if website:
                record["website"] = website
            if city:
                record["city"] = city
            if notes:
                record["notes"] = (record.get("notes") or "") + " " + notes

    res = calculate_lead_score(record)

    # Persist score to CRM database
    with _connection() as conn:
        conn.execute(
            """UPDATE businesses SET
               opportunity_score=?,
               lead_tier=?,
               proposal_path=?,
               updated_at=datetime('now')
               WHERE name=?""",
            (res["opportunity_score"], res["lead_tier"], res["proposal_path"], clean_name),
        )

    return res


def score_all_unscored_leads(limit: int = 100) -> dict[str, Any]:
    """Batch score all unpopulated or unscored leads in the unified database."""
    limit = max(1, min(int(limit), 500))
    with _connection() as conn:
        rows = conn.execute(
            "SELECT id, name, website, city, phone, email, notes, social_profiles, source "
            "FROM businesses WHERE opportunity_score IS NULL OR opportunity_score = 0 OR outreach_status = 'unpopulated' "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    scored_count = 0
    tiers: dict[str, int] = {"HOT": 0, "WARM": 0, "COLD": 0}

    for r in rows:
        biz_data = dict(r)
        result = calculate_lead_score(biz_data)
        tier = result["lead_tier"]
        tiers[tier] = tiers.get(tier, 0) + 1

        with _connection() as conn:
            conn.execute(
                """UPDATE businesses SET
                   opportunity_score=?,
                   lead_tier=?,
                   proposal_path=?,
                   updated_at=datetime('now')
                   WHERE id=?""",
                (result["opportunity_score"], tier, result["proposal_path"], biz_data["id"]),
            )
        scored_count += 1

    return {
        "scored_count": scored_count,
        "tier_summary": tiers,
        "limit": limit,
    }

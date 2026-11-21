"""
STEP 4 — qualify_lead
======================
Scores and summarises a business lead for outreach. Uses the data collected
in Steps 1–3 to produce a priority score, a pain-point analysis, and a
ready-to-use outreach summary.

This is the last step. After this, the lead is ready for your CRM or
outreach sequence.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime

from ..crawlers._common import sanitize_text
from .contact_store import create_draft, save_research
from .website_crawler import crawl_website_for_contacts


def _clean_urlish(value: object) -> str | None:
    """Keep URL fields safe and JSON friendly by stripping markdown wrappers.

    This directly guards the class of malformed URL payloads seen in the
    Groq tool-argument parser failure trace.
    """
    if not value:
        return None
    text = str(value).strip()
    text = sanitize_text(text)
    # Remove initial bracket wrappers and markdown-style wrappers.
    text = re.sub(r"^\[+", "", text)
    text = re.sub(r"\]+$", "", text)
    if "http" in text and "](" in text:
        text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\2", text)
    text = re.sub(r"\[+https?://", "https://", text)
    # Strip trailing punctuation or comments that can sit after a URL in text.
    text = re.sub(r"[\]\)\},;:]+$", "", text)
    # Keep the token only if a real URL is present and no raw markdown remains.
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return None


def _clean_profile(profile: dict | None) -> dict:
    """Return a JSON-safe profile object with noisy website text removed."""
    if not isinstance(profile, dict):
        return {}
    out = {}
    for key, value in profile.items():
        if not isinstance(key, str):
            continue
        if key == "description":
            v = sanitize_text(str(value or ""))
            # Drop very long raw crawls or disallowed markdown/link noise
            v = re.sub(r"\[[^\]]+\]\([^\)]+\)", "", v)
            v = re.sub(r"\s+", " ", v)
            out[key] = v[:400]
        elif key in {"services", "keywords"}:
            out[key] = [str(x).strip() for x in value or [] if str(x).strip()][:20]
        elif isinstance(value, str):
            out[key] = sanitize_text(value)[:200]
        else:
            out[key] = value
    return out


def _clean_decision_makers(decision_makers: list[dict] | None) -> list[dict]:
    """Remove malformed markdown/wrapper URLs and keep a pure JSON shape."""
    if not isinstance(decision_makers, list):
        return []
    clean = []
    for dm in decision_makers:
        if not isinstance(dm, dict):
            continue
        person = {
            "name": sanitize_text(str(dm.get("name") or ""))[:80],
            "title": sanitize_text(str(dm.get("title") or ""))[:80],
            "linkedin_url": _clean_urlish(dm.get("linkedin_url")),
            "facebook_url": _clean_urlish(dm.get("facebook_url")),
            "twitter_url": _clean_urlish(dm.get("twitter_url")),
            "email": sanitize_text(str(dm.get("email") or ""))[:120].lower(),
            "phone": sanitize_text(str(dm.get("phone") or ""))[:40],
            "confidence": str(dm.get("confidence") or "low")[:20],
            "source": sanitize_text(str(dm.get("source") or ""))[:80],
        }
        # Avoid feeding broken bracket mis-encoded URLs into the model context.
        for k in ("linkedin_url", "facebook_url", "twitter_url"):
            if person[k] and ("\n" in person[k] or "[https://" in person[k] or "https://www.linkedin.com/in/" not in person[k] and k == "linkedin_url"):
                person[k] = None
        if person["name"]:
            clean.append(person)
    return clean[:20]


def _clean_contact_hints(contact_hints: dict | None) -> dict:
    """Tighten the contact-hints envelope so only safe arrays and maps pass."""
    if not isinstance(contact_hints, dict):
        return {}
    out = {
        "emails_found": [],
        "phones_found": [],
        "social_profiles": {},
        "google_maps_url": _clean_urlish(contact_hints.get("google_maps_url")),
        "email_pattern": sanitize_text(str(contact_hints.get("email_pattern") or ""))[:80] or None,
    }
    for key in ("emails_found", "phones_found"):
        values = contact_hints.get(key) or []
        if isinstance(values, list):
            out[key] = []
            for item in values:
                if isinstance(item, str):
                    safe = sanitize_text(item)
                    if "@" in key or "email" in key:
                        safe = safe.lower()
                    if safe:
                        out[key].append(safe)
    social = contact_hints.get("social_profiles") or {}
    if isinstance(social, dict):
        out["social_profiles"] = {}
        for platform, url in list(social.items()):
            safe = _clean_urlish(url)
            if safe:
                out["social_profiles"][str(platform)] = safe
    return out


# Signals that suggest a business needs website/automation help
WEBSITE_PAIN_SIGNALS = [
    "no website", "coming soon", "under construction", "wix", "godaddy builder",
    "squarespace", "wordpress.com", "blogspot", "weebly", "jimdo",
    "last updated", "outdated", "slow", "not mobile",
]
AUTOMATION_PAIN_SIGNALS = [
    "manual", "spreadsheet", "excel", "paper", "phone only",
    "call us", "fax", "whatsapp only", "no online booking",
    "no online order", "email only",
]
BUYING_SIGNALS = [
    "hiring", "growing", "expansion", "new location", "launched",
    "funded", "investment", "scaling",
]


async def qualify_lead(
    business_name: str,
    website: str | None = None,
    location: str | None = None,
    profile: dict | None = None,
    decision_makers: list[dict] | None = None,
    contact_hints: dict | None = None,
    raw_website_text: str | None = None,
) -> dict[str, object]:
    """
    STEP 4 — Score and summarise a lead for outreach.

    Takes everything collected in Steps 1–3 and produces:
    - A 0–100 priority score
    - A breakdown of why this lead is worth pursuing
    - Pain-point hypotheses (website issues, automation gaps)
    - A short outreach pitch tailored to the business

    Args:
        business_name:      Business name.
        website:            Website URL (or None if none found).
        profile:            Structured profile dict from `enrich_business`.
        decision_makers:    List of decision-maker dicts from `find_decision_makers`.
        contact_hints:      Contact hints dict from `find_decision_makers`.
        raw_website_text:   Combined website page text for signal scanning.
                            Use `" ".join(p["text"] for p in raw_website["pages"])`.

    Returns:
        {
          "business_name": str,
          "score":         int,          # 0–100
          "priority":      "hot" | "warm" | "cold",
          "score_breakdown": {
            "has_website":          int,
            "website_quality":      int,
            "has_decision_maker":   int,
            "has_contact_info":     int,
            "buying_signals":       int,
            "automation_gap":       int,
          },
          "pain_points":   list[str],
          "buying_signals": list[str],
          "best_contact": {
            "name":         str | None,
            "title":        str | None,
            "linkedin_url": str | None,
            "email":        str | None,
          },
          "outreach_summary": str,       # 3–5 sentence pitch
          "recommended_service": str,    # "website" | "automation" | "both"
          "qualified_at": str,           # ISO timestamp
        }
    """
    # Normalize the city/location key at the start of the function so the
    # business-key is preserved and malformed social URLs never escape the tool.
    profile = _clean_profile(profile or {})
    decision_makers = _clean_decision_makers(decision_makers or [])
    contact_hints = _clean_contact_hints(contact_hints or {})
    location = sanitize_text(str(location or "")) if location else None
    # Normalize the business name once for the CRM key and hidden profile.
    business_name = sanitize_text(business_name).strip()
    text_corpus = sanitize_text(raw_website_text or "").lower()

    breakdown: dict[str, int] = {}
    pain_points: list[str] = []
    buying_signals_found: list[str] = []

    # ── Website presence ─────────────────────────────────────────────────────
    if not website:
        breakdown["has_website"] = 0
        pain_points.append("No website found — strong candidate for a new website project.")
    else:
        breakdown["has_website"] = 15

    # ── Website quality signals ──────────────────────────────────────────────
    quality_score = 10
    for signal in WEBSITE_PAIN_SIGNALS:
        if signal in text_corpus:
            quality_score -= 3
            pain_points.append(f"Website quality issue detected: '{signal}'.")
    breakdown["website_quality"] = max(0, quality_score)

    # ── Automation gap ───────────────────────────────────────────────────────
    auto_score = 0
    for signal in AUTOMATION_PAIN_SIGNALS:
        if signal in text_corpus:
            auto_score += 5
            pain_points.append(f"Automation opportunity: uses '{signal}' workflow.")
    breakdown["automation_gap"] = min(20, auto_score)

    # ── Decision-maker found ─────────────────────────────────────────────────
    if decision_makers:
        breakdown["has_decision_maker"] = 20
    else:
        breakdown["has_decision_maker"] = 0
        pain_points.append("No public decision-maker found — may need cold email to generic contact.")

    # ── Contact info ─────────────────────────────────────────────────────────
    emails = contact_hints.get("emails_found", [])
    phones = contact_hints.get("phones_found", [])
    if emails or phones:
        breakdown["has_contact_info"] = 15
    else:
        breakdown["has_contact_info"] = 0

    # ── Buying signals ───────────────────────────────────────────────────────
    buy_score = 0
    for signal in BUYING_SIGNALS:
        if signal in text_corpus:
            buy_score += 5
            buying_signals_found.append(signal)
    breakdown["buying_signals"] = min(20, buy_score)

    # ── Final score ──────────────────────────────────────────────────────────
    score = sum(breakdown.values())
    score = min(100, max(0, score))

    if score >= 65:
        priority = "hot"
    elif score >= 40:
        priority = "warm"
    else:
        priority = "cold"

    # ── Recommended service ──────────────────────────────────────────────────
    has_auto_gap = breakdown["automation_gap"] > 0
    has_web_gap = breakdown["has_website"] == 0 or breakdown["website_quality"] < 5
    if has_auto_gap and has_web_gap:
        recommended_service = "both"
    elif has_auto_gap:
        recommended_service = "automation"
    else:
        recommended_service = "website"

    # ── Best contact ─────────────────────────────────────────────────────────
    best = None
    for dm in decision_makers:
        if dm.get("confidence") == "high":
            best = dm
            break
    if not best and decision_makers:
        best = decision_makers[0]

    best_contact = {
        "name": best["name"] if best else None,
        "title": best.get("title") if best else None,
        "linkedin_url": best.get("linkedin_url") if best else None,
        "email": best.get("email") if best else (emails[0] if emails else None),
    }

    # ── Outreach summary ─────────────────────────────────────────────────────
    outreach_summary = _build_outreach_summary(
        business_name, website, profile, best_contact,
        pain_points, recommended_service,
    )

    # ── Crawl website for contact details + social profiles ─────────────────
    # Do this at qualification time so DB always has fresh contact data even
    # when the caller did not run find_decision_makers first.
    crawl_result: dict = {}
    if website:
        try:
            crawl_result = await crawl_website_for_contacts(website, max_pages=3)
        except Exception:
            pass

    # Merge crawled contact data into contact_hints for scoring / storage
    if crawl_result:
        ch = contact_hints  # mutating a local copy is fine
        for email in crawl_result.get("emails_found", []):
            if email not in ch.get("emails_found", []):
                ch.setdefault("emails_found", []).append(email)
        for phone in crawl_result.get("phones_found", []):
            if phone not in ch.get("phones_found", []):
                ch.setdefault("phones_found", []).append(phone)
        # Merge social_profiles
        existing_social = ch.get("social_profiles") or {}
        crawled_social = crawl_result.get("social_profiles") or {}
        ch["social_profiles"] = {**existing_social, **crawled_social}

    # Re-compute contact score after crawl enrichment
    emails = contact_hints.get("emails_found", [])
    phones = contact_hints.get("phones_found", [])
    if emails or phones:
        breakdown["has_contact_info"] = 15
    score = sum(breakdown.values())
    score = min(100, max(0, score))
    if score >= 65:
        priority = "hot"
    elif score >= 40:
        priority = "warm"
    else:
        priority = "cold"

    # Persist everything to DB
    try:
        save_research(
            business_name=business_name,
            website=website,
            decision_makers=decision_makers,
            contact_hints=contact_hints,
            social_profiles=crawl_result.get("social_profiles"),
        )
    except Exception:
        pass

    # ── Auto-queue email draft when recipient email is known ─────────────────
    # Creates a pending draft automatically so it appears in Email Approvals.
    # The user still has to explicitly approve before anything is sent.
    auto_draft_id: str | None = None
    recipient_email = best_contact.get("email") or (
        contact_hints.get("emails_found") or [None]
    )[0]
    if recipient_email:
        try:
            auto_draft_id = secrets.token_urlsafe(12)
            subject = f"Your digital presence should match your business — {business_name}"
            create_draft(
                draft_id=auto_draft_id,
                business_name=business_name,
                recipient=recipient_email,
                subject=subject,
                body=outreach_summary,
            )
        except Exception:
            auto_draft_id = None

    return {
        "business_name": business_name,
        "score": score,
        "priority": priority,
        "score_breakdown": breakdown,
        "pain_points": pain_points,
        "buying_signals": buying_signals_found,
        "best_contact": best_contact,
        "outreach_summary": outreach_summary,
        "recommended_service": recommended_service,
        "contact_details": {
            "emails": contact_hints.get("emails_found", []),
            "phones": contact_hints.get("phones_found", []),
            "social_profiles": contact_hints.get("social_profiles", {}),
            "google_maps_url": crawl_result.get("google_maps_url"),
        },
        "auto_draft_id": auto_draft_id,
        "auto_draft_note": (
            f"Email draft auto-queued (id: {auto_draft_id}). "
            "It is visible in Email Approvals — edit and approve before sending."
            if auto_draft_id else
            "No email auto-queued (no recipient email found)."
        ),
        "qualified_at": datetime.utcnow().isoformat() + "Z",
    }


def _build_outreach_summary(
    business_name: str,
    website: str | None,
    profile: dict,
    best_contact: dict,
    pain_points: list[str],
    recommended_service: str,
) -> str:
    name_part = f"Hi {best_contact['name'].split()[0]}," if best_contact.get("name") else "Hi,"

    service_line = {
        "website": "a modern, fast website that converts visitors into customers",
        "automation": "custom automation to eliminate the manual work slowing your team down",
        "both": "a new website plus back-office automation to help your team work faster",
    }[recommended_service]

    pain_line = (
        f" I noticed {pain_points[0].lower()}" if pain_points else ""
    )

    website_line = (
        f"I came across {business_name} at {website}" if website
        else f"I was researching {business_name}"
    )

    return (
        f"{name_part} {website_line}.{pain_line} "
        f"We build {service_line} for businesses like yours. "
        f"I'd love to show you a quick demo — would 15 minutes this week work?"
    )

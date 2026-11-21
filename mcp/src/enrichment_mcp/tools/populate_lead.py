"""Strengthened Lead Population Engine for MCP.

Takes unpopulated businesses (from Google Maps, web crawls, or raw imports),
enriches missing websites, extracts contact points & decision makers, runs
explainable scoring, assigns to sales team members, and updates the central CRM.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .contact_store import (
    _connection,
    upsert_business,
    add_business_contact,
    assign_business_lead,
)
from .enrich_business import enrich_business
from .find_decision_makers import find_decision_makers
from .website_crawler import crawl_website_for_contacts
from .lead_scoring import calculate_lead_score

logger = logging.getLogger(__name__)


async def populate_lead(
    business_name: str,
    city_hint: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Populate and enrich an unpopulated business lead in the central CRM.

    1. Checks current CRM record.
    2. Finds website if missing via live search & enrichment.
    3. Crawls website for emails, phones, social channels.
    4. Finds decision makers (Owners, Directors, Founders).
    5. Calculates transparent lead opportunity score and tier.
    6. Automatically allocates the lead to an active sales team member.
    7. Updates CRM database from 'unpopulated' to qualified status.
    """
    clean_name = str(business_name or "").strip()
    if not clean_name:
        raise ValueError("business_name is required")

    # 1. Fetch current CRM record
    with _connection() as conn:
        row = conn.execute("SELECT * FROM businesses WHERE name=?", (clean_name,)).fetchone()
        existing = dict(row) if row else {}

    city = city_hint or existing.get("city")
    website = existing.get("website")
    phone = existing.get("phone")
    email = existing.get("email")
    social_profiles = {}
    if existing.get("social_profiles"):
        try:
            social_profiles = json.loads(existing["social_profiles"])
        except Exception:
            pass

    notes = existing.get("notes") or ""

    # 2. Enrich website and social if missing
    if not website:
        try:
            enrichment = await enrich_business(business_name=clean_name, city=city)
            website = enrichment.get("website") or website
            if enrichment.get("social_profiles"):
                social_profiles.update(enrichment["social_profiles"])
            if enrichment.get("description"):
                notes = f"{notes} | {enrichment['description']}".strip()
        except Exception as exc:
            logger.debug("Live enrichment lookup failed for %s: %s", clean_name, exc)

    # 3. Crawl website for direct contacts if website is known
    contacts_crawled = {"emails_found": [], "phones_found": [], "social_profiles": {}}
    if website:
        try:
            crawl_res = await crawl_website_for_contacts(website_url=website, max_pages=3)
            contacts_crawled = crawl_res
            if crawl_res.get("emails_found") and not email:
                email = crawl_res["emails_found"][0]
            if crawl_res.get("phones_found") and not phone:
                phone = crawl_res["phones_found"][0]
            if crawl_res.get("social_profiles"):
                social_profiles.update(crawl_res["social_profiles"])
        except Exception as exc:
            logger.debug("Website contact crawling failed for %s: %s", website, exc)

    # 4. Search for decision makers
    decision_makers: list[dict[str, Any]] = []
    try:
        dm_res = await find_decision_makers(business_name=clean_name, website=website)
        decision_makers = dm_res.get("decision_makers") or []
    except Exception as exc:
        logger.debug("Decision maker lookup failed for %s: %s", clean_name, exc)

    # 5. Score opportunity and determine tier
    record_for_scoring = {
        "name": clean_name,
        "website": website,
        "phone": phone,
        "email": email,
        "notes": notes,
        "social_profiles": social_profiles,
        "city": city,
    }
    score_data = calculate_lead_score(record_for_scoring)

    # 6. Save decision makers into contacts table
    for dm in decision_makers:
        try:
            add_business_contact(
                business_name=clean_name,
                name=dm.get("name") or "Key Decision Maker",
                title=dm.get("title"),
                linkedin_url=dm.get("linkedin_url"),
                source="mcp_populate",
            )
        except Exception:
            pass

    for em in contacts_crawled.get("emails_found", []):
        try:
            add_business_contact(
                business_name=clean_name,
                name="Inquiry Contact",
                email=em,
                source="website_crawl",
            )
        except Exception:
            pass

    # 7. Update business record in central CRM
    outreach_status = "not_contacted" if existing.get("outreach_status") in ("unpopulated", None) else existing.get("outreach_status")
    biz_id = upsert_business(
        name=clean_name,
        website=website,
        city=city,
        phone=phone,
        email=email,
        notes=notes,
        outreach_status=outreach_status,
        opportunity_score=score_data["opportunity_score"],
        social_profiles=social_profiles,
        source=existing.get("source") or "populated_mcp",
    )

    # Update extra fields
    with _connection() as conn:
        conn.execute(
            """UPDATE businesses SET
               lead_tier=?,
               proposal_path=?,
               updated_at=datetime('now')
               WHERE id=?""",
            (score_data["lead_tier"], score_data["proposal_path"], biz_id),
        )

    # 8. Auto-assign to team member if unassigned
    assignment = {}
    try:
        preferred_channel = "call" if phone else ("mail" if email else "dm")
        assignment = assign_business_lead(clean_name, channel=preferred_channel, notify=True)
    except Exception as exc:
        logger.debug("Auto-assignment failed for %s: %s", clean_name, exc)

    return {
        "business_name": clean_name,
        "status": "populated",
        "website": website,
        "phone": phone,
        "email": email,
        "city": city,
        "opportunity_score": score_data["opportunity_score"],
        "lead_tier": score_data["lead_tier"],
        "recommended_service": score_data["recommended_service"],
        "contacts_stored": len(decision_makers) + len(contacts_crawled.get("emails_found", [])),
        "assigned_to": assignment.get("assigned_name"),
        "assigned_role": assignment.get("assigned_role"),
        "outreach_status": outreach_status,
    }


async def populate_unpopulated_leads(limit: int = 5) -> dict[str, Any]:
    """Batch populate unpopulated leads currently in the central database."""
    limit = max(1, min(int(limit), 20))
    with _connection() as conn:
        rows = conn.execute(
            "SELECT name, city FROM businesses WHERE outreach_status='unpopulated' OR opportunity_score=0 OR opportunity_score IS NULL "
            "ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()

    populated = []
    for r in rows:
        try:
            res = await populate_lead(business_name=r["name"], city_hint=r["city"])
            populated.append(res)
        except Exception as exc:
            logger.error("Failed to populate %s: %s", r["name"], exc)

    return {
        "attempted": len(rows),
        "successfully_populated": len(populated),
        "results": populated,
    }

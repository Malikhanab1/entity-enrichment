"""Central CRM façade for the product UI.

This module is intentionally tiny and stable: it gathers contact-store,
lead-ingestion, and employee persistence into one "final CRM" envelope so
there is no separate employee/lead-ingestion persistence branch.
"""

from __future__ import annotations

from typing import Any

try:
    from enrichment_mcp.tools.contact_store import build_crm_record, save_employee
except Exception:
    build_crm_record = None
    save_employee = None


def crm_record(
    business_name: str,
    website: str | None = None,
    location: str | None = None,
    normalized_entity: dict[str, Any] | None = None,
    compressed_evidence: str | None = None,
    opportunity_score: int | float | None = None,
    proposal_path: str | None = None,
    strategy: str | None = None,
) -> dict[str, Any]:
    """Return the canonical CRM record shape using the workspace contact-store."""
    if build_crm_record is None:
        raise RuntimeError("CRM contact-store bridge is not importable")
    return build_crm_record(
        business_name=business_name,
        website=website,
        location=location,
        normalized_entity=normalized_entity,
        compressed_evidence=compressed_evidence,
        opportunity_score=opportunity_score,
        proposal_path=proposal_path,
        strategy=strategy,
    )


def upsert_employee(
    business_name: str,
    name: str,
    role: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    team: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Save one person/employee record in the same DB as the CRM artifact."""
    if save_employee is None:
        raise RuntimeError("Employee persistence bridge is not importable")
    return save_employee(
        business_name=business_name,
        name=name,
        role=role,
        email=email,
        phone=phone,
        team=team,
        source=source,
    )

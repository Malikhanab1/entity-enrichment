"""Workspace-scoped persistence for researched businesses and email drafts."""

from __future__ import annotations

import os
import sqlite3
import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .workspace_files import WORKSPACE


def build_crm_record(
    business_name: str,
    website: str | None = None,
    location: str | None = None,
    normalized_entity: dict[str, Any] | None = None,
    compressed_evidence: str | None = None,
    opportunity_score: int | float | None = None,
    proposal_path: str | None = None,
    strategy: str | None = None,
) -> dict[str, Any]:
    """Build a compact CRM artifact that carries the normalized entity,
    compressed evidence, scored opportunity, proposal path, and strategy.

    This keeps the six-step sequence lightweight and queryable while staying
    compatible with the existing SQLite-backed contact store.
    """
    entity = normalized_entity or {}
    business_name = str(business_name or entity.get("canonical_name") or "Unknown Business").strip()
    city = str(location or entity.get("location") or entity.get("city_key") or "").strip()
    score = int(opportunity_score) if isinstance(opportunity_score, int) else int(opportunity_score or 0)

    return {
        "business_name": business_name,
        "city": city,
        "business_key": f"{business_name.lower()}|{city.lower()}" if city else business_name.lower(),
        "website": website or entity.get("website"),
        "normalized_entity": {
            "canonical_name": entity.get("canonical_name") or business_name,
            "location": city,
            "city_key": entity.get("city_key") or city,
            "business_key": entity.get("business_key") or f"{business_name.lower()}|{city.lower()}" if city else business_name.lower(),
            "website": entity.get("website") or website,
            "entity_type": entity.get("entity_type") or "business",
            "tokens": entity.get("tokens") or [],
            "industry": entity.get("industry"),
            "services": entity.get("services") or [],
            "confidence": entity.get("confidence") or 0.0,
            "next_best_action": entity.get("next_best_action") or "enrich",
        },
        "compressed_evidence": compressed_evidence or "",
        "opportunity_score": max(0, min(100, score)),
        "proposal_path": proposal_path or "Website + automation modernization path",
        "strategy": strategy or "Normalize -> enrich -> find contacts -> qualify -> CRM",
        "entity_type": entity.get("entity_type") or "business",
        "industry": entity.get("industry"),
        "services": entity.get("services") or [],
        "confidence": entity.get("confidence") or 0.0,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _db_path() -> Path:
    env_override = os.getenv("COGNIVELLE_DB_PATH") or os.getenv("CENTRAL_DB_PATH")
    if env_override:
        p = Path(env_override).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    return WORKSPACE / ".enrichment_contacts.sqlite3"


@contextmanager
def _connection():
    connection = sqlite3.connect(_db_path())
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        connection.executescript(
            """
        CREATE TABLE IF NOT EXISTS businesses (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, website TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, name TEXT,
            title TEXT, email TEXT, phone TEXT, source TEXT, confidence TEXT,
            contact_key TEXT,
            FOREIGN KEY(business_id) REFERENCES businesses(id)
        );
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY,
            business_id INTEGER,
            name TEXT NOT NULL,
            role TEXT,
            email TEXT,
            phone TEXT,
            team TEXT,
            source TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(business_id) REFERENCES businesses(id)
        );
        CREATE TABLE IF NOT EXISTS email_drafts (
            id TEXT PRIMARY KEY, business_id INTEGER, recipient TEXT NOT NULL,
            subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, sent_at TEXT,
            FOREIGN KEY(business_id) REFERENCES businesses(id)
        );
        CREATE TABLE IF NOT EXISTS tool_runs (
            id INTEGER PRIMARY KEY, tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL,
            result_json TEXT, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tool_runs_name_time ON tool_runs(tool_name, created_at DESC);
        CREATE TABLE IF NOT EXISTS followups (
            id INTEGER PRIMARY KEY,
            business_id INTEGER NOT NULL,
            contact_id INTEGER,
            channel TEXT NOT NULL DEFAULT 'email',
            status TEXT NOT NULL DEFAULT 'pending',
            notes TEXT,
            scheduled_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(business_id) REFERENCES businesses(id),
            FOREIGN KEY(contact_id) REFERENCES contacts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_followups_business ON followups(business_id);
        CREATE INDEX IF NOT EXISTS idx_followups_status ON followups(status, scheduled_at);
        CREATE TABLE IF NOT EXISTS whatsapp_drafts (
            id TEXT PRIMARY KEY,
            business_id INTEGER,
            recipient_phone TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            sent_at TEXT,
            FOREIGN KEY(business_id) REFERENCES businesses(id)
        );
        CREATE TABLE IF NOT EXISTS team_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact_number TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'all',
            email TEXT,
            is_active INTEGER DEFAULT 1,
            assigned_count INTEGER DEFAULT 0,
            last_assigned_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_team_members_role_active ON team_members(is_active, role);

        CREATE TABLE IF NOT EXISTS places (
            place_id TEXT PRIMARY KEY,
            name TEXT,
            address TEXT,
            address_components TEXT,
            plus_code TEXT,
            lat REAL,
            lng REAL,
            rating REAL,
            review_count INTEGER,
            website TEXT,
            phone TEXT,
            email TEXT,
            fax TEXT,
            price_level TEXT,
            description TEXT,
            categories TEXT,
            primary_type TEXT,
            hours TEXT,
            photos TEXT,
            about TEXT,
            menu TEXT,
            booking_links TEXT,
            social_links TEXT,
            reviews_fetched INTEGER DEFAULT 0,
            reviews_cursor TEXT DEFAULT '',
            reviews_total_saved INTEGER DEFAULT 0,
            scraped_at TEXT DEFAULT (datetime('now')),
            hotel_class TEXT,
            business_status TEXT
        );

        CREATE TABLE IF NOT EXISTS reviews (
            review_id TEXT PRIMARY KEY,
            place_id TEXT NOT NULL,
            reviewer_name TEXT,
            reviewer_profile_url TEXT,
            reviewer_avatar_url TEXT,
            reviewer_user_id TEXT,
            reviewer_review_count TEXT,
            reviewer_is_local_guide INTEGER,
            rating INTEGER,
            text TEXT,
            date TEXT,
            language TEXT,
            photos TEXT,
            owner_reply TEXT,
            owner_reply_date TEXT,
            scraped_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            query TEXT,
            status TEXT DEFAULT 'running',
            places_total INTEGER DEFAULT 0,
            places_done INTEGER DEFAULT 0,
            reviews_total INTEGER DEFAULT 0,
            reviews_done INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS job_places (
            job_id TEXT NOT NULL,
            place_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            reviews_cursor TEXT DEFAULT '',
            reviews_count INTEGER DEFAULT 0,
            PRIMARY KEY(job_id, place_id)
        );

        CREATE TABLE IF NOT EXISTS leads (
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            source TEXT NOT NULL,
            posted_by TEXT NOT NULL,
            user TEXT NOT NULL,
            group_name TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('discarded', 'duplicate', 'assigned')),
            reason TEXT,
            channel TEXT CHECK (channel IN ('call', 'dm', 'mail')),
            contact_value TEXT,
            assigned_name TEXT,
            assigned_role TEXT,
            assigned_number TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS assignment_counters (
            role TEXT PRIMARY KEY,
            next_index INTEGER NOT NULL DEFAULT 0
        );
            """
        )
        # Seed default team members if empty
        tm_count = connection.execute("SELECT COUNT(*) FROM team_members").fetchone()[0]
        if tm_count == 0:
            defaults = [
                ("Malik Hanab", "923151442665", "caller"),
                ("Faizan Hassan", "923264414043", "DM"),
                ("Faisal Khan", "923085333392", "DM"),
                ("Rizwan Abdullah", "923085333392", "email"),
            ]
            now_seed = datetime.now(UTC).isoformat()
            for tname, tnum, trole in defaults:
                connection.execute(
                    "INSERT INTO team_members(name, contact_number, role, is_active, assigned_count, created_at) "
                    "VALUES (?, ?, ?, 1, 0, ?)",
                    (tname, tnum, trole, now_seed),
                )

        # Migration: add columns to businesses if they don't exist
        biz_columns = {row["name"] for row in connection.execute("PRAGMA table_info(businesses)")}
        if "outreach_status" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN outreach_status TEXT DEFAULT 'not_contacted'")
        if "notes" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN notes TEXT")
        if "followup_count" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN followup_count INTEGER DEFAULT 0")
        if "last_contacted_at" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN last_contacted_at TEXT")
        if "social_profiles" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN social_profiles TEXT")  # JSON
        if "phone" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN phone TEXT")
        if "email" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN email TEXT")
        if "city" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN city TEXT")
        if "industry" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN industry TEXT")
        if "opportunity_score" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN opportunity_score INTEGER DEFAULT 0")
        if "lead_tier" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN lead_tier TEXT DEFAULT 'COLD'")
        if "detected_tech" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN detected_tech TEXT")
        if "source" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN source TEXT DEFAULT 'manual'")
        if "assigned_member_id" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN assigned_member_id INTEGER")
        if "assigned_member_name" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN assigned_member_name TEXT")
        if "proposal_path" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN proposal_path TEXT")
        if "strategy" not in biz_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN strategy TEXT")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(contacts)")}
        if "contact_key" not in columns:
            connection.execute("ALTER TABLE contacts ADD COLUMN contact_key TEXT")
        if "linkedin_url" not in columns:
            connection.execute("ALTER TABLE contacts ADD COLUMN linkedin_url TEXT")
        if "facebook_url" not in columns:
            connection.execute("ALTER TABLE contacts ADD COLUMN facebook_url TEXT")
        if "twitter_url" not in columns:
            connection.execute("ALTER TABLE contacts ADD COLUMN twitter_url TEXT")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_business_key ON contacts(business_id, contact_key)")
        yield connection
        connection.commit()
    finally:
        connection.close()


def save_research(
    business_name: str,
    website: str | None,
    decision_makers: list[dict],
    contact_hints: dict,
    social_profiles: dict | None = None,
) -> dict[str, int]:
    """Upsert public research results without duplicating a contact.

    Args:
        business_name:    Business name.
        website:          Business website URL.
        decision_makers:  List of person dicts from find_decision_makers.
                          Each may contain linkedin_url, facebook_url, twitter_url.
        contact_hints:    Dict with emails_found, phones_found, social_profiles,
                          google_maps_url from extract_contacts_from_pages or
                          crawl_website_for_contacts.
        social_profiles:  Business-level social page URLs keyed by platform
                          (facebook, instagram, linkedin, whatsapp, …).
                          If None, falls back to contact_hints["social_profiles"].
    """
    now = datetime.now(UTC).isoformat()

    # Merge social_profiles from argument and contact_hints
    merged_social: dict = {}
    hints_social = contact_hints.get("social_profiles") or {}
    if isinstance(hints_social, dict):
        merged_social.update(hints_social)
    if social_profiles and isinstance(social_profiles, dict):
        merged_social.update(social_profiles)

    # Best business-level phone / email
    biz_phone: str | None = (contact_hints.get("phones_found") or [None])[0]
    biz_email: str | None = (contact_hints.get("emails_found") or [None])[0]

    with _connection() as connection:
        connection.execute(
            """INSERT INTO businesses(name, website, updated_at, social_profiles, phone, email)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 website        = COALESCE(excluded.website, businesses.website),
                 updated_at     = excluded.updated_at,
                 social_profiles= COALESCE(excluded.social_profiles, businesses.social_profiles),
                 phone          = COALESCE(excluded.phone, businesses.phone),
                 email          = COALESCE(excluded.email, businesses.email)""",
            (
                business_name,
                website,
                now,
                json.dumps(merged_social, ensure_ascii=False) if merged_social else None,
                biz_phone,
                biz_email,
            ),
        )
        business_id = connection.execute(
            "SELECT id FROM businesses WHERE name=?", (business_name,)
        ).fetchone()["id"]

        # Build flat list of records to upsert
        records = list(decision_makers)
        for email in contact_hints.get("emails_found", []):
            records.append({"email": email, "source": "website", "confidence": "public"})
        for phone in contact_hints.get("phones_found", []):
            records.append({"phone": phone, "source": "website", "confidence": "public"})

        for person in records:
            contact_key = _contact_key(person)
            connection.execute(
                """INSERT INTO contacts(
                     business_id, name, title, email, phone, source, confidence,
                     contact_key, linkedin_url, facebook_url, twitter_url
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(business_id, contact_key) DO UPDATE SET
                     name        = COALESCE(excluded.name,        contacts.name),
                     title       = COALESCE(excluded.title,       contacts.title),
                     email       = COALESCE(excluded.email,       contacts.email),
                     phone       = COALESCE(excluded.phone,       contacts.phone),
                     source      = COALESCE(excluded.source,      contacts.source),
                     confidence  = COALESCE(excluded.confidence,  contacts.confidence),
                     linkedin_url= COALESCE(excluded.linkedin_url,contacts.linkedin_url),
                     facebook_url= COALESCE(excluded.facebook_url,contacts.facebook_url),
                     twitter_url = COALESCE(excluded.twitter_url, contacts.twitter_url)""",
                (
                    business_id,
                    person.get("name"),
                    person.get("title"),
                    person.get("email"),
                    person.get("phone"),
                    person.get("source"),
                    person.get("confidence"),
                    contact_key,
                    person.get("linkedin_url"),
                    person.get("facebook_url"),
                    person.get("twitter_url"),
                ),
            )
        return {"business_id": business_id, "contacts_stored": len(records)}


def save_employee(
    business_name: str,
    name: str,
    role: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    team: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Persist a CRM employee / ownership record into the workspace database.

    This unifies the previous lead-ingestion employee ownership data model
    with the final CRM contract in one SQLite-backed record store.
    """
    now = datetime.now(UTC).isoformat()
    with _connection() as connection:
        biz = connection.execute(
            "SELECT id FROM businesses WHERE name=?",
            (business_name,),
        ).fetchone()
        business_id = biz["id"] if biz else None
        if business_id is None:
            connection.execute(
                "INSERT INTO businesses(name, website, updated_at) VALUES (?, ?, ?)",
                (business_name, None, now),
            )
            business_id = connection.execute(
                "SELECT id FROM businesses WHERE name=?",
                (business_name,),
            ).fetchone()["id"]
        connection.execute(
            """
            INSERT INTO employees(business_id, name, role, email, phone, team, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (business_id, name, role, email, phone, team, source or "lead_ingestion", now),
        )
        return {"business_id": business_id, "name": name, "role": role, "email": email, "phone": phone}


def search_stored_contacts(query: str = "", limit: int = 20) -> dict[str, Any]:
    """Search saved businesses and public contacts in the workspace database."""
    limit = max(1, min(int(limit), 100))
    like = f"%{query.strip()}%"
    with _connection() as connection:
        rows = connection.execute(
            "SELECT b.name AS business_name,b.website,c.name,c.title,c.email,c.phone,c.source,c.confidence "
            "FROM contacts c JOIN businesses b ON b.id=c.business_id "
            "WHERE b.name LIKE ? OR COALESCE(c.name,'') LIKE ? OR COALESCE(c.email,'') LIKE ? "
            "ORDER BY b.updated_at DESC LIMIT ?",
            (like, like, like, limit),
        ).fetchall()
    return {"contacts": [dict(row) for row in rows], "database": _db_path().name}


def record_tool_run(tool_name: str, arguments: dict[str, Any], result: Any = None, error: str | None = None) -> None:
    """Persist every MCP operation, including failed calls, as an audit event."""
    status = "error" if error else "ok"
    with _connection() as connection:
        connection.execute(
            "INSERT INTO tool_runs(tool_name,arguments_json,result_json,status,error,created_at) VALUES(?,?,?,?,?,?)",
            (
                tool_name,
                json.dumps(arguments, ensure_ascii=False, default=str),
                json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                status,
                error[:2_000] if error else None,
                datetime.now(UTC).isoformat(),
            ),
        )


def list_tool_runs(tool_name: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Read durable system activity records for auditing and recovery."""
    limit = max(1, min(int(limit), 200))
    with _connection() as connection:
        if tool_name:
            rows = connection.execute(
                "SELECT id,tool_name,arguments_json,result_json,status,error,created_at FROM tool_runs WHERE tool_name=? ORDER BY id DESC LIMIT ?",
                (tool_name, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id,tool_name,arguments_json,result_json,status,error,created_at FROM tool_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return {"runs": [dict(row) for row in rows], "database": _db_path().name}


def _contact_key(person: dict) -> str:
    email = str(person.get("email") or "").strip().lower()
    if email:
        return f"email:{email}"
    phone = re.sub(r"\D", "", str(person.get("phone") or ""))
    if phone:
        return f"phone:{phone}"
    name = re.sub(r"\s+", " ", str(person.get("name") or "").strip().lower())
    if name:
        return f"name:{name}"
    return "unknown:" + json.dumps(person, sort_keys=True, default=str)


def create_draft(draft_id: str, business_name: str, recipient: str, subject: str, body: str) -> None:
    now = datetime.now(UTC).isoformat()
    with _connection() as connection:
        business = connection.execute("SELECT id FROM businesses WHERE name=?", (business_name,)).fetchone()
        business_id = business["id"] if business else None
        connection.execute(
            "INSERT INTO email_drafts(id,business_id,recipient,subject,body,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
            (draft_id, business_id, recipient, subject, body, now),
        )


def update_draft_body(draft_id: str, subject: str | None, body: str | None) -> None:
    """Update the subject and/or body of a pending draft (for Streamlit editing)."""
    with _connection() as connection:
        if subject is not None and body is not None:
            connection.execute(
                "UPDATE email_drafts SET subject=?, body=? WHERE id=? AND status='pending'",
                (subject, body, draft_id),
            )
        elif subject is not None:
            connection.execute(
                "UPDATE email_drafts SET subject=? WHERE id=? AND status='pending'",
                (subject, draft_id),
            )
        elif body is not None:
            connection.execute(
                "UPDATE email_drafts SET body=? WHERE id=? AND status='pending'",
                (body, draft_id),
            )


def pending_draft(draft_id: str) -> dict[str, Any] | None:
    with _connection() as connection:
        row = connection.execute(
            "SELECT d.*, COALESCE(b.name, '') AS business_name "
            "FROM email_drafts d "
            "LEFT JOIN businesses b ON b.id = d.business_id "
            "WHERE d.id=? AND d.status='pending'",
            (draft_id,),
        ).fetchone()
    return dict(row) if row else None


def mark_draft_sent(draft_id: str) -> None:
    with _connection() as connection:
        connection.execute("UPDATE email_drafts SET status='sent', sent_at=? WHERE id=?", (datetime.now(UTC).isoformat(), draft_id))


# ---------------------------------------------------------------------------
# Followup / outreach tracking helpers  (used by Streamlit dashboard)
# ---------------------------------------------------------------------------

def log_followup(
    business_name: str,
    channel: str = "email",
    notes: str = "",
    contact_name: str | None = None,
) -> dict[str, Any]:
    """Record that the user has followed up with a business.

    Increments followup_count on the business and updates outreach_status
    to 'contacted'. Returns the new followup_count.
    """
    now = datetime.now(UTC).isoformat()
    with _connection() as connection:
        business = connection.execute(
            "SELECT id, followup_count FROM businesses WHERE name=?", (business_name,)
        ).fetchone()
        if not business:
            connection.execute(
                "INSERT INTO businesses(name, updated_at, outreach_status, followup_count) VALUES(?,?,'contacted',1)",
                (business_name, now),
            )
            business_id = connection.execute(
                "SELECT id FROM businesses WHERE name=?", (business_name,)
            ).fetchone()["id"]
            new_count = 1
        else:
            business_id = business["id"]
            new_count = (business["followup_count"] or 0) + 1
            connection.execute(
                "UPDATE businesses SET followup_count=?, outreach_status='contacted', last_contacted_at=?, updated_at=? WHERE id=?",
                (new_count, now, now, business_id),
            )
        # Resolve optional contact_id
        contact_id = None
        if contact_name:
            row = connection.execute(
                "SELECT id FROM contacts WHERE business_id=? AND name LIKE ? LIMIT 1",
                (business_id, f"%{contact_name}%"),
            ).fetchone()
            if row:
                contact_id = row["id"]
        connection.execute(
            "INSERT INTO followups(business_id, contact_id, channel, status, notes, created_at) VALUES(?,?,?,?,?,?)",
            (business_id, contact_id, channel, "completed", notes, now),
        )
    return {"business_name": business_name, "followup_count": new_count, "channel": channel}


def update_business_notes(business_name: str, notes: str) -> None:
    """Update free-text notes for a business."""
    now = datetime.now(UTC).isoformat()
    with _connection() as connection:
        connection.execute(
            "UPDATE businesses SET notes=?, updated_at=? WHERE name=?",
            (notes, now, business_name),
        )


def update_outreach_status(business_name: str, status: str) -> None:
    """Update the outreach status (not_contacted / contacted / closed / not_interested)."""
    valid = {"not_contacted", "contacted", "closed", "not_interested", "demo_scheduled"}
    if status not in valid:
        raise ValueError(f"status must be one of {valid}")
    now = datetime.now(UTC).isoformat()
    with _connection() as connection:
        connection.execute(
            "UPDATE businesses SET outreach_status=?, updated_at=? WHERE name=?",
            (status, now, business_name),
        )


def get_outreach_summary() -> dict[str, Any]:
    """Return aggregate outreach stats for the dashboard."""
    with _connection() as connection:
        total = connection.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        contacted = connection.execute(
            "SELECT COUNT(*) FROM businesses WHERE outreach_status='contacted'"
        ).fetchone()[0]
        closed = connection.execute(
            "SELECT COUNT(*) FROM businesses WHERE outreach_status='closed'"
        ).fetchone()[0]
        demo = connection.execute(
            "SELECT COUNT(*) FROM businesses WHERE outreach_status='demo_scheduled'"
        ).fetchone()[0]
        not_interested = connection.execute(
            "SELECT COUNT(*) FROM businesses WHERE outreach_status='not_interested'"
        ).fetchone()[0]
        total_followups = connection.execute("SELECT COUNT(*) FROM followups").fetchone()[0]
        recent = connection.execute(
            "SELECT b.name, b.outreach_status, b.followup_count, b.last_contacted_at, b.notes "
            "FROM businesses b ORDER BY b.updated_at DESC LIMIT 20"
        ).fetchall()
    return {
        "total_businesses": total,
        "contacted": contacted,
        "not_contacted": total - contacted - closed - not_interested - demo,
        "closed": closed,
        "demo_scheduled": demo,
        "not_interested": not_interested,
        "total_followups": total_followups,
        "recent": [dict(r) for r in recent],
    }


# ---------------------------------------------------------------------------
# WhatsApp Draft Management
# ---------------------------------------------------------------------------

def create_whatsapp_draft(business_name: str, recipient_phone: str, message: str) -> dict[str, Any]:
    """Create a pending WhatsApp outreach draft for human-in-the-loop review."""
    import uuid
    draft_id = f"wa-{uuid.uuid4().hex[:10]}"
    now = datetime.now(UTC).isoformat()
    with _connection() as connection:
        biz = connection.execute("SELECT id FROM businesses WHERE name=?", (business_name,)).fetchone()
        business_id = biz["id"] if biz else None
        clean_phone = re.sub(r"[^\d+]", "", recipient_phone.strip())
        connection.execute(
            "INSERT INTO whatsapp_drafts(id, business_id, recipient_phone, message, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (draft_id, business_id, clean_phone, message.strip(), now),
        )
    return {
        "draft_id": draft_id,
        "business_name": business_name,
        "recipient_phone": recipient_phone,
        "message": message,
        "status": "pending",
    }


def update_whatsapp_draft(draft_id: str, message: str, recipient_phone: str | None = None) -> None:
    """Update message or phone on a pending WhatsApp draft."""
    with _connection() as connection:
        if recipient_phone:
            clean_phone = re.sub(r"[^\d+]", "", recipient_phone.strip())
            connection.execute(
                "UPDATE whatsapp_drafts SET message=?, recipient_phone=? WHERE id=? AND status='pending'",
                (message.strip(), clean_phone, draft_id),
            )
        else:
            connection.execute(
                "UPDATE whatsapp_drafts SET message=? WHERE id=? AND status='pending'",
                (message.strip(), draft_id),
            )


def mark_whatsapp_draft_sent(draft_id: str) -> None:
    """Mark a WhatsApp draft as sent."""
    now = datetime.now(UTC).isoformat()
    with _connection() as connection:
        connection.execute(
            "UPDATE whatsapp_drafts SET status='sent', sent_at=? WHERE id=?",
            (now, draft_id),
        )


def discard_whatsapp_draft(draft_id: str) -> None:
    """Discard a pending WhatsApp draft."""
    with _connection() as connection:
        connection.execute(
            "UPDATE whatsapp_drafts SET status='discarded' WHERE id=?",
            (draft_id,),
        )


def list_whatsapp_drafts(status: str = "pending") -> list[dict[str, Any]]:
    """List WhatsApp outreach drafts."""
    with _connection() as connection:
        rows = connection.execute(
            "SELECT w.id, w.business_id, w.recipient_phone, w.message, w.status, w.created_at, w.sent_at, "
            "COALESCE(b.name, '') AS business_name "
            "FROM whatsapp_drafts w LEFT JOIN businesses b ON b.id = w.business_id "
            "WHERE w.status=? ORDER BY w.created_at DESC",
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# CRM Entity Operations
# ---------------------------------------------------------------------------

def upsert_business(
    name: str,
    website: str | None = None,
    city: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    notes: str | None = None,
    outreach_status: str | None = None,
    opportunity_score: int | None = None,
    detected_tech: str | None = None,
    social_profiles: dict | None = None,
    source: str = "manual",
    lead_tier: str | None = None,
    assigned_member_name: str | None = None,
) -> int:
    """Create or update a business entity in the unified CRM."""
    now = datetime.now(UTC).isoformat()
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Business name cannot be empty")

    with _connection() as connection:
        row = connection.execute("SELECT id, social_profiles, notes, outreach_status, opportunity_score FROM businesses WHERE name=?", (clean_name,)).fetchone()
        if row:
            biz_id = row["id"]
            merged_social = {}
            if row["social_profiles"]:
                try:
                    merged_social.update(json.loads(row["social_profiles"]))
                except Exception:
                    pass
            if social_profiles:
                merged_social.update(social_profiles)

            connection.execute(
                "UPDATE businesses SET "
                "website=COALESCE(?, website), "
                "city=COALESCE(?, city), "
                "phone=COALESCE(?, phone), "
                "email=COALESCE(?, email), "
                "notes=COALESCE(?, notes), "
                "outreach_status=COALESCE(?, outreach_status), "
                "opportunity_score=COALESCE(?, opportunity_score), "
                "detected_tech=COALESCE(?, detected_tech), "
                "lead_tier=COALESCE(?, lead_tier), "
                "assigned_member_name=COALESCE(?, assigned_member_name), "
                "social_profiles=?, "
                "updated_at=? "
                "WHERE id=?",
                (
                    website,
                    city,
                    phone,
                    email,
                    notes,
                    outreach_status,
                    opportunity_score,
                    detected_tech,
                    lead_tier,
                    assigned_member_name,
                    json.dumps(merged_social) if merged_social else None,
                    now,
                    biz_id,
                ),
            )
            return biz_id
        else:
            cur = connection.execute(
                "INSERT INTO businesses(name, website, city, phone, email, notes, outreach_status, "
                "opportunity_score, detected_tech, social_profiles, source, lead_tier, assigned_member_name, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    clean_name,
                    website,
                    city,
                    phone,
                    email,
                    notes,
                    outreach_status or "not_contacted",
                    opportunity_score or 0,
                    detected_tech,
                    json.dumps(social_profiles) if social_profiles else None,
                    source,
                    lead_tier or ("UNPOPULATED" if outreach_status == "unpopulated" else "COLD"),
                    assigned_member_name,
                    now,
                ),
            )
            return cur.lastrowid


def delete_business(business_name: str) -> bool:
    """Delete a business and its associated contacts from the CRM."""
    with _connection() as connection:
        biz = connection.execute("SELECT id FROM businesses WHERE name=?", (business_name,)).fetchone()
        if not biz:
            return False
        biz_id = biz["id"]
        connection.execute("DELETE FROM followups WHERE business_id=?", (biz_id,))
        connection.execute("DELETE FROM contacts WHERE business_id=?", (biz_id,))
        connection.execute("DELETE FROM email_drafts WHERE business_id=?", (biz_id,))
        connection.execute("DELETE FROM whatsapp_drafts WHERE business_id=?", (biz_id,))
        connection.execute("DELETE FROM employees WHERE business_id=?", (biz_id,))
        connection.execute("DELETE FROM businesses WHERE id=?", (biz_id,))
        return True


def add_business_contact(
    business_name: str,
    name: str,
    title: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    linkedin_url: str | None = None,
    source: str = "manual",
) -> int:
    """Add a contact/decision maker to a business."""
    with _connection() as connection:
        biz = connection.execute("SELECT id FROM businesses WHERE name=?", (business_name,)).fetchone()
        if not biz:
            biz_id = upsert_business(business_name)
        else:
            biz_id = biz["id"]

        contact_key = _contact_key({"email": email, "phone": phone, "name": name})
        cur = connection.execute(
            "INSERT INTO contacts(business_id, name, title, email, phone, linkedin_url, source, contact_key, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual') "
            "ON CONFLICT(business_id, contact_key) DO UPDATE SET "
            "name=COALESCE(excluded.name, contacts.name), "
            "title=COALESCE(excluded.title, contacts.title), "
            "email=COALESCE(excluded.email, contacts.email), "
            "phone=COALESCE(excluded.phone, contacts.phone), "
            "linkedin_url=COALESCE(excluded.linkedin_url, contacts.linkedin_url)",
            (biz_id, name, title, email, phone, linkedin_url, source, contact_key),
        )
        return cur.lastrowid or 0


def delete_contact(contact_id: int) -> bool:
    """Delete a contact by ID."""
    with _connection() as connection:
        connection.execute("DELETE FROM followups WHERE contact_id=?", (contact_id,))
        cur = connection.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Scraper Output Ingestion Bridges (Preserving Brittle Scrapers)
# ---------------------------------------------------------------------------

def ingest_maps_db_places(gmaps_db_path: str | Path | None = None, limit: int | None = None) -> int:
    """Import places from pymapscraper output/gmaps.db directly into the central CRM."""
    if gmaps_db_path is None:
        repo_root = Path(__file__).resolve().parents[3]
        gmaps_db_path = repo_root / "pymapscraper" / "output" / "gmaps.db"
    db_path = Path(gmaps_db_path)
    if not db_path.exists():
        return 0

    gconn = sqlite3.connect(str(db_path))
    gconn.row_factory = sqlite3.Row
    query = "SELECT name, address, website, phone, email, categories, social_links, rating, review_count, description FROM places"
    if limit:
        query += f" LIMIT {int(limit)}"
    places = gconn.execute(query).fetchall()
    gconn.close()

    imported = 0
    for p in places:
        name = (p["name"] or "").strip()
        if not name:
            continue
        # Extract city from address if possible
        address = p["address"] or ""
        city = None
        if address:
            parts = [part.strip() for part in address.split(",") if part.strip()]
            if len(parts) >= 2:
                city = parts[-2]
            elif parts:
                city = parts[0]

        # Parse social links
        social_dict = {}
        if p["social_links"]:
            try:
                s_links = json.loads(p["social_links"])
                if isinstance(s_links, list):
                    for link in s_links:
                        if isinstance(link, dict) and link.get("platform") and link.get("url"):
                            social_dict[link["platform"].lower()] = link["url"]
            except Exception:
                pass

        notes = f"Imported from Google Maps. Rating: {p['rating'] or '—'} ({p['review_count'] or 0} reviews). {p['description'] or ''}".strip()
        upsert_business(
            name=name,
            website=p["website"],
            city=city,
            phone=p["phone"],
            email=p["email"],
            notes=notes,
            social_profiles=social_dict if social_dict else None,
            source="maps",
        )
        imported += 1
    return imported


def ingest_hot_leads_excel(excel_path: str | Path | None = None, limit: int | None = None) -> int:
    """Import enriched leads from isolated_crawler hot_leads.xlsx into the central CRM."""
    if excel_path is None:
        repo_root = Path(__file__).resolve().parents[3]
        excel_path = repo_root / "isolated_crawler" / "hot_leads.xlsx"
    file_path = Path(excel_path)
    if not file_path.exists():
        return 0

    import pandas as pd
    df = pd.read_excel(file_path)
    if limit:
        df = df.head(int(limit))

    imported = 0
    for _, row in df.iterrows():
        name = str(row.get("BusinessName") or row.get("name") or "").strip()
        if not name or name == "nan":
            continue

        website = str(row.get("NormalizedWebsite") or row.get("Website") or "").strip()
        if website == "nan":
            website = None
        city = str(row.get("City") or "").strip()
        if city == "nan":
            city = None
        phone = str(row.get("Phone") or row.get("phone") or "").strip()
        if phone == "nan":
            phone = None
        email = str(row.get("PrimaryEmail") or row.get("email") or "").strip()
        if email == "nan":
            email = None

        score = row.get("LeadScore")
        try:
            opp_score = int(score) if pd.notna(score) else 0
        except Exception:
            opp_score = 0

        tech = str(row.get("Technology") or row.get("CMS") or "").strip()
        detected_tech = None if tech == "nan" or not tech else json.dumps({"cms": tech, "tier": str(row.get("LeadTier") or "")})

        social_dict = {}
        for plat in ["Facebook", "Instagram", "LinkedIn", "Twitter", "WhatsApp"]:
            val = str(row.get(plat) or "").strip()
            if val and val != "nan":
                social_dict[plat.lower()] = val

        opp_reasons = str(row.get("OpportunityReasons") or "").strip()
        rec_service = str(row.get("RecommendedService") or "").strip()
        hook = str(row.get("PersonalizationHook") or "").strip()
        notes_parts = []
        if rec_service and rec_service != "nan":
            notes_parts.append(f"Recommended: {rec_service}")
        if opp_reasons and opp_reasons != "nan":
            notes_parts.append(f"Reasons: {opp_reasons}")
        if hook and hook != "nan":
            notes_parts.append(f"Hook: {hook}")
        notes = " | ".join(notes_parts) if notes_parts else "Imported from isolated_crawler"

        biz_id = upsert_business(
            name=name,
            website=website,
            city=city,
            phone=phone,
            email=email,
            notes=notes,
            opportunity_score=opp_score,
            detected_tech=detected_tech,
            social_profiles=social_dict if social_dict else None,
            source="isolated_crawler",
        )
        if email:
            add_business_contact(name, name="Owner / Contact", email=email, phone=phone, source="isolated_crawler")
        imported += 1

    return imported


def ingest_social_pipeline_leads(leads_db_path: str | Path | None = None, limit: int | None = None) -> int:
    """Import qualified social media leads from IngestionAutomationPipeline/data/leads.db."""
    if leads_db_path is None:
        repo_root = Path(__file__).resolve().parents[3]
        leads_db_path = repo_root / "IngestionAutomationPipeline" / "data" / "leads.db"
    db_path = Path(leads_db_path)
    if not db_path.exists():
        return 0

    sconn = sqlite3.connect(str(db_path))
    sconn.row_factory = sqlite3.Row
    query = "SELECT title, text, source, posted_by, channel, contact_value, created_at FROM leads WHERE status='assigned'"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = sconn.execute(query).fetchall()
    sconn.close()

    imported = 0
    for r in rows:
        name = (r["posted_by"] or r["title"] or "").strip()
        if not name:
            continue
        phone = r["contact_value"] if r["channel"] == "call" else None
        email = r["contact_value"] if r["channel"] == "mail" else None
        notes = f"Social lead ({r['source']}): {r['title']}. {r['text'][:300]}"

        upsert_business(
            name=name,
            phone=phone,
            email=email,
            notes=notes,
            source=f"social_{r['source']}",
        )
        imported += 1
    return imported


def dispatch_whatsapp_message(recipient_phone: str, message: str, business_id: int | None = None) -> dict[str, Any]:
    """Send a WhatsApp notification message via the background gateway and record in drafts."""
    import uuid
    import urllib.request

    draft_id = f"wa-{uuid.uuid4().hex[:10]}"
    now = datetime.now(UTC).isoformat()
    clean_phone = re.sub(r"[^\d+]", "", str(recipient_phone or "").strip())

    url = os.getenv("WHATSAPP_SERVICE_URL", "http://localhost:3000/sendMessage")
    api_key = os.getenv("WHATSAPP_INTERNAL_API_KEY", "dummy")
    payload = json.dumps({"phone": clean_phone, "text": message}).encode("utf-8")

    sent = False
    error_msg = None
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "X-Api-Key": api_key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status in (200, 201):
                sent = True
    except Exception as exc:
        error_msg = str(exc)

    status = "sent" if sent else "pending"
    with _connection() as connection:
        connection.execute(
            """INSERT INTO whatsapp_drafts(id, business_id, recipient_phone, message, status, created_at, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (draft_id, business_id, clean_phone, message, status, now, now if sent else None),
        )

    return {
        "draft_id": draft_id,
        "recipient_phone": clean_phone,
        "status": status,
        "sent": sent,
        "error": error_msg,
    }


def get_team_members(active_only: bool = True, role: str | None = None) -> list[dict[str, Any]]:
    """Return configured team members for sales distribution."""
    with _connection() as connection:
        query = "SELECT id, name, contact_number, role, email, is_active, assigned_count, last_assigned_at FROM team_members"
        params = []
        clauses = []
        if active_only:
            clauses.append("is_active=1")
        if role:
            clauses.append("(role=? OR role='all')")
            params.append(role)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY assigned_count ASC, id ASC"
        rows = connection.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def add_team_member(name: str, contact_number: str, role: str = "all", email: str | None = None) -> int:
    """Add a new sales team member to the unified database."""
    now = datetime.now(UTC).isoformat()
    with _connection() as connection:
        cur = connection.execute(
            "INSERT INTO team_members(name, contact_number, role, email, is_active, assigned_count, created_at) "
            "VALUES (?, ?, ?, ?, 1, 0, ?)",
            (name.strip(), contact_number.strip(), role.strip(), email, now),
        )
        return cur.lastrowid or 0


def assign_business_lead(
    business_name: str,
    channel: str | None = None,
    role: str | None = None,
    notify: bool = True,
) -> dict[str, Any]:
    """Assign a business lead to an active sales team member and notify them via WhatsApp."""
    now = datetime.now(UTC).isoformat()
    clean_name = str(business_name or "").strip()

    with _connection() as connection:
        biz = connection.execute("SELECT * FROM businesses WHERE name=?", (clean_name,)).fetchone()
        if not biz:
            return {"error": f"Business '{clean_name}' not found"}

        biz_id = biz["id"]
        # Determine target role based on channel or available contacts
        target_role = role
        if not target_role:
            ch = (channel or "").lower()
            if ch in ("call", "phone") or (biz["phone"] and not biz["email"]):
                target_role = "caller"
            elif ch in ("dm", "whatsapp", "social"):
                target_role = "DM"
            elif ch in ("mail", "email") or (biz["email"] and not biz["phone"]):
                target_role = "email"
            else:
                target_role = None

        # Pick candidate member with lowest assigned_count
        candidates = get_team_members(active_only=True, role=target_role)
        if not candidates and target_role:
            candidates = get_team_members(active_only=True)

        if not candidates:
            return {"error": "No active team members found in CRM"}

        member = candidates[0]
        member_id = member["id"]
        member_name = member["name"]
        member_phone = member["contact_number"]
        member_role = member["role"]

        # Update business assignment
        connection.execute(
            "UPDATE businesses SET assigned_member_id=?, assigned_member_name=?, updated_at=? WHERE id=?",
            (member_id, member_name, now, biz_id),
        )
        # Update team member counter
        connection.execute(
            "UPDATE team_members SET assigned_count=assigned_count+1, last_assigned_at=? WHERE id=?",
            (now, member_id),
        )

    # Send message to member
    notification_result = {}
    if notify and member_phone:
        text = (
            f"🚨 New Lead Assigned!\n"
            f"Business: {clean_name}\n"
            f"City: {biz['city'] or 'N/A'}\n"
            f"Phone: {biz['phone'] or 'N/A'}\n"
            f"Email: {biz['email'] or 'N/A'}\n"
            f"Website: {biz['website'] or 'N/A'}\n"
            f"Score: {biz['opportunity_score'] or 0} ({biz['lead_tier'] or 'COLD'})\n"
            f"Notes: {biz['notes'] or 'New discovery lead'}"
        )
        notification_result = dispatch_whatsapp_message(member_phone, text, business_id=biz_id)

    return {
        "business_name": clean_name,
        "business_id": biz_id,
        "assigned_name": member_name,
        "assigned_id": member_id,
        "assigned_role": member_role,
        "contact_number": member_phone,
        "notification": notification_result,
    }


def sync_places_to_unpopulated_leads(limit: int | None = None) -> int:
    """Sync places table in the single database into businesses as unpopulated leads."""
    with _connection() as connection:
        query = (
            "SELECT p.name, p.address, p.website, p.phone, p.email, p.social_links, p.rating, p.review_count, p.description "
            "FROM places p LEFT JOIN businesses b ON b.name = p.name "
            "WHERE b.id IS NULL"
        )
        if limit:
            query += f" LIMIT {int(limit)}"
        places = connection.execute(query).fetchall()

    imported = 0
    for p in places:
        name = (p["name"] or "").strip()
        if not name:
            continue
        address = p["address"] or ""
        city = None
        if address:
            parts = [part.strip() for part in address.split(",") if part.strip()]
            if len(parts) >= 2:
                city = parts[-2]
            elif parts:
                city = parts[0]

        social_dict = {}
        if p["social_links"]:
            try:
                s_links = json.loads(p["social_links"])
                if isinstance(s_links, list):
                    for link in s_links:
                        if isinstance(link, dict) and link.get("platform") and link.get("url"):
                            social_dict[link["platform"].lower()] = link["url"]
            except Exception:
                pass

        notes = f"Discovered via pymapscraper. Rating: {p['rating'] or '—'} ({p['review_count'] or 0} reviews). {p['description'] or ''}".strip()
        upsert_business(
            name=name,
            website=p["website"],
            city=city,
            phone=p["phone"],
            email=p["email"],
            notes=notes,
            social_profiles=social_dict if social_dict else None,
            source="maps",
            outreach_status="unpopulated",
            lead_tier="UNPOPULATED",
        )
        imported += 1
    return imported


# ---------------------------------------------------------------------------
# MCP-native scraper lead ingestion (Reddit / FB → CRM → assignment)
# ---------------------------------------------------------------------------

def ingest_scraper_lead(
    title: str,
    body: str,
    source: str,
    posted_by: str,
    contact_phone: str | None = None,
    contact_email: str | None = None,
    group_name: str | None = None,
    city: str | None = None,
    auto_assign: bool = True,
    auto_score: bool = True,
) -> dict[str, Any]:
    """Ingest a single lead coming from Reddit, Facebook, or any social scraper.

    Flow: raw post → normalise → upsert into businesses (unpopulated) →
          score → assign to team member → WhatsApp notification.

    Returns the full assignment result so the caller can record it.
    """
    from datetime import UTC, datetime as _dt

    clean_title = str(title or "").strip()
    clean_body = str(body or "").strip()
    clean_posted_by = str(posted_by or "").strip()

    # Derive a usable business name: prefer poster handle + source
    biz_name = clean_posted_by or clean_title[:60] or "Unknown Lead"
    notes = (
        f"[{source.upper()} LEAD] {clean_title}\n"
        f"Group: {group_name or 'N/A'}\n"
        f"Posted by: {clean_posted_by}\n"
        f"Body: {clean_body[:500]}"
    )

    biz_id = upsert_business(
        name=biz_name,
        phone=contact_phone,
        email=contact_email,
        city=city,
        notes=notes,
        source=f"social_{source}",
        outreach_status="not_contacted",
    )

    result: dict[str, Any] = {
        "business_name": biz_name,
        "business_id": biz_id,
        "source": source,
        "score": None,
        "tier": None,
        "assignment": None,
    }

    # Auto-score via local scoring pipeline (no MCP round-trip needed here)
    if auto_score:
        try:
            from .lead_scoring import calculate_lead_score
            biz_record = {"name": biz_name, "phone": contact_phone, "email": contact_email,
                          "city": city, "notes": notes, "website": None, "source": source}
            sc = calculate_lead_score(biz_record)
            now = _dt.now(UTC).isoformat()
            with _connection() as conn:
                conn.execute(
                    "UPDATE businesses SET opportunity_score=?, lead_tier=?, proposal_path=?, updated_at=? WHERE id=?",
                    (sc["opportunity_score"], sc["lead_tier"], sc["proposal_path"], now, biz_id),
                )
            result["score"] = sc["opportunity_score"]
            result["tier"] = sc["lead_tier"]
        except Exception as exc:
            result["score_error"] = str(exc)

    # Auto-assign to a team member
    if auto_assign:
        try:
            channel = "phone" if contact_phone else ("email" if contact_email else "dm")
            asgn = assign_business_lead(biz_name, channel=channel, notify=True)
            result["assignment"] = asgn
        except Exception as exc:
            result["assignment_error"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# Extended team member management
# ---------------------------------------------------------------------------

def remove_team_member(member_id: int) -> bool:
    """Permanently delete a team member by ID."""
    with _connection() as connection:
        cur = connection.execute("DELETE FROM team_members WHERE id=?", (member_id,))
        return cur.rowcount > 0


def toggle_team_member(member_id: int, is_active: bool) -> bool:
    """Activate or deactivate a team member without deleting them."""
    with _connection() as connection:
        cur = connection.execute(
            "UPDATE team_members SET is_active=? WHERE id=?",
            (1 if is_active else 0, member_id),
        )
        return cur.rowcount > 0


def update_team_member(member_id: int, name: str | None = None, contact_number: str | None = None,
                       role: str | None = None, email: str | None = None) -> bool:
    """Update mutable fields of a team member."""
    now = datetime.now(UTC).isoformat()
    updates: list[str] = []
    params: list[Any] = []
    if name is not None:
        updates.append("name=?"); params.append(name.strip())
    if contact_number is not None:
        updates.append("contact_number=?"); params.append(contact_number.strip())
    if role is not None:
        updates.append("role=?"); params.append(role.strip())
    if email is not None:
        updates.append("email=?"); params.append(email)
    if not updates:
        return False
    params.append(member_id)
    with _connection() as connection:
        cur = connection.execute(
            f"UPDATE team_members SET {', '.join(updates)} WHERE id=?", params
        )
        return cur.rowcount > 0




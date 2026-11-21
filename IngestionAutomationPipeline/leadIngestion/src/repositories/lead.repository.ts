import { db } from "../db/database";
import { IngestRequestBody, LeadChannel, SalesRole } from "../types";

export interface LeadRecord {
  id: string;
  fingerprint: string;
  status: "discarded" | "duplicate" | "assigned";
  reason?: string;
  channel?: LeadChannel;
  contactValue?: string;
  assignedName?: string;
  assignedRole?: SalesRole;
  assignedNumber?: string;
}

const insertStmt = db.prepare(`
  INSERT INTO leads (
    id, fingerprint, title, text, source, posted_by, user, group_name, posted_at,
    status, reason, channel, contact_value, assigned_name, assigned_role, assigned_number
  ) VALUES (
    @id, @fingerprint, @title, @text, @source, @posted_by, @user, @group_name, @posted_at,
    @status, @reason, @channel, @contactValue, @assignedName, @assignedRole, @assignedNumber
  )
`);

const findByFingerprintStmt = db.prepare(`SELECT id FROM leads WHERE fingerprint = ? LIMIT 1`);

/** Durable dedupe check — backs up the in-memory cache across restarts / cache eviction. */
export function existsByFingerprint(fingerprint: string): boolean {
  return findByFingerprintStmt.get(fingerprint) !== undefined;
}

export function saveLead(body: IngestRequestBody, fingerprint: string, record: LeadRecord): void {
  insertStmt.run({
    id: record.id,
    fingerprint,
    title: body.title,
    text: body.text,
    source: body.source,
    posted_by: body.posted_by,
    user: body.user,
    group_name: body.group,
    posted_at: body.posted_at,
    status: record.status,
    reason: record.reason ?? null,
    channel: record.channel ?? null,
    contactValue: record.contactValue ?? null,
    assignedName: record.assignedName ?? null,
    assignedRole: record.assignedRole ?? null,
    assignedNumber: record.assignedNumber ?? null,
  });

  // Mirror into unified CRM businesses table
  if (record.status === "assigned") {
    try {
      const bizName = (body.posted_by || body.title || "Social Lead").trim();
      const phone = record.channel === "call" ? record.contactValue : null;
      const email = record.channel === "mail" ? record.contactValue : null;
      db.prepare(`
        INSERT INTO businesses (name, phone, email, notes, source, outreach_status, assigned_member_name, updated_at)
        VALUES (?, ?, ?, ?, ?, 'contacted', ?, datetime('now'))
        ON CONFLICT(name) DO UPDATE SET
          phone = COALESCE(excluded.phone, businesses.phone),
          email = COALESCE(excluded.email, businesses.email),
          assigned_member_name = COALESCE(excluded.assigned_member_name, businesses.assigned_member_name),
          notes = COALESCE(businesses.notes || ' | ', '') || excluded.notes,
          updated_at = datetime('now')
      `).run(
        bizName,
        phone,
        email,
        `Social lead via ${body.source} (${body.group}): ${body.title}`,
        `social_${body.source}`,
        record.assignedName ?? null
      );
    } catch (_err) {
      // Table may not be ready if running isolated
    }
  }
}

export function getLeadStats(): { status: string; count: number }[] {
  return db.prepare(`SELECT status, COUNT(*) as count FROM leads GROUP BY status`).all() as {
    status: string;
    count: number;
  }[];
}

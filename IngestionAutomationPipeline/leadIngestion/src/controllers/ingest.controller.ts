// src/controllers/ingest.controller.ts

import { Request, Response } from "express";
import { z } from "zod";
import crypto from "crypto";
import { logger } from "../utils/logger";
import { fingerprint } from "../utils/hash";
import { cacheService } from "../services/cache.service";
import { classifyIntent, QueueFullError } from "../services/classifier.service";
import { verifyLeadAndPropose } from "../services/llm.service";
import { extractContact } from "../services/extraction.service";
import { assignLead } from "../services/leadAssignment.service";
import { sendMessage } from "../services/messaging.service";
import { existsByFingerprint, saveLead } from "../repositories/lead.repository";
import { IngestRequestBody, IngestResult } from "../types";

const ingestSchema = z.object({
  title: z.string().min(1),
  text: z.string().min(1),
  source: z.string().min(1),
  posted_by: z.string().min(1),
  user: z.string().min(1),
  group: z.string().min(1),
  posted_at: z.string().min(1),
});

function buildOutreachMessage(
  body: IngestRequestBody,
  channel: string,
  proposal?: string
): string {
  const base =
    `New lead via ${body.source} (${channel.toUpperCase()})\n` +
    `Title: ${body.title}\n` +
    `From: ${body.posted_by} (user: ${body.user}, group: ${body.group})\n` +
    `Posted at: ${body.posted_at}\n\n` +
    `${body.text}`;

  if (!proposal) return base;

  return `${base}\n\n---\nSuggested approach:\n${proposal}`;
}

export async function ingestHandler(req: Request, res: Response): Promise<void> {
  const parsed = ingestSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Invalid payload", details: parsed.error.flatten() });
    return;
  }
  const body: IngestRequestBody = parsed.data;

  // 1. Dedupe — fast path via in-memory cache, durable fallback via SQLite.
  const key = fingerprint([body.source, body.user, body.title, body.text]);
  if (cacheService.has(key) || existsByFingerprint(key)) {
    logger.info({ key }, "Duplicate ingest request, skipping");
    const result: IngestResult = { status: "duplicate", reason: "Already processed this exact post before" };
    res.status(200).json(result);
    return;
  }
  cacheService.set(key, { receivedAt: new Date().toISOString() });

  // 2. Intent classification via the fine-tuned SEQ_CLS head (finetuning/serve.py).
  let intent;
  try {
    intent = await classifyIntent(body.title, body.text);
  } catch (err) {
    cacheService.del(key);
    if (err instanceof QueueFullError) {
      logger.warn({ key }, "Classifier queue full, rejecting request");
      res.status(429).json({ error: "Classification service is busy, please retry shortly" });
      return;
    }
    logger.error({ err }, "Intent classification failed");
    res.status(502).json({ error: "Intent classification service unavailable" });
    return;
  }

  if (!intent.isLead) {
    logger.info({ reason: intent.reason }, "Discarding non-lead post");
    const reason = intent.reason || "Not a sales lead";
    saveLead(body, key, { id: crypto.randomUUID(), fingerprint: key, status: "discarded", reason });
    const result: IngestResult = { status: "discarded", reason };
    res.status(200).json(result);
    return;
  }

  // 3. LLM double-check: confirm it is a genuine lead and get a proposal.
  //    verifyLeadAndPropose is non-fatal — if it fails it returns confirmed=true
  //    so the pipeline continues without a proposal rather than dropping the lead.
  const verdict = await verifyLeadAndPropose(body.title, body.text, body.posted_by);

  if (!verdict.confirmed) {
    logger.info({ title: body.title }, "LLM rejected classifier lead — discarding");
    const reason = "LLM verification: not a genuine lead";
    saveLead(body, key, { id: crypto.randomUUID(), fingerprint: key, status: "discarded", reason });
    const result: IngestResult = { status: "discarded", reason };
    res.status(200).json(result);
    return;
  }

  // 4. Regex-based contact extraction (call > dm > mail precedence).
  const contact = extractContact(body.text);

  // 5. Assign lead to sales team member using round robin
  const assigned = assignLead(contact.channel, contact);

  // 6. Build outreach notification and dispatch to WhatsApp service
  const outreachMsg = buildOutreachMessage(body, contact.channel, verdict.proposal);
  try {
    await sendMessage(outreachMsg, assigned.member.contactNumber);
    logger.info({ member: assigned.member.name, phone: assigned.member.contactNumber }, "Dispatched lead notification message via WhatsApp");
  } catch (msgErr) {
    logger.warn({ msgErr, member: assigned.member.name }, "WhatsApp dispatch failed or pending; lead is still assigned in CRM");
  }

  // 7. Lead verified — save with assigned team member in CRM database
  const leadId = crypto.randomUUID();
  saveLead(body, key, {
    id: leadId,
    fingerprint: key,
    status: "assigned",
    channel: contact.channel,
    contactValue: contact.value,
    assignedName: assigned.member.name,
    assignedRole: assigned.member.role,
    assignedNumber: assigned.member.contactNumber,
  });

  const result: IngestResult = {
    status: "assigned",
    leadId,
    assignedTo: assigned.member,
  };

  logger.info({ leadId, channel: contact.channel, assignedTo: assigned.member.name }, "Lead verified, assigned, and notified");
  res.status(200).json(result);
}

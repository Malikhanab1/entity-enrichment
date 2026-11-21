import { logger } from "../utils/logger";
import { getMembersByRole } from "./salesTeam.service";
import { takeNextIndex } from "../repositories/assignment.repository";
import { AssignedLead, CHANNEL_TO_ROLE, ExtractedContact, LeadChannel } from "../types";

/**
 * Picks the next sales-team member for the given channel using round-robin,
 * so leads for each role (caller/DM/email) are spread evenly across the
 * people who hold that role. The pointer is persisted in SQLite
 * (assignment_counters table) via a transaction, so distribution stays
 * fair both across restarts and under concurrent requests.
 */
export function assignLead(channel: LeadChannel, contact: ExtractedContact): AssignedLead {
  const role = CHANNEL_TO_ROLE[channel];
  const pool = getMembersByRole(role);

  if (pool.length === 0) {
    throw new Error(`No sales team members configured for role "${role}"`);
  }

  const index = takeNextIndex(role, pool.length);
  const member = pool[index];

  logger.info({ role, member: member.name, poolSize: pool.length }, "Assigned lead");

  return { member, channel, contact };
}

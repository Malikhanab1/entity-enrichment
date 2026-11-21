import fs from "fs";
import path from "path";
import Papa from "papaparse";
import { env } from "../config/env";
import { logger } from "../utils/logger";
import { SalesRole, SalesTeamMember } from "../types";

const VALID_ROLES: SalesRole[] = ["DM", "caller", "email"];

function validateMember(raw: any, index: number): SalesTeamMember {
  const { name, contactNumber, role } = raw;
  if (!name || !contactNumber || !role) {
    throw new Error(`Sales team entry #${index} is missing name/contactNumber/role: ${JSON.stringify(raw)}`);
  }
  if (!VALID_ROLES.includes(role)) {
    throw new Error(`Sales team entry #${index} has invalid role "${role}". Must be one of ${VALID_ROLES.join(", ")}`);
  }
  return { name: String(name), contactNumber: String(contactNumber), role };
}

function loadJson(filePath: string): SalesTeamMember[] {
  const raw = JSON.parse(fs.readFileSync(filePath, "utf-8"));
  if (!Array.isArray(raw)) throw new Error("Sales team JSON must be an array");
  return raw.map(validateMember);
}

function loadCsv(filePath: string): SalesTeamMember[] {
  const raw = fs.readFileSync(filePath, "utf-8");
  const parsed = Papa.parse(raw, { header: true, skipEmptyLines: true });
  if (parsed.errors.length > 0) {
    throw new Error(`Failed to parse sales team CSV: ${parsed.errors[0].message}`);
  }
  return (parsed.data as any[]).map(validateMember);
}

let cachedTeam: SalesTeamMember[] | null = null;

/**
 * Loads the sales team roster from the configured file (JSON or CSV,
 * inferred from extension). Cached in-process; call with `forceReload`
 * to pick up changes without restarting the service.
 */
export function loadSalesTeam(forceReload = false): SalesTeamMember[] {
  if (cachedTeam && !forceReload) return cachedTeam;

  const ext = path.extname(env.salesTeamFile).toLowerCase();
  const members = ext === ".csv" ? loadCsv(env.salesTeamFile) : loadJson(env.salesTeamFile);

  logger.info({ count: members.length, file: env.salesTeamFile }, "Loaded sales team roster");
  cachedTeam = members;
  return members;
}

export function getMembersByRole(role: SalesRole): SalesTeamMember[] {
  return loadSalesTeam().filter((m) => m.role === role);
}

import Database from "better-sqlite3";
import fs from "fs";
import path from "path";
import { env } from "../config/env";
import { logger } from "../utils/logger";

fs.mkdirSync(path.dirname(env.dbFile), { recursive: true });

export const db = new Database(env.dbFile);
db.pragma("journal_mode = WAL");
db.pragma("foreign_keys = ON");

/**
 * Idempotent schema bootstrap — safe to run on every process start.
 *
 * leads: one row per ingest request, regardless of outcome. `fingerprint`
 *   is unique so it also acts as the durable (restart-safe) dedupe guard,
 *   backing up the in-memory cache which only covers the TTL window.
 *
 * assignment_counters: next round-robin index per sales role, so lead
 *   distribution stays fair across restarts.
 */
function migrate(): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS leads (
      id              TEXT PRIMARY KEY,
      fingerprint     TEXT NOT NULL UNIQUE,
      title           TEXT NOT NULL,
      text            TEXT NOT NULL,
      source          TEXT NOT NULL,
      posted_by       TEXT NOT NULL,
      user            TEXT NOT NULL,
      group_name      TEXT NOT NULL,
      posted_at       TEXT NOT NULL,
      status          TEXT NOT NULL CHECK (status IN ('discarded', 'duplicate', 'assigned')),
      reason          TEXT,
      channel         TEXT CHECK (channel IN ('call', 'dm', 'mail')),
      contact_value   TEXT,
      assigned_name   TEXT,
      assigned_role   TEXT,
      assigned_number TEXT,
      created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_leads_fingerprint ON leads (fingerprint);
    CREATE INDEX IF NOT EXISTS idx_leads_status ON leads (status);
    CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads (created_at);

    CREATE TABLE IF NOT EXISTS assignment_counters (
      role       TEXT PRIMARY KEY,
      next_index INTEGER NOT NULL DEFAULT 0
    );
  `);
  logger.info({ file: env.dbFile }, "SQLite schema ready");
}

migrate();

// src/config/env.ts

import dotenv from "dotenv";
import path from "path";

dotenv.config();

function required(name: string, fallback?: string): string {
  const value = process.env[name] ?? fallback;
  if (value === undefined) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

function optionalNumber(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number(raw);
  if (Number.isNaN(parsed)) {
    throw new Error(`Environment variable ${name} must be a number, got "${raw}"`);
  }
  return parsed;
}

export const env = {
  port: optionalNumber("PORT", 8000),
  nodeEnv: required("NODE_ENV", "development"),

  // ── Fine-tuned SEQ_CLS classifier (finetuning/serve.py) ─────────────────
  classifier: {
    baseUrl: required("CLASSIFIER_BASE_URL", "http://localhost:8001"),
    timeoutMs: optionalNumber("CLASSIFIER_TIMEOUT_MS", 10000),
    maxConcurrency: optionalNumber("CLASSIFIER_MAX_CONCURRENCY", 1),
    maxQueueSize: optionalNumber("CLASSIFIER_MAX_QUEUE_SIZE", 20),
  },

  // ── LLM double-check + proposal generation ───────────────────────────────
  // Supported providers: openai | anthropic | google | groq
  // Set LLM_PROVIDER and its matching LLM_API_KEY; pick any model the provider
  // exposes. LLM_MIN_INTERVAL_MS enforces a pause between successive calls so
  // free-tier RPM limits are not hit.
  llm: {
    provider: required("LLM_PROVIDER", "openai") as "openai" | "anthropic" | "google" | "groq",
    model: required("LLM_MODEL", "gpt-4o-mini"),
    apiKey: required("LLM_API_KEY"),
    // Optional: override the API base URL (useful for OpenAI-compatible proxies)
    baseUrl: process.env["LLM_BASE_URL"],
    // Minimum milliseconds between consecutive LLM calls (free-tier guard)
    minIntervalMs: optionalNumber("LLM_MIN_INTERVAL_MS", 4000),
  },

  messagingServiceUrl: required("MESSAGING_SERVICE_URL", "http://localhost:3000/sendMessage"),
  messagingServiceApiKey: required("MESSAGING_SERVICE_API_KEY", "dummy"),
  messagingServiceMaxRetries: optionalNumber("MESSAGING_SERVICE_MAX_RETRIES", 3),

  salesTeamFile: path.resolve(process.cwd(), required("SALES_TEAM_FILE", "./src/data/salesTeam.json")),

  cacheTtlSeconds: optionalNumber("CACHE_TTL_SECONDS", 86400),

  dbFile: path.resolve(
    process.cwd(),
    process.env["COGNIVELLE_DB_PATH"] ||
      process.env["CENTRAL_DB_PATH"] ||
      process.env["DB_FILE"] ||
      path.resolve(__dirname, "../../../../workspace/.enrichment_contacts.sqlite3")
  ),

  logLevel: required("LOG_LEVEL", "info"),
};

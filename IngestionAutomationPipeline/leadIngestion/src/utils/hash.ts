import crypto from "crypto";

/** Deterministic key used to dedupe near-identical ingest requests. */
export function fingerprint(parts: string[]): string {
  const normalized = parts.map((p) => (p ?? "").trim().toLowerCase()).join("|");
  return crypto.createHash("sha256").update(normalized).digest("hex");
}

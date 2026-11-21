import axios from "axios";
import { env } from "../config/env";
import { logger } from "../utils/logger";

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Forwards a lead notification to the WhatsApp gateway (apps/whatsapp).
 *
 * Contract (per apps/whatsapp/README.md):
 *   POST {MESSAGING_SERVICE_URL}
 *   Header: X-Api-Key: {MESSAGING_SERVICE_API_KEY}   (must match the gateway's INTERNAL_API_KEY)
 *   Body:   { "phone": string, "text": string }
 *
 * The gateway returns 503 while its Baileys socket isn't ready yet — its own
 * docs say callers should retry, so we do that with a short linear backoff
 * before giving up.
 */
export async function sendMessage(text: string, phone: string): Promise<void> {
  const maxAttempts = Math.max(1, env.messagingServiceMaxRetries);

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      await axios.post(
        env.messagingServiceUrl,
        { phone, text },
        {
          timeout: 10000,
          headers: { "X-Api-Key": env.messagingServiceApiKey },
        }
      );
      logger.info({ phone, attempt }, "Message dispatched to WhatsApp gateway");
      return;
    } catch (err) {
      const status = axios.isAxiosError(err) ? err.response?.status : undefined;

      if (status === 401) {
        // Not retryable — a key mismatch won't fix itself.
        logger.error(
          { phone },
          "WhatsApp gateway rejected request with 401 — check MESSAGING_SERVICE_API_KEY matches the gateway's INTERNAL_API_KEY"
        );
        throw new Error("Messaging service auth failed (check MESSAGING_SERVICE_API_KEY)");
      }

      const isLastAttempt = attempt === maxAttempts;
      if (status === 503 && !isLastAttempt) {
        // Gateway's Baileys socket isn't ready yet — worth a retry.
        logger.warn({ phone, attempt, maxAttempts }, "WhatsApp gateway not ready (503), retrying");
        await sleep(500 * attempt);
        continue;
      }

      logger.error({ err, phone, status }, "Failed to dispatch message to WhatsApp gateway");
      throw new Error("Messaging service unavailable");
    }
  }
}

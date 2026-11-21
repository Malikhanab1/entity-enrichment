// src/services/llm.service.ts

import { generateText } from "ai";
import { createOpenAI } from "@ai-sdk/openai";
import { createAnthropic } from "@ai-sdk/anthropic";
import { createGoogleGenerativeAI } from "@ai-sdk/google";
import { createGroq } from "@ai-sdk/groq";
import { env } from "../config/env";
import { logger } from "../utils/logger";

// ── Provider factory ──────────────────────────────────────────────────────────

function buildModel() {
  const { provider, model, apiKey, baseUrl } = env.llm;

  switch (provider) {
    case "openai": {
      const client = createOpenAI({ apiKey, ...(baseUrl ? { baseURL: baseUrl } : {}) });
      return client(model);
    }
    case "anthropic": {
      const client = createAnthropic({ apiKey });
      return client(model as Parameters<ReturnType<typeof createAnthropic>>[0]);
    }
    case "google": {
      const client = createGoogleGenerativeAI({ apiKey });
      return client(model);
    }
    case "groq": {
      const client = createGroq({ apiKey });
      return client(model);
    }
    default:
      throw new Error(`Unsupported LLM_PROVIDER: "${provider}". Supported: openai, anthropic, google, groq`);
  }
}

// ── Rate-limit pause ──────────────────────────────────────────────────────────

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

let lastLlmCallAt = 0;

async function rateLimit(): Promise<void> {
  const now = Date.now();
  const elapsed = now - lastLlmCallAt;
  if (elapsed < env.llm.minIntervalMs) {
    const wait = env.llm.minIntervalMs - elapsed;
    logger.debug({ wait }, "LLM rate-limit pause");
    await sleep(wait);
  }
  lastLlmCallAt = Date.now();
}

// ── Public API ────────────────────────────────────────────────────────────────

export interface LlmLeadVerdict {
  /** True only when the LLM agrees this is a genuine lead */
  confirmed: boolean;
  /**
   * ≤50-word proposal/CTA written in the exact tone and style of the post
   * author. Undefined when confirmed=false.
   */
  proposal?: string;
}

/**
 * Double-checks a post that the classifier already labelled as a lead and,
 * when confirmed, generates a short proposal in the author's own writing style.
 *
 * The pause before every call keeps free-tier RPM limits safe.
 */
export async function verifyLeadAndPropose(
  title: string,
  text: string,
  postedBy: string
): Promise<LlmLeadVerdict> {
  await rateLimit();

  const prompt = `You are a lead-qualification and outreach specialist.

A post was automatically classified as a sales lead for a website or AI automation or CRM. If this is a business facing problem or a business explicitly asking for developers or product to be done approve it as a lead
--- POST ---
Author: ${postedBy}
Title: ${title}
Body:
${text}
--- END ---

Reply ONLY with a JSON object — no markdown fences, no extra text — in this exact shape:
{
  "confirmed": true | false,
  "proposal": "<50 words or fewer, plain text, or null if confirmed is false>"
}

Rules for "confirmed":
- true  → the post clearly expresses a genuine buying intent, service need, or business inquiry.
- false → it is vague, just a question with no purchase intent, spam, or a false positive.

Rules for "proposal" (only when confirmed=true):
- Mirror the author's exact tone, vocabulary, and sentence length — no formal corporate language unless that is how they write.
- Do NOT open with "Hey", "Hi", "I saw your post", or any greeting.
- Be direct: state what you offer and a single clear call to action.
- Hard limit: 50 words.`;

  try {
    const model = buildModel();
    const { text: raw } = await generateText({ model, prompt });

    // Strip any accidental markdown fences the model may add despite instructions
    const clean = raw.replace(/```[a-z]*\n?/gi, "").trim();
    const parsed = JSON.parse(clean) as { confirmed: boolean; proposal?: string | null };

    if (typeof parsed.confirmed !== "boolean") {
      throw new Error("LLM returned unexpected shape");
    }

    logger.info({ confirmed: parsed.confirmed }, "LLM lead verification result");

    return {
      confirmed: parsed.confirmed,
      proposal: parsed.confirmed && parsed.proposal ? parsed.proposal : undefined,
    };
  } catch (err) {
    // Non-fatal — log and fall back to trusting the classifier
    logger.error({ err }, "LLM lead verification failed — falling back to classifier result");
    return { confirmed: true };
  }
}

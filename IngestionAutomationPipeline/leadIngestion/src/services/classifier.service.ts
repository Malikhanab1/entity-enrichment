// leadIngestion/src/services/classifier.service.ts
//
// HTTP client for the fine-tuned SEQ_CLS inference server (finetuning/serve.py).
//
// The Python server loads the LoRA adapter directly via PEFT and runs a single
// forward pass through the trained `score` classification head — no generative
// sampling, no prompt engineering. This is the same path that produced 100%
// test accuracy during fine-tuning.
//
// Contract with finetuning/serve.py:
//   POST /classify  { title: string, text: string }
//               ->  { lead: 0|1, confidence: number }
//   GET  /health -> { status: "ok" }

import axios from "axios";
import { env } from "../config/env";
import { logger } from "../utils/logger";
import { BoundedQueue } from "../utils/boundedQueue";
import { IntentClassification } from "../types";

// Reuse the same bounded-queue pattern as the old ollama.service so bursts
// don't flood the Python server. The Python server is single-threaded by
// default (one uvicorn worker), so maxConcurrency=1 is still correct for a
// CPU-only machine.
const classifierQueue = new BoundedQueue(
  env.classifier.maxConcurrency,
  env.classifier.maxQueueSize
);

export { QueueFullError } from "../utils/boundedQueue";

export function getQueueDepth() {
  return { running: classifierQueue.running, pending: classifierQueue.pending };
}

interface ClassifierResponse {
  lead: 0 | 1;
  confidence: number;
}

/**
 * Classify a post using the fine-tuned SEQ_CLS head.
 *
 * Returns IntentClassification so the rest of the pipeline (ingest.controller)
 * is unchanged — only this service and env.ts were modified.
 */
export async function classifyIntent(title: string, text: string): Promise<IntentClassification> {
  if (classifierQueue.pending > 0) {
    logger.warn({ pending: classifierQueue.pending }, "Classifier queue backed up, request will wait");
  }
  return classifierQueue.add(() => callClassifier(title, text));
}

async function callClassifier(title: string, text: string): Promise<IntentClassification> {
  try {
    const { data } = await axios.post<ClassifierResponse>(
      `${env.classifier.baseUrl}/classify`,
      { title, text },
      { timeout: env.classifier.timeoutMs }
    );

    if (data.lead !== 0 && data.lead !== 1) {
      logger.warn({ data }, "Classifier returned unexpected lead value, defaulting to isLead=false");
      return { isLead: false, reason: "Unexpected classifier response" };
    }

    const isLead = data.lead === 1;
    const reason = isLead
      ? `Classified as lead (confidence ${(data.confidence * 100).toFixed(1)}%)`
      : `Not a lead (confidence ${(data.confidence * 100).toFixed(1)}%)`;

    logger.info({ isLead, confidence: data.confidence }, "Classifier result");
    return { isLead, reason };
  } catch (err) {
    logger.error({ err }, "Classifier request failed");
    throw new Error("Intent classification service unavailable");
  }
}

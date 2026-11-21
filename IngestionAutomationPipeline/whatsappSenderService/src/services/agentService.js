// src/services/agentService.js
const axios = require("axios");
const { FASTAPI_URL, INTERNAL_API_KEY } = require("../config/env");  // ← added INTERNAL_API_KEY

const TIMEOUT_MS = 3000;

async function getAgentReply(phone, message) {
  if (!FASTAPI_URL) {
    console.error("[agentService] FASTAPI_URL is not set in environment.");
    return "AI bot is facing some issues.";
  }

  try {
    await axios.post(
      FASTAPI_URL,
      { phone, message },
      {
        timeout: TIMEOUT_MS,
        headers: {
          "X-Api-Key": INTERNAL_API_KEY,   // ← added
        },
      }
    );
    return null;
  } catch (err) {
    if (err.code === "ECONNABORTED") {
      console.warn(
        `[agentService] Request to ${FASTAPI_URL} timed out after ${TIMEOUT_MS} ms. ` +
        "The agent may still be processing — no user-facing error sent."
      );
      return null;
    }

    if (err.response) {
      // Specific warning for auth failures so misconfigured keys are obvious in logs
      if (err.response.status === 401) {
        console.error(
          "[agentService] FastAPI rejected the request — INTERNAL_API_KEY mismatch. " +
          "Check that both services have the same key in their .env files."
        );
      } else {
        console.error(
          `[agentService] FastAPI returned HTTP ${err.response.status}:`,
          err.response.data
        );
      }
    } else if (err.request) {
      console.error(
        `[agentService] No response from ${FASTAPI_URL}:`,
        err.message
      );
    } else {
      console.error("[agentService] Unexpected error:", err.message);
    }

    return "AI bot is facing some issues.";
  }
}

module.exports = { getAgentReply };

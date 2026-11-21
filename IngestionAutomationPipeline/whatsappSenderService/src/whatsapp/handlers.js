// src/whatsapp/handlers.js
const { getAgentReply } = require("../services/agentService");

// FIX: Use the single canonical text-extraction helper from socket.js.
// The duplicate `extractMessageText` that used to live here has been removed.
const { extractMessageText } = require("./socket");

/**
 * Convert a bare phone number to a WhatsApp JID.
 * Handles Pakistani numbers in both 03xx and 92xx format.
 */
function formatJid(number) {
  if (!number) return null;

  let raw = number.toString().replace(/\D/g, "");

  // 03xxxxxxxxx → 923xxxxxxxxx
  if (raw.startsWith("0") && raw.length === 11) {
    raw = "92" + raw.slice(1);
  }

  if (raw.startsWith("92") && raw.length === 12) {
    return `${raw}@s.whatsapp.net`;
  }

  return null;
}

/**
 * Send a WhatsApp message through the live socket.
 */
async function sendMessage(phone, text) {
  try {
    const { getSock } = require("./socket");
    const sock = getSock();

    const jid = formatJid(phone);
    if (!jid) {
      console.error("❌ Invalid phone number:", phone);
      return false;
    }

    if (!text) return false;

    await sock.sendMessage(jid, { text });
    console.log(`✅ Sent message to ${jid}`);
    return true;
  } catch (err) {
    console.error("❌ Failed to send message:", err.message);
    return false;
  }
}

/**
 * Entry point for every incoming WhatsApp message.
 *
 * Flow:
 *   1. Forward the message to the FastAPI agent service.
 *   2. If the agent service returns an error string, send it back immediately.
 *      (Normally the agent replies itself via the FastAPI → WhatsApp route.)
 */
async function handleIncomingMessage({ phoneNumber, text }) {
  try {
    if (!phoneNumber || !text) return;

    console.log(`📩 Message from ${phoneNumber}: ${text}`);

    const errorMessage = await getAgentReply(phoneNumber, text);

    // `errorMessage` is non-null only when the agent service is unreachable
    if (errorMessage) {
      await sendMessage(phoneNumber, errorMessage);
    }
  } catch (err) {
    console.error("❌ Handler error:", err.message);
    try {
      await sendMessage(phoneNumber, "⚠️ AI bot is facing some issues.");
    } catch (_) {}
  }
}

module.exports = {
  handleIncomingMessage,
  sendMessage,
};

// src/whatsapp/socket.js
const path = require("path");
const qrcodeTerminal = require("qrcode-terminal");
const QRCode = require("qrcode");
const {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
} = require("@whiskeysockets/baileys");
const { delay } = require("../utils/delay");

let sock;
let isInitializing = false;
let waiters = [];

// FIX: Keep a short-lived set of recently-seen message IDs to deduplicate
// messages that WhatsApp re-delivers after a reconnect.
const seenMessageIds = new Set();
const SEEN_TTL_MS = 5 * 60 * 1000; // 5 minutes

function markSeen(msgId) {
  seenMessageIds.add(msgId);
  setTimeout(() => seenMessageIds.delete(msgId), SEEN_TTL_MS);
}

/**
 * Returns true only for individual user JIDs (not groups / broadcasts / LIDs).
 */
const isUserJid = (jid) => {
  if (!jid) return false;
  return jid.endsWith("@s.whatsapp.net") || jid.endsWith("@lid");
};

/**
 * Extract the plain text from any supported message type.
 * Single canonical implementation — handlers.js imports this instead of
 * duplicating it.
 */
function extractMessageText(msg) {
  if (!msg?.message) return "";
  return (
    msg.message.conversation ||
    msg.message.extendedTextMessage?.text ||
    msg.message.imageMessage?.caption ||
    msg.message.videoMessage?.caption ||
    msg.message.documentMessage?.caption ||
    msg.message.buttonsResponseMessage?.selectedButtonId ||
    msg.message.listResponseMessage?.singleSelectReply?.selectedRowId ||
    msg.message.templateButtonReplyMessage?.selectedId ||
    ""
  );
}

/**
 * Initialize the WhatsApp bot and return the socket.
 * Subsequent calls return the same socket (or wait for it to finish init).
 */
async function initWhatsAppBot() {
  if (sock) return sock;
  if (isInitializing) return waitForSock();

  isInitializing = true;

  const authFolder = path.join(__dirname, "auth_session_stable");
  const { state, saveCreds } = await useMultiFileAuthState(authFolder);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    defaultQueryTimeoutMs: undefined,
  });

  // Persist session credentials automatically
  sock.ev.on("creds.update", saveCreds);

  // ── Connection lifecycle ─────────────────────────────────────────────────
  sock.ev.on("connection.update", async ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      qrcodeTerminal.generate(qr, { small: true });
      try {
        await QRCode.toFile("/tmp/whatsapp-qr.png", qr, { type: "png", width: 300 });
        console.log("📂 QR code saved to /tmp/whatsapp-qr.png");
      } catch (err) {
        console.error("⚠️  QR Save Error:", err.message);
      }
    }

    if (connection === "close") {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

      if (shouldReconnect) {
        console.log("🔄 Connection dropped. Reconnecting in 5 s…");
        await delay(5000);
        sock = undefined;
        isInitializing = false;
        await initWhatsAppBot();
      } else {
        console.log("❌ Logged out. Delete auth_session_stable and restart.");
        process.exit(1);
      }
    }

    if (connection === "open") {
      console.log("✅ Connected to WhatsApp.");
      isInitializing = false;
      resolveWaiters(sock);
    }
  });

  // ── Incoming messages ────────────────────────────────────────────────────
  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;

    for (const msg of messages) {
      if (!msg.message) continue;
      if (msg.key.fromMe) continue;

      // FIX: Deduplicate messages WhatsApp re-delivers after reconnect
      const msgId = msg.key.id;
      if (!msgId || seenMessageIds.has(msgId)) continue;
      markSeen(msgId);

      // ── JID resolution ──────────────────────────────────────────────────
      let jid = msg.key.remoteJid;
      const altJid = msg.key.remoteJidAlt;

      // Prefer a proper @s.whatsapp.net JID over an @lid JID when both exist
      if (jid?.includes("@lid") && altJid?.includes("@s.whatsapp.net")) {
        jid = altJid;
      } else if (!jid && altJid) {
        jid = altJid;
      }

      if (!jid || !isUserJid(jid)) continue;

      const phoneOrId = jid.split("@")[0];

      // ── Text extraction ─────────────────────────────────────────────────
      // FIX: Use the single shared helper — no more duplicate implementations
      const text = extractMessageText(msg);
      if (!text.trim()) continue;

      console.log(`📩 Message from ${phoneOrId}: "${text}"`);

      try {
        const { handleIncomingMessage } = require("./handlers");
        await handleIncomingMessage({ phoneNumber: phoneOrId, text, raw: msg });
      } catch (err) {
        console.error("❌ Error processing message:", err);
      }
    }
  });

  // Note: resolveWaiters is called inside the "open" handler above.
  // We still return here so the caller that triggered init gets the socket.
  return waitForSock();
}

function waitForSock() {
  return new Promise((resolve) => waiters.push(resolve));
}

function resolveWaiters(resolvedSock) {
  waiters.forEach((r) => r(resolvedSock));
  waiters = [];
}

/**
 * Returns the initialized socket.  Throws if called before initWhatsAppBot().
 */
function getSock() {
  if (!sock) {
    throw new Error("WhatsApp socket not initialized. Call initWhatsAppBot() first.");
  }
  return sock;
}

module.exports = { initWhatsAppBot, getSock, extractMessageText };

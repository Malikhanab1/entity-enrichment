// src/routes/sendMessage.js
const express = require("express");
const router = express.Router();
const { sendMessage } = require("../whatsapp/handlers");
const { INTERNAL_API_KEY } = require("../config/env"); 
router.post("/", async (req, res) => {
  const incomingKey = req.headers["x-api-key"];
  if (!incomingKey || incomingKey !== INTERNAL_API_KEY) {
    return res.status(401).json({ error: "Unauthorized" });
  }

  const { getSock } = require("../whatsapp/socket");
  try {
    getSock(); // throws if not initialized
  } catch (_) {
    return res.status(503).json({ error: "WhatsApp socket not ready yet. Retry shortly." });
  }

  const { phone, text } = req.body;
  if (!phone || !text) {
    return res.status(400).json({ error: "phone and text are required" });
  }

  try {
    const sent = await sendMessage(phone, text);
    if (!sent) {
      return res.status(500).json({ error: "Failed to send message — check phone number format." });
    }
    res.json({ status: "sent", phone, text });
  } catch (err) {
    console.error("Error in /sendMessage route:", err);
    res.status(500).json({ error: "Internal server error." });
  }
});

module.exports = router;

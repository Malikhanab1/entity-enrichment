// src/index.js
const app = require("./app");
const { PORT } = require("./config/env");
const { initWhatsAppBot } = require("./whatsapp/socket");

app.listen(PORT, () => {
  console.log(`Express server running on port ${PORT}`);
});

// Initialize WhatsApp bot asynchronously
(async () => {
  try {
    const sock = await initWhatsAppBot();
    console.log("WhatsApp bot initialized successfully.");
  } catch (err) {
    console.error("❌ Failed to initialize WhatsApp bot:", err.message);
    process.exit(1); // Exit process if WhatsApp session fails
  }
})();

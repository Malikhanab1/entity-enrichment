const express = require("express");
const healthRoute = require("./routes/health");
const sendMessageRoute = require("./routes/sendMessage");

const app = express();
app.use(express.json());
app.use("/sendMessage", sendMessageRoute);
app.use("/health", healthRoute);

module.exports = app;

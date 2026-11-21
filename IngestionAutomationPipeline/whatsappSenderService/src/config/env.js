require("dotenv").config();

module.exports = {
  PORT: process.env.PORT || 3000,
  FASTAPI_URL: process.env.FASTAPI_URL || "http://localhost:8000/chat",
  FASTAPI_LOG_URL: process.env.FASTAPI_LOG_URL || "http://localhost:8000/db/log_message",
  INTERNAL_API_KEY: process.env.INTERNAL_API_KEY || "dummy"
};

# DASBot WhatsApp gateway (`apps/whatsapp`)

Production-style **WhatsApp Web** client using [**Baileys**](https://github.com/WhiskeySockets/Baileys) plus a small **Express** server. This process owns the live socket to WhatsApp; it does **not** talk to Supabase or run the LLM. All intelligence lives in [`apps/api`](../api/).

---

## Responsibilities

| Concern | Owner |
|---------|--------|
| Receive WhatsApp messages | This app (Baileys) |
| Forward `{ phone, message }` to the agent | `agentService.js` → FastAPI `POST /chat` |
| Deliver outbound text to users | `POST /sendMessage` (called by FastAPI) |
| Conversation memory, tools, DB | FastAPI only |

---

## Run locally

```bash
cd apps/whatsapp
npm install
npm start
```

Equivalent:

```bash
node src/index.js
```

- **Express** listens on `PORT` (default **3000**): health check and authenticated outbound route.
- **Baileys** starts in the same process; scan the **QR** in the terminal on first login (session is persisted per your `socket` / auth config).

---

## Environment variables

Create `.env` in `apps/whatsapp/` (loaded via `dotenv` in `src/config/env.js`).

| Variable | Default (if unset) | Purpose |
|----------|-------------------|---------|
| `PORT` | `3000` | Express listen port |
| `FASTAPI_URL` | `http://localhost:8000/chat` | Agent ingress (must include path `/chat`) |
| `FASTAPI_LOG_URL` | `http://localhost:8000/db/log_message` | Optional logging endpoint (if used) |
| `INTERNAL_API_KEY` | `dummy` | **Must match** `apps/api` `INTERNAL_API_KEY` for `/chat` and for inbound `/sendMessage` |

**Critical:** Use the same `INTERNAL_API_KEY` in both services. A mismatch produces HTTP **401** on `/chat` and failed outbound sends from the API.

---

## HTTP surface

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | — | Liveness JSON |
| `POST` | `/sendMessage` | `X-Api-Key` | Body: `{ "phone", "text" }` — sends via Baileys |

FastAPI calls `/sendMessage` with `X-Api-Key` and JSON `{ phone, text }` (see `apps/api` `WHATSAPP_SERVICE_URL`).

---

## Code map

```text
src/
├── index.js              # Starts Express + initializes WhatsApp
├── app.js                # Express app: /health, /sendMessage
├── config/env.js         # Environment
├── services/agentService.js   # axios POST to FASTAPI_URL + X-Api-Key
├── whatsapp/
│   ├── socket.js         # Baileys connection, events, JID helpers
│   └── handlers.js       # Incoming messages → getAgentReply; optional error reply to user
└── routes/
    ├── health.js
    └── sendMessage.js    # Authenticated outbound send
```

### Inbound flow

```text
WhatsApp user
    → Baileys (socket.js)
    → handlers.handleIncomingMessage
    → agentService.getAgentReply(phone, message)  →  POST FastAPI /chat
```

If the API is unreachable, `getAgentReply` may return a short error string; the handler sends that to the user. Under normal operation the **agent** sends replies through **`POST /sendMessage`**, not through the return value of `/chat`.

### Phone numbers

Handlers normalize Pakistani-style numbers (`03…` → `923…` JID) in `handlers.js` (`formatJid`). Keep API and gateway consistent with the digits you store in Supabase for escalation matching.

---

## Dependencies (npm)

- `@whiskeysockets/baileys` — WhatsApp Web protocol
- `express` — HTTP server for health and outbound API
- `axios` — Call FastAPI
- `dotenv` — Configuration
- `qrcode` / `qrcode-terminal` — QR login UX

---

## Operations notes

- Run **FastAPI** before or alongside this service so `/chat` accepts traffic.
- Outbound **`/sendMessage`** returns **503** if the Baileys socket is not ready yet; callers can retry.
- Do not commit real session secrets or `.env` files to version control.

---

## See also

- [Root README](../../README.md) — full architecture
- [`apps/api/README.md`](../api/README.md) — API run command, env, escalation

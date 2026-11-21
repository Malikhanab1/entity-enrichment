# lead-ingest-service

Microservice that other services `POST` raw text posts to. It decides whether the
post is a genuine lead (someone wanting a website/automation built), extracts a
contact channel, assigns it to a sales-team member (evenly, round-robin, per role),
and forwards a message to your messaging microservice.

## Pipeline

```
POST /ingest
   │
   ▼
1. Dedupe check (cache, keyed by source+user+title+text hash)
   │  duplicate → return { status: "duplicate" }
   ▼
2. Ollama classification: "is this a real lead?"
   │  no → return { status: "discarded", reason }
   ▼
3. Regex extraction: phone / dm-signal / email
   precedence: call > dm > mail
   ▼
4. Round-robin assignment within matching sales-team role
   (caller / DM / email)
   ▼
5. POST { message, number } to MESSAGING_SERVICE_URL
   ▼
   return { status: "assigned", leadId, assignedTo }
```

## Setup

```bash
cp .env.example .env   # edit values, especially PORT, OLLAMA_*, MESSAGING_SERVICE_URL
npm install
npm run dev             # ts-node-dev, hot reload
# or
npm run build && npm start
```

## Request contract

```
POST /ingest
Content-Type: application/json

{
  "title": "string",
  "text": "string",
  "source": "string",
  "posted_by": "string",
  "user": "string",
  "group": "string",
  "posted_at": "ISO-8601 string"
}
```

Responses:

| status      | meaning                                             |
|-------------|------------------------------------------------------|
| `duplicate` | Same post already processed within the cache TTL     |
| `discarded` | Ollama decided this is not a website/automation lead |
| `assigned`  | Lead extracted, assigned, and dispatched              |

## Protecting Ollama from being flooded

A local, CPU-bound model doesn't parallelize — throwing concurrent requests
at it just makes each one slower until they start timing out together. All
calls to Ollama go through a small in-process bounded queue
(`src/utils/boundedQueue.ts`):

- `OLLAMA_MAX_CONCURRENCY` — how many inferences run at once. **Leave this
  at 1** for a single CPU-only local model; raise it only if you have a
  GPU or multiple model instances that can genuinely run in parallel.
- `OLLAMA_MAX_QUEUE_SIZE` — how many requests may wait in line once the
  model is busy. Once the backlog hits this size, new `/ingest` calls get
  an immediate `429 { error: "Classification service is busy..." }`
  instead of queuing indefinitely — callers (your other microservices)
  should treat that as "retry with backoff," not a hard failure.
- `GET /health` reports current queue depth (`{ ollamaQueue: { running, pending } }`)
  so you can see backpressure building before it becomes a problem.

If a request fails at any stage after the dedupe key is reserved (queue
full, Ollama unreachable, assignment failure, messaging failure), the
dedupe cache entry is released so a legitimate retry of the same post
isn't permanently — and incorrectly — treated as a duplicate.

## Database (SQLite)

Every processed request is persisted to SQLite (`DB_FILE`, default `./data/leads.db`,
via `better-sqlite3`), regardless of outcome:

- **`leads`** — one row per request: full payload, `status` (discarded/duplicate/assigned),
  extracted `channel`/`contact_value`, and who it was assigned to. `fingerprint` is
  `UNIQUE` and is the durable dedupe guard (the in-memory cache is just a fast path —
  SQLite backs it up across restarts and cache TTL expiry).
- **`assignment_counters`** — the round-robin pointer per role (`caller`/`DM`/`email`),
  updated inside a transaction so concurrent requests never double-assign the same slot.

Schema is created automatically on boot (`src/db/database.ts`), no separate migration
step needed. Inspect it directly:

```bash
sqlite3 data/leads.db "select status, count(*) from leads group by status;"
```

or hit `GET /stats` for the same breakdown over HTTP.

## Messaging service contract (apps/whatsapp)

This service is written to match the `apps/whatsapp` gateway's `/sendMessage`
endpoint exactly:

```
POST {MESSAGING_SERVICE_URL}      # e.g. http://localhost:3000/sendMessage
Header: X-Api-Key: {MESSAGING_SERVICE_API_KEY}
Body:   { "phone": "923001234567", "text": "..." }
```

Two things you must get right or every send fails:
1. **`MESSAGING_SERVICE_API_KEY` must equal `INTERNAL_API_KEY`** in the
   WhatsApp gateway's own `.env`. A mismatch is a `401` and this service
   won't retry it (no backoff will fix a wrong key).
2. **`MESSAGING_SERVICE_URL` must include the path**, i.e. end in
   `/sendMessage`, not just the host.

If the gateway responds `503` (its Baileys socket isn't authenticated yet —
this is documented behaviour on their side), this service retries up to
`MESSAGING_SERVICE_MAX_RETRIES` times with a short linear backoff before
giving up and returning a `502` to the original caller.

## Sales team roster

Configured via `SALES_TEAM_FILE` (`.json` or `.csv`, auto-detected by extension).

`src/data/salesTeam.json` (default, JSON):
```json
[
  { "name": "Ali Raza", "contactNumber": "+923001234567", "role": "caller" },
  { "name": "Bilal Ahmed", "contactNumber": "+923009998887", "role": "DM" },
  { "name": "Usman Tariq", "contactNumber": "usman@salesteam.com", "role": "email" }
]
```

Or CSV, headers: `name,contactNumber,role`. `role` must be one of `DM`, `caller`, `email`.

Assignment is round-robin **per role**, and state is persisted to
`ASSIGNMENT_STATE_FILE` so restarts don't reset fairness.

## Contact extraction rules

- **Phone number found** → channel = `call` (uses the first plausible number).
- Else **DM/inbox/message-me phrasing found** → channel = `dm`.
- Else **email found** → channel = `mail`.
- If nothing is found at all, defaults to `dm` as the lowest-friction fallback
  (tune this in `extraction.service.ts` if you'd rather discard those instead).

Precedence when multiple are present: **call > dm > mail**.

## Extending

- **Cache**: `services/cache.service.ts` wraps `node-cache`; swap the internals
  for Redis/Memcached without touching any caller.
- **LLM**: `services/ollama.service.ts` isolates the Ollama HTTP call and prompt;
  swap models via `.env`, or replace the whole module to call a different provider.
- **Messaging service contract**: `services/messaging.service.ts` — currently
  `POST { message, number }` to `MESSAGING_SERVICE_URL`.

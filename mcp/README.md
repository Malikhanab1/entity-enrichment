# Agentic Business Research MCP

Evidence-led MCP tools for discovering and enriching businesses. The agent
chooses what to investigate and when to stop; the server does not automatically
crawl every result or run a fixed social-media sweep.

## Agent-directed workflow

Use the smallest next action that answers the agent's current question:

```
find_businesses → assess_business → crawl_business_pages → synthesize_business_profile
  discovery        identity/site       selected evidence           structured entity
```

`find_decision_makers` is optional, only when a named contact is needed.
`qualify_lead` remains available for lead scoring. The original four tools are
kept for compatibility, but new agents should use the composable flow.

1. `find_businesses(need, location)` searches only. Select a candidate; do not
   crawl all results.
2. `assess_business(business_name, location)` evaluates public evidence and
   returns a ranked `recommended_website` (or explicitly says one was not
   found). It does not crawl.
3. `crawl_business_pages(website)` crawls only the homepage and returns
   ranked same-site `recommended_pages`. The agent then calls it again with
   only the pages relevant to its goal, such as leadership, staff, or contact.
4. `synthesize_business_profile(website_pages)` converts the pages the agent
   chose into a structured entity profile without any network access.

This makes “does it have a website?” a cheap assessment question, while a
full owner/contact investigation is an intentional follow-up.

---

## Legacy four-step lead pipeline

### Step 1 — `find_leads`

> *"Find businesses by niche and location that might need my services."*

| Param | Type | Description |
|---|---|---|
| `niche` | str | Industry, e.g. `"law firm"`, `"logistics company"` |
| `location` | str | City or region, e.g. `"Lahore"`, `"Dubai"` |
| `num_results` | int | How many candidates to return (default 10, max 20) |
| `require_website` | bool | Only return businesses with a discoverable website |

Returns a ranked list of lead candidates with name, website URL, and snippet.
The tool also ships a `suggested_niches` list so the agent knows what industries
to target without guessing.

### Step 2 — `enrich_business`

> *"Get the full picture on this specific business."*

| Param | Type | Description |
|---|---|---|
| `business_name` | str | Exact name from Step 1 |
| `website` | str &#124; None | Website URL from Step 1 (tool discovers if omitted) |
| `location` | str &#124; None | For disambiguation |
| `skip_social` | bool | Skip LinkedIn/Facebook (faster) |

Returns a structured `profile` (description, services, address, phone, email,
industry) plus raw crawl data you can pass into Step 3.

### Step 3 — `find_decision_makers`

> *"Who do I actually email?"*

| Param | Type | Description |
|---|---|---|
| `business_name` | str | From Step 2 |
| `website` | str &#124; None | From Step 2 |
| `location` | str &#124; None | For disambiguation |
| `website_pages` | list &#124; None | `result["raw_website"]["pages"]` from Step 2 — avoids re-crawl |

Searches Google and LinkedIn for founders, owners, CEOs, and directors.
Returns a list of decision-makers with name, title, LinkedIn URL, email hint,
and confidence level (`high` / `medium` / `low`). Also extracts all emails and
phone numbers from the website and guesses the company's email pattern.

### Step 4 — `qualify_lead`

> *"Is this worth pursuing, and how do I pitch them?"*

| Param | Type | Description |
|---|---|---|
| `business_name` | str | From Step 3 |
| `website` | str &#124; None | From Step 2 |
| `profile` | dict &#124; None | `result["profile"]` from Step 2 |
| `decision_makers` | list &#124; None | `result["decision_makers"]` from Step 3 |
| `contact_hints` | dict &#124; None | `result["contact_hints"]` from Step 3 |
| `raw_website_text` | str &#124; None | All page text joined with spaces |

Produces a **0–100 priority score**, a breakdown of scoring signals, detected
pain points (no website, uses spreadsheets, manual processes), buying signals
(hiring, expanding), and a **ready-to-send outreach message** personalised to
the business and decision-maker.

---

## Typical agent conversation

```
User: Find law firms in Lahore that might need a new website.

Agent →  find_leads(niche="law firm", location="Lahore")
         # Returns 8 candidates

Agent →  enrich_business(business_name="Ali & Co Law Associates",
                          website="https://aliandco.com.pk")
         # Returns profile + raw pages

Agent →  find_decision_makers(business_name="Ali & Co Law Associates",
                               website="https://aliandco.com.pk",
                               website_pages=<from step 2>)
         # Returns: Imran Ali, Managing Partner, linkedin.com/in/imran-ali-law

Agent →  qualify_lead(business_name="Ali & Co Law Associates",
                       website="https://aliandco.com.pk",
                       profile=<from step 2>,
                       decision_makers=<from step 3>,
                       contact_hints=<from step 3>)
         # Returns: score=72, priority="hot", outreach_summary="Hi Imran, ..."
```

---

## Setup

```bash
cd enrichment-mcp
uv sync
uv run playwright install chromium
./run.sh
```

The default transport is **stdio** (for Claude Desktop / agent MCP clients).

### Search providers

SearXNG is not required. Add at least one of `EXA_API_KEY`,
`FIRECRAWL_API_KEY`, or `TAVILY_API_KEY` to `mcp/.env` (copy
`.env.example` for a clean setup). At startup, Firecrawl and Tavily credit
balances are checked concurrently. The router sends each query to the
configured provider with the highest known remaining balance, persists quota
and cooldown failures in `.search_provider_state.json`, and only fails over
after a real request fails. Exa is validated on its first real search because
it does not provide a stable public credit-usage endpoint.

### Sandboxed workspace tools

The MCP also exposes bounded file tools for Markdown/text, CSV/TSV, XLSX, and
legacy XLS: listing and searching files, metadata, text reads, exact text
replacements, text creation, spreadsheet reads/writes, and safe file copies.
They operate only below the dedicated `workspace/` folder at the repository
root. Path traversal and symlinks leading out of that folder are rejected.
Reads and writes have configurable size/row/result limits, and replacing an
existing file requires `overwrite=true`. Set `MCP_FILE_WORKSPACE` in `mcp/.env`
to choose another dedicated folder; relative paths are resolved from `mcp/`.

### HTTP transport

Set in `.env`:

```dotenv
ENRICHMENT_MCP_TRANSPORT=streamable-http
ENRICHMENT_MCP_HOST=127.0.0.1
ENRICHMENT_MCP_PORT=8000
ENRICHMENT_MCP_PATH=/mcp
```

### MCP Inspector (testing)

```bash
npx @modelcontextprotocol/inspector uv run enrichment-mcp
```

Call `find_leads` first:
```json
{"niche": "dental clinic", "location": "Lahore"}
```

---

## Crawl policy

The server only reads **publicly reachable pages**. It never logs in, solves
CAPTCHAs, rotates proxies, or bypasses access controls. Single worker, 4–7 s
random delay between requests, one retry max. All limits are in `.env`.

---

## Project structure

```
src/enrichment_mcp/
├── server.py                    # MCP server + tool registration
├── tools/
│   ├── find_leads.py            # Step 1: discover candidates
│   ├── enrich_business.py       # Step 2: deep-dive one business
│   ├── find_decision_makers.py  # Step 3: surface owner / contacts
│   └── qualify_lead.py          # Step 4: score + outreach summary
├── crawlers/
│   ├── _common.py               # Conservative Playwright primitives
│   ├── google.py                # Exa / Firecrawl / Tavily search router
│   ├── website.py               # Same-origin website crawler
│   ├── linkedin.py              # Public LinkedIn company page
│   └── facebook.py              # Public Facebook business page
└── extractors/
    ├── leads.py                 # Parse Google results → lead candidates
    ├── business.py              # Merge crawl data → structured profile
    └── contacts.py              # Extract emails, phones, email pattern
```

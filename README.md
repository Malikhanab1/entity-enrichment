# Entity Enrichment

An end-to-end lead generation and business enrichment toolkit. It collects raw leads from social platforms and the web, enriches them with contact and company data, scores and qualifies them, and supports automated outreach.

## What's inside

| Folder | Purpose |
|---|---|
| `IngestionAutomationPipeline/` | Multi-service pipeline that scrapes, classifies and routes incoming leads |
| `mcp/` | MCP server exposing enrichment tools (business enrichment, decision-maker search, lead scoring, qualification, outreach email) |
| `agent/` | Enrichment agent with a Streamlit UI |
| `isolated_crawler/` | Standalone website crawler for email extraction, tech detection and lead scoring |
| `mvp/` | Lightweight CRM, database and ingestion layer |
| `workspace/` | Working directory for generated files |

### Ingestion pipeline

- **FbScraper** (Go): Facebook group scraper with an MCP interface
- **RedditScraper** (Python): subreddit scraper with scheduling and state tracking
- **leadIngestion** (TypeScript/Node): ingests posts, classifies and extracts lead data with an LLM, assigns leads to sales team members
- **whatsappSenderService** (Node): sends WhatsApp messages to assigned leads
- **finetuning**: fine-tuning and serving scripts for a local model (Ollama compatible)

### MCP enrichment tools

- Business enrichment and website crawling
- Decision-maker discovery
- Lead finding, scoring and qualification
- Outreach email generation and sending
- Search and crawl sources: Google, LinkedIn, Facebook, company websites

## Tech stack

Python (uv), TypeScript/Node.js, Go, Streamlit, SQLite, Ollama, MCP.

## Getting started

```bash
git clone https://github.com/Malikhanab1/entity-enrichment.git
cd entity-enrichment
```

Each component is self-contained. Copy its `.env.example` to `.env`, fill in your own keys, and follow the README inside that folder.

```bash
# Example: MCP server
cd mcp
cp .env.example .env     # add EXA_API_KEY, FIRECRAWL_API_KEY, TAVILY_API_KEY
./run.sh

# Example: enrichment agent UI
cd agent
uv sync
uv run streamlit run streamlit_app.py

# Example: run the whole ingestion pipeline
cd IngestionAutomationPipeline
./run-all.sh
```

## Configuration

API keys and service URLs live in per-folder `.env` files. Never commit real keys. Only `.env.example` files are tracked.

## Disclaimer

Scraping and automated outreach must comply with the terms of service of each platform and with local data-protection and anti-spam laws. Use responsibly.

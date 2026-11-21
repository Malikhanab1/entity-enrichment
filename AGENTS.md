# Repository guide

## Purpose

This repository contains a business-enrichment MCP server and a Groq-backed
CLI agent:

- `mcp/` — MCP server for business discovery, enrichment, contact research,
  lead qualification, search-provider routing, and bounded workspace files.
- `agent/` — LangChain/LangGraph CLI client that launches the MCP over stdio.
- `workspace/` — the only directory that MCP file tools may read or modify.
  Treat it as user data; do not write elsewhere through file tools.

## Current architecture

### Search

`mcp/src/enrichment_mcp/crawlers/google.py` retains its historic import name,
but it is not Google or SearXNG. It routes each query to Exa, Firecrawl, or
Tavily using a common result schema.

- Keys belong in `mcp/.env`: `EXA_API_KEY`, `FIRECRAWL_API_KEY`, and
  `TAVILY_API_KEY`. At least one is required for live search.
- Tavily and Firecrawl remaining-credit APIs are refreshed concurrently once
  per MCP process. The provider with the largest known balance is selected.
- Exa does not have a stable public usage endpoint, so it is checked by the
  first real query. Quota/auth failures and temporary cooldowns are persisted
  in `.search_provider_state.json` (ignored by Git). A depleted Exa key is
  eligible for a recheck after `SEARCH_EXHAUSTED_RECHECK_SECONDS`.
- Search query groups in `find_leads` and `enrich_business` run concurrently;
  the router does not fan one query out to every provider, preserving credits.
- `search_google` is a compatibility name. Do not reintroduce SearXNG.

The retired `searxng/` directory is not used. One root-owned legacy settings
file may remain because the workspace mount denied deletion; it is inert.

### Agent behavior

The CLI is `agent/main.py` and reads `agent/.env`.

- `MCP_SERVER_PATH` should point to `mcp/run.sh`.
- `AGENT_MAX_TOKENS` defaults to 512 and has a 150-token minimum. Matching is
  performed in MCP tools, not by relying on long final model output.
- The system prompt deliberately uses the smallest tool sequence: enrich a
  named business first; seek contacts only when requested; score/outreach only
  when requested; for general searches, return candidates before deep research.
- Keep tool results compact. Do not restore the old mandatory 3–4 tool chain
  for every user request.

### Workspace file tools

`mcp/src/enrichment_mcp/tools/workspace_files.py` registers these MCP tools:

- `list_files`, `file_info`, `search_files`, `read_text_file`
- `write_text_file`, `replace_in_text_file`, `copy_workspace_file`
- `read_spreadsheet`, `write_spreadsheet`

Supported formats are UTF-8 text/Markdown/source files, CSV/TSV, XLSX, and
legacy XLS. Excel support uses `openpyxl`, `xlrd`, and `xlwt`.

Safety rules:

- `MCP_FILE_WORKSPACE` is configured in `mcp/.env` as `../workspace`; relative
  values are anchored to `mcp/`, not the process cwd.
- Reject paths outside that folder, `..` traversal, and symlinks that resolve
  outside the folder.
- Keep read/search/write limits controlled by `MCP_FILE_MAX_BYTES` and
  `MCP_FILE_MAX_RESULTS`.
- Existing files require `overwrite=true`; exact replacements can enforce an
  expected replacement count. Do not add arbitrary shell execution or broad
  deletion tools without explicit user authorization.

## Local development

```bash
cd mcp
uv sync
uv run enrichment-mcp
```

Run the agent from `agent/` with its Groq keys and `MCP_SERVER_PATH` set.
Do not put secrets in source, test fixtures, output, or documentation. Copy
`mcp/.env.example` to configure a fresh MCP environment.

## Verification

After changing MCP code, run:

```bash
uv run --project mcp python -m unittest discover -s mcp/tests -v
uv run --project mcp python -m compileall -q mcp/src
uv run --project agent python -m compileall -q agent
git diff --check
```

The current suite includes search-router persistence/selection tests, business
selection tests, and text/CSV/XLSX workspace-tool tests.

## Change discipline

- Preserve the stable `search_google` compatibility API and its return shape.
- Use `asyncio.gather` only for independent query variants; do not spend all
  providers' credits for a single search.
- Load `mcp/.env` using its explicit source-relative path because stdio MCP
  processes may be started from `agent/`.
- The repository currently has untracked content. Treat existing files and
  changes as user-owned; inspect before overwriting or deleting anything.

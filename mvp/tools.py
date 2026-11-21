"""MVP tool registry.

Moves the model-facing, tool-call-safe helpers into one lightweight MCP-ready
layer. The old source-specific sample scrapers stay in the ingestion layer,
while the Streamlit UI and MCP server route through this registry.
"""

from __future__ import annotations

KNOWN_TOOLS = {
    "normalize_business_entity": "mcp.normalize_business_entity",
    "compress_research_context": "mcp.compress_research_context",
    "score_opportunity": "mcp.score_opportunity",
    "build_crm_record": "mcp.build_crm_record",
    "generate_strategy_and_proposal_path": "mcp.generate_strategy_and_proposal_path",
    "crawl_website_for_contacts": "mcp.crawl_website_for_contacts",
    "normalize_ingestion_payload": "mvp.normalize_ingestion_payload",
    "scrape_google_maps": "mvp.scrape_google_maps",
    "score_lead": "mcp.score_lead",
    "populate_lead": "mcp.populate_lead",
    "populate_unpopulated_leads": "mcp.populate_unpopulated_leads",
}


def tool_registry() -> dict[str, str]:
    return dict(KNOWN_TOOLS)

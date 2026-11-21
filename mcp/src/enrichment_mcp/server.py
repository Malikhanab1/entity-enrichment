"""
Lead Generation & Enrichment MCP
=================================
Structured MCP server for finding and qualifying leads for a software
development business (automation + websites).

AGENT WORKFLOW — always follow this order:
  1. find_leads            → discover candidate businesses by niche/location
  2. enrich_business       → get website, social, description for a candidate
  3. find_decision_makers  → surface owner/CEO/CTO names and contacts
  4. qualify_lead          → score the lead and write a summary for outreach

Never jump ahead. Never crawl a random domain you haven't found in step 1.
"""

from __future__ import annotations

import os
import inspect
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from .tools.find_leads import find_leads
from .tools.enrich_business import enrich_business
from .tools.find_decision_makers import find_decision_makers
from .tools.qualify_lead import qualify_lead
from .tools.agentic_research import (
    find_businesses,
    assess_business,
    crawl_business_pages,
    synthesize_business_profile,
    compress_research_context,
    normalize_business_entity,
    plan_research_strategy,
    score_opportunity,
    generate_strategy_and_proposal_path,
)
from .tools.workspace_files import (
    add_spreadsheet_column,
    append_spreadsheet_rows,
    copy_workspace_file,
    file_info,
    list_files,
    read_spreadsheet,
    read_text_file,
    replace_in_text_file,
    search_files,
    update_spreadsheet_rows,
    write_spreadsheet,
    write_text_file,
)
from .tools.contact_store import (
    list_tool_runs,
    record_tool_run,
    search_stored_contacts,
    build_crm_record,
    save_employee,
    ingest_scraper_lead,
)
from .tools.outreach_email import create_email_draft, send_approved_email
from .tools.website_crawler import crawl_website_for_contacts
from .tools.lead_scoring import score_lead
from .tools.populate_lead import populate_lead, populate_unpopulated_leads
from .ingestion_layer import ingest_records

mcp = MCPServer(
    name="lead-gen-enrichment",
    title="Lead Generation & Enrichment",
    description=(
        "Evidence-led business research. Prefer the composable workflow: "
        "find_businesses → assess_business → crawl_business_pages → "
        "synthesize_business_profile. The agent chooses whether to continue at "
        "each step and which recommended page to crawl. Legacy lead tools remain available."
    ),
)

def persisted_tool(function):
    """Wrap every MCP operation so the workspace database is the audit record."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            result = function(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            record_tool_run(function.__name__, kwargs, error=f"{type(exc).__name__}: {exc}")
            raise
        record_tool_run(function.__name__, kwargs, result=result)
        return result
    return wrapped


def register(function) -> None:
    mcp.tool(structured_output=True)(persisted_tool(function))


for _tool in (
    find_leads, enrich_business, find_decision_makers, qualify_lead,
    find_businesses, assess_business, crawl_business_pages,
    synthesize_business_profile, compress_research_context,
    normalize_business_entity, plan_research_strategy,
    score_opportunity, generate_strategy_and_proposal_path,
    crawl_website_for_contacts,
    list_files, file_info, read_text_file, search_files, write_text_file,
    replace_in_text_file, read_spreadsheet, write_spreadsheet,
    add_spreadsheet_column, update_spreadsheet_rows, append_spreadsheet_rows,
    copy_workspace_file, search_stored_contacts, list_tool_runs,
    create_email_draft, send_approved_email, build_crm_record,
    save_employee, score_lead, populate_lead, populate_unpopulated_leads,
    ingest_scraper_lead,
):
    register(_tool)

register(ingest_records)


def main() -> None:
    transport = os.getenv("ENRICHMENT_MCP_TRANSPORT", "stdio").lower()

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=os.getenv("ENRICHMENT_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("ENRICHMENT_MCP_PORT", "8000")),
            streamable_http_path=os.getenv("ENRICHMENT_MCP_PATH", "/mcp"),
            json_response=os.getenv("ENRICHMENT_MCP_JSON_RESPONSE", "false").lower() == "true",
            stateless_http=True,
        )
        return

    raise ValueError(
        f"ENRICHMENT_MCP_TRANSPORT must be 'stdio' or 'streamable-http', not {transport!r}."
    )

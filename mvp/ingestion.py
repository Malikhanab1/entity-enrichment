"""MVP ingestion adapter.

This is the single application-facing bridge for old Reddit/Facebook/Maps
scrapers that previously lived in separate automation folders. The raw
scrapers stay out of the app-facing tool contract and only feed the common
normalization layer here.
"""

from __future__ import annotations

from typing import Any

try:
    from enrichment_mcp.ingestion_layer import IngestionAutomationPipeline, search_google_maps
except Exception:
    IngestionAutomationPipeline = None
    search_google_maps = None


def normalize_ingestion_payload(source: str, payload: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a unified DB-ready record list from any legacy source."""
    if IngestionAutomationPipeline is None:
        raise RuntimeError("Ingestion adapter is not importable from the MCP package")
    return IngestionAutomationPipeline().ingest(source, payload)


def scrape_google_maps(query: str, city: str | None = None, max_results: int = 5) -> list[dict[str, Any]]:
    """Public MVP helper that delegates the actual map-scraper search into
    the shared ingestion normalization plane without revealing the legacy
    pymapscraper folder as a separate product layer.
    """
    if search_google_maps is None:
        raise RuntimeError("Google Maps scraper adapter is not importable from the MCP package")
    return search_google_maps(query, city=city, max_results=max_results)

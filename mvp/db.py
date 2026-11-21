"""Single-database layer for the MVP.

The old repository had isolated artifacts, draft stores, and research stores.
This package provides one simple, importable database contract for the
Streamlit UI and the MCP-aware tool layer. It delegates to the workspace
SQLite database already created by the contact-store implementation.
"""

from __future__ import annotations

from pathlib import Path

from enrichment_mcp.tools.contact_store import _db_path


def single_database_path() -> Path:
    """Return the canonical workspace database path for all research records.

    One consistent file is the end-state desired by the user; this package
    keeps the workspace contact-store as the single source of truth.
    """
    return _db_path()


def single_database_exists() -> bool:
    return single_database_path().exists()

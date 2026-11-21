"""MVP product façade.

This package is deliberately small and final: it provides one importable
surface for the UI and MCP server to route through a single normalized
business-ingestion + CRM + employee-record pathway. Old source-specific
folders remain as external source adapters, not as independent product
layers.
"""

from .crm import crm_record, upsert_employee
from .ingestion import normalize_ingestion_payload
from .tools import tool_registry

__all__ = [
    "tool_registry",
    "crm_record",
    "upsert_employee",
    "normalize_ingestion_payload",
]

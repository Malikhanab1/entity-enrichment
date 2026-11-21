"""
enrichment_agent package entry point.

The CLI script `enrichment-agent` (defined in pyproject.toml) calls `main()`
from this module.  All the actual logic lives in `agent/main.py` at the
project root, which is on sys.path when running via `uv run`.
"""

import asyncio
import sys
import os


def main() -> None:
    """Entry point for `enrichment-agent` CLI script."""
    # agent/main.py sits two levels above this package file:
    #   agent/src/enrichment_agent/__init__.py  →  agent/main.py
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # Import after sys.path adjustment
    from main import main as async_main  # type: ignore[import]

    asyncio.run(async_main())

#!/usr/bin/env bash
# Launch the MCP server from any working directory with its own uv project.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required. Install it from https://docs.astral.sh/uv/." >&2
    exit 1
fi

exec uv run --project "$script_dir" enrichment-mcp

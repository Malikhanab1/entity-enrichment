#!/usr/bin/env bash
# run-poller.sh – build and run the Facebook group poller
# Usage: ./run-poller.sh [--build-only]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[poller]${NC} $*"; }
warn()  { echo -e "${YELLOW}[poller]${NC} $*"; }
error() { echo -e "${RED}[poller]${NC} $*" >&2; }

# ── load .env if present ───────────────────────────────────────────────────────
if [[ -f .env ]]; then
  info "Loading .env"
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# ── check required env vars ────────────────────────────────────────────────────
missing=()
[[ -z "${FACEBOOK_C_USER:-}" ]] && missing+=("FACEBOOK_C_USER")
[[ -z "${FACEBOOK_XS:-}"     ]] && missing+=("FACEBOOK_XS")
[[ -z "${INGEST_URL:-}"      ]] && missing+=("INGEST_URL")

if (( ${#missing[@]} )); then
  error "Missing required environment variables:"
  for v in "${missing[@]}"; do
    error "  $v"
  done
  echo
  echo "Set them in a .env file or export them before running this script."
  echo "Minimum .env example:"
  cat <<'EXAMPLE'

    FACEBOOK_C_USER=123456789
    FACEBOOK_XS=your_xs_cookie_value
    FACEBOOK_SB=your_sb_value          # optional but recommended
    FACEBOOK_DATR=your_datr_value      # optional but recommended
    INGEST_URL=https://your-service.example.com
    GROUPS_CSV=groups.csv
    POLL_INTERVAL=60
    SEEN_FILE=seen_posts.log

EXAMPLE
  exit 1
fi

# ── defaults for optional vars ─────────────────────────────────────────────────
export GROUPS_CSV="${GROUPS_CSV:-groups.csv}"
export POLL_INTERVAL="${POLL_INTERVAL:-60}"
export SEEN_FILE="${SEEN_FILE:-seen_posts.log}"

# ── check groups CSV ───────────────────────────────────────────────────────────
if [[ ! -f "$GROUPS_CSV" ]]; then
  error "Groups CSV not found: $GROUPS_CSV"
  echo "Create it with one group ID (and optional name) per row, e.g.:"
  echo "  group_id,group_name"
  echo "  123456789012345,My Group"
  exit 1
fi

group_count=$(tail -n +2 "$GROUPS_CSV" | grep -c '[0-9]' || true)
info "Groups CSV: $GROUPS_CSV ($group_count group(s))"

# ── check Go toolchain ─────────────────────────────────────────────────────────
if ! command -v go &>/dev/null; then
  error "Go is not installed or not on PATH."
  echo "Install Go 1.21+: https://go.dev/dl/"
  exit 1
fi
GO_VERSION=$(go version | awk '{print $3}')
info "Go: $GO_VERSION"

# ── build ──────────────────────────────────────────────────────────────────────
BINARY="./poller"
info "Building $BINARY …"
go build -o "$BINARY" ./cmd/poller
info "Build complete"

[[ "${1:-}" == "--build-only" ]] && { info "Done (--build-only)."; exit 0; }

# ── seen-posts log info ────────────────────────────────────────────────────────
if [[ -f "$SEEN_FILE" ]]; then
  seen_count=$(wc -l < "$SEEN_FILE" | tr -d ' ')
  info "Seen-posts log: $SEEN_FILE ($seen_count post(s) already known – will never be re-ingested)"
else
  info "Seen-posts log: $SEEN_FILE (new – will be created on first run)"
fi

# ── run ────────────────────────────────────────────────────────────────────────
info "Starting poller (INGEST_URL=$INGEST_URL, POLL_INTERVAL=${POLL_INTERVAL}s)"
info "Press Ctrl-C to stop gracefully."
echo
exec "$BINARY"

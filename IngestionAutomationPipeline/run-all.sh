#!/bin/bash
# IngestionAutomationPipeline/run-all.sh
#
# Single entry point for the Cognivelle Ingestion Automation Pipeline.
#
# Usage:
#   ./run-all.sh            Start all services (builds only when necessary)
#   ./run-all.sh --build    Force a clean rebuild of the Node.js services
#   ./run-all.sh --help     Show this help
#
# Start order:
#   1. lead-classifier  (finetuning/serve.py  — Python FastAPI, :8001)
#   2. whatsappSenderService               (Node.js,          :3000)
#   3. leadIngestion                       (Node.js/TS,       :8000)
#   4. RedditScraper                       (Python)
#   5. FbScraper                           (Go binary)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
FORCE_BUILD=false
declare -a PIDS=()

# ── Argument parsing ──────────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --build) FORCE_BUILD=true ;;
    --help|-h)
      sed -n '2,9p' "$0" | sed 's/^# //'
      exit 0
      ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

mkdir -p "$LOG_DIR"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[  OK  ]${NC} $*"; }
info() { echo -e "${CYAN}[ INFO ]${NC} $*"; }
warn() { echo -e "${YELLOW}[ WARN ]${NC} $*"; }
err()  { echo -e "${RED}[ERROR ]${NC} $*" >&2; }
sep()  { echo -e "${CYAN}────────────────────────────────────────────────${NC}"; }

# ── Helpers ───────────────────────────────────────────────────────────────────
wait_for_port() {
  local port="$1" name="$2" timeout="${3:-30}" elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    if (echo > /dev/tcp/127.0.0.1/"$port") > /dev/null 2>&1; then
      log "$name is listening on :$port"
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  warn "$name did not open :$port within ${timeout}s — check $LOG_DIR/${name}.log"
  return 1
}

wait_for_http() {
  local url="$1" name="$2" timeout="${3:-60}" elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    if curl -fsS "$url" > /dev/null 2>&1; then
      log "$name responded at $url"
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  warn "$name did not respond at $url within ${timeout}s — check $LOG_DIR/${name}.log"
  return 1
}

npm_install_if_needed() {
  local dir="$1"
  if [ "$FORCE_BUILD" = true ] || [ ! -d "$dir/node_modules" ]; then
    info "npm install in $dir..."
    npm install --prefix "$dir" > "$LOG_DIR/$(basename "$dir")-install.log" 2>&1 \
      || { err "npm install failed in $dir — see $LOG_DIR/$(basename "$dir")-install.log"; return 1; }
  fi
}

needs_build() {
  local src_dir="$1" dist_file="$2"
  [ "$FORCE_BUILD" = true ] && return 0
  [ ! -f "$dist_file" ] && return 0
  [ -n "$(find "$src_dir" -name '*.ts' -newer "$dist_file" 2>/dev/null)" ] && return 0
  return 1
}

# ── Graceful shutdown ─────────────────────────────────────────────────────────
cleanup() {
  echo ""
  info "Shutting down all services..."
  if [ ${#PIDS[@]} -gt 0 ]; then
    for pid in "${PIDS[@]}"; do
      kill "$pid" 2>/dev/null && wait "$pid" 2>/dev/null || true
    done
  fi
  log "All services stopped."
  exit 0
}
trap cleanup SIGINT SIGTERM

sep
info "Cognivelle Ingestion Automation Pipeline"
sep

# ── 1. lead-classifier (finetuning/serve.py) ──────────────────────────────────
info "Starting lead-classifier (finetuning/serve.py) on :8001..."
CLASSIFIER_DIR="$SCRIPT_DIR/finetuning"

if [ ! -d "$CLASSIFIER_DIR" ]; then
  err "finetuning/ directory not found — cannot start classifier."
  exit 1
fi

if [ ! -f "$CLASSIFIER_DIR/serve.py" ]; then
  err "finetuning/serve.py not found."
  exit 1
fi

CLASSIFIER_PORT=8001
if [ -f "$SCRIPT_DIR/leadIngestion/.env" ]; then
  _url="$(grep -E '^CLASSIFIER_BASE_URL=' "$SCRIPT_DIR/leadIngestion/.env" 2>/dev/null | tail -n1 | cut -d= -f2-)" || true
  if [ -n "${_url:-}" ]; then
    _port="$(echo "$_url" | grep -oE ':[0-9]+$' | tr -d ':')" || true
    [ -n "${_port:-}" ] && CLASSIFIER_PORT="$_port"
  fi
fi

if command -v uv > /dev/null 2>&1; then
  (
    CLASSIFIER_PORT="$CLASSIFIER_PORT" \
      uv run --project "$CLASSIFIER_DIR" python "$CLASSIFIER_DIR/serve.py" || true
  ) > "$LOG_DIR/classifier.log" 2>&1 &
else
  warn "uv not found — falling back to python3. Consider installing uv."
  (
    cd "$CLASSIFIER_DIR"
    CLASSIFIER_PORT="$CLASSIFIER_PORT" python3 serve.py || true
  ) > "$LOG_DIR/classifier.log" 2>&1 &
fi

CLASSIFIER_PID=$!
PIDS+=("$CLASSIFIER_PID")
info "lead-classifier PID: $CLASSIFIER_PID"
info "Waiting for model weights to load (may take up to 60s on first run)..."
wait_for_http "http://localhost:${CLASSIFIER_PORT}/health" "lead-classifier" 60 || \
  warn "Classifier may still be loading — /ingest will 502 until it responds. Check $LOG_DIR/classifier.log"

sep

# ── 2. whatsappSenderService ──────────────────────────────────────────────────
info "Starting whatsappSenderService on :3000..."
if [ -d "$SCRIPT_DIR/whatsappSenderService" ]; then
  npm_install_if_needed "$SCRIPT_DIR/whatsappSenderService"
  node "$SCRIPT_DIR/whatsappSenderService/src/index.js" \
    > "$LOG_DIR/whatsappSenderService.log" 2>&1 &
  WA_PID=$!
  PIDS+=("$WA_PID")
  info "whatsappSenderService PID: $WA_PID"
  wait_for_port 3000 "whatsappSenderService" 20 || true
  warn "If this is the first run, scan the WhatsApp QR code in $LOG_DIR/whatsappSenderService.log"
else
  err "whatsappSenderService directory not found — skipping."
fi

sep

# ── 3. leadIngestion (TypeScript/Node.js) ─────────────────────────────────────
info "Starting leadIngestion on :8000..."
if [ -d "$SCRIPT_DIR/leadIngestion" ]; then
  npm_install_if_needed "$SCRIPT_DIR/leadIngestion"
  if needs_build "$SCRIPT_DIR/leadIngestion/src" "$SCRIPT_DIR/leadIngestion/dist/index.js"; then
    info "Building leadIngestion..."
    npm run build --prefix "$SCRIPT_DIR/leadIngestion" \
      > "$LOG_DIR/leadIngestion-build.log" 2>&1 \
      || { err "leadIngestion build failed — see $LOG_DIR/leadIngestion-build.log"; }
  else
    info "leadIngestion dist is up-to-date (use --build to force rebuild)."
  fi
  (cd "$SCRIPT_DIR/leadIngestion" && exec node dist/index.js) \
    > "$LOG_DIR/leadIngestion.log" 2>&1 &
  LI_PID=$!
  PIDS+=("$LI_PID")
  info "leadIngestion PID: $LI_PID"
  wait_for_port 8000 "leadIngestion" 20 || true
else
  err "leadIngestion directory not found — skipping."
fi

sep

# ── 4. RedditScraper ──────────────────────────────────────────────────────────
info "Starting RedditScraper..."
if [ -d "$SCRIPT_DIR/RedditScraper" ]; then
  (
    cd "$SCRIPT_DIR/RedditScraper"
    uv run python main.py || true
  ) > "$LOG_DIR/redditScraper.log" 2>&1 &
  RS_PID=$!
  PIDS+=("$RS_PID")
  info "RedditScraper PID: $RS_PID"
else
  err "RedditScraper directory not found — skipping."
fi

# ── 5. FbScraper ──────────────────────────────────────────────────────────────
info "Starting FbScraper..."
if [ -d "$SCRIPT_DIR/FbScraper" ]; then
  if [ -f "$SCRIPT_DIR/FbScraper/run-poller.sh" ]; then
    chmod +x "$SCRIPT_DIR/FbScraper/run-poller.sh"
    "$SCRIPT_DIR/FbScraper/run-poller.sh" > "$LOG_DIR/fbScraper.log" 2>&1 &
    FB_PID=$!
    PIDS+=("$FB_PID")
    info "FbScraper PID: $FB_PID"
  else
    warn "FbScraper/run-poller.sh not found — skipping."
  fi
else
  err "FbScraper directory not found — skipping."
fi

sep
log "All services started."
echo ""
echo "  Service endpoints:"
echo "    lead-classifier       http://localhost:${CLASSIFIER_PORT}  (GET /health, POST /classify)"
echo "    leadIngestion         http://localhost:8000               (POST /ingest, GET /health, GET /stats)"
echo "    whatsappSenderService http://localhost:3000"
echo ""
echo "  Log files:"
echo "    $LOG_DIR/classifier.log"
echo "    $LOG_DIR/leadIngestion.log"
echo "    $LOG_DIR/whatsappSenderService.log"
echo "    $LOG_DIR/redditScraper.log"
echo "    $LOG_DIR/fbScraper.log"
echo ""
echo "  Press Ctrl+C to stop all services."
sep

wait

"""Configuration loaded from environment / .env file."""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _get_int(key: str, default: int) -> int:
    try:
        return int(_get(key, str(default)))
    except ValueError:
        return default


# ── Ingestion ──────────────────────────────────────────────────────────────────
LEAD_INGESTION_SERVER_URL: str = _get(
    "LEAD_INGESTION_SERVER_URL", "http://localhost:8000"
)

# ── Reddit / RSS ───────────────────────────────────────────────────────────────
REDDIT_SUBREDDITS_CSV: str = _get("REDDIT_SUBREDDITS_CSV", "subreddits.csv")

REDDIT_MIN_REQUEST_INTERVAL: int = _get_int("REDDIT_MIN_REQUEST_INTERVAL", 60)
REDDIT_RSS_CACHE_TTL: int = _get_int("REDDIT_RSS_CACHE_TTL", 60)

REDDIT_MIN_FEED_INTERVAL: int = _get_int("REDDIT_MIN_FEED_INTERVAL", 300)
REDDIT_MAX_FEED_INTERVAL: int = _get_int("REDDIT_MAX_FEED_INTERVAL", 3600)

REDDIT_BOOTSTRAP_MODE: str = _get("REDDIT_BOOTSTRAP_MODE", "latest").lower()

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL: str = _get("LOG_LEVEL", "INFO").upper()

# ── SQLite ─────────────────────────────────────────────────────────────────────
_default_db = os.environ.get("COGNIVELLE_DB_PATH") or os.environ.get("CENTRAL_DB_PATH") or str(
    Path(__file__).resolve().parents[2] / "workspace" / ".enrichment_contacts.sqlite3"
)
DB_PATH: str = _get("DB_PATH", _default_db)

# ── HTTP / User-Agent ──────────────────────────────────────────────────────────
USER_AGENT: str = "reddit-monitor/1.0 (research project; contact via GitHub)"


def configure_logging() -> None:
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Quiet noisy third-party loggers
    for name in ("urllib3", "charset_normalizer", "feedparser"):
        logging.getLogger(name).setLevel(logging.WARNING)

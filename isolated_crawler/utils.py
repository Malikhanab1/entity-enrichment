"""Small stateless helpers shared across modules."""
from __future__ import annotations

import re
import urllib.parse

import pandas as pd


def normalize_url(url: str) -> str:
    """Ensure a URL has a scheme; return '' for empty/NaN input."""
    if pd.isna(url) or not str(url).strip():
        return ""
    url = str(url).strip()
    if not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


def get_domain(url: str) -> str:
    """Return the normalized registrable-ish domain (netloc, lowercase, no www)."""
    if not url:
        return ""
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # strip port if present
    netloc = netloc.split(":")[0]
    return netloc


def deobfuscate_text(text: str) -> str:
    text = re.sub(r"\s*\[at\]\s*|\s*\(at\)\s*", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\[dot\]\s*|\s*\(dot\)\s*", ".", text, flags=re.IGNORECASE)
    return text


def clean_text(text: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if limit:
        text = text[:limit]
    return text


def safe_bool_to_str(value: bool) -> str:
    return "TRUE" if value else "FALSE"

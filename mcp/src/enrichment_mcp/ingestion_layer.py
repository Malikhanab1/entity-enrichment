"""Unified ingestion automation façade.

This file deliberately merges the old three-source intake path into one MCP
accessible layer:

- Reddit source posts
- Facebook source posts/pages
- Google Maps business discovery via the local pymapscraper-style data

The point is to expose one route for the Streamlit app and the MCP tool layer
so the repository stops looking like a set of disconnected scrapers.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def ingest_records(source: str, payload: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MCP-facing tool adapter for the one-layer ingestion plane.

    This helper is intentionally pure and deterministic so the UI and the
    server can both route Reddit/Facebook/Maps source payloads through the
    same normalization path without reading the old pipeline folders.
    """
    return IngestionAutomationPipeline().ingest(source, payload)


@dataclass
class IngestionRecord:
    """Canonical normalized ingestion record for the single database path."""

    source: str
    source_id: str
    title: str
    body: str
    business_name: str
    city: str | None = None
    website: str | None = None
    social_url: str | None = None
    contact_hint: dict[str, Any] | None = None
    extra: dict[str, Any] | None = None

    def as_db_row(self) -> dict[str, Any]:
        """Return a JSON-safe record object for the workspace database binder."""
        return {
            "source": self.source,
            "source_id": self.source_id,
            "title": self.title,
            "body": self.body,
            "business_name": self.business_name,
            "city": self.city or "",
            "website": self.website,
            "social_url": self.social_url,
            "contact_hint": self.contact_hint or {},
            "extra": self.extra or {},
        }


class IngestionAutomationPipeline:
    """Single ingestion layer that can accept Reddit/Facebook/Maps records and
    normalize them into one database-friendly envelope.

    It also provides a bridge for the repository's local pymapscraper
    Google Maps scraper by accepting Place-like objects and converting them
    into the canonical ingestion row shape used by the UI and the shared
    workspace database.
    """

    def __init__(self, workspace_dir: str | Path | None = None) -> None:
        self.workspace_dir = Path(workspace_dir) if workspace_dir else Path.cwd()
        self.sources = {
            "reddit": "reddit",
            "facebook": "facebook",
            "google_maps": "google_maps",
            "maps": "google_maps",
        }

    def _clean_text(self, value: Any) -> str:
        text = str(value or "").strip()
        text = re.sub(r"\s+", " ", text)
        return text[:1000]

    def normalize_reditt_post(self, post: dict[str, Any]) -> IngestionRecord:
        """Convert a Reddit post into the canonical ingestion row shape."""
        source_id = str(post.get("id") or post.get("source_id") or "reddit-post")
        title = self._clean_text(post.get("title") or post.get("headline") or "Reddit mention")
        body = self._clean_text(post.get("body") or post.get("text") or post.get("excerpt") or title)
        business_name = self._clean_text(post.get("business_name") or post.get("business") or title)
        city = self._clean_text(post.get("city") or post.get("location") or "") or None
        website = post.get("website") or None
        social_url = post.get("social_url") or None
        return IngestionRecord(
            source="reddit",
            source_id=source_id,
            title=title,
            body=body,
            business_name=business_name,
            city=city,
            website=website,
            social_url=social_url,
            contact_hint={"source": "reddit"},
            extra={"raw": post},
        )

    def normalize_facebook_post(self, post: dict[str, Any]) -> IngestionRecord:
        """Convert a Facebook post or page record into the canonical shape."""
        source_id = str(post.get("id") or post.get("source_id") or "facebook-post")
        title = self._clean_text(post.get("title") or post.get("headline") or "Facebook mention")
        body = self._clean_text(post.get("body") or post.get("text") or post.get("excerpt") or title)
        business_name = self._clean_text(post.get("business_name") or post.get("business") or title)
        city = self._clean_text(post.get("city") or post.get("location") or "") or None
        website = post.get("website") or None
        social_url = post.get("social_url") or None
        return IngestionRecord(
            source="facebook",
            source_id=source_id,
            title=title,
            body=body,
            business_name=business_name,
            city=city,
            website=website,
            social_url=social_url,
            contact_hint={"source": "facebook"},
            extra={"raw": post},
        )

    def normalize_google_maps_business(self, business: dict[str, Any]) -> IngestionRecord:
        """Convert a Google Maps business discovery result into the canonical row."""
        source_id = str(business.get("place_id") or business.get("source_id") or business.get("name") or "maps-place")
        title = self._clean_text(business.get("name") or "Google Maps business")
        body = self._clean_text(business.get("description") or business.get("snippet") or title)
        business_name = self._clean_text(business.get("business_name") or business.get("name") or title)
        city = self._clean_text(business.get("city") or business.get("location") or business.get("address") or "") or None
        website = business.get("website") or business.get("url") or None
        social_url = business.get("social_url") or business.get("facebook_url") or None
        return IngestionRecord(
            source="google_maps",
            source_id=source_id,
            title=title,
            body=body,
            business_name=business_name,
            city=city,
            website=website,
            social_url=social_url,
            contact_hint={
                "phone": business.get("phone"),
                "rating": business.get("rating"),
                "source": "google_maps",
            },
            extra={"raw": business},
        )

    def normalize_pymapscraper_place(self, place: Any) -> IngestionRecord:
        """Convert the local pymapscraper Place object or dict into the shared
        canonical ingestion record. This lets the app import and normalize
        the same Google Maps-style records the old automation used.
        """
        if isinstance(place, dict):
            data = place
            source_id = str(data.get("place_id") or data.get("source_id") or data.get("name") or "maps-place")
            title = self._clean_text(data.get("name") or data.get("business_name") or "Google Maps business")
            body = self._clean_text(data.get("description") or data.get("snippet") or data.get("address") or title)
            business_name = self._clean_text(data.get("business_name") or data.get("name") or title)
            city = self._clean_text(data.get("city") or data.get("location") or data.get("address") or "") or None
            website = data.get("website") or data.get("url") or data.get("website_url") or None
            phone = data.get("phone")
            social_url = data.get("social_url") or data.get("facebook_url") or None
            categories = data.get("categories") or []
            coords = data.get("coordinates") or {}
            extra = {"raw": data, "categories": categories, "coordinates": coords, "review_count": data.get("review_count")}
        else:
            data = getattr(place, "to_dict", lambda: {})()
            source_id = str(getattr(place, "place_id", "") or getattr(place, "source_id", "") or getattr(place, "name", "") or "maps-place")
            title = self._clean_text(getattr(place, "name", "") or "Google Maps business")
            body = self._clean_text(getattr(place, "description", "") or getattr(place, "address", "") or title)
            business_name = self._clean_text(getattr(place, "name", "") or title)
            city = self._clean_text(getattr(place, "address", "") or "") or None
            website = getattr(place, "website", "") or None
            phone = getattr(place, "phone", "") or None
            social_links = getattr(place, "social_links", None) or []
            social_url = None
            if isinstance(social_links, list) and social_links:
                candidate = social_links[0]
                if isinstance(candidate, dict):
                    social_url = candidate.get("url")
                elif isinstance(candidate, str):
                    social_url = candidate
            categories = list(getattr(place, "categories", []) or [])
            coords = getattr(place, "coordinates", getattr(place, "lat", None) and {"lat": getattr(place, "lat"), "lng": getattr(place, "lng")} or {})
            extra = {"raw": data, "categories": categories, "coordinates": coords, "review_count": getattr(place, "review_count", None)}
            phone = phone or None
            website = website or None
        return IngestionRecord(
            source="google_maps",
            source_id=source_id,
            title=title,
            body=body,
            business_name=business_name,
            city=city,
            website=website,
            social_url=social_url,
            contact_hint={
                "phone": phone,
                "rating": data.get("rating") if isinstance(data, dict) else getattr(place, "rating", None),
                "source": "google_maps",
            },
            extra=extra,
        )

    def ingest(self, source: str, payload: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Run the common single-layer normalization route.

        Accepts one payload object or a list of records from Reddit,
        Facebook, or Google Maps and returns a list of DB-safe records.
        """
        normalized = []
        records = payload if isinstance(payload, list) else [payload]
        source_key = self.sources.get(str(source).lower(), str(source).lower())
        for record in records:
            if source_key == "reddit":
                item = self.normalize_reditt_post(record)
            elif source_key == "facebook":
                item = self.normalize_facebook_post(record)
            elif source_key in {"google_maps", "maps"}:
                if hasattr(record, "place_id") or hasattr(record, "name") or isinstance(record, dict):
                    item = self.normalize_pymapscraper_place(record)
                else:
                    item = self.normalize_google_maps_business(record)
            else:
                item = IngestionRecord(
                    source=source_key,
                    source_id=str(record.get("id") or record.get("source_id") or "unknown"),
                    title=self._clean_text(record.get("title") or "Unknown source"),
                    body=self._clean_text(record.get("body") or record.get("text") or ""),
                    business_name=self._clean_text(record.get("business_name") or record.get("name") or "Unknown business"),
                    city=self._clean_text(record.get("city") or record.get("location") or "") or None,
                    website=record.get("website") or None,
                    social_url=record.get("social_url") or None,
                    contact_hint={"source": source_key},
                    extra={"raw": record},
                )
            normalized.append(item.as_db_row())
        return normalized


def search_google_maps(query: str, city: str | None = None, max_results: int = 5) -> list[dict[str, Any]]:
    """One-shot MCP/MVP adapter for the local pymapscraper backend.

    It keeps the UI and the MCP tool surface in the same canonical ingestion
    contract: search results are normalized into DB-safe rows without leaking
    the old scraper-specific data model to the rest of the app.
    """
    repo_root = Path(__file__).resolve().parents[3]
    scraper_dir = repo_root / "pymapscraper"
    if str(scraper_dir) not in sys.path:
        sys.path.insert(0, str(scraper_dir))

    try:
        from scraper import GoogleMapsScraper
        from helpers.parsers import parse_search_response
    except Exception:
        return []

    try:
        with GoogleMapsScraper(
            max_delay=3.0,
            min_delay=1.0,
            workers=1,
            session_file=str(repo_root / "workspace" / "maps_session.json"),
        ) as scraper:
            scraper.start()
            results = scraper.search(query=f"{query} {city or ''}".strip(), max_results=max_results)
    except Exception:
        return []

    pipeline = IngestionAutomationPipeline()
    normalized = []
    for row in results:
        obj = pipeline.normalize_pymapscraper_place(row)
        normalized.append(obj.as_db_row())
    return normalized

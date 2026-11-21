"""
Caches full enrichment results by normalized domain so that when multiple
input rows point at the same website, we crawl it only once.
"""
from __future__ import annotations

import asyncio


class DomainCache:
    def __init__(self):
        self._results: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def get_lock(self, domain: str) -> asyncio.Lock:
        async with self._global_lock:
            if domain not in self._locks:
                self._locks[domain] = asyncio.Lock()
            return self._locks[domain]

    def get(self, domain: str) -> dict | None:
        return self._results.get(domain)

    def set(self, domain: str, result: dict) -> None:
        self._results[domain] = result

    def has(self, domain: str) -> bool:
        return domain in self._results

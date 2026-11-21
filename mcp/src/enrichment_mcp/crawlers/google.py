"""Quota-aware web-search router for Exa, Firecrawl, and Tavily.

``search_google`` remains as a compatibility name for the existing tools; no
Google or SearXNG service is used.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from dotenv import load_dotenv

from ._common import sanitize_text

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

_TIMEOUT_SECONDS = float(os.getenv("SEARCH_TIMEOUT_SECONDS", "15"))
_STATE_PATH = Path(os.getenv("SEARCH_PROVIDER_STATE_PATH", str(Path(__file__).resolve().parents[3] / ".search_provider_state.json")))
_COOLDOWN_SECONDS = int(os.getenv("SEARCH_PROVIDER_COOLDOWN_SECONDS", "900"))
_EXHAUSTED_RECHECK_SECONDS = int(os.getenv("SEARCH_EXHAUSTED_RECHECK_SECONDS", "86400"))


class ProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, *, exhausted: bool = False, retry_after: int | None = None):
        super().__init__(message)
        self.provider, self.exhausted, self.retry_after = provider, exhausted, retry_after


@dataclass
class ProviderState:
    remaining: int | None = None
    checked_at: float = 0.0
    cooldown_until: float = 0.0
    exhausted: bool = False
    failures: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProviderState":
        return cls(raw.get("remaining") if isinstance(raw.get("remaining"), int) else None, float(raw.get("checked_at", 0)), float(raw.get("cooldown_until", 0)), bool(raw.get("exhausted", False)), int(raw.get("failures", 0)))

    def as_dict(self) -> dict[str, Any]:
        return {"remaining": self.remaining, "checked_at": self.checked_at, "cooldown_until": self.cooldown_until, "exhausted": self.exhausted, "failures": self.failures}


SearchFn = Callable[[str, int], Awaitable[list[dict[str, str]]]]
UsageFn = Callable[[], Awaitable[int | None]]


@dataclass
class SearchProvider:
    name: str
    search: SearchFn
    usage: UsageFn | None = None
    state: ProviderState = field(default_factory=ProviderState)


class SearchRouter:
    """Use the provider with the largest known balance; preserve state on disk."""

    def __init__(self, providers: list[SearchProvider], state_path: Path = _STATE_PATH):
        self.providers, self.state_path, self._initialized, self._lock = providers, state_path, False, asyncio.Lock()
        try:
            saved = json.loads(state_path.read_text())
            for provider in providers:
                if isinstance(saved.get(provider.name), dict):
                    provider.state = ProviderState.from_dict(saved[provider.name])
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".search-state-", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({p.name: p.state.as_dict() for p in self.providers}, handle)
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def initialize(self) -> None:
        """Refresh credit APIs concurrently once per MCP process.

        Exa has no dependable public usage endpoint, so its first *real* query
        validates it; a quota response is then persisted as exhausted.
        """
        async with self._lock:
            if self._initialized:
                return
            await asyncio.gather(*(self._refresh(provider) for provider in self.providers if provider.usage))
            self._initialized = True
            self._save()

    async def _refresh(self, provider: SearchProvider) -> None:
        try:
            remaining = await provider.usage() if provider.usage else None
            if remaining is not None:
                provider.state.remaining, provider.state.exhausted = max(0, remaining), remaining <= 0
            provider.state.checked_at = time.time()
        except ProviderError as exc:
            self._fail(provider, exc)
        except Exception:
            provider.state.checked_at = time.time()  # Usage outage is not a search outage.

    def _fail(self, provider: SearchProvider, error: ProviderError) -> None:
        provider.state.failures += 1
        provider.state.checked_at = time.time()
        if error.exhausted:
            provider.state.exhausted, provider.state.remaining = True, 0
        else:
            provider.state.cooldown_until = time.time() + (error.retry_after or _COOLDOWN_SECONDS)

    async def search(self, query: str, limit: int) -> tuple[list[dict[str, str]], str | None, list[str]]:
        await self.initialize()
        now = time.time()
        for provider in self.providers:
            # Exa does not expose usage. Let a previously depleted free tier be
            # retried after a day so a monthly credit reset is eventually seen.
            if provider.state.exhausted and provider.usage is None and now - provider.state.checked_at >= _EXHAUSTED_RECHECK_SECONDS:
                provider.state.exhausted, provider.state.remaining = False, None
        usable = [p for p in self.providers if not p.state.exhausted and p.state.cooldown_until <= now]
        # Known balances win. Unknown balances are a fallback, so Exa is not
        # blindly invoked on every request once a metered provider is known good.
        usable.sort(key=lambda p: (p.state.remaining is not None, p.state.remaining or -1, -p.state.failures), reverse=True)
        errors: list[str] = []
        for provider in usable:
            try:
                result = await provider.search(query, limit)
                provider.state.failures = 0
                if provider.state.remaining is not None:
                    provider.state.remaining = max(0, provider.state.remaining - 1)
                self._save()
                return result, provider.name, errors
            except ProviderError as exc:
                self._fail(provider, exc)
                errors.append(f"{provider.name}: {exc}")
                self._save()
        if not self.providers:
            errors.append("No search provider is configured. Set EXA_API_KEY, FIRECRAWL_API_KEY, or TAVILY_API_KEY in mcp/.env.")
        elif not errors:
            errors.append("All configured search providers are exhausted or cooling down.")
        return [], None, errors


def _provider_error(name: str, response: httpx.Response) -> ProviderError:
    text = response.text[:300]
    retry_after = response.headers.get("retry-after")
    exhausted = response.status_code in {401, 402, 432} or any(term in text.lower() for term in ("credit", "quota", "exhaust"))
    return ProviderError(name, f"HTTP {response.status_code}: {text}", exhausted=exhausted, retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None)


async def _request(name: str, method: str, url: str, headers: dict[str, str], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.request(method, url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise ProviderError(name, f"network error: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise _provider_error(name, response)
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(name, "invalid JSON response") from exc


def _normalise(items: list[dict[str, Any]], limit: int) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        url = str(item.get("url", "")).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        output.append({"title": sanitize_text(str(item.get("title", "")))[:160], "url": url, "snippet": sanitize_text(str(item.get("snippet") or item.get("text") or item.get("description") or ""))[:500]})
        if len(output) >= limit:
            break
    return output


def _build_router() -> SearchRouter:
    providers: list[SearchProvider] = []
    exa, firecrawl, tavily = (os.getenv(name, "").strip() for name in ("EXA_API_KEY", "FIRECRAWL_API_KEY", "TAVILY_API_KEY"))
    if exa:
        async def exa_search(query: str, limit: int) -> list[dict[str, str]]:
            data = await _request("exa", "POST", "https://api.exa.ai/search", {"x-api-key": exa}, {"query": query, "numResults": limit, "contents": {"text": {"maxCharacters": 500}}})
            return _normalise(data.get("results", []), limit)
        providers.append(SearchProvider("exa", exa_search))
    if firecrawl:
        headers = {"Authorization": f"Bearer {firecrawl}"}
        async def firecrawl_search(query: str, limit: int) -> list[dict[str, str]]:
            data = await _request("firecrawl", "POST", "https://api.firecrawl.dev/v2/search", headers, {"query": query, "limit": limit, "sources": ["web"]})
            return _normalise(data.get("data", {}).get("web", []), limit)
        async def firecrawl_usage() -> int | None:
            data = await _request("firecrawl", "GET", "https://api.firecrawl.dev/v2/team/credit-usage", headers)
            value = data.get("data", {}).get("remainingCredits")
            return int(value) if isinstance(value, (int, float)) else None
        providers.append(SearchProvider("firecrawl", firecrawl_search, firecrawl_usage))
    if tavily:
        headers = {"Authorization": f"Bearer {tavily}"}
        async def tavily_search(query: str, limit: int) -> list[dict[str, str]]:
            data = await _request("tavily", "POST", "https://api.tavily.com/search", headers, {"query": query, "search_depth": "basic", "max_results": limit, "include_answer": False, "include_raw_content": False})
            return _normalise(data.get("results", []), limit)
        async def tavily_usage() -> int | None:
            data = await _request("tavily", "GET", "https://api.tavily.com/usage", headers)
            key = data.get("key", {})
            return int(key["limit"] - key["usage"]) if isinstance(key.get("limit"), (int, float)) and isinstance(key.get("usage"), (int, float)) else None
        providers.append(SearchProvider("tavily", tavily_search, tavily_usage))
    return SearchRouter(providers)


_router = _build_router()


async def search_google(query: str, num_results: int = 10) -> dict[str, Any]:
    query = sanitize_text(query).strip()
    if not query:
        return {"query": query, "results": [], "errors": ["Search query must not be empty."], "provider": None}
    results, provider, errors = await _router.search(query, min(max(1, num_results), 30))
    if not results and not errors:
        errors.append(f"No results found from {provider}.")
    return {"query": query, "results": results, "errors": errors, "provider": provider}


search_duckduckgo = search_google

import asyncio
import tempfile
import unittest
from pathlib import Path

from enrichment_mcp.crawlers.google import ProviderError, ProviderState, SearchProvider, SearchRouter


class SearchRouterTests(unittest.TestCase):
    def test_uses_largest_known_balance_without_fanout(self):
        calls = []

        async def low_balance(query, limit):
            calls.append("low")
            return []

        async def high_balance(query, limit):
            calls.append("high")
            return [{"title": "Acme", "url": "https://acme.example", "snippet": "Official"}]

        with tempfile.TemporaryDirectory() as directory:
            low = SearchProvider("low", low_balance, state=ProviderState(remaining=2))
            high = SearchProvider("high", high_balance, state=ProviderState(remaining=20))
            result, provider, errors = asyncio.run(SearchRouter([low, high], Path(directory) / "state.json").search("acme", 5))

        self.assertEqual(provider, "high")
        self.assertEqual(calls, ["high"])
        self.assertFalse(errors)
        self.assertEqual(result[0]["url"], "https://acme.example")

    def test_quota_failure_is_persisted_and_skipped(self):
        calls = []

        async def exhausted(query, limit):
            calls.append("exhausted")
            raise ProviderError("exa", "quota exhausted", exhausted=True)

        async def fallback(query, limit):
            calls.append("fallback")
            return []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            router = SearchRouter([SearchProvider("exa", exhausted), SearchProvider("tavily", fallback)], path)
            asyncio.run(router.search("acme", 5))
            # A new router process reads the exhausted marker and does not
            # waste another request testing the same depleted provider.
            second = SearchRouter([SearchProvider("exa", exhausted), SearchProvider("tavily", fallback)], path)
            asyncio.run(second.search("acme", 5))

        self.assertEqual(calls, ["exhausted", "fallback", "fallback"])

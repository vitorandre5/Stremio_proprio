import asyncio
import unittest
from unittest.mock import patch

from app import search_cache


class SearchCacheTests(unittest.TestCase):
    def setUp(self):
        search_cache._local_cache.clear()
        search_cache._redis_retry_after = 0

    def test_local_cache_reuses_case_and_whitespace_variants(self):
        payload = {"results": [{"title": "Mr. Robot"}], "addon_search_errors": []}
        with patch.object(search_cache, "REDIS_URL", ""):
            asyncio.run(search_cache.set_search_response("  Mr. Robot ", "all", payload))
            cached = asyncio.run(search_cache.get_search_response("mr.   robot", "all"))

        self.assertEqual(cached, payload)

    def test_local_cache_returns_a_copy(self):
        payload = {"results": [{"title": "Mr. Robot"}]}
        with patch.object(search_cache, "REDIS_URL", ""):
            asyncio.run(search_cache.set_search_response("Mr Robot", "all", payload))
            cached = asyncio.run(search_cache.get_search_response("Mr Robot", "all"))
            cached["results"].clear()
            second_read = asyncio.run(search_cache.get_search_response("Mr Robot", "all"))

        self.assertEqual(second_read, payload)

    def test_redis_cache_round_trips_json(self):
        class FakeRedis:
            def __init__(self):
                self.values = {}

            async def set(self, key, value, ex):
                self.values[key] = value

            async def get(self, key):
                return self.values.get(key)

        fake = FakeRedis()
        payload = {"results": [{"title": "Eu, Robô"}], "addon_search_errors": []}
        with patch.object(search_cache, "REDIS_URL", "redis://cache.invalid/0"), patch.object(
            search_cache, "_redis_client", fake
        ):
            asyncio.run(search_cache.set_search_response("Eu, Robô", "movie", payload))
            search_cache._local_cache.clear()
            cached = asyncio.run(search_cache.get_search_response("Eu, Robô", "movie"))

        self.assertEqual(cached, payload)

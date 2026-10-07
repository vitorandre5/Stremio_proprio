import unittest
from unittest.mock import AsyncMock, patch

from app.stremio.client import direct_urls
from app.stremio.resolver import find_direct_streams, sort_streams


class StreamLanguageTests(unittest.TestCase):
    def test_labels_dubbed_subtitled_and_unspecified_portuguese(self):
        streams = direct_urls([
            {"name": "FrostStream 720p", "title": "🇧🇷 Dublado", "url": "https://8.8.8.8/dub"},
            {"name": "FrostStream 1080p", "title": "🧩 Legendado", "url": "https://8.8.8.8/sub"},
            {"name": "FrostStream", "title": "🌎 Português", "url": "https://8.8.8.8/pt"},
            {"name": "FrostStream", "title": "Original", "url": "https://8.8.8.8/original"},
        ])

        self.assertEqual([stream["language"] for stream in streams], [
            "dubbed", "subtitled", "portuguese_unspecified", "unknown",
        ])


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_queries_all_addons_and_deduplicates_urls(self):
        addons = [
            {"id": "frost", "name": "FrostStream", "manifest_url": "https://frost.example/manifest.json"},
            {"id": "fenix", "name": "FenixFlix", "manifest_url": "https://fenix.example/manifest.json"},
        ]
        shared = {"url": "https://8.8.8.8/shared", "title": "Dublado", "name": "720p"}
        unique = {"url": "https://8.8.8.8/unique", "title": "Legendado", "name": "1080p"}

        with patch("app.stremio.resolver.request_streams", new=AsyncMock(side_effect=[[shared], [shared, unique]])) as request:
            result = await find_direct_streams(addons, "series", "tt4158110:1:1")

        self.assertEqual(request.await_count, 2)
        self.assertEqual([stream["url"] for stream in result["streams"]], [shared["url"], unique["url"]])
        self.assertEqual([stream["provider"] for stream in result["streams"]], ["FrostStream", "FenixFlix"])


class StreamOrderingTests(unittest.TestCase):
    def test_dubbed_precedes_higher_quality_subtitled_stream(self):
        streams = [
            {"language": "subtitled", "quality": "2160p", "provider_id": "frost"},
            {"language": "dubbed", "quality": "720p", "provider_id": "fenix"},
        ]
        addons = [{"id": "frost"}, {"id": "fenix"}]

        sort_streams(streams, addons, "1080p")

        self.assertEqual([stream["language"] for stream in streams], ["dubbed", "subtitled"])

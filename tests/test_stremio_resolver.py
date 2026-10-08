import unittest
from unittest.mock import AsyncMock, patch

from app.stremio.client import _catalog_supports_search, direct_urls, request_catalog_search
from app.stremio.resolver import find_direct_streams, sort_streams, torrent_urls


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

    def test_torrent_stream_requires_a_valid_info_hash_and_keeps_file_index(self):
        parsed = torrent_urls([
            {"infoHash": "A" * 40, "fileIdx": 0, "name": "1080p Dublado"},
            {"infoHash": "malicious", "url": "https://8.8.8.8/video.mkv"},
        ])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["info_hash"], "a" * 40)
        self.assertEqual(parsed[0]["file_idx"], 0)
        self.assertEqual(parsed[0]["language"], "dubbed")


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_queries_all_addons_and_deduplicates_urls(self):
        addons = [
            {"id": "frost", "name": "FrostStream", "manifest_url": "https://frost.example/manifest.json"},
            {"id": "fenix", "name": "FenixFlix", "manifest_url": "https://fenix.example/manifest.json"},
        ]
        shared = {"url": "https://8.8.8.8/shared", "title": "Dublado", "name": "720p"}
        torrent = {"infoHash": "a" * 40, "title": "Torrent dublado", "fileIdx": 0}
        unique = {"url": "https://8.8.8.8/unique", "title": "Legendado", "name": "1080p"}

        with patch("app.stremio.resolver.request_streams", new=AsyncMock(side_effect=[[shared, torrent], [shared, unique, torrent]])) as request:
            result = await find_direct_streams(addons, "series", "tt4158110:1:1")

        self.assertEqual(request.await_count, 2)
        self.assertEqual([stream["url"] for stream in result["streams"]], [shared["url"], unique["url"]])
        self.assertEqual([stream["provider"] for stream in result["streams"]], ["FrostStream", "FenixFlix"])
        self.assertEqual(len(result["torrents"]), 1)
        self.assertEqual(result["torrents"][0]["provider"], "FrostStream")


class StreamOrderingTests(unittest.TestCase):
    def test_dubbed_precedes_higher_quality_subtitled_stream(self):
        streams = [
            {"language": "subtitled", "quality": "2160p", "provider_id": "frost"},
            {"language": "dubbed", "quality": "720p", "provider_id": "fenix"},
        ]
        addons = [{"id": "frost"}, {"id": "fenix"}]

        sort_streams(streams, addons, "1080p")

        self.assertEqual([stream["language"] for stream in streams], ["dubbed", "subtitled"])


class CatalogSearchManifestTests(unittest.TestCase):
    def test_only_catalogs_declaring_search_are_queried(self):
        manifest = {
            "resources": ["stream", "catalog"],
            "types": ["movie", "series"],
            "catalogs": [
                {"type": "movie", "id": "popular", "extra": [{"name": "skip"}]},
                {"type": "movie", "id": "searchable", "extra": [{"name": "search", "isRequired": False}]},
                {"type": "movie", "id": "search-by-genre", "extra": [{"name": "search"}, {"name": "genre", "isRequired": True}]},
                {"type": "series", "id": "series-search", "extra": [{"name": "search"}]},
            ],
        }
        self.assertEqual(_catalog_supports_search(manifest, "movie"), ["searchable"])
        self.assertEqual(_catalog_supports_search(manifest, "series"), ["series-search"])

    def test_brazuca_manifest_is_stream_only_and_has_no_search_catalog(self):
        manifest = {
            "resources": [{"name": "stream", "types": ["movie", "series", "anime"], "idPrefixes": ["tt", "kitsu"]}],
            "types": ["movie", "series", "anime", "other"],
            "catalogs": [],
            "behaviorHints": {"p2p": True},
        }
        self.assertEqual(_catalog_supports_search(manifest, "movie"), [])


class CatalogSearchRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_declared_catalog_route_and_escapes_query(self):
        manifest = {
            "resources": ["catalog"],
            "types": ["movie"],
            "catalogs": [{"type": "movie", "id": "movies", "extra": [{"name": "search"}]}],
        }
        with patch("app.stremio.client.read_manifest", new=AsyncMock(return_value=manifest)), patch(
            "app.stremio.client.fetch_json", new=AsyncMock(return_value={"metas": [{"id": "tt0343818", "type": "movie"}]})
        ) as fetch:
            metas = await request_catalog_search("https://addon.example/config/manifest.json", "movie", "Eu, Robô")

        self.assertEqual(metas, [{"id": "tt0343818", "type": "movie"}])
        self.assertEqual(fetch.await_args.args[0], "https://addon.example/config/catalog/movie/movies/search=Eu%2C%20Rob%C3%B4.json")

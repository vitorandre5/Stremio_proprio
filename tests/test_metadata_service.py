import unittest
from unittest.mock import AsyncMock, patch

from app.metadata import service, tmdb


class MetadataServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_combines_portuguese_and_original_titles_by_imdb(self):
        tmdb_item = {
            "id": "tt0343818", "imdb_id": "tt0343818", "media_type": "movie",
            "tmdb_id": 123, "title": "Eu, Robô", "original_title": "I, Robot",
            "search_titles": ["Eu, Robô", "I, Robot"], "overview": "Sinopse em português.",
            "poster_url": None, "year": 2004, "metadata_language": "pt-BR",
        }
        cinemeta_item = {
            "id": "tt0343818", "imdb_id": "tt0343818", "media_type": "movie",
            "title": "I, Robot", "overview": "English synopsis", "poster_url": None,
        }
        with patch.object(tmdb, "TMDB_API_TOKEN", "server-token"), \
             patch.object(service.cinemeta, "search", new=AsyncMock(return_value=[cinemeta_item])), \
             patch.object(service.tmdb, "search", new=AsyncMock(return_value=[tmdb_item])):
            results = await service.search("Eu Robô", "movie")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Eu, Robô")
        self.assertEqual(results[0]["overview"], "Sinopse em português.")
        self.assertIn("I, Robot", results[0]["search_titles"])

    async def test_localizes_series_and_episode_summaries(self):
        fallback = {
            "id": "tt4158110", "imdb_id": "tt4158110", "media_type": "series",
            "title": "Mr. Robot", "overview": "English overview", "episodes": [
                {"season_number": 1, "episode_number": 1, "title": "eps1.0_hellofriend.mov", "overview": "English episode", "released": None, "id": "tt4158110:1:1"},
            ],
        }

        async def get(path, **params):
            if path == "tv/10":
                return {"id": 10, "name": "Mr. Robot", "overview": "Sinopse em português.", "first_air_date": "2015-06-24", "seasons": [{"season_number": 1, "name": "Temporada 1", "episode_count": 10}], "credits": {"cast": []}}
            if path == "tv/10/season/1":
                return {"episodes": [{"episode_number": 1, "name": "Saudação inicial", "overview": "Resumo em português.", "air_date": "2015-06-24"}]}
            raise AssertionError(path)

        with patch.object(tmdb, "TMDB_API_TOKEN", "server-token"), \
             patch.object(tmdb, "_find_by_imdb", new=AsyncMock(return_value=("series", 10))), \
             patch.object(tmdb, "_get", new=AsyncMock(side_effect=get)):
            result = await tmdb.localized_details("series", "tt4158110", fallback)

        self.assertEqual(result["overview"], "Sinopse em português.")
        self.assertEqual(result["episodes"][0]["overview"], "Resumo em português.")
        self.assertEqual(result["episodes"][0]["title"], "Saudação inicial")

    async def test_home_catalog_uses_watched_items_as_personalized_seeds(self):
        async def category(path, media_type, **params):
            return [({"id": 1, "title": "Catalog result"}, media_type)]

        async def normalize(rows):
            return [{"id": f"tt{item['id']}", "media_type": media_type, "title": item["title"], "overview": "", "poster_url": None, "year": None, "tmdb_id": item["id"], "imdb_id": f"tt{item['id']}"} for item, media_type in rows]

        with patch.object(tmdb, "TMDB_API_TOKEN", "server-token"), \
             patch.object(tmdb, "_category", new=AsyncMock(side_effect=category)) as category_mock, \
             patch.object(tmdb, "_with_imdb_ids", new=AsyncMock(side_effect=normalize)):
            result = await tmdb.home_catalog([("movie", 77)])

        requested_paths = [call.args[0] for call in category_mock.await_args_list]
        self.assertIn("movie/77/recommendations", requested_paths)
        self.assertTrue(any(path.startswith("discover/movie") for path in requested_paths))
        self.assertEqual([section["id"] for section in result["sections"]], ["for-you", "releases", "trending"])


if __name__ == "__main__":
    unittest.main()

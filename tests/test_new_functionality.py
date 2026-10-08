import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = f"sqlite:///{(Path(tempfile.gettempdir()) / 'mlm-new-functionality-test.sqlite3').as_posix()}"
os.environ["SESSION_SECRET"] = "test-session-secret-value-at-least-32-characters"

from app.jobs.manager import create_job, get_job, update_job
from app.library import dynamic
from app.database.db import Base, SessionLocal, engine
from app.database.models import LibraryItem, TemporaryMedia
from app.main import TORRENT_RETENTION_SECONDS, _episode_strm_path, _merge_search_results, _rank_search_results, _run_episode_job, _run_torrent_download, _temporary_torrent_key, app, check_temporary_media_playback
from app.stremio.manifest import supports
from app.security import check_request_rate_limit
from fastapi.testclient import TestClient


class NewFunctionalityTests(unittest.TestCase):
    def test_torrent_tracking_allows_different_episode_files_from_the_same_pack(self):
        first = _temporary_torrent_key({"info_hash": "a" * 40, "file_idx": 2})
        second = _temporary_torrent_key({"info_hash": "a" * 40, "file_idx": 3})

        self.assertNotEqual(first, second)
        self.assertEqual(first, "a" * 40 + ":2")

    def test_torrent_worker_organizes_movie_and_episode_and_registers_expiry(self):
        Base.metadata.create_all(bind=engine)
        with tempfile.TemporaryDirectory() as directory:
            movie_hash = "d" * 40
            episode_hash = "e" * 40
            with SessionLocal() as session:
                session.query(TemporaryMedia).filter(TemporaryMedia.info_hash.in_([movie_hash, episode_hash + ":0"])).delete(synchronize_session=False)
                session.query(LibraryItem).filter(LibraryItem.id.in_(["movie:tt0123000", "series:tt0123000:1:2"])).delete(synchronize_session=False)
                session.commit()
            media = [
                ("movie", movie_hash, {"title": "Public Film", "year": 1930}, {"info_hash": movie_hash, "file_idx": None}),
                ("series", episode_hash, {"title": "Public Show", "year": 2020, "year_end": None, "episodes": [{"season_number": 1, "episode_number": 2, "title": "Second Episode"}]}, {"info_hash": episode_hash, "file_idx": 0}),
            ]

            async def fake_download(info_hash, staging_path, _max_bytes, _report, _file_idx):
                staging_path.mkdir(parents=True, exist_ok=False)
                video = staging_path / "source.mkv"
                video.write_bytes(b"authorized test media")
                return video

            with patch("app.main.MEDIA_ROOT", directory), patch("app.main.download_movie", new=AsyncMock(side_effect=fake_download)), patch("app.main.refresh_library", new=AsyncMock()):
                for media_type, info_hash, details, selected in media:
                    job = create_job("torrent_download", "test-user")
                    season, episode = (1, 2) if media_type == "series" else (None, None)
                    asyncio.run(_run_torrent_download(job["id"], media_type, "tt0123000", details, selected, season, episode))
                    result = get_job(job["id"])
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(result["result"]["retention_seconds"], TORRENT_RETENTION_SECONDS)

            movie_path = Path(directory) / "Public Film (1930)" / "Public Film (1930).mkv"
            episode_path = Path(directory) / "Public Show (2020)" / "Season 01" / "Public Show (2020) - S01E02.mkv"
            self.assertTrue(movie_path.is_file())
            self.assertTrue(episode_path.is_file())
            with SessionLocal() as session:
                movie_record = session.get(TemporaryMedia, movie_hash)
                episode_record = session.get(TemporaryMedia, episode_hash + ":0")
                self.assertEqual(movie_record.path, str(movie_path))
                self.assertEqual(episode_record.path, str(episode_path))
                self.assertGreater(movie_record.downloaded_at, 0)
                self.assertGreater(episode_record.downloaded_at, 0)

    def test_torrent_file_expires_after_eight_hours_and_waits_for_active_playback_to_end(self):
        Base.metadata.create_all(bind=engine)
        info_hash = "c" * 40
        user_id = "a1a1a1a1-1111-4111-8111-111111111111"
        jellyfin_item_id = "b2b2b2b2-2222-4222-8222-222222222222"
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "Public Film (1930)" / "Public Film (1930).mkv"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"movie")
            with SessionLocal() as session:
                session.query(TemporaryMedia).filter_by(info_hash=info_hash).delete()
                session.query(LibraryItem).filter_by(id="movie:tt0123000").delete()
                session.add(TemporaryMedia(info_hash=info_hash, imdb_id="tt0123000", title="Public Film", path=str(media), status="downloaded", downloaded_at=1000))
                session.add(LibraryItem(id="movie:tt0123000", media_type="movie", imdb_id="tt0123000", title="Public Film", path=str(media), stream_url=f"torrent:{info_hash}"))
                session.commit()
            active_session = {"UserId": user_id, "PlayState": {"IsPaused": False}, "NowPlayingItem": {"Id": jellyfin_item_id, "Path": str(media)}}
            with patch("app.main.MEDIA_ROOT", directory), patch("app.main.JELLYFIN_API_KEY", "configured"), \
                    patch("app.main.playback_sessions", new=AsyncMock(side_effect=[[], [active_session], []])), \
                    patch("app.main.refresh_library", new=AsyncMock()) as refresh:
                asyncio.run(check_temporary_media_playback(now=1000 + TORRENT_RETENTION_SECONDS - 1))
                self.assertTrue(media.exists())
                asyncio.run(check_temporary_media_playback(now=1000 + TORRENT_RETENTION_SECONDS))
                self.assertTrue(media.exists())
                asyncio.run(check_temporary_media_playback(now=1000 + TORRENT_RETENTION_SECONDS + 30))
            self.assertFalse(media.exists())
            refresh.assert_awaited_once()
            with SessionLocal() as session:
                self.assertIsNone(session.get(TemporaryMedia, info_hash))

    def test_search_ranks_titles_with_addon_streams_first(self):
        results = [
            {"id": "tt1234567", "imdb_id": "tt1234567", "media_type": "movie", "title": "Mr. Robot", "year": 2025},
            {"id": "tt4158110", "imdb_id": "tt4158110", "media_type": "series", "title": "Mr. Robot", "year": 2015},
            {"id": "tt0343818", "imdb_id": "tt0343818", "media_type": "movie", "title": "I, Robot", "year": 2004},
        ]

        async def streams_for(_media_type, imdb_id):
            if imdb_id == "tt4158110:1:1":
                return {"streams": [{"provider": "FrostStream"}], "addon_errors": []}
            return {"streams": [], "addon_errors": []}

        with patch.dict("app.main._SEARCH_AVAILABILITY_CACHE", {}, clear=True), patch(
            "app.main._streams_for", new=AsyncMock(side_effect=streams_for)
        ), patch("app.main.get_metadata_details", new=AsyncMock(return_value={"episodes": [
            {"id": "tt4158110:1:1", "season_number": 1, "episode_number": 1},
        ]})):
            ranked = asyncio.run(_rank_search_results("Mr. Robot", results))

        self.assertEqual(ranked[0]["imdb_id"], "tt4158110")
        self.assertEqual(ranked[0]["availability"], "available")
        self.assertEqual(ranked[0]["available_providers"], ["FrostStream"])
        self.assertEqual([item["availability"] for item in ranked[1:]], ["unavailable", "unavailable"])

    def test_addon_catalog_search_results_merge_by_imdb_id(self):
        cinemeta = [{"id": "tt0343818", "imdb_id": "tt0343818", "media_type": "movie", "title": "I, Robot", "year": 2004}]
        addon_results = [
            {"id": "tt0343818", "type": "movie", "name": "Eu, Robô", "catalog_provider": "Example"},
            {"id": "tt7654321", "type": "movie", "name": "Outro Filme", "catalog_provider": "Example"},
        ]

        merged = _merge_search_results(cinemeta, addon_results)

        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["catalog_providers"], ["Example"])
        self.assertEqual(merged[1]["imdb_id"], "tt7654321")
        self.assertEqual(merged[1]["poster_url"], None)

    def test_manifest_without_id_prefixes_supports_all_ids(self):
        manifest = {"types": ["series"], "resources": [{"name": "stream", "types": ["series"]}]}
        self.assertTrue(supports(manifest, "stream", "series", "tt4158110"))

    def test_dynamic_episode_url_is_signed_and_resolves_to_selected_stream(self):
        with patch("app.library.dynamic.PUBLIC_BASE_URL", "https://media.example.test"), patch(
            "app.library.dynamic.SESSION_SECRET", "test-session-secret-value-at-least-32-characters"
        ):
            target = dynamic.dynamic_strm_url("series", "tt4158110", "com.froststream", 1, 1)
            signature = target.split("sig=")[1]
            with TestClient(app) as client, patch(
                "app.main._streams_for",
                new=AsyncMock(return_value={"streams": [
                    {"provider_id": "com.froststream", "url": "https://video.example.test/expired.mkv"},
                    {"provider_id": "com.fenixflix", "url": "https://video.example.test/working.mkv"},
                ]}),
            ), patch("app.main._verified_provider", return_value="com.fenixflix"):
                response = client.get(target.replace("https://media.example.test", ""), follow_redirects=False)
                tampered = client.get(target.replace(signature, "0" * 64), follow_redirects=False)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers["location"], "https://video.example.test/working.mkv")
            self.assertEqual(len(signature), 64)
            self.assertEqual(tampered.status_code, 404)

    def test_search_requires_a_jellyfin_session(self):
        with TestClient(app) as client:
            response = client.get("/api/search", params={"query": "Mr. Robot"})
        self.assertEqual(response.status_code, 401)

    def test_job_progress_persists(self):
        job = create_job("test", "user-1", total=10)
        update_job(job["id"], status="running", completed=4, message="4/10")
        current = get_job(job["id"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["completed"], 4)

    def test_request_rate_limit_blocks_after_configured_window_count(self):
        identity = "test-rate-limit-user"
        self.assertTrue(check_request_rate_limit(identity, "test", 2))
        self.assertTrue(check_request_rate_limit(identity, "test", 2))
        self.assertFalse(check_request_rate_limit(identity, "test", 2))

    def test_series_sync_preserves_existing_episode_without_querying_addons(self):
        with tempfile.TemporaryDirectory() as directory:
            details = {"id": "series:tt1234567", "media_type": "series", "imdb_id": "tt1234567", "title": "Example Series", "year": 2020, "year_end": None,
                       "episodes": [{"season_number": 1, "episode_number": 1, "title": "Pilot"}]}
            path = Path(directory) / _episode_strm_path(details, 1, 1)
            path.parent.mkdir(parents=True)
            path.write_text("https://media.example.test/existing\n", encoding="utf-8")
            job = create_job("sync_series", "user-1", total=1)
            with patch("app.main.MEDIA_ROOT", directory), patch(
                "app.main._streams_for", new=AsyncMock(side_effect=AssertionError("existing files must not query addons"))
            ), patch("app.main.refresh_library", new=AsyncMock()) as refresh:
                asyncio.run(_run_episode_job(job["id"], details, details["episodes"], "sync_series"))
            current = get_job(job["id"])
            self.assertEqual(current["status"], "completed")
            self.assertEqual(current["result"]["skipped"], 1)
            refresh.assert_not_awaited()
            self.assertEqual(path.read_text(encoding="utf-8"), "https://media.example.test/existing\n")

    def test_local_movie_upload_is_organized_and_job_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            details = {"id": "movie:tt0123456", "media_type": "movie", "imdb_id": "tt0123456",
                       "title": "Example Movie", "year": 2022, "overview": ""}
            with patch("app.security.SESSION_SECRET", "test-session-secret-value-at-least-32-characters"), \
                    patch("app.main.COOKIE_SECURE", False), patch("app.main.MEDIA_ROOT", directory), \
                    patch("app.main.authenticate_user", new=AsyncMock(return_value={"id": "admin-1", "name": "Admin", "is_admin": True})), \
                    patch("app.main.get_metadata_details", new=AsyncMock(return_value=details)), \
                    patch("app.main.refresh_library", new=AsyncMock()):
                with TestClient(app) as client:
                    login = client.post("/api/login", json={"username": "Admin", "password": "test"})
                    csrf = login.json()["csrf_token"]
                    created = client.post("/api/jobs/import/movie/tt0123456", json={"filename": "source.mp4", "size": 11}, headers={"X-CSRF-Token": csrf})
                    self.assertEqual(created.status_code, 202, created.text)
                    uploaded = client.put(created.json()["upload_url"], files={"file": ("source.mp4", b"video-bytes", "video/mp4")}, headers={"X-CSRF-Token": csrf})
                    self.assertEqual(uploaded.status_code, 200, uploaded.text)
                    for _ in range(30):
                        state = client.get(f"/api/jobs/{created.json()['job_id']}").json()
                        if state["status"] in {"completed", "failed"}:
                            break
                        time.sleep(0.05)
                    self.assertEqual(state["status"], "completed", state)
            organized = Path(directory) / "Example Movie (2022)" / "Example Movie (2022).mp4"
            self.assertEqual(organized.read_bytes(), b"video-bytes")


if __name__ == "__main__":
    unittest.main()

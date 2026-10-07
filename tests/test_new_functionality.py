import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite:////tmp/mlm-new-functionality-test.sqlite3"
os.environ["SESSION_SECRET"] = "test-session-secret-value-at-least-32-characters"

from app.jobs.manager import create_job, get_job, update_job
from app.library import dynamic
from app.main import _episode_strm_path, _run_episode_job, app
from app.stremio.manifest import supports
from app.security import check_request_rate_limit
from fastapi.testclient import TestClient


class NewFunctionalityTests(unittest.TestCase):
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
                new=AsyncMock(return_value={"streams": [{"provider_id": "com.froststream", "url": "https://video.example.test/file.mkv"}]}),
            ):
                response = client.get(target.replace("https://media.example.test", ""), follow_redirects=False)
                tampered = client.get(target.replace(signature, "0" * 64), follow_redirects=False)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers["location"], "https://video.example.test/file.mkv")
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

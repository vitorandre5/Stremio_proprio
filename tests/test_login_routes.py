import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite:////tmp/mlm-login-route-test.sqlite3"
os.environ["SESSION_SECRET"] = "test-session-secret-value-at-least-32-characters"
os.environ["COOKIE_SECURE"] = "false"

from fastapi.testclient import TestClient
from app.main import app
from app import security


class JellyfinLoginRouteTests(unittest.TestCase):
    def test_login_uses_jellyfin_identity_and_session(self):
        with patch("app.security.SESSION_SECRET", "test-session-secret-value-at-least-32-characters"), patch("app.main.COOKIE_SECURE", False), patch("app.main.authenticate_user", new=AsyncMock(return_value={"id": "u1", "name": "Vitor", "is_admin": True})):
            with TestClient(app) as client:
                response = client.post("/api/login", json={"username": "Vitor", "password": "jf-password"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["user"]["name"], "Vitor")
                self.assertNotIn("access_token", response.json())
                session = client.get("/api/session")
                self.assertEqual(session.status_code, 200)
                self.assertTrue(session.json()["user"]["is_admin"])

    def test_admin_can_persist_stream_preferences(self):
        with patch("app.security.SESSION_SECRET", "test-session-secret-value-at-least-32-characters"), patch("app.main.COOKIE_SECURE", False), patch("app.main.authenticate_user", new=AsyncMock(return_value={"id": "u3", "name": "Admin", "is_admin": True})):
            with TestClient(app) as client:
                login = client.post("/api/login", json={"username": "Admin", "password": "jf-password"})
                csrf = login.json()["csrf_token"]
                response = client.patch("/api/preferences", json={"preferred_quality": "720p", "preferred_provider": "automatic"}, headers={"X-CSRF-Token": csrf})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["preferred_quality"], "720p")
                self.assertEqual(client.get("/api/preferences").json()["preferred_provider"], "automatic")

    def test_add_movie_keeps_existing_local_file_without_querying_addons(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Arrival (2016)" / "Arrival.mkv"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"keep this local media")
            with patch("app.security.SESSION_SECRET", "test-session-secret-value-at-least-32-characters"), patch("app.main.COOKIE_SECURE", False), patch("app.main.MEDIA_ROOT", directory), patch("app.main.authenticate_user", new=AsyncMock(return_value={"id": "u4", "name": "Admin", "is_admin": True})), patch("app.main.get_metadata_details", new=AsyncMock(return_value={"title": "Arrival", "year": 2016})):
                with TestClient(app) as client:
                    login = client.post("/api/login", json={"username": "Admin", "password": "jf-password"})
                    response = client.post("/api/library/add/movie/tt2543164", json={"url": "https://stream.example/video"}, headers={"X-CSRF-Token": login.json()["csrf_token"]})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["existing"])
            self.assertEqual(target.read_bytes(), b"keep this local media")

    def test_non_admin_jellyfin_user_cannot_modify_addons(self):
        with patch("app.security.SESSION_SECRET", "test-session-secret-value-at-least-32-characters"), patch("app.main.COOKIE_SECURE", False), patch("app.main.authenticate_user", new=AsyncMock(return_value={"id": "u2", "name": "Guest", "is_admin": False})):
            with TestClient(app) as client:
                login = client.post("/api/login", json={"username": "Guest", "password": "jf-password"})
                self.assertEqual(login.status_code, 200)
                csrf = login.json()["csrf_token"]
                response = client.post("/api/addons", json={"name": "test", "manifest_url": "https://example.test/manifest.json"}, headers={"X-CSRF-Token": csrf})
                self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()

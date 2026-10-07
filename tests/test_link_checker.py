import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["SESSION_SECRET"] = "test-session-secret-value-at-least-32-characters"
os.environ["DATABASE_URL"] = "sqlite:////tmp/mlm-link-check-test.sqlite3"

from app.jobs import link_checker
from app.library.dynamic import dynamic_strm_url
from app.database.db import Base
from app.database.models import LibraryItem, LinkCheck
from app.main import _write_strm


class LinkCheckerTests(unittest.TestCase):
    def test_stored_dynamic_episode_requires_valid_signature_and_identity(self):
        with patch("app.library.dynamic.PUBLIC_BASE_URL", "https://media.example.test"), \
                patch("app.jobs.link_checker.PUBLIC_BASE_URL", "https://media.example.test"), \
                patch("app.library.dynamic.SESSION_SECRET", os.environ["SESSION_SECRET"]):
            url = dynamic_strm_url("series", "tt4158110", "com.froststream", 1, 1)
            item = SimpleNamespace(stream_url=url, media_type="series", imdb_id="tt4158110")
            target = link_checker._stored_target(item)
            self.assertEqual(target[:5], ("series", "tt4158110", 1, 1, "com.froststream"))
            item.stream_url = url.replace("sig=", "sig=0")
            self.assertIsNone(link_checker._stored_target(item))
            item.stream_url = url
            item.imdb_id = "tt0000000"
            self.assertIsNone(link_checker._stored_target(item))

    def test_probe_uses_range_and_accepts_a_one_byte_response(self):
        sent_headers = []

        class Response:
            status_code = 206
            headers = {}

            async def aclose(self):
                return None

            async def aiter_raw(self):
                yield b"x"

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def build_request(self, method, url, headers):
                sent_headers.append((method, url, headers))
                return (method, url, headers)

            async def send(self, _request, stream=False):
                return Response()

        with patch.object(link_checker.httpx, "AsyncClient", side_effect=lambda **_kwargs: Client()), \
                patch.object(link_checker, "validate_public_https_url", side_effect=lambda url: url):
            valid, status = asyncio.run(link_checker._probe_stream({"url": "https://cdn.example.test/video"}))
        self.assertTrue(valid)
        self.assertEqual(status, 206)
        self.assertEqual(sent_headers[0][2], {"Range": "bytes=0-0"})

    def test_created_strm_uses_addon_url_and_checker_refreshes_that_url(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.library.dynamic.PUBLIC_BASE_URL", "https://media.example.test"), \
                patch("app.library.dynamic.SESSION_SECRET", os.environ["SESSION_SECRET"]), \
                patch("app.jobs.link_checker.PUBLIC_BASE_URL", "https://media.example.test"), \
                patch("app.jobs.link_checker.MEDIA_ROOT", directory), \
                patch("app.jobs.link_checker.validate_public_https_url", side_effect=lambda url: url), \
                patch("app.jobs.link_checker.SessionLocal", session_factory), \
                patch("app.main.SessionLocal", session_factory), \
                patch("app.main.MEDIA_ROOT", directory), \
                patch("app.main.start_task", side_effect=lambda task: task.close()), \
                patch("app.jobs.link_checker.find_direct_streams", new=AsyncMock(return_value={"streams": [{
                    "provider": "FenixFlix", "provider_id": "com.fenixflix", "quality": "1080p",
                    "language": "dubbed", "url": "https://cdn.example.test/video", "behavior_hints": {},
                }], "addon_errors": []})), \
                patch("app.jobs.link_checker._probe_stream", new=AsyncMock(return_value=(True, 206))):
            addon_url = "https://froststream.example.test/series/addon-stream-id"
            created = _write_strm(
                Path("Mr Robot (2015)") / "Season 01" / "Mr Robot - S01E01.strm",
                addon_url, "series:tt4158110:1:1", "series", "tt4158110", "eps1.0_hellofriend.mov",
            )
            self.assertTrue(created["added"])
            path = Path(created["path"])
            self.assertEqual(path.read_text(encoding="utf-8").strip(), addon_url)
            asyncio.run(link_checker.check_library_item("series:tt4158110:1:1"))
            with session_factory() as session:
                check = session.get(LinkCheck, "series:tt4158110:1:1")
                item = session.get(LibraryItem, "series:tt4158110:1:1")
                self.assertEqual(check.status, "available")
                self.assertEqual(check.http_status, 206)
                self.assertEqual(item.stream_url, "https://cdn.example.test/video")
            self.assertEqual(path.read_text(encoding="utf-8").strip(), "https://cdn.example.test/video")

            legacy_url = dynamic_strm_url("series", "tt4158110", "com.fenixflix", 1, 1)
            path.write_text(legacy_url + "\n", encoding="utf-8")
            with session_factory() as session:
                session.get(LibraryItem, "series:tt4158110:1:1").stream_url = legacy_url
                session.commit()
            asyncio.run(link_checker.check_library_item("series:tt4158110:1:1"))
            with session_factory() as session:
                item = session.get(LibraryItem, "series:tt4158110:1:1")
                self.assertEqual(item.stream_url, "https://cdn.example.test/video")
            self.assertEqual(path.read_text(encoding="utf-8").strip(), "https://cdn.example.test/video")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()

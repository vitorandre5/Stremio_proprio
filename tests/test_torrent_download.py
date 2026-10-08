import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.torrents.download import download_movie, magnet_uri


class TorrentDownloadTests(unittest.IsolatedAsyncioTestCase):
    def test_magnet_uri_accepts_only_hex_info_hash(self):
        self.assertEqual(magnet_uri("A" * 40), "magnet:?xt=urn:btih:" + "a" * 40)
        with self.assertRaises(ValueError):
            magnet_uri("https://untrusted.example/video.mkv")

    async def test_aria2_is_invoked_without_shell_and_selects_zero_based_file_index(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "job"
            async def spawn(*args, **kwargs):
                (stage / "movie.mkv").write_bytes(b"video")
                process = type("Process", (), {"returncode": 0, "kill": lambda self: None, "wait": AsyncMock()})()
                return process

            with patch("app.torrents.download.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=spawn)) as launch:
                result = await download_movie("b" * 40, stage, 100, lambda _: None, file_idx=0)

        self.assertEqual(result.name, "movie.mkv")
        args, kwargs = launch.await_args
        self.assertIn("--select-file=1", args)
        self.assertIn("--seed-time=0", args)
        self.assertEqual(args[-1], "magnet:?xt=urn:btih:" + "b" * 40)
        self.assertNotIn("shell", kwargs)


if __name__ == "__main__":
    unittest.main()

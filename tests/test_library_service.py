import tempfile
import unittest
from pathlib import Path

from app.library.service import scan_movie, scan_series


class LibraryStatusTests(unittest.TestCase):
    def test_series_status_detects_strm_and_preserves_local_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            season = root / "Mr. Robot (2015)" / "Season 01"
            season.mkdir(parents=True)
            (season / "Mr. Robot - S01E01.mkv").write_bytes(b"local video")
            (season / "Mr. Robot - S01E02.strm").write_text("https://stream.example/video", encoding="utf-8")

            status = scan_series(str(root), "Mr. Robot", 2015, None, [
                {"season_number": 1, "episode_number": 1},
                {"season_number": 1, "episode_number": 2},
                {"season_number": 1, "episode_number": 3},
            ])

        self.assertEqual([episode["status"] for episode in status["episodes"]], ["media", "strm", "missing"])

    def test_movie_status_finds_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            movie = Path(directory) / "Arrival (2016)"
            movie.mkdir()
            (movie / "Arrival.mkv").write_bytes(b"local video")
            status = scan_movie(directory, "Arrival", 2016)
        self.assertEqual(status["status"], "media")
        self.assertEqual(status["path"], "Arrival (2016)/Arrival.mkv")


if __name__ == "__main__":
    unittest.main()

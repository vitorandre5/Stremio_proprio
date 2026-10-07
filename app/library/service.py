from pathlib import Path
import re

MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".strm"}
EPISODE_CODE = re.compile(r"S(\d{1,2})E(\d{1,3})", re.IGNORECASE)


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:160] or "Untitled"


def series_folder_name(title: str, year: int | None, year_end: str | None = None) -> str:
    if not year:
        return safe_name(title)
    year_text = str(year) + (f"-{year_end}" if year_end else "")
    return safe_name(f"{title} ({year_text})")


def title_folder_name(title: str, year: int | None) -> str:
    return safe_name(f"{title} ({year})" if year else title)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def scan_series(root: str | Path, title: str, year: int | None, year_end: str | None, episodes: list[dict]) -> dict:
    media_root = Path(root).resolve()
    series_root = media_root / series_folder_name(title, year, year_end)
    found: dict[tuple[int, int], Path] = {}
    if series_root.is_dir():
        for path in series_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            match = EPISODE_CODE.search(path.name)
            if not match:
                continue
            key = (int(match.group(1)), int(match.group(2)))
            existing = found.get(key)
            if existing is None or (existing.suffix.lower() == ".strm" and path.suffix.lower() != ".strm"):
                found[key] = path
    items = []
    for episode in episodes:
        key = (episode["season_number"], episode["episode_number"])
        path = found.get(key)
        kind = "missing" if path is None else ("strm" if path.suffix.lower() == ".strm" else "media")
        items.append({
            "season_number": key[0],
            "episode_number": key[1],
            "status": kind,
            "path": _relative(path, media_root) if path else None,
        })
    return {"episodes": items}


def scan_movie(root: str | Path, title: str, year: int | None) -> dict:
    media_root = Path(root).resolve()
    movie_root = media_root / title_folder_name(title, year)
    paths = sorted(path for path in movie_root.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS) if movie_root.is_dir() else []
    path = paths[0] if paths else None
    return {
        "status": "missing" if path is None else ("strm" if path.suffix.lower() == ".strm" else "media"),
        "path": _relative(path, media_root) if path else None,
    }

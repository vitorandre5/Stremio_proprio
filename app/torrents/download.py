import asyncio
from pathlib import Path
import re


MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm"}


def magnet_uri(info_hash: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
        raise ValueError("Hash torrent invalido.")
    return f"magnet:?xt=urn:btih:{info_hash.lower()}"


async def download_movie(info_hash: str, staging_dir: Path, max_bytes: int, report, file_idx: int | None = None) -> Path:
    magnet = magnet_uri(info_hash)
    selection = [f"--select-file={file_idx + 1}", "--bt-remove-unselected-file=true"] if file_idx is not None else []
    staging_dir.mkdir(parents=True, exist_ok=False)
    process = await asyncio.create_subprocess_exec(
        "aria2c", "--dir", str(staging_dir), "--seed-time=0", "--seed-ratio=0",
        "--summary-interval=0", "--console-log-level=warn", "--enable-color=false", "--bt-stop-timeout=1800",
        "--file-allocation=none", "--allow-overwrite=false", *selection, magnet,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while process.returncode is None:
            await asyncio.sleep(0.5)
            size = sum(path.stat().st_size for path in staging_dir.rglob("*") if path.is_file())
            if size > max_bytes:
                process.kill()
                await process.wait()
                raise RuntimeError("O torrent excedeu o limite de armazenamento configurado.")
            report(size)
        if process.returncode != 0:
            raise RuntimeError(f"aria2c encerrou com codigo {process.returncode}.")
        if sum(path.stat().st_size for path in staging_dir.rglob("*") if path.is_file()) > max_bytes:
            raise RuntimeError("O torrent excedeu o limite de armazenamento configurado.")
        root = staging_dir.resolve()
        candidates = []
        for path in staging_dir.rglob("*"):
            if path.suffix.lower() not in MEDIA_EXTENSIONS or path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            if root not in resolved.parents or resolved.stat().st_size == 0:
                continue
            candidates.append(resolved)
        if not candidates:
            raise RuntimeError("O torrent terminou sem arquivo MP4, MKV ou WEBM.")
        return max(candidates, key=lambda path: path.stat().st_size)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise

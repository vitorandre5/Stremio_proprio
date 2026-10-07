import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from app.config import LINK_CHECK_TIMEOUT_SECONDS, MEDIA_ROOT, PUBLIC_BASE_URL
from app.database.db import SessionLocal
from app.database.models import Addon, LibraryItem, LinkCheck, Preference
from app.library.dynamic import verify_dynamic_signature
from app.stremio.manifest import validate_public_https_url
from app.stremio.resolver import find_direct_streams, sort_streams


_MOVIE_PATH = re.compile(r"^/stream/movie/(tt\d+)$")
_EPISODE_PATH = re.compile(r"^/stream/series/(tt\d+)/(\d{1,2})/(\d{1,3})$")
_SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization"}
logger = logging.getLogger("media_library_manager.link_checker")
_CHECK_SEMAPHORE = asyncio.Semaphore(3)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _save_check(item_id: str, status: str, message: str, stream: dict | None = None, http_status: int | None = None) -> None:
    with SessionLocal() as session:
        record = session.get(LinkCheck, item_id)
        if record is None:
            record = LinkCheck(library_item_id=item_id)
            session.add(record)
        record.status = status
        record.checked_at = _now()
        record.provider = stream.get("provider") if stream else None
        record.provider_id = stream.get("provider_id") if stream else None
        record.quality = stream.get("quality") if stream else None
        record.http_status = http_status
        record.message = message[:240]
        session.commit()


def _stored_target(item: LibraryItem) -> tuple[str, str, int | None, int | None, str, str] | None:
    if item.stream_url == "local":
        return None
    parsed = urlsplit(item.stream_url)
    base = urlsplit(PUBLIC_BASE_URL)
    if (parsed.scheme, parsed.hostname, parsed.port or 443) != ("https", base.hostname, base.port or 443):
        return None
    if parsed.username or parsed.password or parsed.fragment:
        return None
    movie = _MOVIE_PATH.fullmatch(parsed.path)
    episode = _EPISODE_PATH.fullmatch(parsed.path)
    if movie and item.media_type == "movie":
        media_type, imdb_id = "movie", movie.group(1)
        season = number = None
    elif episode and item.media_type == "series":
        media_type, imdb_id = "series", episode.group(1)
        season, number = int(episode.group(2)), int(episode.group(3))
    else:
        return None
    try:
        params = parse_qs(parsed.query, strict_parsing=True)
        provider = params.get("provider", [""])[0]
        signature = params.get("sig", [""])[0]
    except ValueError:
        return None
    if not provider or len(provider) > 160 or not re.fullmatch(r"[a-f0-9]{64}", signature):
        return None
    try:
        if not verify_dynamic_signature(media_type, imdb_id, provider, signature, season, number):
            return None
    except Exception:
        return None
    if item.imdb_id != imdb_id:
        return None
    return media_type, imdb_id, season, number, provider, signature


def _item_target(item: LibraryItem, preferred_provider: str = "") -> tuple[str, str, int | None, int | None, str] | None:
    legacy = _stored_target(item)
    if legacy is not None:
        return legacy[:5]
    if item.stream_url == "local":
        return None
    try:
        validate_public_https_url(item.stream_url)
    except Exception:
        return None
    movie = re.fullmatch(r"movie:(tt\d+)", item.id)
    episode = re.fullmatch(r"series:(tt\d+):(\d{1,2}):(\d{1,3})", item.id)
    if movie and item.media_type == "movie" and movie.group(1) == item.imdb_id:
        return "movie", item.imdb_id, None, None, preferred_provider
    if episode and item.media_type == "series" and episode.group(1) == item.imdb_id:
        return "series", item.imdb_id, int(episode.group(2)), int(episode.group(3)), preferred_provider
    return None


def _replace_managed_strm(path: Path, previous_url: str, new_url: str) -> bool:
    """Refresh only a manager-owned STRM whose current contents still match the DB."""
    if previous_url == new_url:
        return True
    try:
        if path.read_text(encoding="utf-8").strip() != previous_url:
            return False
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as temporary_file:
                temporary_file.write(new_url + "\n")
                temporary_path = Path(temporary_file.name)
            os.replace(temporary_path, path)
            return True
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
    except OSError:
        return False


def _store_direct_url(item_id: str, path: Path, previous_url: str, new_url: str) -> bool:
    with SessionLocal() as session:
        item = session.get(LibraryItem, item_id)
        if item is None or item.stream_url != previous_url:
            return False
        if not _replace_managed_strm(path, previous_url, new_url):
            return False
        item.stream_url = new_url
        session.commit()
    return True


async def _probe_stream(stream: dict) -> tuple[bool, int | None]:
    url = stream["url"]
    hints = stream.get("behavior_hints") or {}
    proxy_headers = hints.get("proxyHeaders") if isinstance(hints, dict) else None
    supplied = proxy_headers.get("request", {}) if isinstance(proxy_headers, dict) else {}
    headers = {
        key: value for key, value in supplied.items()
        if isinstance(key, str) and isinstance(value, str)
        and not any(char in key + value for char in "\r\n")
        and key.casefold() not in {"connection", "transfer-encoding", "upgrade"}
    } if isinstance(supplied, dict) else {}
    if not any(key.casefold() == "range" for key in headers):
        headers["Range"] = "bytes=0-0"

    current = url
    try:
        async with httpx.AsyncClient(timeout=LINK_CHECK_TIMEOUT_SECONDS, follow_redirects=False) as client:
            for redirect_number in range(4):
                validate_public_https_url(current)
                response = await client.send(client.build_request("GET", current, headers=headers), stream=True)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    if response.status_code not in {200, 206}:
                        status = response.status_code
                        await response.aclose()
                        return False, status
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                    if content_type in {"text/html", "application/json"}:
                        status = response.status_code
                        await response.aclose()
                        return False, status
                    try:
                        iterator = response.aiter_raw()
                        first_chunk = await asyncio.wait_for(anext(iterator), timeout=LINK_CHECK_TIMEOUT_SECONDS)
                    except (StopAsyncIteration, httpx.HTTPError, asyncio.TimeoutError):
                        await response.aclose()
                        return False, response.status_code
                    if not first_chunk:
                        await response.aclose()
                        return False, response.status_code
                    await response.aclose()
                    return True, response.status_code
                location = response.headers.get("location")
                if not location or redirect_number == 3:
                    await response.aclose()
                    return False, response.status_code
                next_url = urljoin(current, location)
                validate_public_https_url(next_url)
                old, new = urlsplit(current), urlsplit(next_url)
                if (old.scheme, old.hostname, old.port or 443) != (new.scheme, new.hostname, new.port or 443):
                    headers = {key: value for key, value in headers.items() if key.casefold() not in _SENSITIVE_HEADERS}
                await response.aclose()
                current = next_url
    except Exception:
        return False, None
    return False, None


async def _check_library_item(item_id: str) -> None:
    with SessionLocal() as session:
        item = session.get(LibraryItem, item_id)
        if item is None:
            return
        previous_check = session.get(LinkCheck, item_id)
        previous_provider = previous_check.provider_id if previous_check else ""
        target = _item_target(item, previous_provider or "")
        item_snapshot = {"id": item.id, "path": item.path, "stream_url": item.stream_url,
                         "media_type": item.media_type, "imdb_id": item.imdb_id,
                         "legacy": _stored_target(item) is not None}
        addons = session.query(Addon).filter(Addon.enabled.is_(True)).order_by(Addon.id).all()
        addon_config = [{"id": addon.id, "name": addon.name, "manifest_url": addon.manifest_url} for addon in addons]
        preference = session.get(Preference, 1)
        preferred_quality = preference.preferred_quality if preference else "1080p"

    _save_check(item_id, "checking", "Consultando addons e verificando o stream.")
    if target is None:
        _save_check(item_id, "invalid", "O STRM nao contem uma URL HTTPS publica valida do addon.")
        return
    root = Path(MEDIA_ROOT).resolve()
    media_path = Path(item_snapshot["path"]).resolve()
    if root not in media_path.parents or not media_path.is_file():
        _save_check(item_id, "missing_file", "O arquivo .strm não existe no volume de mídia.")
        return
    try:
        if media_path.read_text(encoding="utf-8").strip() != item_snapshot["stream_url"]:
            _save_check(item_id, "invalid", "O conteudo do STRM foi alterado fora do gerenciador.")
            return
    except (OSError, UnicodeError):
        _save_check(item_id, "invalid", "Não foi possível ler o arquivo .strm.")
        return

    media_type, imdb_id, season, episode, preferred_provider = target
    video_id = imdb_id if media_type == "movie" else f"{imdb_id}:{season}:{episode}"
    addon_config.sort(key=lambda addon: (0 if addon["id"] == preferred_provider else 1, addon["name"].casefold()))
    result = await find_direct_streams(addon_config, media_type, video_id)
    streams = result["streams"]
    sort_streams(streams, addon_config, preferred_quality)
    if not streams:
        _save_check(item_id, "no_source", "Nenhum addon ativo retornou um stream direto.")
        return

    last_status = None
    for stream in streams[:5]:
        usable, http_status = await _probe_stream(stream)
        if usable:
            if not _store_direct_url(item_id, media_path, item_snapshot["stream_url"], stream["url"]):
                _save_check(item_id, "invalid", "O STRM foi alterado durante a verificacao e foi preservado.")
                return
            message = (
                "URL direta do addon atualizada e respondendo."
                if item_snapshot["legacy"] or stream["url"] != item_snapshot["stream_url"]
                else "Stream respondeu a uma requisicao de teste."
            )
            _save_check(item_id, "available", message, stream, http_status)
            return
        last_status = http_status or last_status
    if item_snapshot["legacy"]:
        if not _store_direct_url(item_id, media_path, item_snapshot["stream_url"], streams[0]["url"]):
            _save_check(item_id, "invalid", "O STRM foi alterado durante a verificacao e foi preservado.")
            return
    _save_check(item_id, "unavailable", "Os streams retornados falharam na requisicao de teste.", streams[0], last_status)

async def check_library_item(item_id: str) -> None:
    async with _CHECK_SEMAPHORE:
        try:
            await _check_library_item(item_id)
        except Exception:
            logger.exception("Link verification failed for library item %s", item_id)
            _save_check(item_id, "error", "Falha interna durante a verificacao; sera tentada novamente.")


async def check_all_library_items() -> None:
    with SessionLocal() as session:
        item_ids = [item.id for item in session.query(LibraryItem.id).filter(LibraryItem.stream_url != "local").all()]
    await asyncio.gather(*(check_library_item(item_id) for item_id in item_ids), return_exceptions=True)


async def periodic_link_check(interval_seconds: int) -> None:
    interval = max(1, interval_seconds)
    while True:
        started = asyncio.get_running_loop().time()
        try:
            await check_all_library_items()
        except Exception:
            # A falha de uma rodada nao deve encerrar as verificacoes seguintes.
            pass
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(max(0, interval - elapsed))

from contextlib import asynccontextmanager
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
import unicodedata
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Path as PathParam, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import inspect, text
from typing import Literal

from app.database.db import Base, SessionLocal, engine
from app.database.models import Addon, Job, LibraryItem, LinkCheck, MetadataItem, Preference, TemporaryMedia
from app.jellyfin.client import authenticate_user, item_image as get_jellyfin_item_image, item_marked_played, playback_sessions, refresh_library, resume_items as get_resume_items, user_item as get_jellyfin_user_item, watched_media as get_watched_media
from app.library.dynamic import dynamic_signature, dynamic_strm_url, verify_dynamic_signature
from app.library.service import scan_movie, scan_series, series_folder_name, title_folder_name
from app.config import BESTCINE_MANIFEST_URL, BRAZUCA_TORRENTS_MANIFEST_URL, COOKIE_SECURE, FENIXFLIX_MANIFEST_URL, FROST_MANIFEST_URL, JELLYFIN_API_KEY, JELLYFIN_URL, LINK_CHECK_INTERVAL_SECONDS, MAX_TORRENT_BYTES, MAX_UPLOAD_BYTES, MEDIA_ROOT, TMDB_API_TOKEN
from app.metadata.cinemeta import details as get_metadata_details
from app.metadata.service import details as get_localized_metadata_details
from app.metadata.service import localize_results as localize_metadata_results
from app.metadata.service import search as search_metadata
from app.metadata.tmdb import home_catalog as get_tmdb_home_catalog, tmdb_id_for_imdb
from app.stremio.client import read_manifest as read_addon_manifest
from app.stremio.resolver import find_catalog_search_results, find_direct_streams, sort_streams, sort_torrents
from app.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    create_session,
    require_admin_mutation,
    session_user,
    new_csrf_token,
    require_mutation,
    require_session,
    check_login_rate_limit,
    record_login_attempt,
    check_request_rate_limit,
)
from app.jobs.manager import can_access_job, create_job, get_job, mark_interrupted_jobs, start_task, update_job
from app.jobs.link_checker import check_library_item, periodic_link_check
from app.proxy.service import has_proxy_headers, proxy_stream
from app.torrents.download import download_movie


logger = logging.getLogger("media_library_manager")
_SEARCH_AVAILABILITY_CACHE: dict[str, tuple[float, str, list[str]]] = {}


async def check_temporary_media_playback() -> None:
    sessions = await playback_sessions()
    with SessionLocal() as session:
        records = [
            {"info_hash": item.info_hash, "path": item.path, "playback_started": item.playback_started,
             "jellyfin_item_id": item.jellyfin_item_id, "jellyfin_user_id": item.jellyfin_user_id}
            for item in session.query(TemporaryMedia).filter(TemporaryMedia.status == "downloaded").all()
        ]
    for record in records:
        target = str(Path(record["path"]).resolve())
        matching = [item for item in sessions if str(Path((item.get("NowPlayingItem") or {}).get("Path", "")).resolve()) == target]
        if matching:
            playback = matching[0]
            item_id = (playback.get("NowPlayingItem") or {}).get("Id")
            user_id = playback.get("UserId")
            is_playing = not (playback.get("PlayState") or {}).get("IsPaused", False)
            if is_playing and isinstance(item_id, str) and isinstance(user_id, str):
                with SessionLocal() as session:
                    current = session.get(TemporaryMedia, record["info_hash"])
                    if current:
                        current.playback_started = True
                        current.jellyfin_item_id = item_id
                        current.jellyfin_user_id = user_id
                        session.commit()
            continue
        if not record["playback_started"] or not record["jellyfin_item_id"] or not record["jellyfin_user_id"]:
            continue
        if not await item_marked_played(record["jellyfin_user_id"], record["jellyfin_item_id"]):
            continue
        root = Path(MEDIA_ROOT).resolve()
        media_path = Path(record["path"]).resolve()
        if root not in media_path.parents or media_path.suffix.lower() not in {".mp4", ".mkv", ".webm"}:
            logger.error("Refusing to remove temporary media outside allowed library path: %s", record["info_hash"])
            continue
        try:
            media_path.unlink(missing_ok=True)
            with SessionLocal() as session:
                current = session.get(TemporaryMedia, record["info_hash"])
                if current and str(Path(current.path).resolve()) == str(media_path):
                    item = session.query(LibraryItem).filter(LibraryItem.path == str(media_path)).one_or_none()
                    if item:
                        session.delete(item)
                    session.delete(current)
                    session.commit()
            try:
                await refresh_library()
            except HTTPException as exc:
                logger.warning("Temporary media removed; Jellyfin refresh failed: %s", exc.detail)
        except OSError:
            logger.exception("Could not remove watched temporary media: %s", record["info_hash"])


async def periodic_temporary_media_cleanup() -> None:
    while True:
        if JELLYFIN_API_KEY:
            try:
                await check_temporary_media_playback()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Temporary media playback monitor failed")
        await asyncio.sleep(30)


def clear_interrupted_torrent_staging() -> None:
    staging_root = (Path(MEDIA_ROOT).resolve() / ".mlm-torrents").resolve()
    if not staging_root.is_dir():
        return
    with SessionLocal() as session:
        job_ids = [job_id for (job_id,) in session.query(Job.id).filter(Job.kind == "torrent_download", Job.status == "failed").all()]
    for job_id in job_ids:
        candidate = (staging_root / job_id).resolve()
        if staging_root in candidate.parents and candidate.is_dir():
            shutil.rmtree(candidate, ignore_errors=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    check_columns = {column["name"] for column in inspect(engine).get_columns("link_checks")}
    if "provider_id" not in check_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE link_checks ADD COLUMN provider_id VARCHAR(160)"))
    mark_interrupted_jobs()
    clear_interrupted_torrent_staging()
    with SessionLocal() as session:
        for addon_id, name, manifest_url in (
            ("com.froststream", "FrostStream", FROST_MANIFEST_URL),
            ("com.bestcine.multisource", "BestCine", BESTCINE_MANIFEST_URL),
            ("com.fenixflix", "FenixFlix", FENIXFLIX_MANIFEST_URL),
            ("com.stremio.brazuca.addon", "Brazuca Torrents", BRAZUCA_TORRENTS_MANIFEST_URL),
        ):
            if session.get(Addon, addon_id) is None:
                session.add(Addon(id=addon_id, name=name, manifest_url=manifest_url, enabled=True))
        if session.get(Preference, 1) is None:
            session.add(Preference(id=1))
        session.commit()
    link_check_task = asyncio.create_task(periodic_link_check(LINK_CHECK_INTERVAL_SECONDS))
    temporary_media_task = asyncio.create_task(periodic_temporary_media_cleanup())
    try:
        yield
    finally:
        link_check_task.cancel()
        temporary_media_task.cancel()
        try:
            await link_check_task
        except asyncio.CancelledError:
            pass
        try:
            await temporary_media_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Media Library Manager", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def limit_expensive_requests(request: Request, call_next):
    path = request.url.path
    if path == "/api/search":
        scope, limit, window = "search", 40, 60
    elif path.startswith("/api/streams/"):
        scope, limit, window = "streams", 90, 60
    elif path.startswith("/stream/"):
        scope, limit, window = "dynamic-stream", 120, 60
    elif path.startswith("/api/addons"):
        scope, limit, window = "addons", 60 if request.method == "GET" else 20, 60
    elif path.startswith("/api/library/"):
        scope, limit, window = "library", 30, 60
    elif path.startswith("/api/jobs/") and path.endswith("/events"):
        scope, limit, window = "job-events", 20, 60
    elif path.endswith("/upload") and request.method == "PUT":
        scope, limit, window = "upload", 10, 3600
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        if content_length > MAX_UPLOAD_BYTES + 1024 * 1024:
            return JSONResponse({"detail": "O upload excede o limite configurado."}, status_code=413)
    elif path.startswith("/api/jobs/") and request.method in {"POST", "PUT"}:
        scope, limit, window = "jobs", 20, 60
    else:
        return await call_next(request)

    user = session_user(request.cookies.get(SESSION_COOKIE))
    identity = f"user:{user['user_id']}" if user else f"client:{request.client.host if request.client else 'unknown'}"
    if not check_request_rate_limit(identity, scope, limit, window):
        return JSONResponse({"detail": "Muitas solicitações. Aguarde antes de tentar novamente."}, status_code=429)
    return await call_next(request)


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)


class StreamChoice(BaseModel):
    url: str = Field(min_length=1, max_length=4096)


class TorrentChoice(BaseModel):
    info_hash: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    provider_id: str = Field(min_length=1, max_length=160)
    file_idx: int | None = Field(default=None, ge=0)


class AddonPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    manifest_url: str = Field(min_length=1, max_length=2048)


class AddonEnabledPayload(BaseModel):
    enabled: bool


class PreferencesPayload(BaseModel):
    preferred_quality: Literal["2160p", "1440p", "1080p", "720p", "480p"]
    preferred_provider: str = Field(min_length=1, max_length=160)


class ImportJobPayload(BaseModel):
    filename: str = Field(min_length=1, max_length=260)
    size: int = Field(gt=0, le=MAX_UPLOAD_BYTES)


@app.get("/api/session")
async def session_status(request: Request, _: None = Depends(require_session)) -> dict:
    return {"authenticated": True, "csrf_token": request.cookies.get(CSRF_COOKIE), "user": session_user(request.cookies.get(SESSION_COOKIE))}


@app.get("/api/library/resume")
async def library_resume(request: Request, _: None = Depends(require_session)) -> dict:
    user = request.state.user
    records = await get_resume_items(user["user_id"])
    items = []
    base_url = JELLYFIN_URL.rstrip("/")
    for record in records:
        item_id = record.get("Id")
        if not isinstance(item_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{32,36}", item_id):
            continue
        runtime = int(record.get("RunTimeTicks") or 0)
        user_data = record.get("UserData") if isinstance(record.get("UserData"), dict) else {}
        position = int(user_data.get("PlaybackPositionTicks") or 0)
        if runtime <= 0 or position <= 0:
            continue
        progress = float(user_data.get("PlayedPercentage") or (position / runtime * 100))
        if progress >= 100:
            continue
        is_episode = record.get("Type") == "Episode"
        season = record.get("ParentIndexNumber")
        episode = record.get("IndexNumber")
        episode_code = f"S{int(season):02d}E{int(episode):02d}" if is_episode and isinstance(season, int) and isinstance(episode, int) else ""
        image_id = record.get("SeriesId") if is_episode else item_id
        if not isinstance(image_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{32,36}", image_id):
            image_id = item_id
        items.append({
            "id": item_id,
            "title": record.get("SeriesName") if is_episode and record.get("SeriesName") else record.get("Name", "Sem título"),
            "subtitle": f"{episode_code} · {record.get('Name', '')}".strip(" ·") if is_episode else "Filme",
            "media_type": "series" if is_episode else "movie",
            "progress": max(0, min(progress, 99.9)),
            "image_url": f"/api/jellyfin/items/{quote(image_id, safe='')}/image",
            "open_url": f"{base_url}/web/index.html#!/details?id={quote(item_id, safe='')}",
        })
    return {"items": items}


@app.get("/api/home/catalog")
async def home_catalog(request: Request, _: None = Depends(require_session)) -> dict:
    if not TMDB_API_TOKEN:
        return {"configured": False, "sections": []}
    user_id = request.state.user["user_id"]
    try:
        history = await get_watched_media(user_id, limit=60)
    except HTTPException:
        history = []
    seeds: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    hydrated_series: dict[str, dict | None] = {}
    for record in history:
        media_type = "movie" if record.get("Type") == "Movie" else "series" if record.get("Type") == "Episode" else None
        if media_type == "series":
            series_id = record.get("SeriesId")
            if not isinstance(series_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{32,36}", series_id):
                continue
            if series_id not in hydrated_series:
                hydrated_series[series_id] = await get_jellyfin_user_item(user_id, series_id)
            record = hydrated_series[series_id]
            if not record:
                continue
            media_type = "series"
        if media_type is None:
            continue
        provider_ids = record.get("ProviderIds") if isinstance(record.get("ProviderIds"), dict) else {}
        raw_tmdb_id = provider_ids.get("Tmdb") or provider_ids.get("tmdb")
        tmdb_id = int(raw_tmdb_id) if isinstance(raw_tmdb_id, (int, str)) and str(raw_tmdb_id).isdigit() else None
        if tmdb_id is None:
            imdb_id = provider_ids.get("Imdb") or provider_ids.get("imdb")
            if isinstance(imdb_id, str) and re.fullmatch(r"tt\d+", imdb_id):
                try:
                    tmdb_id = await tmdb_id_for_imdb(imdb_id, media_type)
                except Exception:
                    tmdb_id = None
        if tmdb_id is None:
            continue
        key = (media_type, tmdb_id)
        if key not in seen:
            seen.add(key)
            seeds.append(key)
        if len(seeds) >= 4:
            break
    return await get_tmdb_home_catalog(seeds)


@app.get("/api/jellyfin/items/{item_id}/image")
async def jellyfin_item_image(item_id: str, _: None = Depends(require_session)) -> Response:
    content, media_type = await get_jellyfin_item_image(item_id)
    return Response(content=content, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/login")
async def login(payload: LoginPayload, request: Request, response: Response) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    check_login_rate_limit(client_ip)
    try:
        user = await authenticate_user(payload.username.strip(), payload.password)
    except HTTPException as exc:
        if exc.status_code == 401:
            record_login_attempt(client_ip, succeeded=False)
        raise
    record_login_attempt(client_ip, succeeded=True)
    csrf_token = new_csrf_token()
    response.set_cookie(SESSION_COOKIE, create_session(user), max_age=604800, httponly=True, secure=COOKIE_SECURE, samesite="strict", path="/")
    response.set_cookie(CSRF_COOKIE, csrf_token, max_age=604800, httponly=False, secure=COOKIE_SECURE, samesite="strict", path="/")
    return {"authenticated": True, "csrf_token": csrf_token, "user": user}


@app.post("/api/logout")
async def logout(response: Response, _: None = Depends(require_mutation)) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"authenticated": False}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/search")
async def search(
    _: None = Depends(require_session),
    query: str = Query(min_length=1, max_length=100),
    media_type: Literal["all", "movie", "series"] = "all",
) -> dict:
    normalized_query = query.strip()
    if not normalized_query:
        return {"results": []}

    metadata_results = await search_metadata(normalized_query, media_type)
    with SessionLocal() as session:
        addons = session.query(Addon).filter(Addon.enabled.is_(True)).order_by(Addon.id).all()
        addon_config = [{"id": addon.id, "name": addon.name, "manifest_url": addon.manifest_url} for addon in addons]
    catalog_metas = []
    catalog_errors = []
    media_types = ["movie", "series"] if media_type == "all" else [media_type]
    for requested_type in media_types:
        catalog_response = await find_catalog_search_results(addon_config, requested_type, normalized_query)
        catalog_metas.extend(catalog_response["metas"])
        catalog_errors.extend(catalog_response["addon_errors"])
    merged_results = await localize_metadata_results(_merge_search_results(metadata_results, catalog_metas))
    results = await _rank_search_results(normalized_query, merged_results)
    unique_errors = {(item["provider"], item["detail"]): item for item in catalog_errors}
    with SessionLocal() as session:
        for item in results:
            record = session.get(MetadataItem, item["id"])
            if record is None:
                record = MetadataItem(id=item["id"])
                session.add(record)
            for field in ("media_type", "tmdb_id", "imdb_id", "title", "year", "poster_url", "overview"):
                setattr(record, field, item.get(field))
        session.commit()
    return {"results": results, "addon_search_errors": list(unique_errors.values())}


def _merge_search_results(metadata_results: list[dict], catalog_metas: list[dict]) -> list[dict]:
    merged = [dict(item) for item in metadata_results]
    by_imdb_id = {
        (item.get("media_type"), item.get("imdb_id")): index
        for index, item in enumerate(merged)
        if isinstance(item.get("imdb_id"), str)
    }
    for meta in catalog_metas:
        media_type = meta.get("type") or meta.get("media_type")
        imdb_id = meta.get("imdb_id") or meta.get("id")
        if media_type not in {"movie", "series"} or not isinstance(imdb_id, str) or not re.fullmatch(r"tt\d+", imdb_id):
            continue
        key = (media_type, imdb_id)
        index = by_imdb_id.get(key)
        if index is None:
            year_text = meta.get("releaseInfo") or meta.get("year")
            year = int(year_text[:4]) if isinstance(year_text, str) and len(year_text) >= 4 and year_text[:4].isdigit() else None
            tmdb_value = meta.get("moviedb_id")
            tmdb_id = int(tmdb_value) if isinstance(tmdb_value, int) or (isinstance(tmdb_value, str) and tmdb_value.isdigit()) else None
            merged.append({
                "id": imdb_id,
                "media_type": media_type,
                "imdb_id": imdb_id,
                "tmdb_id": tmdb_id,
                "title": meta.get("name") or "Título sem nome",
                "year": year,
                "poster_url": meta.get("poster"),
                "overview": meta.get("description") or meta.get("overview") or "",
                "catalog_providers": [meta["catalog_provider"]],
            })
            by_imdb_id[key] = len(merged) - 1
        else:
            providers = merged[index].setdefault("catalog_providers", [])
            if meta["catalog_provider"] not in providers:
                providers.append(meta["catalog_provider"])
    return merged


def _search_title_rank(query: str, title: str) -> int:
    def normalize(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", value.casefold())
        without_marks = "".join(character for character in decomposed if not unicodedata.combining(character))
        return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()

    normalized_query = normalize(query)
    normalized_title = normalize(title)
    if normalized_title == normalized_query:
        return 0
    if normalized_title.startswith(normalized_query):
        return 1
    if normalized_query and normalized_query in normalized_title:
        return 2
    query_terms = normalized_query.split()
    if query_terms and all(term in normalized_title.split() for term in query_terms):
        return 3
    return 4


def _search_item_title_rank(query: str, item: dict) -> int:
    titles = list(item.get("search_titles")) if isinstance(item.get("search_titles"), list) else []
    titles.extend([item.get("title", ""), item.get("original_title", "")])
    return min((_search_title_rank(query, title) for title in titles if isinstance(title, str) and title), default=4)


async def _rank_search_results(query: str, results: list[dict]) -> list[dict]:
    semaphore = asyncio.Semaphore(8)
    now = time.monotonic()

    async def availability(item: dict) -> tuple[str, list[str]]:
        media_type = item.get("media_type")
        imdb_id = item.get("imdb_id") or item.get("id")
        if media_type not in {"movie", "series"} or not isinstance(imdb_id, str) or not re.fullmatch(r"tt\d+", imdb_id):
            return "unknown", []
        cache_key = f"{media_type}:{imdb_id}"
        cached = _SEARCH_AVAILABILITY_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1], cached[2]
        async with semaphore:
            try:
                async with asyncio.timeout(8):
                    video_id = imdb_id
                    if media_type == "series":
                        details = await get_metadata_details("series", imdb_id)
                        first_episode = next(iter(details.get("episodes") or []), None)
                        if not first_episode:
                            return "unknown", []
                        video_id = first_episode.get("id") or f"{imdb_id}:{first_episode['season_number']}:{first_episode['episode_number']}"
                    streams = await _streams_for(media_type, video_id)
            except Exception:
                _SEARCH_AVAILABILITY_CACHE[cache_key] = (time.monotonic() + 30, "unknown", [])
                return "unknown", []
        providers = list(dict.fromkeys(stream["provider"] for stream in [*streams["streams"], *streams.get("torrents", [])]))
        # A series has no single stream ID: Stremio requests per-video IDs. A
        # negative result for its first episode cannot prove the whole series is unavailable.
        status = "available" if providers else "unknown" if streams["addon_errors"] or media_type == "series" else "unavailable"
        _SEARCH_AVAILABILITY_CACHE[cache_key] = (time.monotonic() + (300 if status != "unknown" else 30), status, providers)
        return status, providers

    checks = await asyncio.gather(*(availability(item) for item in results))
    availability_rank = {"available": 0, "unknown": 1, "unavailable": 2}
    ranked = []
    for original_rank, (item, (status, providers)) in enumerate(zip(results, checks)):
        ranked_item = {**item, "availability": status, "available_providers": providers}
        source_rank = 0 if status == "available" else 1 if item.get("catalog_providers") else availability_rank[status] + 1
        ranked.append((source_rank, _search_item_title_rank(query, item), original_rank, ranked_item))
    ranked.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in ranked]


@app.get("/api/title/{media_type}/{imdb_id}")
async def title_details(
    media_type: Literal["movie", "series"], imdb_id: str, _: None = Depends(require_session)
) -> dict:
    details = await get_localized_metadata_details(media_type, imdb_id)
    with SessionLocal() as session:
        record = session.get(MetadataItem, details["id"])
        if record is None:
            record = MetadataItem(id=details["id"])
            session.add(record)
        for field in ("media_type", "tmdb_id", "imdb_id", "title", "year", "poster_url", "overview"):
            setattr(record, field, details.get(field))
        session.commit()
    if media_type == "movie":
        details["library_status"] = scan_movie(MEDIA_ROOT, details["title"], details.get("year"))
        details["library_status"]["link_check"] = _get_link_check(f"movie:{imdb_id}")
    else:
        details["library_status"] = scan_series(MEDIA_ROOT, details["title"], details.get("year"), details.get("year_end"), details.get("episodes", []))
        checks = _get_link_checks(imdb_id)
        for episode in details["library_status"]["episodes"]:
            episode["link_check"] = checks.get(f"series:{imdb_id}:{episode['season_number']}:{episode['episode_number']}")
    return details


def _get_link_checks(imdb_id: str) -> dict[str, dict]:
    with SessionLocal() as session:
        rows = session.query(LinkCheck).join(LibraryItem, LinkCheck.library_item_id == LibraryItem.id).filter(LibraryItem.imdb_id == imdb_id).all()
        return {row.library_item_id: {"status": row.status, "checked_at": row.checked_at, "provider": row.provider,
                                      "provider_id": row.provider_id,
                                      "quality": row.quality, "http_status": row.http_status, "message": row.message}
                for row in rows}


def _get_link_check(item_id: str) -> dict | None:
    with SessionLocal() as session:
        row = session.get(LinkCheck, item_id)
        if row is None:
            return None
        return {"status": row.status, "checked_at": row.checked_at, "provider": row.provider,
                "provider_id": row.provider_id,
                "quality": row.quality, "http_status": row.http_status, "message": row.message}


def _verified_provider(item_id: str) -> str | None:
    check = _get_link_check(item_id)
    return check.get("provider_id") if check and check.get("status") == "available" else None


@app.get("/api/library/checks/{media_type}/{imdb_id}")
async def library_checks(media_type: Literal["movie", "series"], imdb_id: str, _: None = Depends(require_session)) -> dict:
    if not re.fullmatch(r"tt\d+", imdb_id):
        raise HTTPException(status_code=422, detail="IMDb ID invalido.")
    checks = _get_link_checks(imdb_id)
    if media_type == "movie":
        return {"movie": checks.get(f"movie:{imdb_id}")}
    return {"episodes": {"{}:{}".format(*item_id.split(":")[-2:]): check for item_id, check in checks.items() if item_id.startswith(f"series:{imdb_id}:")}}


@app.get("/api/addons")
async def list_addons(_: None = Depends(require_session)) -> dict:
    with SessionLocal() as session:
        addons = session.query(Addon).order_by(Addon.id).all()
        for addon in addons:
            if not addon.manifest_json:
                try:
                    addon.manifest_json = json.dumps(await read_addon_manifest(addon.manifest_url))
                    session.commit()
                except HTTPException:
                    session.rollback()
        return {"addons": [{"id": addon.id, "name": addon.name, "manifest_url": addon.manifest_url, "enabled": addon.enabled, "manifest": json.loads(addon.manifest_json) if addon.manifest_json else None} for addon in addons]}


@app.post("/api/addons")
async def add_addon(payload: AddonPayload, _: None = Depends(require_admin_mutation)) -> dict:
    manifest = await read_addon_manifest(payload.manifest_url)
    with SessionLocal() as session:
        existing = session.query(Addon).filter(Addon.manifest_url == payload.manifest_url).first()
        if existing:
            raise HTTPException(status_code=409, detail="Esse Manifest URL já está cadastrado.")
        addon = Addon(id=manifest["id"], name=payload.name.strip(), manifest_url=payload.manifest_url, enabled=True, manifest_json=json.dumps(manifest))
        session.add(addon)
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="Já existe um addon cadastrado com este ID.") from exc
    return {"id": manifest["id"], "name": payload.name.strip(), "manifest": manifest}


@app.patch("/api/addons/{addon_id}")
async def set_addon_enabled(addon_id: str, payload: AddonEnabledPayload, _: None = Depends(require_admin_mutation)) -> dict:
    with SessionLocal() as session:
        addon = session.get(Addon, addon_id)
        if addon is None:
            raise HTTPException(status_code=404, detail="Addon não encontrado.")
        addon.enabled = payload.enabled
        session.commit()
    return {"id": addon_id, "enabled": payload.enabled}


@app.get("/api/preferences")
async def get_preferences(_: None = Depends(require_session)) -> dict:
    with SessionLocal() as session:
        preference = session.get(Preference, 1) or Preference(id=1)
        return {"preferred_quality": preference.preferred_quality, "preferred_provider": preference.preferred_provider}


@app.patch("/api/preferences")
async def update_preferences(payload: PreferencesPayload, _: None = Depends(require_admin_mutation)) -> dict:
    with SessionLocal() as session:
        if payload.preferred_provider != "automatic":
            addon = session.get(Addon, payload.preferred_provider)
            if addon is None or not addon.enabled:
                raise HTTPException(status_code=422, detail="Selecione um addon ativo ou Automatico.")
        preference = session.get(Preference, 1)
        if preference is None:
            preference = Preference(id=1)
            session.add(preference)
        preference.preferred_quality = payload.preferred_quality
        preference.preferred_provider = payload.preferred_provider
        session.commit()
    return payload.model_dump()


async def _streams_for(media_type: str, video_id: str, preferred_provider_override: str | None = None) -> dict:
    with SessionLocal() as session:
        addons = session.query(Addon).filter(Addon.enabled.is_(True)).order_by(Addon.id).all()
        config = [{"id": addon.id, "name": addon.name, "manifest_url": addon.manifest_url} for addon in addons]
        preference = session.get(Preference, 1)
        preferred_provider = preferred_provider_override or (preference.preferred_provider if preference else "automatic")
        preferred_quality = preference.preferred_quality if preference else "1080p"
    config.sort(key=lambda addon: (
        0 if preferred_provider != "automatic" and addon["id"] == preferred_provider else 1,
        0 if addon["name"].casefold() == "froststream" else 1,
        addon["name"].casefold(),
    ))
    result = await find_direct_streams(config, media_type, video_id)
    sort_streams(result["streams"], config, preferred_quality)
    sort_torrents(result.get("torrents", []), config, preferred_quality)
    return result

@app.get("/api/streams/movie/{imdb_id}")
async def movie_streams(imdb_id: str, _: None = Depends(require_session)) -> dict:
    if not re.fullmatch(r"tt\d+", imdb_id):
        raise HTTPException(status_code=422, detail="IMDb ID inválido.")
    return await _streams_for("movie", imdb_id)


@app.get("/api/streams/series/{imdb_id}/{season}/{episode}")
async def episode_streams(
    imdb_id: str,
    season: int = PathParam(ge=0, le=99),
    episode: int = PathParam(gt=0, le=999),
    _: None = Depends(require_session),
) -> dict:
    if not re.fullmatch(r"tt\d+", imdb_id):
        raise HTTPException(status_code=422, detail="IMDb ID inválido.")
    video_id = f"{imdb_id}:{season}:{episode}"
    return await _streams_for("series", video_id)


async def _resolve_dynamic_stream(
    request: Request,
    media_type: str,
    imdb_id: str,
    provider: str,
    signature: str,
    season: int | None = None,
    episode: int | None = None,
):
    if not re.fullmatch(r"tt\d+", imdb_id) or len(provider) > 160:
        raise HTTPException(status_code=404, detail="STRM dinamico invalido.")
    if not verify_dynamic_signature(media_type, imdb_id, provider, signature, season, episode):
        raise HTTPException(status_code=404, detail="Assinatura do STRM invalida.")
    video_id = imdb_id if media_type == "movie" else f"{imdb_id}:{season}:{episode}"
    item_id = f"movie:{imdb_id}" if media_type == "movie" else f"series:{imdb_id}:{season}:{episode}"
    selected_provider = _verified_provider(item_id) or provider
    result = await _streams_for(media_type, video_id, preferred_provider_override=selected_provider)
    if not result["streams"]:
        logger.warning("No direct stream for dynamic item %s (%s)", imdb_id, media_type)
        raise HTTPException(status_code=503, detail="Nenhum stream direto esta disponivel neste momento.")
    selected = next((stream for stream in result["streams"] if stream["provider_id"] == selected_provider), result["streams"][0])
    if has_proxy_headers(selected):
        return await proxy_stream(selected["url"], request, selected["behavior_hints"])
    return RedirectResponse(selected["url"], status_code=307)


@app.get("/stream/movie/{imdb_id}")
async def dynamic_movie_stream(
    request: Request,
    imdb_id: str,
    provider: str = Query(min_length=1, max_length=160),
    sig: str = Query(min_length=64, max_length=64),
):
    return await _resolve_dynamic_stream(request, "movie", imdb_id, provider, sig)


@app.get("/stream/series/{imdb_id}/{season}/{episode}")
async def dynamic_episode_stream(
    request: Request,
    imdb_id: str,
    season: int = PathParam(ge=0, le=99),
    episode: int = PathParam(gt=0, le=999),
    provider: str = Query(min_length=1, max_length=160),
    sig: str = Query(min_length=64, max_length=64),
):
    return await _resolve_dynamic_stream(request, "series", imdb_id, provider, sig, season, episode)


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:160] or "Sem título"


def _write_strm(relative_path: Path, url: str, item_id: str, media_type: str, imdb_id: str, title: str) -> dict:
    root = Path(MEDIA_ROOT).resolve()
    target = (root / relative_path).resolve()
    if root not in target.parents:
        raise HTTPException(status_code=422, detail="Caminho de mídia inválido.")
    if urlsplit(url).scheme != "https":
        raise HTTPException(status_code=422, detail="O addon não retornou uma URL HTTPS direta.")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as file:
            file.write(url + "\n")
    except FileExistsError:
        return {"added": False, "existing": True, "path": str(target)}
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Não foi possível gravar o arquivo STRM na biblioteca.") from exc
    with SessionLocal() as session:
        session.add(LibraryItem(id=item_id, media_type=media_type, imdb_id=imdb_id, title=title, path=str(target), stream_url=url))
        session.commit()
    start_task(check_library_item(item_id))
    return {"added": True, "existing": False, "path": str(target)}


@app.post("/api/library/add/movie/{imdb_id}")
async def add_movie(
    imdb_id: str,
    choice: StreamChoice | None = None,
    _: None = Depends(require_admin_mutation),
) -> dict:
    if not re.fullmatch(r"tt\d+", imdb_id):
        raise HTTPException(status_code=422, detail="IMDb ID inválido.")
    details = await get_metadata_details("movie", imdb_id)
    name = title_folder_name(details["title"], details.get("year"))
    existing_movie = scan_movie(MEDIA_ROOT, details["title"], details.get("year"))
    if existing_movie["status"] != "missing":
        return {"added": False, "existing": True, "path": existing_movie["path"], "message": "Arquivo existente mantido sem alteracoes."}
    streams = (await _streams_for("movie", imdb_id))["streams"]
    if not streams:
        raise HTTPException(status_code=404, detail="Nenhuma URL direta acessível foi encontrada nos addons.")
    stream = next((candidate for candidate in streams if choice and candidate["url"] == choice.url), None) if choice else streams[0]
    if stream is None:
        raise HTTPException(status_code=422, detail="A opção selecionada expirou. Consulte os streams novamente.")
    title = name
    rel = Path(name) / f"{name}.strm"
    result = _write_strm(rel, stream["url"], f"movie:{imdb_id}", "movie", imdb_id, title)
    if result["added"]:
        try:
            await refresh_library()
            result["jellyfin_scan"] = "requested"
            result["message"] = "Arquivo .strm criado. Biblioteca Jellyfin atualizada."
        except HTTPException as exc:
            result["jellyfin_scan"] = "failed"
            result["jellyfin_scan_error"] = exc.detail
            result["message"] = f"Arquivo .strm criado, mas o scan Jellyfin falhou: {exc.detail}"
    return result


@app.post("/api/library/add/series/{imdb_id}/{season}/{episode}")
async def add_episode(
    imdb_id: str,
    season: int = PathParam(ge=0, le=99),
    episode: int = PathParam(gt=0, le=999),
    choice: StreamChoice | None = None,
    _: None = Depends(require_admin_mutation),
) -> dict:
    if not re.fullmatch(r"tt\d+", imdb_id):
        raise HTTPException(status_code=422, detail="IMDb ID inválido.")
    details = await get_metadata_details("series", imdb_id)
    selected = next((item for item in details["episodes"] if item["season_number"] == season and item["episode_number"] == episode), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="Episódio não encontrado na metadata.")
    existing_status = scan_series(MEDIA_ROOT, details["title"], details.get("year"), details.get("year_end"), details.get("episodes", []))
    existing_item = next((item for item in existing_status["episodes"] if item["season_number"] == season and item["episode_number"] == episode), None)
    if existing_item and existing_item["status"] != "missing":
        return {"added": False, "existing": True, "path": existing_item["path"], "message": "Arquivo existente mantido sem alteracoes."}
    streams = (await _streams_for("series", f"{imdb_id}:{season}:{episode}"))["streams"]
    if not streams:
        raise HTTPException(status_code=404, detail="Nenhuma URL direta acessível foi encontrada nos addons.")
    stream = next((candidate for candidate in streams if choice and candidate["url"] == choice.url), None) if choice else streams[0]
    if stream is None:
        raise HTTPException(status_code=422, detail="A opção selecionada expirou. Consulte os streams novamente.")
    series_name = series_folder_name(details["title"], details.get("year"), details.get("year_end"))
    season_name = f"Season {season:02d}"
    code = f"S{season:02d}E{episode:02d}"
    filename = _safe_name(f"{series_name} - {code}") + ".strm"
    rel = Path(_safe_name(series_name)) / season_name / filename
    item_id = f"series:{imdb_id}:{season}:{episode}"
    result = _write_strm(rel, stream["url"], item_id, "series", imdb_id, selected["title"])
    if result["added"]:
        try:
            await refresh_library()
            result["jellyfin_scan"] = "requested"
            result["message"] = "Arquivo .strm criado. Biblioteca Jellyfin atualizada."
        except HTTPException as exc:
            result["jellyfin_scan"] = "failed"
            result["jellyfin_scan_error"] = exc.detail
            result["message"] = f"Arquivo .strm criado, mas o scan Jellyfin falhou: {exc.detail}"
    return result


async def _run_torrent_download(job_id: str, imdb_id: str, details: dict, selected: dict) -> None:
    media_root = Path(MEDIA_ROOT).resolve()
    staging_root = media_root / ".mlm-torrents"
    staging_path = staging_root / job_id
    staged_file = None
    published_file = None
    try:
        update_job(job_id, status="running", total=1, completed=0, failed=0, message="Baixando torrent autorizado em pasta temporaria.")
        last_report = 0.0
        last_bytes = -1

        def report(byte_count: int) -> None:
            nonlocal last_report, last_bytes
            now = time.monotonic()
            if byte_count != last_bytes and (now - last_report >= 10 or byte_count == 0):
                update_job(job_id, message=f"Baixando torrent: {byte_count // (1024 * 1024)} MB recebidos.")
                last_report, last_bytes = now, byte_count

        staged_file = await download_movie(selected["info_hash"], staging_path, MAX_TORRENT_BYTES, report, selected.get("file_idx"))
        folder_name = title_folder_name(details["title"], details.get("year"))
        relative = Path(folder_name) / f"{folder_name}{staged_file.suffix.lower()}"
        published_file = _media_path(relative)
        published_file.parent.mkdir(parents=True, exist_ok=True)
        os.link(staged_file, published_file)
        item_id = f"movie:{imdb_id}"
        with SessionLocal() as session:
            tracked = session.get(TemporaryMedia, selected["info_hash"])
            if tracked is not None:
                raise RuntimeError("Este torrent ja esta cadastrado como midia temporaria.")
            library_item = session.get(LibraryItem, item_id)
            if library_item is None:
                library_item = LibraryItem(id=item_id, media_type="movie", imdb_id=imdb_id, title=details["title"], path=str(published_file), stream_url=f"torrent:{selected['info_hash']}")
                session.add(library_item)
            else:
                library_item.path = str(published_file)
                library_item.stream_url = f"torrent:{selected['info_hash']}"
            session.add(TemporaryMedia(info_hash=selected["info_hash"], imdb_id=imdb_id, title=details["title"], path=str(published_file)))
            session.commit()
        try:
            await refresh_library()
            scan_message = "Download concluido. Jellyfin atualizado; o arquivo sera removido quando o Jellyfin marcar o filme como reproduzido."
            scan_state = "requested"
        except HTTPException as exc:
            scan_message = f"Download concluido, mas a atualizacao Jellyfin falhou: {exc.detail}"
            scan_state = "failed"
        update_job(job_id, status="completed", total=1, completed=1, message=scan_message, result={"path": str(published_file), "temporary": True, "delete_when_played": True, "jellyfin_scan": scan_state})
    except Exception as exc:
        logger.exception("Torrent download job %s failed", job_id)
        if published_file is not None:
            with SessionLocal() as session:
                tracked = session.query(TemporaryMedia).filter(TemporaryMedia.path == str(published_file)).one_or_none()
                if tracked is None:
                    published_file.unlink(missing_ok=True)
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)[:400] or "Falha ao baixar torrent."
        update_job(job_id, status="failed", failed=1, message=detail, result={"error": detail})
    finally:
        if staging_path.exists() and staging_root.resolve() in staging_path.resolve().parents:
            shutil.rmtree(staging_path, ignore_errors=True)


@app.post("/api/jobs/download/movie/{imdb_id}", status_code=202)
async def download_movie_job(
    imdb_id: str,
    choice: TorrentChoice,
    request: Request,
    _: None = Depends(require_admin_mutation),
) -> dict:
    if not re.fullmatch(r"tt\d+", imdb_id):
        raise HTTPException(status_code=422, detail="IMDb ID invalido.")
    details = await get_metadata_details("movie", imdb_id)
    existing = scan_movie(MEDIA_ROOT, details["title"], details.get("year"))
    if existing["status"] != "missing":
        raise HTTPException(status_code=409, detail="Ja existe um arquivo para este filme; nada foi sobrescrito.")
    streams = await _streams_for("movie", imdb_id)
    torrent = next((item for item in streams.get("torrents", []) if item["info_hash"] == choice.info_hash.lower() and item["provider_id"] == choice.provider_id and item.get("file_idx") == choice.file_idx), None)
    if torrent is None:
        errors = "; ".join(f"{item['provider']}: {item['detail']}" for item in streams.get("addon_errors", []))
        detail = "O torrent selecionado nao aparece mais na resposta atual do addon."
        if errors:
            detail += " " + errors
        raise HTTPException(status_code=409, detail=detail)
    with SessionLocal() as session:
        if session.get(TemporaryMedia, torrent["info_hash"]):
            raise HTTPException(status_code=409, detail="Este torrent ja esta sendo mantido como arquivo temporario.")
    user = session_user(request.cookies.get(SESSION_COOKIE)) or {}
    job = create_job("torrent_download", user.get("user_id", ""), total=1, message="Preparando download torrent.")
    start_task(_run_torrent_download(job["id"], imdb_id, details, torrent))
    return {"job_id": job["id"], "status": "queued"}


def _save_metadata(details: dict) -> None:
    with SessionLocal() as session:
        record = session.get(MetadataItem, details["id"])
        if record is None:
            record = MetadataItem(id=details["id"])
            session.add(record)
        for field in ("media_type", "tmdb_id", "imdb_id", "title", "year", "poster_url", "overview"):
            setattr(record, field, details.get(field))
        session.commit()


def _media_path(relative_path: Path) -> Path:
    root = Path(MEDIA_ROOT).resolve()
    target = (root / relative_path).resolve()
    if root not in target.parents:
        raise HTTPException(status_code=422, detail="Caminho de midia invalido.")
    return target


def _episode_strm_path(details: dict, season: int, episode: int) -> Path:
    series_name = series_folder_name(details["title"], details.get("year"), details.get("year_end"))
    code = f"S{season:02d}E{episode:02d}"
    return Path(_safe_name(series_name)) / f"Season {season:02d}" / f"{_safe_name(series_name)} - {code}.strm"


def _episode_item_id(imdb_id: str, season: int, episode: int) -> str:
    return f"series:{imdb_id}:{season}:{episode}"


async def _run_episode_job(job_id: str, details: dict, episodes: list[dict], kind: str) -> None:
    update_job(job_id, status="running", total=len(episodes), completed=0, failed=0, message="Atualizando metadata e verificando arquivos existentes.")
    _save_metadata(details)
    added = skipped = unavailable = failed = 0
    for index, episode in enumerate(episodes, start=1):
        season = int(episode["season_number"])
        number = int(episode["episode_number"])
        try:
            status = scan_series(MEDIA_ROOT, details["title"], details.get("year"), details.get("year_end"), details.get("episodes", []))
            existing = next((item for item in status["episodes"] if item["season_number"] == season and item["episode_number"] == number), None)
            if existing and existing["status"] != "missing":
                skipped += 1
                message = f"S{season:02d}E{number:02d}: arquivo existente preservado."
            else:
                item_id = _episode_item_id(details["imdb_id"], season, number)
                streams = (await _streams_for("series", f"{details['imdb_id']}:{season}:{number}"))["streams"]
                if not streams:
                    unavailable += 1
                    message = f"S{season:02d}E{number:02d}: nenhuma fonte direta disponivel."
                else:
                    outcome = _write_strm(
                        _episode_strm_path(details, season, number),
                        streams[0]["url"],
                        item_id,
                        "series",
                        details["imdb_id"],
                        episode["title"],
                    )
                    if outcome["added"]:
                        added += 1
                    else:
                        skipped += 1
                    message = f"S{season:02d}E{number:02d}: " + ("STRM criado." if outcome["added"] else "arquivo existente preservado.")
        except Exception as exc:
            failed += 1
            logger.exception("Job %s failed on S%02dE%02d", job_id, season, number)
            message = f"S{season:02d}E{number:02d}: falha ao processar."
        update_job(job_id, completed=index, failed=failed + unavailable, message=message)

    scan_result = "not_needed"
    if added:
        try:
            await refresh_library()
            scan_result = "requested"
        except HTTPException as exc:
            scan_result = f"failed: {exc.detail}"
            logger.warning("Jellyfin library refresh failed for job %s: %s", job_id, exc.detail)
    message = f"Concluido: {added} adicionados, {skipped} preservados, {unavailable} sem fonte, {failed} com erro."
    if scan_result == "requested":
        message += " Biblioteca Jellyfin atualizada."
    elif scan_result.startswith("failed:"):
        message += " Scan Jellyfin falhou; configure a chave no backend."
    update_job(job_id, status="completed", completed=len(episodes), failed=failed + unavailable, message=message,
               result={"added": added, "skipped": skipped, "unavailable": unavailable, "failed": failed, "jellyfin_scan": scan_result})


async def _start_series_job(imdb_id: str, owner_id: str, kind: str, season_number: int | None = None) -> dict:
    details = await get_metadata_details("series", imdb_id)
    details["imdb_id"] = imdb_id
    episodes = [episode for episode in details.get("episodes", []) if season_number is None or episode["season_number"] == season_number]
    if not episodes:
        raise HTTPException(status_code=404, detail="A metadata nao retornou episodios para essa selecao.")
    job = create_job(kind, owner_id, total=len(episodes), message="Preparando episodios.")
    start_task(_run_episode_job(job["id"], details, episodes, kind))
    return {"job_id": job["id"], "status": job["status"], "total": job["total"]}


@app.post("/api/library/add/series/{imdb_id}/season/{season}", status_code=202)
async def add_season_job(
    imdb_id: str,
    request: Request,
    season: int = PathParam(ge=0, le=99),
    _: None = Depends(require_admin_mutation),
) -> dict:
    if not re.fullmatch(r"tt\d+", imdb_id):
        raise HTTPException(status_code=422, detail="IMDb ID invalido.")
    return await _start_series_job(imdb_id, request.state.user["user_id"], "add_season", season)


@app.post("/api/library/add/series/{imdb_id}", status_code=202)
async def add_full_series_job(
    imdb_id: str,
    request: Request,
    _: None = Depends(require_admin_mutation),
) -> dict:
    if not re.fullmatch(r"tt\d+", imdb_id):
        raise HTTPException(status_code=422, detail="IMDb ID invalido.")
    return await _start_series_job(imdb_id, request.state.user["user_id"], "add_series")


@app.post("/api/library/sync/series/{imdb_id}", status_code=202)
async def sync_series_job(
    imdb_id: str,
    request: Request,
    _: None = Depends(require_admin_mutation),
) -> dict:
    if not re.fullmatch(r"tt\d+", imdb_id):
        raise HTTPException(status_code=422, detail="IMDb ID invalido.")
    return await _start_series_job(imdb_id, request.state.user["user_id"], "sync_series")


def _authorized_job(job_id: str, request: Request) -> dict:
    job = get_job(job_id)
    if job is None or not can_access_job(job, request.state.user):
        raise HTTPException(status_code=404, detail="Job nao encontrado.")
    return job


@app.get("/api/jobs/{job_id}")
async def read_job(job_id: str, request: Request, _: None = Depends(require_session)) -> dict:
    return _authorized_job(job_id, request)


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, _: None = Depends(require_session)) -> StreamingResponse:
    _authorized_job(job_id, request)

    async def events():
        while True:
            job = _authorized_job(job_id, request)
            yield f"data: {json.dumps(job, ensure_ascii=False)}\n\n"
            if job["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


ALLOWED_MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm"}


async def _create_import_job(media_type: str, imdb_id: str, filename: str, size: int, owner_id: str, season: int | None = None, episode: int | None = None) -> dict:
    if not re.fullmatch(r"tt\d+", imdb_id):
        raise HTTPException(status_code=422, detail="IMDb ID invalido.")
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_MEDIA_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Envie um arquivo MP4, MKV ou WEBM.")
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"O limite de upload e {MAX_UPLOAD_BYTES // (1024 ** 3)} GB.")
    if media_type == "series":
        details = await get_metadata_details("series", imdb_id)
        if not any(item["season_number"] == season and item["episode_number"] == episode for item in details.get("episodes", [])):
            raise HTTPException(status_code=404, detail="Episodio nao encontrado na metadata.")
    else:
        await get_metadata_details("movie", imdb_id)
    kind = "import_movie" if media_type == "movie" else "import_episode"
    job = create_job(kind, owner_id, total=size, message="Aguardando envio do arquivo.")
    update_job(job["id"], result={
        "media_type": media_type,
        "imdb_id": imdb_id,
        "season": season,
        "episode": episode,
        "filename": Path(filename).name,
        "size": size,
    })
    return {"job_id": job["id"], "status": "queued", "upload_url": f"/api/jobs/{job['id']}/upload"}


@app.post("/api/jobs/import/movie/{imdb_id}", status_code=202)
async def create_movie_import(imdb_id: str, payload: ImportJobPayload, request: Request, _: None = Depends(require_admin_mutation)) -> dict:
    return await _create_import_job("movie", imdb_id, payload.filename, payload.size, request.state.user["user_id"])


@app.post("/api/jobs/import/series/{imdb_id}/{season}/{episode}", status_code=202)
async def create_episode_import(
    imdb_id: str,
    request: Request,
    payload: ImportJobPayload,
    season: int = PathParam(ge=0, le=99),
    episode: int = PathParam(gt=0, le=999),
    _: None = Depends(require_admin_mutation),
) -> dict:
    return await _create_import_job("series", imdb_id, payload.filename, payload.size, request.state.user["user_id"], season, episode)


async def _finish_import(job_id: str, stage_path: Path) -> None:
    job = get_job(job_id)
    if job is None:
        stage_path.unlink(missing_ok=True)
        return
    payload = job["result"]
    try:
        update_job(job_id, status="running", total=1, completed=0, message="Organizando o arquivo e verificando substituicoes.")
        media_type = payload["media_type"]
        imdb_id = payload["imdb_id"]
        details = await get_metadata_details(media_type, imdb_id)
        details["imdb_id"] = imdb_id
        _save_metadata(details)
        extension = Path(payload["filename"]).suffix.lower()
        if media_type == "movie":
            existing = scan_movie(MEDIA_ROOT, details["title"], details.get("year"))
            if existing["status"] != "missing":
                stage_path.unlink(missing_ok=True)
                update_job(job_id, status="completed", completed=1, message="Arquivo existente preservado; nada foi sobrescrito.", result={"existing": existing["path"]})
                return
            folder = title_folder_name(details["title"], details.get("year"))
            relative = Path(folder) / f"{_safe_name(folder)}{extension}"
            item_id = f"movie:{imdb_id}"
            item_title = details["title"]
        else:
            season = int(payload["season"])
            episode = int(payload["episode"])
            existing_status = scan_series(MEDIA_ROOT, details["title"], details.get("year"), details.get("year_end"), details.get("episodes", []))
            existing = next((item for item in existing_status["episodes"] if item["season_number"] == season and item["episode_number"] == episode), None)
            if existing and existing["status"] != "missing":
                stage_path.unlink(missing_ok=True)
                update_job(job_id, status="completed", completed=1, message="Arquivo existente preservado; nada foi sobrescrito.", result={"existing": existing["path"]})
                return
            selected = next((item for item in details["episodes"] if item["season_number"] == season and item["episode_number"] == episode), None)
            if selected is None:
                raise HTTPException(status_code=404, detail="Episodio nao encontrado na metadata.")
            relative = _episode_strm_path(details, season, episode).with_suffix(extension)
            item_id = _episode_item_id(imdb_id, season, episode)
            item_title = selected["title"]

        target = _media_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(stage_path, target)
        except FileExistsError:
            stage_path.unlink(missing_ok=True)
            update_job(job_id, status="completed", completed=1, message="Arquivo existente preservado; nada foi sobrescrito.", result={"existing": str(target)})
            return
        stage_path.unlink(missing_ok=True)
        with SessionLocal() as session:
            if session.get(LibraryItem, item_id) is None:
                session.add(LibraryItem(id=item_id, media_type=media_type, imdb_id=imdb_id, title=item_title, path=str(target), stream_url="local"))
                session.commit()
        scan_message = "Arquivo importado."
        scan_result = "not_configured"
        try:
            await refresh_library()
            scan_message = "Arquivo importado. Biblioteca Jellyfin atualizada."
            scan_result = "requested"
        except HTTPException as exc:
            scan_message += " Scan Jellyfin pendente: " + exc.detail
            scan_result = f"failed: {exc.detail}"
        update_job(job_id, status="completed", completed=1, message=scan_message, result={"path": str(target), "jellyfin_scan": scan_result})
    except Exception as exc:
        stage_path.unlink(missing_ok=True)
        detail = exc.detail if isinstance(exc, HTTPException) else "Falha ao organizar arquivo importado."
        logger.exception("Import job %s failed", job_id)
        update_job(job_id, status="failed", message=detail, result={"error": detail})


@app.put("/api/jobs/{job_id}/upload")
async def upload_import_file(
    job_id: str,
    request: Request,
    file: UploadFile = File(...),
    _: None = Depends(require_admin_mutation),
) -> dict:
    job = _authorized_job(job_id, request)
    if job["kind"] not in {"import_movie", "import_episode"} or job["status"] != "queued":
        raise HTTPException(status_code=409, detail="Esse job nao esta aguardando upload.")
    if Path(file.filename or "").suffix.lower() not in ALLOWED_MEDIA_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Envie um arquivo MP4, MKV ou WEBM.")
    root = (Path(MEDIA_ROOT).resolve() / ".mlm-staging")
    root.mkdir(parents=True, exist_ok=True)
    stage_path = root / f"{job_id}.part"
    written = 0
    update_job(job_id, status="uploading", message="Recebendo arquivo.")
    try:
        with stage_path.open("xb") as output:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES or written > job["total"]:
                    raise HTTPException(status_code=413, detail="O arquivo excede o tamanho informado ou o limite configurado.")
                output.write(chunk)
                if written == job["total"] or written % (5 * 1024 * 1024) < len(chunk):
                    update_job(job_id, completed=written, message=f"Enviado {written // (1024 * 1024)} MB de {job['total'] // (1024 * 1024)} MB.")
        if written != job["total"]:
            raise HTTPException(status_code=422, detail="O tamanho enviado nao confere com o arquivo selecionado.")
        update_job(job_id, status="running", total=1, completed=0, message="Upload recebido; organizando arquivo.")
        start_task(_finish_import(job_id, stage_path))
        return {"job_id": job_id, "status": "running"}
    except Exception as exc:
        stage_path.unlink(missing_ok=True)
        detail = exc.detail if isinstance(exc, HTTPException) else "Falha ao receber arquivo."
        update_job(job_id, status="failed", message=detail, result={"error": detail})
        raise
    finally:
        await file.close()


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")

from contextlib import asynccontextmanager
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Path as PathParam, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import inspect, text
from typing import Literal

from app.database.db import Base, SessionLocal, engine
from app.database.models import Addon, LibraryItem, LinkCheck, MetadataItem, Preference
from app.jellyfin.client import authenticate_user, refresh_library
from app.library.dynamic import dynamic_signature, dynamic_strm_url, verify_dynamic_signature
from app.library.service import scan_movie, scan_series, series_folder_name, title_folder_name
from app.config import BESTCINE_MANIFEST_URL, COOKIE_SECURE, FENIXFLIX_MANIFEST_URL, FROST_MANIFEST_URL, LINK_CHECK_INTERVAL_SECONDS, MAX_UPLOAD_BYTES, MEDIA_ROOT
from app.metadata.cinemeta import details as get_metadata_details
from app.metadata.cinemeta import search as search_metadata
from app.stremio.client import read_manifest as read_addon_manifest
from app.stremio.resolver import find_direct_streams, sort_streams
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


logger = logging.getLogger("media_library_manager")
_SEARCH_AVAILABILITY_CACHE: dict[str, tuple[float, str, list[str]]] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    check_columns = {column["name"] for column in inspect(engine).get_columns("link_checks")}
    if "provider_id" not in check_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE link_checks ADD COLUMN provider_id VARCHAR(160)"))
    mark_interrupted_jobs()
    with SessionLocal() as session:
        for addon_id, name, manifest_url in (
            ("com.froststream", "FrostStream", FROST_MANIFEST_URL),
            ("com.bestcine.multisource", "BestCine", BESTCINE_MANIFEST_URL),
            ("com.fenixflix", "FenixFlix", FENIXFLIX_MANIFEST_URL),
        ):
            if session.get(Addon, addon_id) is None:
                session.add(Addon(id=addon_id, name=name, manifest_url=manifest_url, enabled=True))
        if session.get(Preference, 1) is None:
            session.add(Preference(id=1))
        session.commit()
    link_check_task = asyncio.create_task(periodic_link_check(LINK_CHECK_INTERVAL_SECONDS))
    try:
        yield
    finally:
        link_check_task.cancel()
        try:
            await link_check_task
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

    results = await _rank_search_results(normalized_query, await search_metadata(normalized_query, media_type))
    with SessionLocal() as session:
        for item in results:
            record = session.get(MetadataItem, item["id"])
            if record is None:
                record = MetadataItem(id=item["id"])
                session.add(record)
            for field in ("media_type", "tmdb_id", "imdb_id", "title", "year", "poster_url", "overview"):
                setattr(record, field, item.get(field))
        session.commit()
    return {"results": results}


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
                    streams = await _streams_for(media_type, imdb_id)
            except Exception:
                _SEARCH_AVAILABILITY_CACHE[cache_key] = (time.monotonic() + 30, "unknown", [])
                return "unknown", []
        providers = list(dict.fromkeys(stream["provider"] for stream in streams["streams"]))
        status = "available" if providers else "unknown" if streams["addon_errors"] else "unavailable"
        _SEARCH_AVAILABILITY_CACHE[cache_key] = (time.monotonic() + (300 if status != "unknown" else 30), status, providers)
        return status, providers

    checks = await asyncio.gather(*(availability(item) for item in results))
    availability_rank = {"available": 0, "unknown": 1, "unavailable": 2}
    ranked = []
    for original_rank, (item, (status, providers)) in enumerate(zip(results, checks)):
        ranked_item = {**item, "availability": status, "available_providers": providers}
        ranked.append((availability_rank[status], _search_title_rank(query, item["title"]), original_rank, ranked_item))
    ranked.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in ranked]


@app.get("/api/title/{media_type}/{imdb_id}")
async def title_details(
    media_type: Literal["movie", "series"], imdb_id: str, _: None = Depends(require_session)
) -> dict:
    details = await get_metadata_details(media_type, imdb_id)
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

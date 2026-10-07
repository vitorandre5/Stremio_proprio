from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from app.database.db import Base, SessionLocal, engine
from app.database.models import Addon, LibraryItem, MetadataItem, Preference
from app.jellyfin.client import authenticate_user, refresh_library
from app.library.service import scan_movie, scan_series, series_folder_name, title_folder_name
from app.config import BESTCINE_MANIFEST_URL, COOKIE_SECURE, FROST_MANIFEST_URL, MEDIA_ROOT
from app.metadata.cinemeta import details as get_metadata_details
from app.metadata.cinemeta import search as search_metadata
from app.stremio.client import read_manifest as read_addon_manifest
from app.stremio.resolver import find_direct_streams
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
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        for addon_id, name, manifest_url in (
            ("com.froststream", "FrostStream", FROST_MANIFEST_URL),
            ("com.bestcine.multisource", "BestCine", BESTCINE_MANIFEST_URL),
        ):
            if session.get(Addon, addon_id) is None:
                session.add(Addon(id=addon_id, name=name, manifest_url=manifest_url, enabled=True))
        if session.get(Preference, 1) is None:
            session.add(Preference(id=1))
        session.commit()
    yield


app = FastAPI(title="Media Library Manager", version="0.1.0", lifespan=lifespan)


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
    query: str = Query(min_length=1, max_length=100),
    media_type: Literal["all", "movie", "series"] = "all",
) -> dict:
    normalized_query = query.strip()
    if not normalized_query:
        return {"results": []}

    results = await search_metadata(normalized_query, media_type)
    with SessionLocal() as session:
        for item in results:
            record = session.get(MetadataItem, item["id"])
            if record is None:
                record = MetadataItem(id=item["id"])
                session.add(record)
            for field, value in item.items():
                if field != "id":
                    setattr(record, field, value)
        session.commit()
    return {"results": results}


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
    else:
        details["library_status"] = scan_series(MEDIA_ROOT, details["title"], details.get("year"), details.get("year_end"), details.get("episodes", []))
    return details


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


async def _streams_for(media_type: str, video_id: str) -> dict:
    with SessionLocal() as session:
        addons = session.query(Addon).filter(Addon.enabled.is_(True)).order_by(Addon.id).all()
        config = [{"id": addon.id, "name": addon.name, "manifest_url": addon.manifest_url} for addon in addons]
        preference = session.get(Preference, 1)
        preferred_provider = preference.preferred_provider if preference else "automatic"
        preferred_quality = preference.preferred_quality if preference else "1080p"
    config.sort(key=lambda addon: (
        0 if preferred_provider != "automatic" and addon["id"] == preferred_provider else 1,
        0 if addon["name"].casefold() == "froststream" else 1,
        addon["name"].casefold(),
    ))
    result = await find_direct_streams(config, media_type, video_id)
    quality_order = [preferred_quality, "2160p", "1440p", "1080p", "720p", "480p"]
    rank = {quality: index for index, quality in enumerate(dict.fromkeys(quality_order))}
    result["streams"].sort(key=lambda stream: rank.get(stream["quality"], len(rank)))
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


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")

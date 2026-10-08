from urllib.parse import urlsplit
import re

import httpx
from fastapi import HTTPException

from app.config import JELLYFIN_API_KEY, JELLYFIN_URL


def _base_url() -> str:
    parsed = urlsplit(JELLYFIN_URL)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=503, detail="Configure JELLYFIN_URL com a URL HTTPS do servidor.")
    return JELLYFIN_URL


async def authenticate_user(username: str, password: str) -> dict:
    base_url = _base_url()
    headers = {
        "X-Emby-Authorization": 'MediaBrowser Client="Media Library Manager", Device="Web", DeviceId="media-library-manager", Version="0.1.0"',
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.post(
                f"{base_url}/Users/AuthenticateByName",
                headers=headers,
                json={"Username": username, "Pw": password},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Nao foi possivel conectar ao Jellyfin.") from exc

    if response.status_code in {400, 401, 403}:
        raise HTTPException(status_code=401, detail="Usuario ou senha invalidos.")
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail=f"O Jellyfin respondeu HTTP {response.status_code}.")

    try:
        result = response.json()
        user = result["User"]
        access_token = result["AccessToken"]
        if not user.get("Id") or not user.get("Name") or not access_token:
            raise ValueError("Resposta de autenticacao incompleta")
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="O Jellyfin retornou uma resposta de autenticacao invalida.") from exc

    return {"id": user["Id"], "name": user["Name"], "is_admin": bool(user.get("Policy", {}).get("IsAdministrator", False))}


async def refresh_library() -> None:
    if not JELLYFIN_API_KEY:
        raise HTTPException(status_code=503, detail="Configure JELLYFIN_API_KEY no Coolify.")
    base_url = _base_url()

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.post(
                f"{base_url}/Library/Refresh",
                headers={"X-Emby-Token": JELLYFIN_API_KEY},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Nao foi possivel solicitar a atualizacao da biblioteca Jellyfin.") from exc

    if response.status_code not in {200, 204}:
        raise HTTPException(status_code=503, detail=f"O Jellyfin recusou a atualizacao (HTTP {response.status_code}).")


def _valid_jellyfin_id(value: str) -> bool:
    if not isinstance(value, str) or len(value) not in {32, 36}:
        return False
    try:
        import uuid
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


async def resume_items(user_id: str, limit: int = 12) -> list[dict]:
    """Return the authenticated Jellyfin user's real resumable video items."""
    if not JELLYFIN_API_KEY:
        raise HTTPException(status_code=503, detail="Configure JELLYFIN_API_KEY no Coolify.")
    if not _valid_jellyfin_id(user_id):
        raise HTTPException(status_code=401, detail="Sessao Jellyfin invalida.")
    params = {
        "Limit": str(max(1, min(limit, 24))),
        "Recursive": "true",
        "MediaTypes": "Video",
        "Fields": "PrimaryImageAspectRatio,DateCreated,BasicSyncInfo,ProviderIds",
        "ImageTypeLimit": "1",
        "EnableImageTypes": "Primary,Backdrop,Thumb",
        "EnableTotalRecordCount": "false",
        "EnableUserData": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
            response = await client.get(
                f"{_base_url()}/Users/{user_id}/Items/Resume",
                params=params,
                headers={"X-Emby-Token": JELLYFIN_API_KEY},
            )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Nao foi possivel carregar Continuar assistindo do Jellyfin.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="O Jellyfin retornou uma resposta invalida.") from exc
    items = payload.get("Items", []) if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


async def watched_media(user_id: str, limit: int = 60) -> list[dict]:
    """Return recently played movies and episodes for a recommendation profile."""
    if not JELLYFIN_API_KEY:
        return []
    if not _valid_jellyfin_id(user_id):
        return []
    params = {
        "Limit": str(max(1, min(limit, 100))),
        "Recursive": "true",
        "IncludeItemTypes": "Movie,Episode",
        "Filters": "IsPlayed",
        "SortBy": "DatePlayed",
        "SortOrder": "Descending",
        "Fields": "ProviderIds,DateCreated,Genres",
        "EnableUserData": "true",
        "EnableTotalRecordCount": "false",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(
                f"{_base_url()}/Users/{user_id}/Items",
                params=params,
                headers={"X-Emby-Token": JELLYFIN_API_KEY},
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Nao foi possivel ler o historico Jellyfin para recomendacoes.") from exc
    items = payload.get("Items", []) if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


async def user_item(user_id: str, item_id: str) -> dict | None:
    if not JELLYFIN_API_KEY or not _valid_jellyfin_id(user_id) or not _valid_jellyfin_id(item_id):
        return None
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
            response = await client.get(
                f"{_base_url()}/Users/{user_id}/Items/{item_id}",
                params={"Fields": "ProviderIds,Genres"},
                headers={"X-Emby-Token": JELLYFIN_API_KEY},
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


async def item_image(item_id: str, max_width: int = 420) -> tuple[bytes, str]:
    """Fetch an image through the server so the Jellyfin API key stays private."""
    if not JELLYFIN_API_KEY:
        raise HTTPException(status_code=503, detail="Configure JELLYFIN_API_KEY no Coolify.")
    if not _valid_jellyfin_id(item_id):
        raise HTTPException(status_code=404, detail="Imagem nao encontrada.")
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
            response = await client.get(
                f"{_base_url()}/Items/{item_id}/Images/Primary",
                params={"maxWidth": max(160, min(max_width, 640))},
                headers={"X-Emby-Token": JELLYFIN_API_KEY},
            )
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Imagem nao encontrada.")
        response.raise_for_status()
        media_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
        if not media_type.startswith("image/"):
            raise HTTPException(status_code=502, detail="O Jellyfin retornou um tipo de imagem invalido.")
        return response.content, media_type
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Nao foi possivel carregar a capa do Jellyfin.") from exc


async def playback_sessions() -> list[dict]:
    if not JELLYFIN_API_KEY:
        raise HTTPException(status_code=503, detail="Configure JELLYFIN_API_KEY no Coolify.")
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(f"{_base_url()}/Sessions", headers={"X-Emby-Token": JELLYFIN_API_KEY})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Nao foi possivel consultar sessoes do Jellyfin.") from exc
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


async def item_marked_played(user_id: str, item_id: str) -> bool:
    if not JELLYFIN_API_KEY or not re.fullmatch(r"[0-9a-fA-F-]{36}", user_id) or not re.fullmatch(r"[0-9a-fA-F-]{36}", item_id):
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(
                f"{_base_url()}/Users/{user_id}/Items/{item_id}",
                headers={"X-Emby-Token": JELLYFIN_API_KEY},
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    return bool(payload.get("UserData", {}).get("Played")) if isinstance(payload, dict) else False

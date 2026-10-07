from urllib.parse import urlsplit

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

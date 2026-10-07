import hashlib
import hmac
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException

from app.config import PUBLIC_BASE_URL, SESSION_SECRET


def _canonical(media_type: str, imdb_id: str, season: int | None, episode: int | None, provider_id: str) -> str:
    parts = [media_type, imdb_id, str(season or ""), str(episode or ""), provider_id]
    return "\n".join(parts)


def dynamic_signature(media_type: str, imdb_id: str, season: int | None, episode: int | None, provider_id: str) -> str:
    if len(SESSION_SECRET) < 32:
        raise HTTPException(status_code=503, detail="Configure SESSION_SECRET para habilitar STRM dinamico.")
    return hmac.new(
        SESSION_SECRET.encode("utf-8"),
        _canonical(media_type, imdb_id, season, episode, provider_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def dynamic_strm_url(media_type: str, imdb_id: str, provider_id: str, season: int | None = None, episode: int | None = None) -> str:
    parsed = urlsplit(PUBLIC_BASE_URL)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=503, detail="Configure PUBLIC_BASE_URL com o dominio HTTPS do aplicativo.")
    if media_type == "movie":
        path = f"/stream/movie/{imdb_id}"
        signature = dynamic_signature(media_type, imdb_id, None, None, provider_id)
    elif media_type == "series" and season is not None and episode is not None:
        path = f"/stream/series/{imdb_id}/{season}/{episode}"
        signature = dynamic_signature(media_type, imdb_id, season, episode, provider_id)
    else:
        raise HTTPException(status_code=422, detail="Tipo de STRM dinamico invalido.")
    return f"{PUBLIC_BASE_URL}{path}?{urlencode({'provider': provider_id, 'sig': signature})}"


def verify_dynamic_signature(
    media_type: str,
    imdb_id: str,
    provider_id: str,
    signature: str,
    season: int | None = None,
    episode: int | None = None,
) -> bool:
    expected = dynamic_signature(media_type, imdb_id, season, episode, provider_id)
    return hmac.compare_digest(expected, signature)

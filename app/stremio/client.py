import json
import re
import unicodedata
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException

from app.stremio.manifest import parse_manifest, supports, validate_public_https_url


async def fetch_json(url: str) -> dict:
    validate_public_https_url(url)
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Addon respondeu HTTP {exc.response.status_code}.") from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Não foi possível consultar o addon Stremio.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Resposta do addon não é um objeto JSON.")
    return payload


async def read_manifest(manifest_url: str) -> dict:
    return parse_manifest(await fetch_json(manifest_url))


async def request_streams(manifest_url: str, media_type: str, video_id: str) -> list[dict]:
    manifest = await read_manifest(manifest_url)
    item_id = video_id.split(":", 1)[0]
    if not supports(manifest, "stream", media_type, item_id):
        return []
    base_url = manifest_url.rsplit("/manifest.json", 1)[0].rstrip("/")
    resource_url = f"{base_url}/stream/{media_type}/{quote(video_id, safe=':')}.json"
    payload = await fetch_json(resource_url)
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return []
    return [stream for stream in streams if isinstance(stream, dict)]


def direct_urls(streams: list[dict]) -> list[dict]:
    results = []
    for stream in streams:
        url = stream.get("url")
        if not isinstance(url, str) or urlsplit(url).scheme != "https":
            continue
        try:
            validate_public_https_url(url)
        except HTTPException:
            continue
        hints = stream.get("behaviorHints") or {}
        results.append({
            "name": stream.get("name") or "Stream",
            "title": stream.get("title") or stream.get("name") or "Stream",
            "url": url,
            "quality": _quality(stream),
            "language": _language(stream),
            "behavior_hints": hints,
        })
    return results


def _quality(stream: dict) -> str | None:
    text = f"{stream.get('name', '')} {stream.get('title', '')}"
    for quality in ("2160p", "1440p", "1080p", "720p", "480p"):
        if quality.lower() in text.lower():
            return quality
    return None


def _language(stream: dict) -> str:
    text = f"{stream.get('name', '')} {stream.get('title', '')}"
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    if re.search(r"\b(dublado|dubbed|dub|dual[ -]?audio|audio[ -]?dub)\b", normalized):
        return "dubbed"
    if re.search(r"\b(legendado|subtitles?|subbed|legendas?)\b", normalized):
        return "subtitled"
    if re.search(r"\b(portugues|portuguese|pt[ -]?br)\b", normalized):
        return "portuguese_unspecified"
    return "unknown"

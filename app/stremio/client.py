import json
import re
import unicodedata
from urllib.parse import quote, urlsplit, urlunsplit

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


def _resource_url(manifest_url: str, resource_path: str) -> str:
    parsed = urlsplit(manifest_url)
    base_path = parsed.path.rsplit("/manifest.json", 1)[0].rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, f"{base_path}/{resource_path.lstrip('/')}", parsed.query, ""))


def _catalog_supports_search(manifest: dict, media_type: str) -> list[str]:
    resources = manifest.get("resources") or []
    supports_catalog = any(
        resource == "catalog" or (
            isinstance(resource, dict)
            and resource.get("name") == "catalog"
            and (not resource.get("types") or media_type in resource["types"])
        )
        for resource in resources
    )
    if not supports_catalog or media_type not in (manifest.get("types") or []):
        return []
    catalog_ids = []
    for catalog in manifest.get("catalogs") or []:
        if not isinstance(catalog, dict) or catalog.get("type") != media_type:
            continue
        extras = catalog.get("extra") or []
        supports_search = any(isinstance(extra, dict) and extra.get("name") == "search" for extra in extras)
        has_unset_required_extra = any(
            isinstance(extra, dict)
            and extra.get("isRequired") is True
            and extra.get("name") != "search"
            for extra in extras
        )
        if supports_search and not has_unset_required_extra:
            catalog_id = catalog.get("id")
            if isinstance(catalog_id, str) and catalog_id:
                catalog_ids.append(catalog_id)
    return catalog_ids


async def request_catalog_search(manifest_url: str, media_type: str, query: str) -> list[dict]:
    manifest = await read_manifest(manifest_url)
    results = []
    for catalog_id in _catalog_supports_search(manifest, media_type):
        resource_path = f"catalog/{quote(media_type, safe='')}/{quote(catalog_id, safe='')}/search={quote(query, safe='')}.json"
        payload = await fetch_json(_resource_url(manifest_url, resource_path))
        metas = payload.get("metas")
        if isinstance(metas, list):
            results.extend(meta for meta in metas if isinstance(meta, dict))
    return results


async def request_streams(manifest_url: str, media_type: str, video_id: str) -> list[dict]:
    manifest = await read_manifest(manifest_url)
    item_id = video_id.split(":", 1)[0]
    if not supports(manifest, "stream", media_type, item_id):
        return []
    resource_url = _resource_url(manifest_url, f"stream/{quote(media_type, safe='')}/{quote(video_id, safe=':')}.json")
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

import asyncio

from fastapi import HTTPException

from app.stremio.client import _language, _quality, direct_urls, request_catalog_search, request_streams


def torrent_urls(streams: list[dict]) -> list[dict]:
    results = []
    seen = set()
    for stream in streams:
        info_hash = stream.get("infoHash")
        if not isinstance(info_hash, str) or not (len(info_hash) == 40 and all(c in "0123456789abcdefABCDEF" for c in info_hash)):
            continue
        info_hash = info_hash.lower()
        if info_hash in seen:
            continue
        seen.add(info_hash)
        results.append({
            "name": stream.get("name") or "Torrent",
            "title": stream.get("description") or stream.get("title") or stream.get("name") or "Torrent",
            "info_hash": info_hash,
            "file_idx": stream.get("fileIdx") if isinstance(stream.get("fileIdx"), int) and stream.get("fileIdx") >= 0 else None,
            "quality": _quality(stream),
            "language": _language(stream),
        })
    return results


async def find_catalog_search_results(addons: list[dict], media_type: str, query: str) -> dict:
    async def search_addon(addon: dict) -> tuple[dict, list[dict], str | None]:
        try:
            metas = await request_catalog_search(addon["manifest_url"], media_type, query)
            return addon, metas, None
        except HTTPException as exc:
            return addon, [], str(exc.detail)
        except Exception:
            return addon, [], "Falha inesperada ao consultar o catálogo do addon."

    responses = await asyncio.gather(*(search_addon(addon) for addon in addons))
    results = []
    errors = []
    for addon, metas, error in responses:
        if error:
            errors.append({"provider": addon["name"], "detail": error})
        for meta in metas:
            results.append({**meta, "catalog_provider": addon["name"], "catalog_provider_id": addon["id"]})
    return {"metas": results, "addon_errors": errors}


async def find_direct_streams(addons: list[dict], media_type: str, video_id: str) -> dict:
    errors = []
    streams = []
    torrents = []
    seen_urls = set()
    seen_torrents = set()
    for addon in addons:
        try:
            response = await request_streams(addon["manifest_url"], media_type, video_id)
        except HTTPException as exc:
            errors.append({"provider": addon["name"], "detail": exc.detail})
            continue
        except Exception:
            errors.append({"provider": addon["name"], "detail": "Falha inesperada ao consultar addon."})
            continue
        candidates = direct_urls(response)
        for stream in candidates:
            if stream["url"] in seen_urls:
                continue
            seen_urls.add(stream["url"])
            streams.append({**stream, "provider": addon["name"], "provider_id": addon["id"]})
        for stream in torrent_urls(response):
            if stream["info_hash"] in seen_torrents:
                continue
            seen_torrents.add(stream["info_hash"])
            torrents.append({**stream, "provider": addon["name"], "provider_id": addon["id"]})
    return {"streams": streams, "torrents": torrents, "addon_errors": errors}


def sort_streams(streams: list[dict], addons: list[dict], preferred_quality: str) -> None:
    quality_order = [preferred_quality, "2160p", "1440p", "1080p", "720p", "480p"]
    quality_rank = {quality: index for index, quality in enumerate(dict.fromkeys(quality_order))}
    language_rank = {"dubbed": 0, "subtitled": 1, "portuguese_unspecified": 2, "unknown": 3}
    provider_rank = {addon["id"]: index for index, addon in enumerate(addons)}
    streams.sort(key=lambda stream: (
        language_rank.get(stream["language"], 4),
        quality_rank.get(stream["quality"], len(quality_rank)),
        provider_rank.get(stream["provider_id"], len(provider_rank)),
    ))


def sort_torrents(streams: list[dict], addons: list[dict], preferred_quality: str) -> None:
    sort_streams(streams, addons, preferred_quality)

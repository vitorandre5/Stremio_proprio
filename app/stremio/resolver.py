from app.stremio.client import direct_urls, request_streams
from fastapi import HTTPException


async def find_direct_streams(addons: list[dict], media_type: str, video_id: str) -> dict:
    errors = []
    streams = []
    seen_urls = set()
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
    return {"streams": streams, "addon_errors": errors}


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

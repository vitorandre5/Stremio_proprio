from app.stremio.client import direct_urls, request_streams
from fastapi import HTTPException


async def find_direct_streams(addons: list[dict], media_type: str, video_id: str) -> dict:
    errors = []
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
        if candidates:
            return {"streams": [{**stream, "provider": addon["name"]} for stream in candidates], "addon_errors": errors}
    return {"streams": [], "addon_errors": errors}

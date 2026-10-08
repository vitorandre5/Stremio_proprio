import asyncio

from app.metadata import cinemeta, tmdb


async def search(query: str, media_filter: str = "all") -> list[dict]:
    cinemeta_results, tmdb_results = await asyncio.gather(
        cinemeta.search(query, media_filter),
        tmdb.search(query, media_filter),
        return_exceptions=True,
    )
    merged: dict[tuple[str, str], dict] = {}
    for batch, is_localized in ((tmdb_results, True), (cinemeta_results, False)):
        if isinstance(batch, Exception):
            continue
        for item in batch:
            imdb_id = item.get("imdb_id")
            media_type = item.get("media_type")
            if not isinstance(imdb_id, str) or not isinstance(media_type, str):
                continue
            key = (media_type, imdb_id)
            current = merged.get(key, {})
            search_titles = list(dict.fromkeys([*(current.get("search_titles") or []), *(item.get("search_titles") or []), item.get("title", ""), item.get("original_title", "")]))
            preferred = {**item, **current} if not is_localized else {**current, **item}
            preferred["search_titles"] = [value for value in search_titles if isinstance(value, str) and value]
            if not preferred.get("overview") and not tmdb.is_configured():
                preferred["overview"] = current.get("overview") or item.get("overview") or ""
            for field in ("tmdb_id", "poster_url", "year", "title"):
                if not preferred.get(field):
                    preferred[field] = current.get(field) or item.get(field)
            merged[key] = preferred
    return list(merged.values())


async def details(media_type: str, imdb_id: str) -> dict:
    fallback = await cinemeta.details(media_type, imdb_id)
    try:
        return await tmdb.localized_details(media_type, imdb_id, fallback)
    except Exception:
        if tmdb.is_configured():
            fallback["overview"] = ""
            fallback["episodes"] = [{**episode, "overview": ""} for episode in fallback.get("episodes", [])]
        return fallback


async def localize_results(items: list[dict]) -> list[dict]:
    if not tmdb.is_configured():
        return items
    semaphore = asyncio.Semaphore(6)

    async def localize(item: dict) -> dict:
        if item.get("metadata_language") == tmdb.TMDB_LANGUAGE:
            return item
        async with semaphore:
            try:
                return await tmdb.localized_preview(item)
            except Exception:
                return {**item, "overview": "", "metadata_language": "pt-BR"}

    return await asyncio.gather(*(localize(item) for item in items))

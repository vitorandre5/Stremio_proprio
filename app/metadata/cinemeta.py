import asyncio
from urllib.parse import quote

import httpx
from fastapi import HTTPException


CINEMETA_URL = "https://v3-cinemeta.strem.io"


def _year(value: object) -> int | None:
    if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return None


def _preview(item: dict, media_type: str) -> dict:
    imdb_id = item.get("imdb_id") or item.get("id")
    year_text = item.get("releaseInfo") or item.get("year")
    return {
        "id": imdb_id,
        "media_type": media_type,
        "tmdb_id": item.get("moviedb_id"),
        "imdb_id": imdb_id,
        "title": item.get("name") or "Título sem nome",
        "year": _year(year_text),
        "poster_url": item.get("poster"),
        "overview": item.get("description") or item.get("overview") or "",
    }


async def _get(path: str) -> dict:
    try:
        async with httpx.AsyncClient(base_url=CINEMETA_URL, timeout=12.0) as client:
            response = await client.get(path)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Não foi possível consultar o serviço público de metadata Cinemeta.",
        ) from exc


async def search(query: str, media_filter: str = "all") -> list[dict]:
    types = ["movie", "series"] if media_filter == "all" else [media_filter]
    encoded_query = quote(query, safe="")
    responses = await asyncio.gather(
        *(_get(f"/catalog/{media_type}/top/search={encoded_query}.json") for media_type in types)
    )
    catalogs = [
        [_preview(item, media_type) for item in response.get("metas", [])]
        for media_type, response in zip(types, responses)
    ]
    results = []
    for index in range(max((len(catalog) for catalog in catalogs), default=0)):
        for catalog in catalogs:
            if index < len(catalog):
                results.append(catalog[index])
    return results


async def details(media_type: str, imdb_id: str) -> dict:
    if not imdb_id.startswith("tt") or not imdb_id[2:].isdigit():
        raise HTTPException(status_code=404, detail="IMDb ID inválido.")
    response = await _get(f"/meta/{media_type}/{quote(imdb_id, safe='')}.json")
    item = response.get("meta")
    if not item:
        raise HTTPException(status_code=404, detail="Metadata indisponível para este título.")

    normalized = _preview(item, media_type)
    normalized["tagline"] = ""
    normalized["genres"] = item.get("genres") or item.get("genre") or []
    normalized["cast"] = [
        {"name": person, "character": "", "profile_url": None}
        for person in (item.get("cast") or [])[:12]
        if isinstance(person, str)
    ]
    videos = []
    for video in item.get("videos") or []:
        season = video.get("season")
        episode = video.get("episode") or video.get("number")
        if not isinstance(season, int) or not isinstance(episode, int):
            continue
        videos.append(
            {
                "season_number": season,
                "episode_number": episode,
                "title": video.get("name") or f"Episódio {episode:02d}",
                "overview": video.get("overview") or video.get("description") or "",
                "released": video.get("released") or video.get("firstAired"),
                "id": video.get("id"),
            }
        )
    seasons: dict[int, list[dict]] = {}
    for video in videos:
        seasons.setdefault(video["season_number"], []).append(video)
    normalized["episodes"] = videos
    normalized["seasons"] = [
        {
            "season_number": season_number,
            "name": f"Temporada {season_number:02d}" if season_number else "Especiais",
            "episode_count": len(season_episodes),
            "air_date": None,
        }
        for season_number, season_episodes in sorted(seasons.items())
    ]
    normalized["season_count"] = sum(1 for season in seasons if season > 0)
    normalized["episode_count"] = sum(len(episodes) for number, episodes in seasons.items() if number > 0)
    return normalized

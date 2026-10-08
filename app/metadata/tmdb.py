import asyncio
from copy import deepcopy
from datetime import date, timedelta
import time
from urllib.parse import quote

import httpx

from app.config import TMDB_API_TOKEN, TMDB_LANGUAGE


TMDB_API_URL = "https://api.themoviedb.org/3"
IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"
_external_id_cache: dict[tuple[str, int], str | None] = {}
_imdb_id_cache: dict[tuple[str, str], tuple[str, int] | None] = {}
_home_cache: dict[tuple[tuple[str, int], ...], tuple[float, dict]] = {}


def is_configured() -> bool:
    return bool(TMDB_API_TOKEN)


async def _get(path: str, **params) -> dict:
    if not TMDB_API_TOKEN:
        return {}
    params.setdefault("language", TMDB_LANGUAGE)
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
            response = await client.get(
                f"{TMDB_API_URL}/{path.lstrip('/')}",
                params=params,
                headers={
                    "Authorization": f"Bearer {TMDB_API_TOKEN}",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("Não foi possível consultar o TMDb.") from exc
    return payload if isinstance(payload, dict) else {}


def _year(value: object) -> int | None:
    if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return None


def _type_path(media_type: str) -> str:
    return "tv" if media_type == "series" else "movie"


def _metadata_item(item: dict, media_type: str, imdb_id: str | None = None) -> dict | None:
    tmdb_id = item.get("id")
    if not isinstance(tmdb_id, int):
        return None
    path = item.get("poster_path")
    title = item.get("title") if media_type == "movie" else item.get("name")
    original_title = item.get("original_title") if media_type == "movie" else item.get("original_name")
    release_date = item.get("release_date") if media_type == "movie" else item.get("first_air_date")
    titles = [value.strip() for value in (title, original_title) if isinstance(value, str) and value.strip()]
    return {
        "id": imdb_id or f"tmdb:{tmdb_id}",
        "media_type": media_type,
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "title": title or original_title or "Título sem nome",
        "original_title": original_title or "",
        "search_titles": list(dict.fromkeys(titles)),
        "year": _year(release_date),
        "poster_url": f"{IMAGE_BASE_URL}{path}" if isinstance(path, str) and path else None,
        "overview": item.get("overview") or "",
        "metadata_language": TMDB_LANGUAGE,
    }


async def _external_id(media_type: str, tmdb_id: int) -> str | None:
    key = (media_type, tmdb_id)
    if key in _external_id_cache:
        return _external_id_cache[key]
    response = await _get(f"{_type_path(media_type)}/{tmdb_id}/external_ids")
    imdb_id = response.get("imdb_id")
    normalized = imdb_id if isinstance(imdb_id, str) and imdb_id.startswith("tt") else None
    _external_id_cache[key] = normalized
    return normalized


async def _with_imdb_ids(rows: list[tuple[dict, str]]) -> list[dict]:
    semaphore = asyncio.Semaphore(8)

    async def normalize(row: tuple[dict, str]) -> dict | None:
        item, media_type = row
        tmdb_id = item.get("id")
        if not isinstance(tmdb_id, int):
            return None
        async with semaphore:
            imdb_id = await _external_id(media_type, tmdb_id)
        return _metadata_item(item, media_type, imdb_id) if imdb_id else None

    normalized = await asyncio.gather(*(normalize(row) for row in rows), return_exceptions=True)
    result = []
    for item in normalized:
        if isinstance(item, dict):
            result.append(item)
    return result


async def search(query: str, media_filter: str = "all") -> list[dict]:
    if not TMDB_API_TOKEN:
        return []
    types = ["movie", "tv"] if media_filter == "all" else [_type_path(media_filter)]
    responses = await asyncio.gather(
        *(_get(f"search/{media_type}", query=query, include_adult="false", page=1) for media_type in types),
        return_exceptions=True,
    )
    rows = []
    for media_type, response in zip(types, responses):
        if isinstance(response, Exception):
            continue
        normalized_type = "series" if media_type == "tv" else "movie"
        for item in response.get("results", [])[:12]:
            if isinstance(item, dict):
                rows.append((item, normalized_type))
    return await _with_imdb_ids(rows)


async def _find_by_imdb(imdb_id: str, expected_type: str | None = None) -> tuple[str, int] | None:
    if not TMDB_API_TOKEN:
        return None
    cache_key = (imdb_id, expected_type or "")
    if cache_key in _imdb_id_cache:
        return _imdb_id_cache[cache_key]
    payload = await _get(f"find/{quote(imdb_id, safe='')}", external_source="imdb_id")
    order = [expected_type] if expected_type in {"movie", "series"} else ["movie", "series"]
    for media_type in order:
        key = "movie_results" if media_type == "movie" else "tv_results"
        results = payload.get(key)
        if isinstance(results, list) and results and isinstance(results[0], dict) and isinstance(results[0].get("id"), int):
            found = (media_type, results[0]["id"])
            _imdb_id_cache[cache_key] = found
            return found
    _imdb_id_cache[cache_key] = None
    return None


async def tmdb_id_for_imdb(imdb_id: str, media_type: str) -> int | None:
    found = await _find_by_imdb(imdb_id, media_type)
    return found[1] if found and found[0] == media_type else None


async def localized_details(media_type: str, imdb_id: str, fallback: dict) -> dict:
    """Enrich Cinemeta episode IDs with Brazilian Portuguese TMDb metadata."""
    if not TMDB_API_TOKEN:
        return fallback
    found = await _find_by_imdb(imdb_id, media_type)
    if not found:
        fallback["overview"] = ""
        fallback["episodes"] = [{**episode, "overview": ""} for episode in fallback.get("episodes", [])]
        return fallback
    found_type, tmdb_id = found
    payload = await _get(f"{_type_path(found_type)}/{tmdb_id}", append_to_response="credits")
    normalized = _metadata_item({**payload, "id": tmdb_id}, found_type, imdb_id)
    if not normalized:
        return fallback
    result = {**fallback, **normalized}
    credits = payload.get("credits") if isinstance(payload.get("credits"), dict) else {}
    cast = credits.get("cast") if isinstance(credits.get("cast"), list) else []
    result["cast"] = [
        {
            "name": person.get("name"),
            "character": person.get("character") or "",
            "profile_url": f"https://image.tmdb.org/t/p/w185{person['profile_path']}" if person.get("profile_path") else None,
        }
        for person in cast[:12]
        if isinstance(person, dict) and isinstance(person.get("name"), str)
    ]
    result["overview"] = payload.get("overview") or ""
    if found_type == "series":
        seasons = payload.get("seasons") if isinstance(payload.get("seasons"), list) else []
        season_by_number = {
            season.get("season_number"): season
            for season in seasons
            if isinstance(season, dict) and isinstance(season.get("season_number"), int)
        }
        result["seasons"] = [
            {
                "season_number": number,
                "name": season.get("name") or f"Temporada {number:02d}",
                "episode_count": season.get("episode_count") or 0,
                "air_date": season.get("air_date"),
            }
            for number, season in sorted(season_by_number.items())
        ]
        result["season_count"] = sum(number > 0 for number in season_by_number)
        episodes = list(fallback.get("episodes") or [])
        episode_keys = sorted({episode.get("season_number") for episode in episodes if isinstance(episode.get("season_number"), int) and episode["season_number"] > 0})
        semaphore = asyncio.Semaphore(5)

        async def season_details(number: int) -> tuple[int, dict]:
            async with semaphore:
                return number, await _get(f"tv/{tmdb_id}/season/{number}")

        localized_seasons = await asyncio.gather(*(season_details(number) for number in episode_keys), return_exceptions=True)
        by_episode = {}
        for season_result in localized_seasons:
            if isinstance(season_result, Exception):
                continue
            number, data = season_result
            for episode in data.get("episodes", []) if isinstance(data.get("episodes"), list) else []:
                if isinstance(episode, dict) and isinstance(episode.get("episode_number"), int):
                    by_episode[(number, episode["episode_number"])] = episode
        for episode in episodes:
            localized = by_episode.get((episode.get("season_number"), episode.get("episode_number")))
            if not localized:
                episode["overview"] = ""
                continue
            episode["title"] = localized.get("name") or episode.get("title") or ""
            episode["overview"] = localized.get("overview") or ""
            episode["released"] = localized.get("air_date") or episode.get("released")
        result["episodes"] = episodes
        result["episode_count"] = len([episode for episode in episodes if episode.get("season_number", 0) > 0])
    else:
        result["seasons"] = []
        result["episodes"] = []
        result["season_count"] = 0
        result["episode_count"] = 0
    return result


async def localized_preview(item: dict) -> dict:
    imdb_id = item.get("imdb_id")
    media_type = item.get("media_type")
    if not TMDB_API_TOKEN or not isinstance(imdb_id, str) or media_type not in {"movie", "series"}:
        return item
    found = await _find_by_imdb(imdb_id, media_type)
    if not found:
        return {**item, "overview": "", "metadata_language": TMDB_LANGUAGE}
    found_type, tmdb_id = found
    payload = await _get(f"{_type_path(found_type)}/{tmdb_id}")
    normalized = _metadata_item({**payload, "id": tmdb_id}, found_type, imdb_id)
    if not normalized:
        return {**item, "overview": "", "metadata_language": TMDB_LANGUAGE}
    return {**item, **normalized, "search_titles": list(dict.fromkeys([*(item.get("search_titles") or []), *(normalized.get("search_titles") or [])]))}


async def _category(path: str, media_type: str, **params) -> list[dict]:
    response = await _get(path, **params)
    rows = response.get("results")
    return [(item, media_type) for item in rows[:10] if isinstance(item, dict)] if isinstance(rows, list) else []


async def home_catalog(seeds: list[tuple[str, int]]) -> dict:
    if not TMDB_API_TOKEN:
        return {"configured": False, "sections": []}
    cache_key = tuple(seeds[:4])
    cached = _home_cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return deepcopy(cached[1])
    today = date.today()
    end_date = today + timedelta(days=90)
    jobs = [
        _category("trending/movie/week", "movie"),
        _category("trending/tv/week", "series"),
        _category("discover/movie", "movie", region="BR", sort_by="primary_release_date.asc", **{"release_date.gte": today.isoformat(), "release_date.lte": end_date.isoformat()}),
        _category("discover/tv", "series", sort_by="first_air_date.asc", **{"first_air_date.gte": today.isoformat(), "first_air_date.lte": end_date.isoformat()}),
    ]
    recommendation_jobs = [
        _category(f"{_type_path(media_type)}/{tmdb_id}/recommendations", media_type)
        for media_type, tmdb_id in seeds[:4]
    ]
    results = await asyncio.gather(*(jobs + recommendation_jobs), return_exceptions=True)
    trending_rows = []
    release_rows = []
    recommendation_rows: list[tuple[dict, str]] = []
    if not isinstance(results[0], Exception):
        trending_rows.extend(results[0])
    if not isinstance(results[1], Exception):
        trending_rows.extend(results[1])
    if not isinstance(results[2], Exception):
        release_rows.extend(results[2])
    if not isinstance(results[3], Exception):
        release_rows.extend(results[3])
    for result in results[4:]:
        if not isinstance(result, Exception):
            recommendation_rows.extend(result)

    def unique(rows: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
        seen = set()
        output = []
        for item, media_type in rows:
            key = (media_type, item.get("id"))
            if key not in seen:
                seen.add(key)
                output.append((item, media_type))
            if len(output) >= 20:
                break
        return output

    trending, releases, recommendations = await asyncio.gather(
        _with_imdb_ids(unique(trending_rows)),
        _with_imdb_ids(unique(release_rows)),
        _with_imdb_ids(unique(recommendation_rows)),
    )
    result = {
        "configured": True,
        "warning": "Nao foi possivel consultar os catalogos do TMDb. Verifique o token da API no Coolify." if all(isinstance(item, Exception) for item in results[:4]) else None,
        "sections": [
            {"id": "for-you", "title": "Para você", "items": recommendations[:12]},
            {"id": "releases", "title": "Lançamentos", "items": releases[:12]},
            {"id": "trending", "title": "Em alta", "items": trending[:12]},
        ],
    }
    _home_cache[cache_key] = (time.monotonic() + 900, result)
    return deepcopy(result)

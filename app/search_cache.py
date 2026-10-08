import asyncio
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import logging
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import REDIS_URL


logger = logging.getLogger("media_library_manager.search_cache")
SEARCH_CACHE_TTL_SECONDS = 120
LOCAL_CACHE_LIMIT = 128
REDIS_TIMEOUT_SECONDS = 0.5
_local_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_redis_client: Redis | None = None
_redis_retry_after = 0.0


def _key(query: str, media_type: str) -> str:
    normalized_query = " ".join(query.casefold().split())
    digest = hashlib.sha256(f"{media_type}\0{normalized_query}".encode("utf-8")).hexdigest()
    return f"mlm:search:v1:{digest}"


def _local_get(key: str) -> dict | None:
    entry = _local_cache.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if expires_at <= time.monotonic():
        _local_cache.pop(key, None)
        return None
    _local_cache.move_to_end(key)
    return deepcopy(value)


def _local_set(key: str, value: dict) -> None:
    _local_cache[key] = (time.monotonic() + SEARCH_CACHE_TTL_SECONDS, deepcopy(value))
    _local_cache.move_to_end(key)
    while len(_local_cache) > LOCAL_CACHE_LIMIT:
        _local_cache.popitem(last=False)


def _client() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
            health_check_interval=30,
        )
    return _redis_client


async def get_search_response(query: str, media_type: str) -> dict | None:
    global _redis_retry_after
    key = _key(query, media_type)
    cached = _local_get(key)
    if cached is not None:
        return cached
    if not REDIS_URL or time.monotonic() < _redis_retry_after:
        return None
    try:
        raw = await asyncio.wait_for(_client().get(key), timeout=REDIS_TIMEOUT_SECONDS)
        value = json.loads(raw) if raw else None
    except (RedisError, asyncio.TimeoutError, OSError, ValueError):
        _redis_retry_after = time.monotonic() + 5
        logger.warning("Redis search cache unavailable; continuing without Redis.")
        return None
    if isinstance(value, dict):
        _local_set(key, value)
        return deepcopy(value)
    return None


async def set_search_response(query: str, media_type: str, value: dict) -> None:
    global _redis_retry_after
    key = _key(query, media_type)
    _local_set(key, value)
    if not REDIS_URL or time.monotonic() < _redis_retry_after:
        return
    try:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        await asyncio.wait_for(
            _client().set(key, serialized, ex=SEARCH_CACHE_TTL_SECONDS),
            timeout=REDIS_TIMEOUT_SECONDS,
        )
    except (RedisError, asyncio.TimeoutError, OSError, TypeError, ValueError):
        _redis_retry_after = time.monotonic() + 5
        logger.warning("Redis search cache unavailable; continuing without Redis.")


async def close_search_cache() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None

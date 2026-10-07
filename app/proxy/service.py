from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.stremio.manifest import validate_public_https_url


HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
RELAY_RESPONSE_HEADERS = {
    "accept-ranges", "content-disposition", "content-encoding", "content-length",
    "content-range", "content-type", "etag", "last-modified", "expires",
    "cache-control",
}
SENSITIVE_REQUEST_HEADERS = {"authorization", "cookie", "proxy-authorization"}


def has_proxy_headers(stream: dict) -> bool:
    hints = stream.get("behavior_hints") or {}
    proxy_headers = hints.get("proxyHeaders") if isinstance(hints, dict) else None
    return isinstance(proxy_headers, dict) and any(
        isinstance(proxy_headers.get(key), dict) and proxy_headers[key]
        for key in ("request", "response")
    )


def _valid_headers(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    headers = {}
    for name, content in value.items():
        if not isinstance(name, str) or not isinstance(content, str):
            continue
        if "\r" in name or "\n" in name or "\r" in content or "\n" in content:
            continue
        if name.casefold() in HOP_BY_HOP_HEADERS:
            continue
        headers[name] = content
    return headers


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return parsed.scheme, (parsed.hostname or "").lower(), parsed.port or 443


async def proxy_stream(url: str, request: Request, behavior_hints: dict) -> StreamingResponse:
    validate_public_https_url(url)
    proxy_headers = behavior_hints.get("proxyHeaders") if isinstance(behavior_hints, dict) else {}
    proxy_headers = proxy_headers if isinstance(proxy_headers, dict) else {}
    request_headers = _valid_headers(proxy_headers.get("request"))
    response_overrides = _valid_headers(proxy_headers.get("response"))
    incoming_range = request.headers.get("range")
    if incoming_range and not any(name.casefold() == "range" for name in request_headers):
        request_headers["Range"] = incoming_range
    if_range = request.headers.get("if-range")
    if if_range and not any(name.casefold() == "if-range" for name in request_headers):
        request_headers["If-Range"] = if_range

    client = httpx.AsyncClient(timeout=None, follow_redirects=False)
    current_url = url
    current_headers = dict(request_headers)
    upstream = None
    try:
        for redirect_number in range(4):
            validate_public_https_url(current_url)
            upstream_request = client.build_request("GET", current_url, headers=current_headers)
            upstream = await client.send(upstream_request, stream=True)
            if upstream.status_code not in {301, 302, 303, 307, 308}:
                break
            location = upstream.headers.get("location")
            if not location or redirect_number == 3:
                raise HTTPException(status_code=502, detail="O servidor do stream retornou um redirecionamento que nao pode ser validado.")
            next_url = urljoin(current_url, location)
            validate_public_https_url(next_url)
            if _origin(next_url) != _origin(current_url):
                current_headers = {
                    name: value for name, value in current_headers.items()
                    if name.casefold() not in SENSITIVE_REQUEST_HEADERS
                }
            await upstream.aclose()
            upstream = None
            current_url = next_url
        if upstream is None:
            raise HTTPException(status_code=502, detail="O servidor do stream nao retornou uma resposta.")
    except HTTPException:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        raise
    except httpx.HTTPError as exc:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="Nao foi possivel conectar ao stream autorizado.") from exc

    headers = {
        name: upstream.headers[name]
        for name in RELAY_RESPONSE_HEADERS
        if name in upstream.headers
    }
    headers.update(response_overrides)

    async def content():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(content(), status_code=upstream.status_code, headers=headers, background=BackgroundTask(client.aclose))

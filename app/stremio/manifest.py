import ipaddress
import socket
from urllib.parse import urlsplit

from fastapi import HTTPException


def validate_public_https_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="Manifest e streams devem usar URL HTTPS pública.")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "metadata.google.internal"} or "." not in hostname:
        raise HTTPException(status_code=422, detail="Host local não permitido.")
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise HTTPException(status_code=422, detail="Não foi possível validar o host do addon.") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise HTTPException(status_code=422, detail="O host resolve para uma rede não pública.")
    return url


def parse_manifest(value: object) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(status_code=502, detail="O manifest do addon não é um objeto JSON.")
    for field in ("id", "name", "version"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise HTTPException(status_code=502, detail=f"Manifest inválido: campo {field} ausente.")
    if not isinstance(value.get("resources"), list) or not value["resources"]:
        raise HTTPException(status_code=502, detail="Manifest inválido: resources ausentes.")
    if not isinstance(value.get("types"), list):
        raise HTTPException(status_code=502, detail="Manifest inválido: types ausente.")
    return value


def supports(manifest: dict, resource_name: str, media_type: str, item_id: str) -> bool:
    resources = manifest.get("resources") or []
    root_prefixes = manifest.get("idPrefixes") or []
    for resource in resources:
        if isinstance(resource, str):
            if resource != resource_name:
                continue
            resource_types = manifest.get("types") or []
            prefixes = root_prefixes
        elif isinstance(resource, dict):
            if resource.get("name") != resource_name:
                continue
            resource_types = resource.get("types") or manifest.get("types") or []
            prefixes = resource.get("idPrefixes") or root_prefixes
        else:
            continue
        valid_prefixes = [prefix for prefix in prefixes if isinstance(prefix, str) and prefix]
        if media_type in resource_types and (not valid_prefixes or any(item_id.startswith(prefix) for prefix in valid_prefixes)):
            return True
    return False

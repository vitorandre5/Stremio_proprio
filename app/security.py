import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict

from fastapi import Depends, Header, HTTPException, Request

from app.config import SESSION_SECRET


SESSION_COOKIE = "mlm_session"
CSRF_COOKIE = "mlm_csrf"
SESSION_TTL = 7 * 24 * 60 * 60
_login_attempts: dict[str, list[float]] = defaultdict(list)


def _secret() -> bytes:
    if len(SESSION_SECRET) < 32:
        raise HTTPException(status_code=503, detail="Configure SESSION_SECRET com pelo menos 32 caracteres.")
    return SESSION_SECRET.encode("utf-8")


def _sign(value: str) -> str:
    return hmac.new(_secret(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session(user: dict) -> str:
    payload = json.dumps({
        "issued_at": int(time.time()),
        "nonce": secrets.token_urlsafe(24),
        "user_id": user["id"],
        "name": user["name"],
        "is_admin": bool(user["is_admin"]),
    }, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(encoded)}"


def session_user(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        encoded, signature = value.split(".", 1)
        if not hmac.compare_digest(signature, _sign(encoded)):
            return None
        payload = encoded + "=" * (-len(encoded) % 4)
        user = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        age = int(time.time()) - int(user["issued_at"])
        if not 0 <= age <= SESSION_TTL:
            return None
        if not user.get("user_id") or not user.get("name"):
            return None
        return user
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, HTTPException):
        return None


def valid_session(value: str | None) -> bool:
    return session_user(value) is not None


def check_login_rate_limit(client_ip: str) -> None:
    now = time.time()
    attempts = [timestamp for timestamp in _login_attempts[client_ip] if now - timestamp < 900]
    _login_attempts[client_ip] = attempts
    if len(attempts) >= 8:
        raise HTTPException(status_code=429, detail="Muitas tentativas. Aguarde 15 minutos.")


def record_login_attempt(client_ip: str, succeeded: bool) -> None:
    if succeeded:
        _login_attempts.pop(client_ip, None)
    else:
        _login_attempts[client_ip].append(time.time())


async def require_session(request: Request) -> None:
    user = session_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="Faca login para continuar.")
    request.state.user = user


async def require_mutation(
    request: Request,
    x_csrf_token: str | None = Header(default=None),
    _: None = Depends(require_session),
) -> None:
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not x_csrf_token or not hmac.compare_digest(cookie, x_csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")


async def require_admin_mutation(
    request: Request,
    _: None = Depends(require_mutation),
) -> None:
    if not getattr(request.state, "user", {}).get("is_admin"):
        raise HTTPException(status_code=403, detail="Somente administradores Jellyfin podem alterar a biblioteca.")


def new_csrf_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")

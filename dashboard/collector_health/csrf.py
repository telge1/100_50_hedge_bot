"""CSRF token + same-origin POST guard for collector-health mutating routes."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Any

from stoch_universe_51.origin import content_type_is_json, origin_is_allowed, _origin_from_referer

TOKEN_TTL_S = 3600
COOKIE_NAME = "collector_health_csrf"


def _secret() -> bytes:
    raw = (
        os.environ.get("COLLECTOR_HEALTH_CSRF_SECRET")
        or os.environ.get("DASHBOARD_SECRET")
        or os.environ.get("SESSION_SECRET")
        or "collector-health-dev-only-change-me"
    )
    return hashlib.sha256(raw.encode("utf-8")).digest()


def issue_csrf_token(*, now: float | None = None) -> str:
    ts = int(now if now is not None else time.time())
    nonce = secrets.token_hex(16)
    payload = f"{ts}:{nonce}"
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def validate_csrf_token(token: str | None, *, now: float | None = None) -> bool:
    if not token or token.count(":") != 2:
        return False
    ts_s, nonce, sig = token.split(":", 2)
    try:
        ts = int(ts_s)
    except ValueError:
        return False
    if not nonce or not sig:
        return False
    payload = f"{ts}:{nonce}"
    expect = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return False
    current = now if now is not None else time.time()
    if current - ts > TOKEN_TTL_S or ts > current + 60:
        return False
    return True


def mutate_post_guard(
    *,
    origin: str | None,
    referer: str | None,
    content_type: str | None,
    csrf_header: str | None,
    csrf_cookie: str | None,
) -> str | None:
    """Return error code or None if allowed."""
    if not content_type_is_json(content_type):
        return "JSON_CONTENT_TYPE_REQUIRED"
    candidate = (origin or "").strip() or _origin_from_referer(referer or "")
    if not origin_is_allowed(candidate):
        return "ORIGIN_FORBIDDEN"
    token = (csrf_header or "").strip() or (csrf_cookie or "").strip()
    if not validate_csrf_token(token):
        return "CSRF_INVALID"
    if csrf_header and csrf_cookie and csrf_header.strip() != csrf_cookie.strip():
        return "CSRF_MISMATCH"
    return None


def csrf_issue_payload() -> dict[str, Any]:
    token = issue_csrf_token()
    return {"csrf_token": token, "header": "X-CSRF-Token", "cookie": COOKIE_NAME, "ttl_s": TOKEN_TTL_S}

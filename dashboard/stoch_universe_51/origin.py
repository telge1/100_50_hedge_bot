"""Same-origin / Origin allowlist for candle-write POST. No new auth stack."""

from __future__ import annotations

from urllib.parse import urlparse

from .config import ALLOWED_UPDATE_ORIGINS

ALLOWED_HOSTS = {"dash.immotel.de", "127.0.0.1", "localhost"}


def _origin_from_referer(referer: str) -> str:
    parsed = urlparse(referer)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def origin_is_allowed(origin: str, *, extra_origins: tuple[str, ...] = ()) -> bool:
    text = (origin or "").strip().rstrip("/")
    if not text:
        return False
    allowed = set(ALLOWED_UPDATE_ORIGINS) | set(extra_origins)
    if text in allowed:
        return True
    parsed = urlparse(text)
    return parsed.hostname in ALLOWED_HOSTS


def content_type_is_json(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";")[0].strip().lower() == "application/json"


def update_post_guard(
    *,
    origin: str | None,
    referer: str | None,
    content_type: str | None,
    extra_origins: tuple[str, ...] = (),
) -> str | None:
    """Return error code or None if the POST may proceed."""
    if not content_type_is_json(content_type):
        return "JSON_CONTENT_TYPE_REQUIRED"
    candidate = (origin or "").strip() or _origin_from_referer(referer or "")
    if not origin_is_allowed(candidate, extra_origins=extra_origins):
        return "ORIGIN_FORBIDDEN"
    return None

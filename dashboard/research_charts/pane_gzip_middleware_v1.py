"""Scoped gzip transport for POST /api/research/pane only (MC4O-2).

Decompressed bytes must match the legacy JSON body bit-for-bit.
Controlled by RESEARCH_PANE_GZIP_ENABLED=1.
"""

from __future__ import annotations

import gzip
import os
from typing import Callable, Awaitable

MIN_GZIP_BYTES = 1_048_576
GZIP_LEVEL = 3
PANE_PATH = "/api/research/pane"


def research_pane_gzip_enabled() -> bool:
    val = os.environ.get("RESEARCH_PANE_GZIP_ENABLED", "").strip()
    return val == "1"


def _header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


def client_accepts_gzip(scope: dict) -> bool:
    enc = _header_value(scope.get("headers") or [], b"accept-encoding")
    if not enc:
        return False
    return b"gzip" in enc.lower()


class ResearchPaneGzipMiddleware:
    """ASGI middleware: gzip JSON pane responses when flag ON and body >= 1 MiB."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if (
            not research_pane_gzip_enabled()
            or scope.get("method") != "POST"
            or scope.get("path") != PANE_PATH
            or not client_accepts_gzip(scope)
        ):
            await self.app(scope, receive, send)
            return

        start_message: dict | None = None
        body = bytearray()
        more_body = True

        async def send_wrapper(message: dict) -> None:
            nonlocal start_message, body, more_body
            if message["type"] == "http.response.start":
                start_message = message
                return
            if message["type"] == "http.response.body":
                body.extend(message.get("body") or b"")
                more_body = bool(message.get("more_body"))
                if more_body:
                    return
                await self._emit(send, start_message, bytes(body))
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _emit(self, send, start_message: dict | None, body: bytes) -> None:
        if start_message is None:
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return

        status = int(start_message.get("status", 200))
        headers = list(start_message.get("headers") or [])

        if (
            status < 200
            or status >= 300
            or len(body) < MIN_GZIP_BYTES
            or _header_value(headers, b"content-encoding") is not None
        ):
            await send(start_message)
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return

        compressed = gzip.compress(body, compresslevel=GZIP_LEVEL)
        new_headers: list[tuple[bytes, bytes]] = []
        vary_present = False
        for key, value in headers:
            kl = key.lower()
            if kl in (b"content-length", b"content-encoding"):
                continue
            if kl == b"vary":
                vary_present = True
                lower = value.lower()
                if b"accept-encoding" not in lower:
                    value = value + b", Accept-Encoding"
                new_headers.append((key, value))
            else:
                new_headers.append((key, value))
        if not vary_present:
            new_headers.append((b"vary", b"Accept-Encoding"))
        new_headers.append((b"content-encoding", b"gzip"))
        new_headers.append((b"content-length", str(len(compressed)).encode("ascii")))

        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": new_headers,
            }
        )
        await send({"type": "http.response.body", "body": compressed, "more_body": False})
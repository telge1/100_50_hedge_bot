#!/usr/bin/env python3
"""Serve Wall Decision V1 fixture preview on a free port (never 3000)."""

from __future__ import annotations

import argparse
import socket
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1]
STATIC_ROOT = DASHBOARD / "static"
FIXTURES = DASHBOARD / "wall_decision_v1" / "fixtures"
PREVIEW_DIR = DASHBOARD / "static" / "market_profile_v1" / "wall_decision_preview"


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(preferred: int = 3011) -> int:
    if preferred == 3000:
        raise SystemExit("Refusing port 3000 (live dashboard).")
    if port_free(preferred):
        return preferred
    for p in range(3012, 3030):
        if p != 3000 and port_free(p):
            return p
    raise SystemExit("No free preview port in 3011-3029")


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:  # noqa: N802
        from urllib.parse import unquote, urlparse

        parsed = urlparse(path)
        rel = unquote(parsed.path)
        if rel in ("/", "/index.html"):
            return str(PREVIEW_DIR / "index.html")
        if rel.startswith("/static/"):
            return str(STATIC_ROOT / rel[len("/static/") :])
        if rel.startswith("/fixtures/"):
            return str(FIXTURES / rel[len("/fixtures/") :])
        return str(PREVIEW_DIR / rel.lstrip("/"))

    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3011)
    args = ap.parse_args()
    if args.port == 3000:
        raise SystemExit("Refusing port 3000")
    port = pick_port(args.port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"WALL_DECISION_PREVIEW listening on http://127.0.0.1:{port}/")
    print("FIXTURE_TEST_DATA only — not live.")
    print(f"Worktree: {DASHBOARD.parent}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()

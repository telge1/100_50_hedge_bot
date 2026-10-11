"""Loopback viewer for one historical close at a time. No productive routes."""

from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import clickhouse_connect

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import ClickHousePublicationSource
from research_charts.live_micro_lld_v2_ch_test.phase11u_f.provider import (
    ORACLES,
    AdapterStop,
    HistoricalHeatmapSession,
    iso,
)

HERE = Path(__file__).resolve().parent
VENDOR = HERE.parents[2] / "static" / "research_trp" / "vendor" / "lightweight-charts.min.js"


class ReadOnlyPublications(ClickHousePublicationSource):
    def closes(self) -> list[str]:
        rows = self._client.query(
            f"""
            SELECT decision_close, snapshot_id, active_state_fingerprint
            FROM {self._database}.research_lmv2_active_snapshot_publish_v1 FINAL
            WHERE symbol = 'DOGEUSDT' AND timeframe = '5m' AND pool_detail = 10
              AND publish_complete = 1
              AND decision_close >= toDateTime64('2026-09-24 00:00:00', 3, 'UTC')
              AND decision_close <= toDateTime64('2026-09-30 23:55:00', 3, 'UTC')
            ORDER BY decision_close
            """
        ).result_rows
        by_close = {}
        for decision, snapshot_id, fingerprint in rows:
            stamp = decision if decision.tzinfo else decision.replace(tzinfo=timezone.utc)
            bucket = by_close.setdefault(stamp, {"ids": set(), "fingerprints": set()})
            bucket["ids"].add(str(snapshot_id))
            bucket["fingerprints"].add(str(fingerprint))
        if any(len(bucket["ids"]) != 1 or len(bucket["fingerprints"]) != 1 for bucket in by_close.values()):
            raise AdapterStop("AMBIGUOUS_PUBLICATION", "a published close has more than one snapshot")
        return [iso(stamp) for stamp in sorted(by_close)]


def _free_port() -> int:
    for port in range(8768, 8780):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit("BLOCKED no free loopback port")


def make_handler(session: HistoricalHeatmapSession):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
                return
            if parsed.path == "/viewer.js":
                self._send(200, (HERE / "viewer.js").read_bytes(), "text/javascript; charset=utf-8")
                return
            if parsed.path == "/vendor/lightweight-charts.min.js":
                self._send(200, VENDOR.read_bytes(), "text/javascript")
                return
            if parsed.path == "/api/index":
                self._send(200, json.dumps(session.index()).encode("utf-8"), "application/json")
                return
            if parsed.path == "/api/payload":
                query = parse_qs(parsed.query)
                try:
                    payload = session.payload_for(
                        query.get("close", [""])[0],
                        focus_pool_id=query.get("focus", [None])[0],
                    )
                except AdapterStop as exc:
                    body = json.dumps({"error": exc.status, "detail": exc.detail}).encode("utf-8")
                    self._send(400, body, "application/json")
                    return
                self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
                return
            self._send(404, b'{"detail":"not found"}', "application/json")

    return Handler


def audit(session: HistoricalHeatmapSession) -> dict:
    import time

    started = time.perf_counter()
    first = session.payload_for("2026-09-24T00:00:00Z")
    miss = time.perf_counter() - started
    hit_started = time.perf_counter()
    session.payload_for("2026-09-24T00:00:00Z")
    hit = time.perf_counter() - hit_started
    neighbor_started = time.perf_counter()
    session.payload_for("2026-09-24T00:05:00Z")
    neighbor = time.perf_counter() - neighbor_started
    jump_started = time.perf_counter()
    jumped = session.payload_for("2026-09-30T12:05:00Z")
    jump = time.perf_counter() - jump_started
    oracle = []
    for stamp, (active, remaining) in ORACLES.items():
        payload = session.payload_for(stamp)
        oracle.append(
            {
                "decision_close": stamp,
                "match": len(payload["original"]) == active and len(payload["remaining"]) == remaining,
                "active": len(payload["original"]),
                "remaining": len(payload["remaining"]),
            }
        )
    upper = next(case for case in session.cases if case["side"] == "upper")
    lower = next(case for case in session.cases if case["side"] == "lower")
    return {
        "first_payload_seconds": round(miss, 3),
        "cache_hit_seconds": round(hit, 3),
        "neighbor_seconds": round(neighbor, 3),
        "jump_seconds": round(jump, 3),
        "payload_bytes": len(json.dumps(first)),
        "jump_payload_bytes": len(json.dumps(jumped)),
        "cache_hits": session.payloads.hits,
        "cache_misses": session.payloads.misses,
        "candle_loads": session.candles_cache.loads,
        "oracle": oracle,
        "upper_case": upper,
        "lower_case": lower,
        "case_count": len(session.cases),
    }


def main() -> None:
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        session = HistoricalHeatmapSession(ReadOnlyPublications(client, cfg.database))
        report = audit(session)
        (HERE / "audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        port = _free_port()
        httpd = HTTPServer(("127.0.0.1", port), make_handler(session))
        (HERE / "server_port.txt").write_text(str(port), encoding="utf-8")
        print(f"http://127.0.0.1:{port}/", flush=True)
        httpd.serve_forever()
    finally:
        client.close()


if __name__ == "__main__":
    main()

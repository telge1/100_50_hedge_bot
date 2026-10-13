#!/usr/bin/env python3
"""Phase 5U — live dashboard static-timeline smoke (authenticated HTTP)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

_SPREAD_DASH = Path(
    os.environ.get(
        "PHASE5U_SPREAD_DASHBOARD",
        "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard",
    )
)
_DASH = _SPREAD_DASH
for _p in (_DASH,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.chart_historical_lmv2_pools import chart_historical_lmv2_static_timeline

BASE = os.environ.get("PHASE5U_DASHBOARD_URL", "http://127.0.0.1:3000")
SEP_FROM = "2026-09-01T00:00:00Z"
SEP_TO = "2026-09-03T23:45:00Z"
APR_FROM = "2026-04-01T00:00:00Z"
APR_TO = "2026-04-03T23:45:00Z"
PIPELINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd10_v1"


def _sorted_zones(zones: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not zones:
        return []
    return sorted(
        zones,
        key=lambda z: (
            str(z.get("pool_id") or ""),
            int(z.get("start_timestamp") or 0),
            int(z.get("end_timestamp") or 0),
        ),
    )


def _canonical_hash(payload: dict[str, Any]) -> str:
    body = {
        "zones": _sorted_zones(payload.get("zones")),
        "segment_count": payload.get("segment_count"),
        "pool_count": payload.get("pool_count"),
        "episode_walk_close_count": payload.get("episode_walk_close_count"),
        "pilot_close_count": payload.get("pilot_close_count"),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _session_cookie() -> str | None:
    sessions_path = _DASH / "logs" / "sessions.json"
    if not sessions_path.exists():
        return None
    data = json.loads(sessions_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data:
        return next(iter(data.keys()))
    return None


def _login(client: httpx.Client) -> bool:
    user = os.environ.get("DASHBOARD_SMOKE_USER", "").strip()
    password = os.environ.get("DASHBOARD_SMOKE_PASSWORD", "").strip()
    if not user or not password:
        return False
    r = client.post("/login", data={"username": user, "password": password}, follow_redirects=False)
    return r.status_code in (302, 303) and "session_id" in r.cookies


def _post_timeline(client: httpx.Client, from_close: str, to_close: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    r = client.post(
        "/api/research/historical-lld-v2-static-timeline",
        json={
            "symbol": "DOGEUSDT",
            "timeframe": "15m",
            "pool_detail": 10,
            "from_close": from_close,
            "to_close": to_close,
            "include_active": True,
        },
        timeout=120.0,
    )
    wall_ms = (time.perf_counter() - t0) * 1000.0
    out: dict[str, Any] = {"http_status": r.status_code, "wall_ms": round(wall_ms, 2)}
    if r.status_code == 200:
        body = r.json()
        payload = {k: v for k, v in body.items() if k not in ("success", "message")}
        out["payload"] = payload
        out["canonical_hash"] = _canonical_hash(payload)
        out["batch_reader_active"] = payload.get("historical_lmv2_batch_reader_active")
        out["pipeline_line_id"] = payload.get("pipeline_line_id")
        out["pilot_close_count"] = payload.get("pilot_close_count")
    else:
        out["error_body"] = r.text[:500]
    return out


def _reference_local(from_close: str, to_close: str, use_batch: bool) -> dict[str, Any]:
    if use_batch:
        os.environ["HISTORICAL_LMV2_BATCH_READER"] = "1"
    else:
        os.environ.pop("HISTORICAL_LMV2_BATCH_READER", None)
    t0 = time.perf_counter()
    payload = chart_historical_lmv2_static_timeline(
        "DOGEUSDT", from_close, to_close, include_active=True
    )
    wall_ms = (time.perf_counter() - t0) * 1000.0
    zones = payload.get("zones") or []
    pool_ids = sorted({str(z.get("pool_id")) for z in zones if z.get("pool_id")})
    return {
        "canonical_hash": _canonical_hash(payload),
        "wall_ms": round(wall_ms, 2),
        "pilot_close_count": payload.get("pilot_close_count"),
        "pipeline_line_id": payload.get("pipeline_line_id"),
        "batch_reader_active": payload.get("historical_lmv2_batch_reader_active"),
        "zone_pool_id_count": len(pool_ids),
    }


def main() -> int:
    cookie = _session_cookie()
    with httpx.Client(base_url=BASE, timeout=120.0) as client:
        authed = False
        if cookie:
            client.cookies.set("session_id", cookie)
            authed = client.get("/dashboard").status_code == 200
        if not authed:
            authed = _login(client)
        if not authed:
            print(json.dumps({"auth": "FAILED", "hint": "session or DASHBOARD_SMOKE_USER/PASSWORD"}))
            return 2

        sep_ref_alt = _reference_local(SEP_FROM, SEP_TO, use_batch=False)
        sep_ref_batch = _reference_local(SEP_FROM, SEP_TO, use_batch=True)
        sep_live = _post_timeline(client, SEP_FROM, SEP_TO)
        apr_live = _post_timeline(client, APR_FROM, APR_TO)
    os.environ.pop("HISTORICAL_LMV2_BATCH_READER", None)

    report = {
        "dashboard_url": BASE,
        "auth": "session_cookie_or_login",
        "september_smoke": {
            "window": [SEP_FROM, SEP_TO],
            "reference_alt_local": sep_ref_alt,
            "reference_batch_local": sep_ref_batch,
            "live_api": {
                k: v for k, v in sep_live.items() if k != "payload"
            },
            "local_alt_vs_batch_hash_match": sep_ref_alt.get("canonical_hash")
            == sep_ref_batch.get("canonical_hash"),
            "live_vs_local_batch_hash_match": sep_ref_batch.get("canonical_hash")
            == sep_live.get("canonical_hash"),
            "pipeline_ok": sep_live.get("pipeline_line_id") == PIPELINE,
            "display_closes_ok": sep_live.get("pilot_close_count") == 288,
            "batch_active_ok": sep_live.get("batch_reader_active") is True,
        },
        "april_smoke": {
            "window": [APR_FROM, APR_TO],
            "live_api": {k: v for k, v in apr_live.items() if k != "payload"},
            "pipeline_ok": apr_live.get("pipeline_line_id") == PIPELINE,
            "batch_active_ok": apr_live.get("batch_reader_active") is True,
        },
    }
    print(json.dumps(report, indent=2))
    ok = (
        report["september_smoke"]["local_alt_vs_batch_hash_match"]
        and report["september_smoke"]["live_vs_local_batch_hash_match"]
        and report["september_smoke"]["pipeline_ok"]
        and report["september_smoke"]["display_closes_ok"]
        and report["september_smoke"]["batch_active_ok"]
        and report["april_smoke"]["pipeline_ok"]
        and report["april_smoke"]["batch_active_ok"]
        and sep_live.get("http_status") == 200
        and apr_live.get("http_status") == 200
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

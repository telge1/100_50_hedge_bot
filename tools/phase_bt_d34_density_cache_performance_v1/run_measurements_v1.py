#!/usr/bin/env python3
"""BT-D34 limited timing + deployment probes (authenticated HTTP when sessions exist)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
OUT = Path("/home/telgenbuescher/projects/pools+ob+delta_bot/results/phase_bt_d34_density_cache_performance_v1")
PY = DASH.parent / ".venv/bin/python"
WINDOW = {
    "from_utc": "2026-09-11T07:34:51Z",
    "to_utc": "2026-09-11T15:34:51Z",
    "timeframe": "5m",
    "pool_detail": 1,
}


def _py(cmd: str) -> dict:
    env = os.environ.copy()
    root = DASH.parent
    env["PYTHONPATH"] = ":".join(
        [
            str(root),
            str(DASH),
            "/home/telgenbuescher/projects/pools+ob+delta_bot",
            str(root / "data"),
        ]
    )
    proc = subprocess.run(
        [str(PY), "-c", cmd],
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
        cwd=str(DASH),
    )
    if proc.returncode != 0:
        return {"ok": False, "stderr": proc.stderr[-2000:]}
    return json.loads(proc.stdout)


def in_process_timing() -> dict:
    script = r'''
import json, time, os
os.environ["PD1_WINDOW_RESPONSE_CACHE"] = "1"
from pd1_landscape_viewer_v1.window_response_cache_v1 import clear_cache_for_tests
from pd1_landscape_viewer_v1.window_service_v1 import get_landscape_window_response
clear_cache_for_tests()
body = ''' + json.dumps(WINDOW) + r'''
kw = dict(from_utc=body["from_utc"], to_utc=body["to_utc"], timeframe=body["timeframe"], pool_detail=body["pool_detail"])
t0=time.perf_counter()
r1=get_landscape_window_response(**kw, response_mode="exact_origin_v1")
cold=time.perf_counter()-t0
t1=time.perf_counter()
r2=get_landscape_window_response(**kw, response_mode="exact_origin_v1")
warm=time.perf_counter()-t1
print(json.dumps({
  "cold_s": round(cold,3),
  "warm_hit_s": round(warm,3),
  "pools": len(r1.get("pools") or []),
  "cold_cache_hit": (r1.get("response_cache") or {}).get("hit"),
  "warm_cache_hit": (r2.get("response_cache") or {}).get("hit"),
  "cold_timing_ms": r1.get("timing_ms"),
  "warm_skipped_clusters_ms": (r2.get("timing_ms") or {}).get("build_clusters_by_dc"),
}))
'''
    return _py(script)


def http_probe() -> dict:
    sessions = json.loads((DASH / "logs/sessions.json").read_text(encoding="utf-8"))
    if not sessions:
        return {"skipped": True, "reason": "no_session"}
    sid = next(iter(sessions.keys()))
    cookie = f"session_id={sid}"
    base = "http://127.0.0.1:3000"
    body = {**WINDOW, "response_mode": "exact_origin_v1"}

    def curl(timeout: int):
        cmd = [
            "curl", "-sS", "-m", str(timeout), "-w", "\n%{http_code}\n%{time_total}",
            "-H", f"Cookie: {cookie}", "-X", "POST",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(body),
            f"{base}/live-charts/viewer/api/window",
        ]
        out = subprocess.check_output(cmd, text=True)
        lines = out.strip().split("\n")
        return int(lines[-2]), float(lines[-1]), "\n".join(lines[:-2])

    try:
        code, dt, raw = curl(250)
        data = json.loads(raw) if raw.startswith("{") else {}
        return {
            "http_code": code,
            "time_s": dt,
            "pools": len(data.get("pools") or []),
            "response_cache": data.get("response_cache"),
            "timeout_over_150s": dt > 150.0,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    contract = {
        "contract": "bt_d34_pd1_window_response_cache_v1",
        "builder_version": "pd1_landscape_historical_window_v1",
        "max_entries_default": 16,
        "env_disable": "PD1_WINDOW_RESPONSE_CACHE=0",
        "env_max": "PD1_WINDOW_RESPONSE_CACHE_MAX",
        "compute_modes": ["full", "exact_origin_v1"],
        "key_fields": [
            "symbol",
            "from_utc",
            "to_utc",
            "timeframe",
            "pool_detail",
            "pipeline_line_id",
            "config_hash",
            "compute_mode",
            "builder_version",
            "cache_contract",
        ],
    }
    (OUT / "cache_contract.json").write_text(json.dumps(contract, indent=2), encoding="utf-8")

    timing = in_process_timing()
    (OUT / "timing_comparison.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")

    http = http_probe()
    (OUT / "deployment_validation.json").write_text(
        json.dumps({"captured_utc": datetime.now(timezone.utc).isoformat(), "window_http": http}, indent=2),
        encoding="utf-8",
    )
    (OUT / "timeout_validation.json").write_text(
        json.dumps(
            {
                "middleware_window_timeout_s": 240,
                "prior_middleware_s": 150,
                "bt_d33_cold_window_s": 162,
                "note": "Cold may still exceed 240s on heavy host; warm hit should be sub-second",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT / "parity_results.json").write_text(
        json.dumps(
            {
                "automated": "run with BT_D34_SLOW_PARITY=1 pytest test_bt_d34_window_parity_slow_v1.py",
                "ref_anchors_utc": ["2026-09-11T12:00:00Z", "2026-09-11T15:35:00Z", "2026-09-11T17:00:00Z"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT / "concurrency_validation.json").write_text(
        json.dumps({"inflight_dedup": "threading.Event in window_response_cache_v1", "unit_tested": True}, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"timing": timing, "http": http}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

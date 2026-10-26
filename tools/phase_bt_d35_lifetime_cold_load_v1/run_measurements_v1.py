#!/usr/bin/env python3
"""BT-D35 timing: legacy per-close vs default batch prefill (limited runs)."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
OUT = Path("/home/telgenbuescher/projects/pools+ob+delta_bot/results/phase_bt_d35_lifetime_cold_load_v1")
PY = DASH.parent / ".venv/bin/python"
WINDOW = ("2026-09-11T07:34:51Z", "2026-09-11T15:34:51Z")


def _run(mode: str) -> dict:
    env_setup = ""
    if mode == "legacy":
        env_setup = (
            'os.environ["PD1_LANDSCAPE_BATCH_READER"]="0"\n'
            'os.environ["HISTORICAL_LMV2_BATCH_READER"]="0"\n'
        )
    else:
        env_setup = (
            'os.environ.pop("PD1_LANDSCAPE_BATCH_READER", None)\n'
            'os.environ.pop("HISTORICAL_LMV2_BATCH_READER", None)\n'
        )
    script = f"""
import hashlib, json, os, time
{env_setup}
from pd1_landscape_viewer_v1.window_response_cache_v1 import clear_cache_for_tests
from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window
clear_cache_for_tests()
t0=time.perf_counter()
r=build_landscape_window("{WINDOW[0]}","{WINDOW[1]}",timeframe="5m",pool_detail=1,compute_mode="exact_origin_v1")
dt=time.perf_counter()-t0
pools=r.get("pools") or []
ph=hashlib.sha256(json.dumps(pools,sort_keys=True,separators=(",",":")).encode()).hexdigest()
print(json.dumps({{
  "mode": "{mode}",
  "total_s": round(dt,3),
  "pools": len(pools),
  "pools_sha256": ph,
  "phase5s": (r.get("provenance") or {{}}).get("phase5s_batch_reader_active"),
  "timing_ms": r.get("timing_ms"),
  "lifetime_builder": (r.get("provenance") or {{}}).get("lifetime_builder_version"),
}}))
"""
    import subprocess

    env = os.environ.copy()
    root = DASH.parent
    env["PYTHONPATH"] = f"{root}:{DASH}:/home/telgenbuescher/projects/pools+ob+delta_bot"
    proc = subprocess.run([str(PY), "-c", script], capture_output=True, text=True, timeout=600, env=env, cwd=str(DASH))
    if proc.returncode != 0:
        return {"mode": mode, "error": proc.stderr[-1500:]}
    return json.loads(proc.stdout.strip())


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    legacy = _run("legacy")
    batch = _run("batch_default")

    warm_script = f"""
import json, time, os
os.environ.pop("PD1_LANDSCAPE_BATCH_READER", None)
from pd1_landscape_viewer_v1.window_response_cache_v1 import clear_cache_for_tests
from pd1_landscape_viewer_v1.window_service_v1 import get_landscape_window_response
clear_cache_for_tests()
kw={{"from_utc":"{WINDOW[0]}","to_utc":"{WINDOW[1]}","timeframe":"5m","pool_detail":1,"response_mode":"exact_origin_v1"}}
get_landscape_window_response(**kw)
t0=time.perf_counter(); r=get_landscape_window_response(**kw); dt=time.perf_counter()-t0
print(json.dumps({{"warm_cache_hit_s": round(dt,4), "hit": (r.get("response_cache") or {{}}).get("hit")}}))
"""
    import subprocess

    env = os.environ.copy()
    root = DASH.parent
    env["PYTHONPATH"] = f"{root}:{DASH}:/home/telgenbuescher/projects/pools+ob+delta_bot"
    warm_proc = subprocess.run([str(PY), "-c", warm_script], capture_output=True, text=True, timeout=600, env=env, cwd=str(DASH))
    warm_hit = json.loads(warm_proc.stdout) if warm_proc.returncode == 0 else {"error": warm_proc.stderr[-500:]}

    parity_ok = (
        legacy.get("pools_sha256") == batch.get("pools_sha256")
        if legacy.get("pools_sha256") and batch.get("pools_sha256")
        else None
    )

    timing = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "reference_window": list(WINDOW),
        "legacy_per_close": legacy,
        "batch_default_cold": batch,
        "parity_pools_count_match": parity_ok,
        "warm_response_cache": warm_hit,
    }
    (OUT / "timing_comparison.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")

    profile = {
        "root_cause": "2113 per-close CH loads in _load_members when Phase 5S batch prefill disabled",
        "with_batch_prefill_build_lifetimes_ms": (batch.get("timing_ms") or {}).get("build_lifetimes"),
        "legacy_build_lifetimes_ms": (legacy.get("timing_ms") or {}).get("build_lifetimes"),
        "opt_out": "PD1_LANDSCAPE_BATCH_READER=0 or HISTORICAL_LMV2_BATCH_READER=0",
    }
    (OUT / "profiling_summary.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(json.dumps(timing, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

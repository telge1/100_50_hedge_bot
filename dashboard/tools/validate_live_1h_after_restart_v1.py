#!/usr/bin/env python3
"""Live HTTP validation: 1h PD1 window API after dashboard restart."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

BASE = "http://127.0.0.1:3000"
SESSION = "viewer_smoke_live"
DASH = Path(__file__).resolve().parent.parent


def post_window(from_utc: str, to_utc: str) -> dict:
    body = json.dumps(
        {
            "from_utc": from_utc,
            "to_utc": to_utc,
            "timeframe": "1h",
            "pool_detail": 1,
            "response_mode": "exact_origin_v1",
        }
    ).encode()
    req = urllib.request.Request(
        f"{BASE}/live-charts/viewer/api/window",
        data=body,
        headers={"Content-Type": "application/json", "Cookie": f"session_id={SESSION}"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
        wall = (time.perf_counter() - t0) * 1000
        data = json.loads(raw)
        if data.get("code"):
            return {"ok": False, "http": resp.status, "wall_ms": wall, "error": data}
        rc = data.get("response_cache") or {}
        return {
            "ok": True,
            "wall_ms": round(wall, 1),
            "cache_hit": bool(rc.get("hit")),
            "lifetime": (data.get("provenance") or {}).get("lifetime_builder_version"),
            "builder": (data.get("provenance") or {}).get("builder_version"),
            "pools": len(data.get("pools") or []),
        }
    except urllib.error.HTTPError as e:
        wall = (time.perf_counter() - t0) * 1000
        return {"ok": False, "http": e.code, "wall_ms": round(wall, 1), "error": e.read()[:200].decode(errors="replace")}


def plan_fixed72_chunks(hit_from: str, hit_to: str) -> list[tuple[str, str]]:
    js = DASH / "logs" / "_plan_chunks.js"
    js.write_text(
        f"""
const fs=require('fs');const vm=require('vm');
const code=fs.readFileSync('{DASH}/static/js/research/pd1_exact_density_trp_adapter.js','utf8');
const sb={{console,Math,Date,Number,Map,Set,parseInt,parseFloat,JSON}};
vm.createContext(sb);vm.runInContext(code,sb);
const AD=sb.PD1ExactDensityTrpAdapter;
const lo=Date.parse('2026-07-01T00:00:00Z'),hi=Date.parse('2026-11-01T00:00:00Z'),sk=AD.exactOriginScopeKey('1h',1);
const h0=Date.parse('{hit_from}'),h1=Date.parse('{hit_to}');
const prefetch=36*3600*1000,tail=6*3600*1000,STEP=5*60*1000;
const from=Math.max(lo,Math.floor((h0-prefetch)/STEP)*STEP);
const to=Math.min(hi,Math.ceil((h1+tail+prefetch)/STEP)*STEP);
const wins=AD.planExactOriginFixedTiles(from,to,lo,hi,72,{{scopeKey:sk}});
console.log(JSON.stringify(wins.map(w=>[new Date(w.fromMs).toISOString().replace('.000',''),new Date(w.toMs).toISOString().replace('.000','')])));
""",
        encoding="utf-8",
    )
    out = subprocess.check_output(["node", str(js)], cwd=str(DASH), timeout=30)
    return [tuple(x) for x in json.loads(out)]


def run_scenario(name: str, hit_from: str, hit_to: str) -> dict:
    chunks = plan_fixed72_chunks(hit_from, hit_to)
    passes = []
    for f, t in chunks:
        passes.append(post_window(f, t))
    hits = sum(1 for p in passes if p.get("cache_hit"))
    cold = sum(1 for p in passes if p.get("ok") and not p.get("cache_hit"))
    fails = [p for p in passes if not p.get("ok")]
    return {
        "name": name,
        "chunks": len(chunks),
        "cold": cold,
        "hits": hits,
        "fails": fails,
        "wall_ms_sum": round(sum(p.get("wall_ms", 0) for p in passes), 1),
        "wall_ms_max": round(max((p.get("wall_ms", 0) for p in passes), default=0), 1),
        "details": passes,
    }


def parallel_window_stress(n: int = 6) -> list[dict]:
    windows = [
        ("2026-08-24T00:00:00Z", "2026-08-26T00:00:00Z"),
        ("2026-08-26T00:00:00Z", "2026-08-28T00:00:00Z"),
        ("2026-08-28T00:00:00Z", "2026-08-30T00:00:00Z"),
        ("2026-08-20T00:00:00Z", "2026-08-22T00:00:00Z"),
        ("2026-08-22T00:00:00Z", "2026-08-24T00:00:00Z"),
        ("2026-08-30T00:00:00Z", "2026-08-31T00:00:00Z"),
    ][:n]
    out = []
    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = {ex.submit(post_window, f, t): (f, t) for f, t in windows}
        for fut in as_completed(futs):
            out.append(fut.result())
    return out


def main() -> None:
    # Health
    try:
        req = urllib.request.Request(f"{BASE}/login", method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            login_http = r.status
    except Exception as e:
        login_http = str(e)

    probe = post_window("2026-08-26T00:00:00Z", "2026-08-27T00:00:00Z")
    cap_req = urllib.request.Request(
        f"{BASE}/live-charts/viewer/api/capabilities",
        headers={"Cookie": f"session_id={SESSION}"},
    )
    with urllib.request.urlopen(cap_req, timeout=60) as r:
        caps = json.loads(r.read())

    from pd1_landscape_viewer_v1.window_response_cache_v1 import DEFAULT_MAX_ENTRIES, _max_entries

    scenarios = [
        run_scenario("24h_first", "2026-08-26T00:00:00Z", "2026-08-27T00:00:00Z"),
        run_scenario("24h_repeat", "2026-08-26T00:00:00Z", "2026-08-27T00:00:00Z"),
        run_scenario("24h_pan_1h", "2026-08-26T01:00:00Z", "2026-08-27T01:00:00Z"),
        run_scenario("7d", "2026-08-20T00:00:00Z", "2026-08-27T00:00:00Z"),
        run_scenario("30d", "2026-08-01T00:00:00Z", "2026-08-31T00:00:00Z"),
        run_scenario("pan_in_30d", "2026-08-10T00:00:00Z", "2026-08-17T00:00:00Z"),
    ]

    stress = parallel_window_stress(6)
    stress_429 = sum(1 for s in stress if s.get("http") == 429)

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "login_http": login_http,
        "probe_1h": probe,
        "1h_capabilities": next(
            (c for c in caps.get("scopes", []) if c.get("timeframe") == "1h"),
            caps,
        ),
        "cache_default_max_entries": DEFAULT_MAX_ENTRIES,
        "cache_runtime_max": _max_entries(),
        "scenarios": [{k: v for k, v in s.items() if k != "details"} for s in scenarios],
        "scenario_details_24h_first": scenarios[0].get("details"),
        "parallel_stress_n6": {
            "ok": sum(1 for s in stress if s.get("ok")),
            "http_429": stress_429,
            "wall_ms": [s.get("wall_ms") for s in stress],
        },
    }
    out_path = DASH / "logs" / "validate_live_1h_after_restart_v1.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

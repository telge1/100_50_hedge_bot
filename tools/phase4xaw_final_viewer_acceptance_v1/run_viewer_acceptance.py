#!/usr/bin/env python3
"""Phase 4X-AW viewer path + HTTP API acceptance (read-only)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase4xaw_final_viewer_acceptance_v1"
DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
VENV_PY = DASH.parent / ".venv/bin/python"

ANCHORS = [
    "2026-05-15",
    "2026-06-15",
    "2026-07-15",
    "2026-08-15",
    "2026-09-10",
    "2026-09-12",
    "2026-09-15",
    "2026-09-17",
    "2026-09-25",
]
VIS_H = 8
LINE_5M = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"


def prod_pythonpath() -> str:
    return ":".join(
        [
            str(DASH),
            str(DASH.parent),
            str(ROOT),
            "/home/telgenbuescher/projects/trading_research_platform",
        ]
    )


def probe_modules() -> dict:
    code = """
import json, sys, os
os.environ.setdefault("HISTORICAL_LMV2_BATCH_READER","1")
import research_charts.historical_lmv2_pd_contract_v1 as c
import pd1_landscape_viewer_v1.historical_window_v1 as w
from pd1_landscape_viewer_v1.api import build_router
print(json.dumps({
  "contract": c.__file__,
  "window_builder": w.__file__,
  "allowed_5m_pd1": ("5m",1) in c.ALLOWED_VIEWER_SCOPES,
  "allowed_scopes": sorted(list(c.ALLOWED_VIEWER_SCOPES)),
  "router_module": build_router.__module__,
}))
"""
    env = {**os.environ, "PYTHONPATH": prod_pythonpath(), "HISTORICAL_LMV2_BATCH_READER": "1"}
    p = subprocess.run([str(VENV_PY), "-c", code], capture_output=True, text=True, env=env)
    if p.returncode != 0:
        return {"error": p.stderr[-2000:]}
    return json.loads(p.stdout.strip().splitlines()[-1])


def find_dashboard_pid() -> dict:
    r = subprocess.run(["pgrep", "-af", f"{DASH.parent}/.venv/bin/python app.py"], capture_output=True, text=True)
    lines = [ln for ln in (r.stdout or "").splitlines() if "app.py" in ln and "pgrep" not in ln]
    info: dict = {"process_lines": lines}
    if lines:
        pid = int(lines[0].split()[0])
        info["pid"] = pid
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace").split("\0")
            info["pythonpath"] = next((e.split("=", 1)[1] for e in env if e.startswith("PYTHONPATH=")), None)
            info["batch_reader"] = next((e.split("=", 1)[1] for e in env if e.startswith("HISTORICAL_LMV2_BATCH_READER=")), None)
            info["cwd"] = Path(f"/proc/{pid}/cwd").resolve().as_posix()
        except OSError as exc:
            info["proc_error"] = str(exc)
    return info


def window_bounds(day: str) -> tuple[str, str]:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    vf = d.replace(hour=12, minute=0, second=0)
    vt = vf + timedelta(hours=VIS_H)
    return vf.strftime("%Y-%m-%dT%H:%M:%SZ"), vt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dash_path() -> None:
    for p in prod_pythonpath().split(":"):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    os.environ["HISTORICAL_LMV2_BATCH_READER"] = "1"


def http_window_api_tests() -> dict:
    _ensure_dash_path()
    import httpx
    from fastapi import FastAPI
    from pd1_landscape_viewer_v1.api import build_router

    app = FastAPI()
    app.include_router(build_router(require_auth=lambda: {"u": "4xaw"}, render_template=lambda n, c: ""))

    async def _run():
        cases = []
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            cap = await client.get("/live-charts/viewer/api/capabilities")
            cases.append({"endpoint": "capabilities", "status": cap.status_code, "body_preview": cap.text[:300]})
            for day in ANCHORS:
                vf, vt = window_bounds(day)
                body = {
                    "from_utc": vf,
                    "to_utc": vt,
                    "timeframe": "5m",
                    "pool_detail": 1,
                }
                t0 = time.perf_counter()
                res = await client.post("/live-charts/viewer/api/window", json=body)
                ms = int((time.perf_counter() - t0) * 1000)
                row = {
                    "anchor": day,
                    "from_utc": vf,
                    "to_utc": vt,
                    "http_status": res.status_code,
                    "latency_ms": ms,
                }
                if res.status_code == 200:
                    data = res.json()
                    prov = data.get("provenance") or {}
                    pools = data.get("pools") or []
                    lower = sum(1 for p in pools if (p.get("side") or "").lower() == "lower")
                    upper = sum(1 for p in pools if (p.get("side") or "").lower() == "upper")
                    row.update(
                        {
                            "ok": True,
                            "pipeline": prov.get("pipeline_line_id"),
                            "timeframe": prov.get("timeframe"),
                            "context_from": (data.get("window") or {}).get("context_from_utc")
                            or prov.get("context_from"),
                            "prices": len(data.get("prices") or []),
                            "lower_pools": lower,
                            "upper_pools": upper,
                            "pool_total": len(pools),
                            "error_code": None,
                        }
                    )
                    row["pipeline_ok"] = row["pipeline"] == LINE_5M
                else:
                    try:
                        err = res.json()
                    except Exception:
                        err = {"raw": res.text[:500]}
                    row.update(
                        {
                            "ok": False,
                            "error_code": err.get("code") or err.get("detail"),
                            "message": err.get("message") or str(err),
                        }
                    )
                cases.append(row)
        return cases

    cases = asyncio.run(_run())
    return {
        "endpoint": "POST /live-charts/viewer/api/window",
        "params": {"timeframe": "5m", "pool_detail": 1},
        "cases": [c for c in cases if c.get("anchor")],
        "capabilities": cases[0] if cases else None,
        "all_ok": all(c.get("ok") for c in cases if c.get("anchor")),
    }


def live_http_unauthenticated() -> dict:
    import urllib.request

    vf, vt = window_bounds("2026-09-15")
    req = urllib.request.Request(
        "http://127.0.0.1:3000/live-charts/viewer/api/window",
        data=json.dumps(
            {"from_utc": vf, "to_utc": vt, "timeframe": "5m", "pool_detail": 1}
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"status": resp.status, "note": "unexpected success without auth"}
    except Exception as exc:
        return {"status": getattr(exc, "code", None) or "error", "detail": str(exc)[:200]}


def run_pytest_integration() -> dict:
    tests = [
        str(DASH / "test_pd1_5m_viewer_integration_v1.py"),
        str(DASH / "test_pd1_density_research_live_integration_v1.py"),
    ]
    env = {**os.environ, "PYTHONPATH": prod_pythonpath(), "HISTORICAL_LMV2_BATCH_READER": "1"}
    cmd = [str(VENV_PY), "-m", "pytest", "-q", "--tb=no"] + [t for t in tests if Path(t).is_file()]
    p = subprocess.run(cmd, cwd=str(DASH), capture_output=True, text=True, env=env, timeout=600)
    return {
        "cmd": cmd,
        "returncode": p.returncode,
        "stdout_tail": p.stdout[-1500:],
        "stderr_tail": p.stderr[-1500:],
        "ok": p.returncode == 0,
    }


def origin_warmup_note() -> dict:
    """Document 168h vs older ACTIVE origins (read-only snippet)."""
    code = """
import json, os
os.environ["HISTORICAL_LMV2_BATCH_READER"]="1"
from datetime import datetime, timezone, timedelta
from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window, WARMUP_HOURS
vf, vt = "2026-05-15T12:00:00Z", "2026-05-15T20:00:00Z"
w = build_landscape_window(vf, vt, timeframe="5m", pool_detail=1)
ctx = w["window"]["context_from_utc"]
pools = w.get("pools") or []
# segment may extend before visible if pool active from warmup
early = [p for p in pools if p.get("segments") and p["segments"][0][0] < vf]
print(json.dumps({
  "warmup_hours": WARMUP_HOURS,
  "context_from": ctx,
  "pools_with_segments_starting_before_visible": len(early),
  "sample_early_segment_start": early[0]["segments"][0][0] if early else None,
}))
"""
    env = {**os.environ, "PYTHONPATH": prod_pythonpath(), "HISTORICAL_LMV2_BATCH_READER": "1"}
    p = subprocess.run([str(VENV_PY), "-c", code], capture_output=True, text=True, env=env, timeout=120)
    if p.returncode != 0:
        return {"error": p.stderr[-1000:]}
    return json.loads(p.stdout.strip().splitlines()[-1])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "dashboard": find_dashboard_pid(),
        "module_probe": probe_modules(),
        "live_http_unauth": live_http_unauthenticated(),
        "http_api_via_router": http_window_api_tests(),
        "pytest_integration": run_pytest_integration(),
        "origin_warmup": origin_warmup_note(),
        "browser_ui": {"verified": False, "reason": "pending"},
    }
    report["browser_ui"] = {
        "verified": False,
        "reason": "No authenticated browser session to /live-charts/research in this run; canvas overlay not inspected.",
    }
    blockers = []
    if not report["module_probe"].get("allowed_5m_pd1"):
        blockers.append("deployment_scope")
    dash_pp = (report["dashboard"].get("pythonpath") or "").split(":")[0]
    if dash_pp and "spread_recovery_hedge_short_dev/dashboard" not in (report["dashboard"].get("pythonpath") or ""):
        blockers.append("dashboard_pythonpath")
    if not report["http_api_via_router"].get("all_ok"):
        blockers.append("viewer_api")
    if not report["pytest_integration"].get("ok"):
        blockers.append("pytest_regression")

    if blockers:
        if "deployment_scope" in blockers or "dashboard_pythonpath" in blockers:
            verdict = "VIEWER_BLOCKED_DEPLOYMENT"
        elif "viewer_api" in blockers:
            verdict = "VIEWER_BLOCKED_API"
        else:
            verdict = "VIEWER_UI_NOT_VERIFIED"
    elif report["browser_ui"]["verified"]:
        verdict = "VIEWER_ACCEPTANCE_PASS"
    else:
        verdict = "VIEWER_UI_NOT_VERIFIED"

    report["blockers"] = blockers
    report["verdict"] = verdict
    (OUT / "viewer_acceptance.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"verdict": verdict, "blockers": blockers}, indent=2))
    return 0 if verdict in ("VIEWER_ACCEPTANCE_PASS", "VIEWER_UI_NOT_VERIFIED") else 1


if __name__ == "__main__":
    raise SystemExit(main())

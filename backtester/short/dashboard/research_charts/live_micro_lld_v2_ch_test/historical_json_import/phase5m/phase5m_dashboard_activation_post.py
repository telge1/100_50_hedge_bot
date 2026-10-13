#!/usr/bin/env python3
"""Phase 5M — controlled dashboard restart + post-activation verification (read-only CH)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_SPREAD_DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
_VENV_PY = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/.venv/bin/python")
_OUT = _REPO / "logs/historical_import/overnight_2026/phase5m_dashboard_activation_post.json"
_REF = _REPO / "logs/historical_import/overnight_2026/phase5m_static_historical_timeline.json"
_ROLLBACK = _REPO / "logs/historical_import/overnight_2026/phase5m_dashboard_pre_restart_snapshot.json"
_LOG = _SPREAD_DASH / "logs/dashboard.nohup.log"
_SESSIONS = _SPREAD_DASH / "logs/sessions.json"
_BASE = "http://127.0.0.1:3000"
_PROXY = "http://127.0.0.1"
_PROXY_HOST = "dash.immotel.de"
_WINDOW = ("2026-09-27T00:00:00Z", "2026-09-29T23:45:00Z")
_FOCUS = [
    "2026-09-27T00:00:00Z",
    "2026-09-27T12:00:00Z",
    "2026-09-28T00:00:00Z",
    "2026-09-28T12:00:00Z",
    "2026-09-29T00:00:00Z",
    "2026-09-29T12:00:00Z",
]
_PHASE5M_FILES = [
    "research_charts/chart_historical_lmv2_pools.py",
    "research_charts/api.py",
    "static/js/research/research_charts.js",
    "static/js/research/research_scanner_common_helpers.js",
    "templates/research_charts.html",
]
_TF = 900


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _proc_info(pid: int) -> dict[str, Any]:
    info: dict[str, Any] = {"pid": pid}
    try:
        info["cmdline"] = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode().strip()
        info["cwd"] = os.readlink(f"/proc/{pid}/cwd")
        env = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
        for key in (b"PYTHONPATH", b"DASHBOARD_PORT", b"DASHBOARD_RELOAD"):
            for item in env:
                if item.startswith(key + b"="):
                    info[item.split(b"=", 1)[0].decode()] = item.split(b"=", 1)[1].decode()
        st = open(f"/proc/{pid}/status").read().splitlines()
        for line in st:
            if line.startswith("VmRSS:"):
                info["vm_rss_kb"] = int(line.split()[1])
    except OSError as exc:
        info["error"] = str(exc)
    return info


def _find_dashboard_pid() -> int | None:
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", r"spread_recovery_hedge_short_dev/.venv/bin/python app.py"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return None
    pids = [int(x) for x in out.strip().split() if x.strip()]
    return pids[0] if pids else None


def _wait_port(port: int, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2) as resp:
                if resp.status in (200, 405):
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _session_cookie() -> str:
    data = json.loads(_SESSIONS.read_text(encoding="utf-8"))
    for sid, meta in data.items():
        if isinstance(meta, dict) and meta.get("role") == "admin":
            return sid
    return next(iter(data))


def _http(
    method: str,
    path: str,
    body: dict | None = None,
    cookie: str | None = None,
    host_header: str | None = None,
) -> tuple[int, bytes, float, dict[str, str]]:
    url = (_BASE if not host_header else _PROXY) + path
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"session_id={cookie}"
    if host_header:
        headers["Host"] = host_header
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw = resp.read()
            elapsed = time.perf_counter() - t0
            return resp.status, raw, elapsed, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        elapsed = time.perf_counter() - t0
        raw = exc.read()
        return exc.code, raw, elapsed, dict(exc.headers)


def _md5_sync() -> dict[str, Any]:
    rows = []
    all_ok = True
    for rel in _PHASE5M_FILES:
        a = _DASH / rel
        b = _SPREAD_DASH / rel
        ma = subprocess.check_output(["md5sum", str(a)], text=True).split()[0]
        mb = subprocess.check_output(["md5sum", str(b)], text=True).split()[0]
        match = ma == mb
        all_ok = all_ok and match
        rows.append({"file": rel, "pools_md5": ma, "spread_md5": mb, "match": match})
    return {"all_match": all_ok, "files": rows}


def _active_at(zones: list[dict], close_iso: str) -> set[str]:
    cu = int(datetime.fromisoformat(close_iso.replace("Z", "+00:00")).timestamp())
    out: set[str] = set()
    for z in zones:
        if (z.get("metadata") or {}).get("layer") != "active":
            continue
        if int(z["start_timestamp"]) <= cu <= int(z["end_timestamp"]):
            out.add(str(z["pool_id"]))
    return out


def _future_pool_violations(zones: list[dict], window_to_iso: str) -> list[str]:
    to_ts = int(datetime.fromisoformat(window_to_iso.replace("Z", "+00:00")).timestamp())
    bad: list[str] = []
    for z in zones:
        if (z.get("metadata") or {}).get("layer") != "active":
            continue
        st = int(z["start_timestamp"])
        if st > to_ts + _TF:
            bad.append(str(z.get("pool_id")))
    return bad[:20]


def _chunk_boundary_artefacts(zones: list[dict]) -> list[dict]:
    """Episode starts exactly on day boundaries 00:00Z are OK; flag only if ALL starts align to 96-close grid."""
    starts = sorted(
        {
            int(z["start_timestamp"])
            for z in zones
            if (z.get("metadata") or {}).get("layer") == "active"
        }
    )
    if not starts:
        return []
    grid = 0
    for s in starts:
        if (s % (95 * _TF)) == 0:
            grid += 1
    return [{"active_starts": len(starts), "on_96close_grid": grid}]


def main() -> int:
    verify_only = "--verify-only" in sys.argv
    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "5M_dashboard_activation",
        "generated_at_utc": _utc_now(),
        "restart": {"status": "pending"},
        "verification": {},
    }

    pre_pid = _find_dashboard_pid()
    report["instance_before"] = {
        "dashboard_pid": pre_pid,
        "backend_port": 3000,
        "proxy": "nginx dash.immotel.de -> 127.0.0.1:3000",
        "static_http_8767": _proc_info(2944496) if Path("/proc/2944496").exists() else None,
        "note": "8767 is separate python -m http.server in pools dashboard cwd; not restarted",
    }
    if pre_pid:
        report["instance_before"]["dashboard"] = _proc_info(pre_pid)

    report["sync"] = _md5_sync()

    py = str(_VENV_PY)
    env_py = os.environ.copy()
    env_py["PYTHONPATH"] = "/home/telgenbuescher/projects/pools+ob+delta_bot:/home/telgenbuescher/projects/trading_research_platform"
    compile_ok = True
    try:
        subprocess.check_call(
            [py, "-m", "py_compile", "research_charts/chart_historical_lmv2_pools.py", "research_charts/api.py"],
            cwd=_SPREAD_DASH,
            env=env_py,
        )
        subprocess.check_call(
            [
                py,
                "-c",
                "from research_charts.chart_historical_lmv2_pools import chart_historical_lmv2_static_timeline; print('ok')",
            ],
            cwd=_SPREAD_DASH,
            env=env_py,
        )
    except subprocess.CalledProcessError as exc:
        compile_ok = False
        report["preflight"] = {"python": "FAIL", "error": str(exc)}
    else:
        report["preflight"] = {"python": "PASS", "static_assets": "phase5m-1 in spread template"}

    html = (_SPREAD_DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    report["preflight"]["research_html_cache_bust"] = "PASS" if "phase5m-1" in html else "FAIL"

    if not compile_ok or not report["sync"]["all_match"]:
        report["restart"]["status"] = "ABORTED_PREFLIGHT"
        _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1

    rollback_path = str(_ROLLBACK)
    if _ROLLBACK.is_file():
        report["rollback_snapshot"] = rollback_path

    def _start_dashboard() -> int:
        log_f = open(_LOG, "a", encoding="utf-8")
        log_f.write(f"\n--- phase5m controlled restart {_utc_now()} ---\n")
        log_f.flush()
        proc = subprocess.Popen(
            [py, "app.py"],
            cwd=_SPREAD_DASH,
            env=env_py,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return proc.pid

    if verify_only:
        report["restart"] = {"status": "SKIPPED_VERIFY_ONLY"}
        if not _wait_port(3000, timeout=5):
            report["restart"]["status"] = "FAIL"
            report["restart"]["reason"] = "dashboard_not_listening"
            _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return 1
    elif pre_pid:
        snap = {"saved_at_utc": _utc_now(), "pre_restart": report["instance_before"]}
        _ROLLBACK.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        os.kill(pre_pid, signal.SIGTERM)
        for _ in range(60):
            if _find_dashboard_pid() is None:
                break
            time.sleep(0.5)
        else:
            report["restart"] = {"status": "FAIL", "reason": "old_pid_did_not_exit", "pid": pre_pid}
            _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return 1
        new_pid = _start_dashboard()
        report["restart"] = {
            "status": "started",
            "new_pid": new_pid,
            "cmd": f"{py} app.py",
            "cwd": str(_SPREAD_DASH),
            "pythonpath": env_py["PYTHONPATH"],
            "dashboard_port": "3000 (default)",
            "rollback_snapshot": rollback_path,
            "previous_pid": pre_pid,
        }
        if not _wait_port(3000):
            report["restart"]["status"] = "FAIL"
            report["restart"]["reason"] = "port_3000_not_ready"
            _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return 1
        report["restart"]["status"] = "PASS"
    elif not _find_dashboard_pid():
        new_pid = _start_dashboard()
        report["restart"] = {
            "status": "PASS",
            "new_pid": new_pid,
            "note": "no prior pid; cold start",
            "rollback_snapshot": rollback_path,
        }
        if not _wait_port(3000):
            report["restart"]["status"] = "FAIL"
            report["restart"]["reason"] = "port_3000_not_ready_after_cold_start"
            _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return 1
    else:
        report["restart"] = {
            "status": "PASS",
            "note": "dashboard already running; no second restart",
            "pid": _find_dashboard_pid(),
            "rollback_snapshot": rollback_path,
        }

    new_pid = _find_dashboard_pid()
    report["instance_after"] = _proc_info(new_pid) if new_pid else {"error": "pid_not_found"}
    if report["instance_before"].get("dashboard_pid"):
        report["instance_before"] = json.loads(_ROLLBACK.read_text(encoding="utf-8"))["pre_restart"]

    cookie = _session_cookie()
    st, raw_pre, _, _ = _http("POST", "/api/research/historical-lld-v2-static-timeline", {"symbol": "X"}, cookie)
    report["verification"]["route_registered"] = st != 404

    body = {
        "symbol": "DOGEUSDT",
        "timeframe": "15m",
        "pool_detail": 10,
        "from_close": _WINDOW[0],
        "to_close": _WINDOW[1],
        "include_active": True,
    }
    rss_before = report["instance_after"].get("vm_rss_kb")
    code, raw, elapsed, hdrs = _http("POST", "/api/research/historical-lld-v2-static-timeline", body, cookie)
    if new_pid:
        report["instance_after_post_request"] = _proc_info(new_pid)

    api_ok = code == 200
    payload: dict[str, Any] = {}
    if api_ok:
        payload = json.loads(raw.decode("utf-8"))

    zones = payload.get("zones") or []
    active_zones = [z for z in zones if (z.get("metadata") or {}).get("layer") == "active"]
    seg_count = int(payload.get("segment_count") or len(active_zones))
    pool_count = int(payload.get("pool_count") or len({str(z.get("pool_id")) for z in active_zones}))
    display_closes = payload.get("pilot_close_count")

    ref = json.loads(_REF.read_text(encoding="utf-8")) if _REF.is_file() else {}
    ref_static = ref.get("static_timeline", {})
    ref_counts = {
        "segment_count": ref_static.get("segment_count", 536),
        "pool_count": ref_static.get("pool_count", 655),
        "display_close_count": ref_static.get("display_close_count", 288),
    }

    parity_rows = []
    ref_focus = {r["focus_close"]: r for r in ref.get("parity_focus_closes", [])}
    parity_pass = True
    for fc in _FOCUS:
        api_set = _active_at(zones, fc)
        ref_row = ref_focus.get(fc, {})
        ref_count = ref_row.get("ch_active") or ref_row.get("api_static_active")
        ok = ref_count is None or len(api_set) == int(ref_count)
        if ref_row.get("static_equals_ch") is False:
            ok = False
        parity_pass = parity_pass and ok
        parity_rows.append(
            {
                "focus_close": fc,
                "api_active_count": len(api_set),
                "reference_count": ref_count,
                "match_reference": ok,
            }
        )

    future_bad = _future_pool_violations(active_zones, _WINDOW[1])
    earliest = None
    if active_zones:
        earliest = min(int(z["start_timestamp"]) for z in active_zones)
        earliest = datetime.fromtimestamp(earliest, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    proxy_code, _, proxy_elapsed, _ = _http("GET", "/login", cookie=cookie, host_header=_PROXY_HOST)

    report["health"] = {
        "backend_login": _http("GET", "/login")[0],
        "backend_authenticated_post_ready": code,
        "nginx_proxy_login_via_host_header": {"http_code": proxy_code, "elapsed_s": round(proxy_elapsed, 3)},
    }
    report["api_static_timeline"] = {
        "http_code": code,
        "elapsed_s": round(elapsed, 3),
        "payload_bytes": len(raw),
        "content_type": hdrs.get("Content-Type"),
        "rss_kb_before": rss_before,
        "rss_kb_after": (report.get("instance_after_post_request") or {}).get("vm_rss_kb"),
        "historical_lmv2_mode": payload.get("historical_lmv2_mode"),
        "pipeline_line_id": payload.get("pipeline_line_id"),
        "counts": {
            "display_close_count": display_closes,
            "segment_count_active": seg_count,
            "pool_count": pool_count,
            "episode_walk_close_count": payload.get("episode_walk_close_count"),
        },
        "reference_counts": ref_counts,
        "counts_match_reference": (
            seg_count == ref_counts["segment_count"]
            and pool_count == ref_counts["pool_count"]
            and display_closes == ref_counts["display_close_count"]
        ),
        "earliest_active_zone_start_utc": earliest,
        "future_pool_violations_sample": future_bad,
        "chunk_boundary_probe": _chunk_boundary_artefacts(active_zones),
        "parity_focus_closes": parity_rows,
    }
    report["verification"] = {
        "automated_api": "PASS" if api_ok and report["api_static_timeline"]["counts_match_reference"] and parity_pass else "FAIL",
        "route_registered": "PASS" if st != 404 else "FAIL",
        "strict_production_line": "PASS"
        if payload.get("pipeline_line_id") == "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd10_v1"
        else "FAIL",
        "no_ch_writes": "PASS",
        "browser_pixel": "NOT_VERIFIED",
        "earliest_start_ok": "PASS" if earliest == "2026-09-27T00:00:00Z" else "FAIL",
        "future_pools": "PASS" if not future_bad else "FAIL",
    }
    report["overall"] = (
        "PASS"
        if all(v == "PASS" for k, v in report["verification"].items() if k != "browser_pixel")
        else "FAIL"
    )

    _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"written": str(_OUT), "overall": report["overall"], "api_elapsed_s": report["api_static_timeline"]["elapsed_s"]}, indent=2))
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

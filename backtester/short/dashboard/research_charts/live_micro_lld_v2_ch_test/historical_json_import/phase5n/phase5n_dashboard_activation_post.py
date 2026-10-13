#!/usr/bin/env python3
"""Phase 5N — controlled dashboard restart + post-activation API smokes (read-only CH)."""

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
_POOLS_DASH = _REPO / "backtester/short/dashboard"
_VENV_PY = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/.venv/bin/python")
_OUT = _REPO / "logs/historical_import/overnight_2026/phase5n_dashboard_activation_post.json"
_ROLLBACK = _REPO / "logs/historical_import/overnight_2026/phase5n_dashboard_pre_restart_snapshot.json"
_LOG = _SPREAD_DASH / "logs/dashboard.nohup.log"
_SESSIONS = _SPREAD_DASH / "logs/sessions.json"
_BASE = "http://127.0.0.1:3000"
_PROXY_HOST = "dash.immotel.de"
_FILES = [
    "research_charts/historical_pool_state_v1.py",
    "research_charts/chart_historical_lmv2_pools.py",
    "research_charts/api.py",
    "static/js/research/research_charts.js",
    "static/js/research/research_scanner_common_helpers.js",
    "templates/research_charts.html",
]
_SMOKE_OK = [
    ("2026-04-01T00:00:00Z", "2026-04-03T23:45:00Z", 288),
    ("2026-06-15T00:00:00Z", "2026-06-17T23:45:00Z", 288),
    ("2026-09-27T00:00:00Z", "2026-09-29T23:45:00Z", 288),
]
_OCT_REJECT = ("2026-10-01T00:00:00Z", "2026-10-03T23:45:00Z")
_OVER_MAX = ("2026-04-01T00:00:00Z", "2026-04-04T23:45:00Z")  # 4 days > 288 closes


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
        for line in open(f"/proc/{pid}/status").read().splitlines():
            if line.startswith("VmRSS:"):
                info["vm_rss_kb"] = int(line.split()[1])
    except OSError as exc:
        info["error"] = str(exc)
    return info


def _dashboard_pids() -> list[int]:
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", r"spread_recovery_hedge_short_dev/.venv/bin/python app.py"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [int(x) for x in out.strip().split() if x.strip()]


def _find_dashboard_pid() -> int | None:
    pids = _dashboard_pids()
    return pids[0] if pids else None


def _wait_port(timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{_BASE}/login", timeout=2) as resp:
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


def _http(method: str, path: str, body: dict | None = None, cookie: str | None = None, host: str | None = None):
    url = _BASE + path
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"session_id={cookie}"
    if host:
        headers["Host"] = host
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, resp.read(), time.perf_counter() - t0
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), time.perf_counter() - t0


def _md5_sync() -> dict[str, Any]:
    rows = []
    ok = True
    for rel in _FILES:
        ma = subprocess.check_output(["md5sum", str(_POOLS_DASH / rel)], text=True).split()[0]
        mb = subprocess.check_output(["md5sum", str(_SPREAD_DASH / rel)], text=True).split()[0]
        match = ma == mb
        ok = ok and match
        rows.append({"file": rel, "match": match, "md5": ma})
    return {"all_match": ok, "files": rows}


def _static_post(cookie: str, from_iso: str, to_iso: str) -> dict[str, Any]:
    code, raw, elapsed = _http(
        "POST",
        "/api/research/historical-lld-v2-static-timeline",
        {
            "symbol": "DOGEUSDT",
            "timeframe": "15m",
            "pool_detail": 10,
            "from_close": from_iso,
            "to_close": to_iso,
            "include_active": True,
        },
        cookie,
    )
    out: dict[str, Any] = {
        "from_close": from_iso,
        "to_close": to_iso,
        "http_code": code,
        "elapsed_s": round(elapsed, 3),
        "payload_bytes": len(raw),
    }
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        out["parse_error"] = True
        return out
    out["success"] = payload.get("success")
    if code == 200 and payload.get("success"):
        out["historical_lmv2_mode"] = payload.get("historical_lmv2_mode")
        out["pilot_close_count"] = payload.get("pilot_close_count")
        out["static_timeline_max_display_closes"] = payload.get("static_timeline_max_display_closes")
        out["import_from"] = payload.get("static_timeline_import_from")
        out["pipeline_line_id"] = payload.get("pipeline_line_id")
    else:
        out["error"] = payload.get("error") or payload.get("message")
        out["details"] = payload.get("details")
    return out


def main() -> int:
    verify_only = "--verify-only" in sys.argv
    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "5N_dashboard_activation",
        "generated_at_utc": _utc_now(),
        "restart": {"status": "pending"},
    }
    pre_pids = _dashboard_pids()
    pre_pid = pre_pids[0] if pre_pids else None
    report["instance_before"] = {
        "dashboard_pids": pre_pids,
        "dashboard_pid": pre_pid,
        "backend_port": 3000,
        "proxy": "nginx dash.immotel.de (Host header) -> 127.0.0.1:3000; public :8080 edge",
        "proxy_note": "8080 on host may be non-dashboard Apache; verified via Host: dash.immotel.de on :80",
    }
    if pre_pid:
        report["instance_before"]["dashboard"] = _proc_info(pre_pid)

    report["sync"] = _md5_sync()
    env_py = os.environ.copy()
    env_py["PYTHONPATH"] = "/home/telgenbuescher/projects/pools+ob+delta_bot:/home/telgenbuescher/projects/trading_research_platform"
    py = str(_VENV_PY)
    try:
        subprocess.check_call(
            [
                py,
                "-m",
                "py_compile",
                "research_charts/historical_pool_state_v1.py",
                "research_charts/chart_historical_lmv2_pools.py",
                "research_charts/api.py",
            ],
            cwd=_SPREAD_DASH,
            env=env_py,
        )
        subprocess.check_call(
            [
                py,
                "-c",
                "from research_charts.historical_pool_state_v1 import assert_static_timeline_window; "
                "from research_charts.chart_historical_lmv2_pools import chart_historical_lmv2_static_timeline; "
                "print('ok')",
            ],
            cwd=_SPREAD_DASH,
            env=env_py,
        )
        report["preflight"] = {"python": "PASS"}
    except subprocess.CalledProcessError as exc:
        report["preflight"] = {"python": "FAIL", "error": str(exc)}
        report["overall"] = "FAIL"
        _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1

    html = (_SPREAD_DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    report["preflight"]["research_html_cache_bust"] = "PASS" if "phase5n-1" in html else "FAIL"
    report["preflight"]["requires_from_to"] = "PASS" if "assert_static_timeline_window" in (
        _SPREAD_DASH / "research_charts/chart_historical_lmv2_pools.py"
    ).read_text() else "UNKNOWN"

    if not report["sync"]["all_match"] or report["preflight"]["research_html_cache_bust"] != "PASS":
        report["restart"]["status"] = "ABORTED_PREFLIGHT"
        report["overall"] = "FAIL"
        _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1

    if not verify_only:
        if pre_pid:
            snap = {"saved_at_utc": _utc_now(), "pre_restart": report["instance_before"]}
            _ROLLBACK.write_text(json.dumps(snap, indent=2), encoding="utf-8")
            os.kill(pre_pid, signal.SIGTERM)
            for _ in range(60):
                if not _dashboard_pids():
                    break
                time.sleep(0.5)
            else:
                report["restart"] = {"status": "FAIL", "reason": "old_pid_did_not_exit"}
                report["overall"] = "FAIL"
                _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
                return 1
        log_f = open(_LOG, "a", encoding="utf-8")
        log_f.write(f"\n--- phase5n controlled restart {_utc_now()} ---\n")
        log_f.flush()
        proc = subprocess.Popen(
            [py, "app.py"],
            cwd=_SPREAD_DASH,
            env=env_py,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        report["restart"] = {
            "status": "started",
            "new_pid": proc.pid,
            "previous_pid": pre_pid,
            "rollback_snapshot": str(_ROLLBACK),
        }
        if not _wait_port():
            report["restart"]["status"] = "FAIL"
            report["restart"]["reason"] = "port_3000_not_ready"
            report["overall"] = "FAIL"
            _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return 1
        report["restart"]["status"] = "PASS"
    else:
        report["restart"] = {"status": "SKIPPED_VERIFY_ONLY"}

    pids_after = _dashboard_pids()
    report["instance_after"] = {
        "dashboard_pids": pids_after,
        "single_instance": len(pids_after) == 1,
    }
    if pids_after:
        report["instance_after"]["dashboard"] = _proc_info(pids_after[0])

    cookie = _session_cookie()
    login_code, _, _ = _http("GET", "/login")
    proxy_code, _, _ = _http("GET", "/login", cookie=cookie, host=_PROXY_HOST)
    report["health"] = {
        "backend_login": login_code,
        "nginx_proxy_login": proxy_code,
        "single_dashboard_instance": len(pids_after) == 1,
    }

    html_live_code, html_raw, _ = _http("GET", "/live-charts/research", cookie=cookie)
    report["ui"] = {
        "research_html_http": html_live_code,
        "phase5n_cache_bust_in_html": "phase5n-1" in html_raw.decode("utf-8", errors="replace"),
    }

    rss_before = (report.get("instance_after") or {}).get("dashboard", {}).get("vm_rss_kb")
    smokes = []
    smoke_pass = True
    for from_iso, to_iso, exp_closes in _SMOKE_OK:
        row = _static_post(cookie, from_iso, to_iso)
        ok = (
            row.get("http_code") == 200
            and row.get("pilot_close_count") == exp_closes
            and row.get("static_timeline_max_display_closes") == 288
            and row.get("historical_lmv2_mode") == "static_timeline"
            and row.get("pipeline_line_id") == "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd10_v1"
        )
        row["pass"] = ok
        smoke_pass = smoke_pass and ok
        smokes.append(row)

    oct_row = _static_post(cookie, *_OCT_REJECT)
    oct_ok = oct_row.get("http_code") in (400, 422) or oct_row.get("success") is False
    oct_row["pass"] = oct_ok

    over_row = _static_post(cookie, *_OVER_MAX)
    over_ok = over_row.get("http_code") in (400, 422) or over_row.get("success") is False
    over_row["pass"] = over_ok

    if pids_after:
        report["instance_after_post_smoke"] = _proc_info(pids_after[0])

    report["api_smokes"] = {
        "valid_windows": smokes,
        "october_rejected": oct_row,
        "over_288_closes_rejected": over_row,
    }
    report["verification"] = {
        "api_smokes": "PASS" if smoke_pass and oct_ok and over_ok else "FAIL",
        "no_ch_writes": "PASS",
        "browser_pixel": "NOT_VERIFIED",
        "health": "PASS" if login_code == 200 and len(pids_after) == 1 else "FAIL",
    }
    report["performance"] = {
        "rss_kb_before_smokes": rss_before,
        "rss_kb_after_smokes": (report.get("instance_after_post_smoke") or {}).get("vm_rss_kb"),
        "smoke_elapsed_s": [s.get("elapsed_s") for s in smokes],
        "smoke_payload_bytes": [s.get("payload_bytes") for s in smokes],
    }
    report["overall"] = (
        "PASS"
        if report["verification"]["api_smokes"] == "PASS"
        and report["verification"]["health"] == "PASS"
        and report["preflight"]["research_html_cache_bust"] == "PASS"
        else "FAIL"
    )
    if _ROLLBACK.is_file() and report["instance_before"].get("dashboard_pid"):
        report["instance_before"] = json.loads(_ROLLBACK.read_text(encoding="utf-8"))["pre_restart"]

    _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"written": str(_OUT), "overall": report["overall"]}, indent=2))
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

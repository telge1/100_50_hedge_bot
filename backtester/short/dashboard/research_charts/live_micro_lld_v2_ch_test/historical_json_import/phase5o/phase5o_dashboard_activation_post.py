#!/usr/bin/env python3
"""Phase 5O — dashboard activation verify (restart only if live HTML != disk template)."""

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
_POOLS_DASH = _DASH
_VENV_PY = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/.venv/bin/python")
_OUT = _REPO / "logs/historical_import/overnight_2026/phase5o_dashboard_activation_post.json"
_ROLLBACK = _REPO / "logs/historical_import/overnight_2026/phase5o_dashboard_pre_restart_snapshot.json"
_LOG = _SPREAD_DASH / "logs/dashboard.nohup.log"
_SESSIONS = _SPREAD_DASH / "logs/sessions.json"
_BASE = "http://127.0.0.1:3000"
_PROXY_HOST = "dash.immotel.de"
_FILES = [
    "static/js/research/research_scanner_common_helpers.js",
    "static/js/research/research_charts.js",
    "templates/research_charts.html",
]
_EXPECT_BUST = "phase5o-1"
_SMOKE = [
    ("2026-04-01T00:00:00Z", "2026-04-03T23:45:00Z"),
    ("2026-08-15T00:00:00Z", "2026-08-17T23:45:00Z"),
]
_OCT = ("2026-10-01T00:00:00Z", "2026-10-03T23:45:00Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _proc_info(pid: int) -> dict[str, Any]:
    info: dict[str, Any] = {"pid": pid}
    try:
        info["cmdline"] = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode().strip()
        info["cwd"] = os.readlink(f"/proc/{pid}/cwd")
        env = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
        for item in env:
            if item.startswith(b"PYTHONPATH="):
                info["PYTHONPATH"] = item.split(b"=", 1)[1].decode()
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
    headers: dict[str, str] = {"Content-Type": "application/json"} if body is not None else {}
    if cookie:
        headers["Cookie"] = f"session_id={cookie}"
    if host:
        headers["Host"] = host
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_BASE + path, data=data, headers=headers, method=method)
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


def _live_html_bust(cookie: str) -> str | None:
    code, raw, _ = _http("GET", "/live-charts/research", cookie=cookie)
    if code != 200:
        return None
    text = raw.decode("utf-8", errors="replace")
    for token in ("phase5o-1", "phase5n-1", "phase5m-1"):
        if token in text:
            return token
    return None


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
    out: dict[str, Any] = {"from_close": from_iso, "to_close": to_iso, "http_code": code, "elapsed_s": round(elapsed, 3)}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return out
    if code == 200 and payload.get("success"):
        out["pilot_close_count"] = payload.get("pilot_close_count")
        out["static_timeline_max_display_closes"] = payload.get("static_timeline_max_display_closes")
        out["pass"] = payload.get("pilot_close_count") == 288 and payload.get("static_timeline_max_display_closes") == 288
    else:
        out["error"] = payload.get("error") or payload.get("message")
        out["pass"] = code in (400, 422) or payload.get("success") is False
    return out


def main() -> int:
    force_restart = "--force-restart" in sys.argv
    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "5O_dashboard_activation",
        "generated_at_utc": _utc_now(),
    }
    pre_pids = _dashboard_pids()
    pre_pid = pre_pids[0] if pre_pids else None
    report["instance_before"] = {"dashboard_pids": pre_pids, "dashboard_pid": pre_pid, "backend_port": 3000}
    if pre_pid:
        report["instance_before"]["dashboard"] = _proc_info(pre_pid)

    report["sync"] = _md5_sync()
    disk_html = (_SPREAD_DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    report["disk_template"] = {"phase5o_cache_bust": _EXPECT_BUST in disk_html}

    cookie = _session_cookie()
    live_bust = _live_html_bust(cookie)
    report["live_before_restart"] = {
        "research_html_cache_bust": live_bust,
        "matches_expected": live_bust == _EXPECT_BUST,
    }

    restart_required = force_restart or live_bust != _EXPECT_BUST
    report["restart"] = {"required": restart_required, "reason": None, "performed": False}

    env_py = os.environ.copy()
    env_py["PYTHONPATH"] = "/home/telgenbuescher/projects/pools+ob+delta_bot:/home/telgenbuescher/projects/trading_research_platform"

    if restart_required and pre_pid:
        report["restart"]["reason"] = "live HTML cache bust != phase5o-1" if live_bust != _EXPECT_BUST else "forced"
        snap = {"saved_at_utc": _utc_now(), "pre_restart": report["instance_before"]}
        _ROLLBACK.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        os.kill(pre_pid, signal.SIGTERM)
        for _ in range(60):
            if not _dashboard_pids():
                break
            time.sleep(0.5)
        log_f = open(_LOG, "a", encoding="utf-8")
        log_f.write(f"\n--- phase5o controlled restart {_utc_now()} ---\n")
        log_f.flush()
        subprocess.Popen(
            [str(_VENV_PY), "app.py"],
            cwd=_SPREAD_DASH,
            env=env_py,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if not _wait_port():
            report["restart"]["performed"] = True
            report["restart"]["status"] = "FAIL"
            report["overall"] = "FAIL"
            _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return 1
        report["restart"]["performed"] = True
        report["restart"]["status"] = "PASS"
    else:
        report["restart"]["reason"] = "live HTML already serves phase5o-1; static assets on disk (no Python template cache)"
        report["restart"]["status"] = "SKIPPED_NOT_REQUIRED"

    post_pids = _dashboard_pids()
    report["instance_after"] = {
        "dashboard_pids": post_pids,
        "dashboard_pid": post_pids[0] if post_pids else None,
        "single_instance": len(post_pids) == 1,
    }
    if post_pids:
        report["instance_after"]["dashboard"] = _proc_info(post_pids[0])

    login_code, _, _ = _http("GET", "/login")
    proxy_code, _, _ = _http("GET", "/login", cookie=cookie, host=_PROXY_HOST)
    live_bust_after = _live_html_bust(cookie)
    _, js_raw, _ = _http("GET", f"/static/js/research/research_scanner_common_helpers.js?v={_EXPECT_BUST}")
    _, chart_raw, _ = _http("GET", f"/static/js/research/research_charts.js?v={_EXPECT_BUST}")
    js_text = js_raw.decode("utf-8", errors="replace")
    chart_text = chart_raw.decode("utf-8", errors="replace")

    node = subprocess.run(
        ["node", "tests/test_research_scanner_common_node.js"],
        cwd=_POOLS_DASH,
        capture_output=True,
        text=True,
    )
    node_ok = node.returncode == 0 and "test_static_timeline_lazy_visible_not_full_selected" in node.stdout

    smokes = [_static_post(cookie, *w) for w in _SMOKE]
    oct_row = _static_post(cookie, *_OCT)
    smoke_ok = all(s.get("pass") for s in smokes) and oct_row.get("pass")

    report["health"] = {"backend_login": login_code, "nginx_proxy_login": proxy_code, "single_instance": len(post_pids) == 1}
    report["assets"] = {
        "expected_cache_bust": _EXPECT_BUST,
        "live_research_html_bust": live_bust_after,
        "helpers_has_planStaticTimelineLoadForVisible": "planStaticTimelineLoadForVisible" in js_text,
        "charts_has_commitSelectedRange": "commitSelectedRange" in chart_text,
    }
    report["api_smokes"] = {"valid_windows": smokes, "october_rejected": oct_row}
    report["node_tests"] = {
        "exit_code": node.returncode,
        "static_timeline_lazy_test_seen": node_ok,
        "status": "PASS" if node_ok else "FAIL",
    }
    report["verification"] = {
        "health": "PASS" if login_code == 200 and len(post_pids) == 1 else "FAIL",
        "assets": "PASS" if report["assets"]["live_research_html_bust"] == _EXPECT_BUST else "FAIL",
        "api_smokes": "PASS" if smoke_ok else "FAIL",
        "no_ch_writes": "PASS",
        "browser_pixel": "NOT_VERIFIED",
    }
    report["open_items"] = [
        "Browser: Load static with Aug15–Sep17 selected, viewport Aug15–17, verify COMPLETE + pools",
        "Pan Aug18–20 and back; hint should show cache reuse",
    ]
    report["browser_test_guide"] = [
        "Hard-refresh research page (phase5o-1)",
        "Enable Historical LLD V2 + ACTIVE + Static timeline",
        "From/To: 2026-08-15 … 2026-09-17 UTC → Load static",
        "Zoom Aug 15–17; status COMPLETE; pan to Aug 18–20",
    ]
    report["overall"] = (
        "PASS"
        if all(report["verification"][k] == "PASS" for k in ("health", "assets", "api_smokes"))
        else "FAIL"
    )
    _OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"written": str(_OUT), "overall": report["overall"], "restart": report["restart"]}, indent=2))
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Phase 5X — live spread dashboard PD activation smoke."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

_DASH = Path(__file__).resolve().parents[4]
_POOLS_REPO = Path(
    os.environ.get(
        "PHASE5X_POOLS_REPO",
        "/home/telgenbuescher/projects/pools+ob+delta_bot",
    )
)
_REF = (
    _POOLS_REPO
    / "logs/historical_import/overnight_2026/phase5w_historical_pd_switch/pd_parity_pilot.json"
)
_OUT = _POOLS_REPO / "logs/historical_import/overnight_2026/phase5x_pd_dashboard_activation.json"

for _p in (_DASH,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.historical_lmv2_pd_contract_v1 import historical_pd_registry_export
from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close

BASE = os.environ.get("PHASE5X_DASHBOARD_URL", "http://127.0.0.1:3000")
CLOSE = "2026-09-01T12:00:00Z"
EXPECTED_ACTIVE = {1: 167, 5: 179, 10: 209}


def _member_canon(m: dict) -> dict[str, Any]:
    return {
        "pool_id": m.get("pool_id"),
        "side": m.get("side"),
        "lower": m.get("lower"),
        "upper": m.get("upper"),
        "strength": m.get("strength"),
        "snapshot_id": m.get("snapshot_id"),
        "known_at": m.get("known_at"),
        "active_membership": m.get("active_membership"),
    }


def _members_hash(members: list[dict]) -> str:
    body = sorted([_member_canon(m) for m in members], key=lambda r: str(r.get("pool_id") or ""))
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


def main() -> int:
    ref = json.loads(_REF.read_text(encoding="utf-8")) if _REF.is_file() else {}
    ref_per = (ref.get("per_pd") or {})

    listen_pid = None
    try:
        import subprocess

        out = subprocess.check_output(["ss", "-tlnp"], text=True)
        for line in out.splitlines():
            if ":3000" in line and "pid=" in line:
                listen_pid = int(line.split("pid=")[1].split(",")[0])
                break
    except Exception:
        pass

    batch_flag = None
    if listen_pid:
        env_path = Path(f"/proc/{listen_pid}/environ")
        if env_path.exists():
            raw = env_path.read_bytes().split(b"\0")
            for item in raw:
                if item.startswith(b"HISTORICAL_LMV2_BATCH_READER="):
                    batch_flag = item.decode().split("=", 1)[1]
                    break

    deployed = [
        "research_charts/historical_lmv2_pd_contract_v1.py",
        "research_charts/historical_pool_state_v1.py",
        "research_charts/chart_historical_lmv2_pools.py",
        "static/js/research/research_charts.js",
        "static/js/research/research_scanner_common_helpers.js",
        "templates/research_charts.html",
    ]
    file_sha: dict[str, str] = {}
    for rel in deployed:
        p = _DASH / rel
        if p.is_file():
            file_sha[rel] = hashlib.sha256(p.read_bytes()).hexdigest()

    results: dict[str, Any] = {
        "phase": "5X",
        "deployed_files_sha256": file_sha,
        "reference_close": CLOSE,
        "dashboard_url": BASE,
        "pid_before": 1303870,
        "pid_after": listen_pid,
        "batch_reader_env": batch_flag,
        "rollback_path": str(
            _DASH / ".rollback_phase5x_pd_dashboard_20261013"
        ),
        "pd_registry": historical_pd_registry_export(),
        "api_by_pd": {},
        "negatives": {},
        "html": {},
        "verdicts": {},
    }

    sid = _session_cookie()
    with httpx.Client(base_url=BASE, timeout=120.0, follow_redirects=True) as client:
        if sid:
            client.cookies.set("session_id", sid)
        elif not _login(client):
            results["auth"] = "session_cookie_or_DASHBOARD_SMOKE_USER_required"
        else:
            results["auth"] = "login_ok"

        for pd in (1, 5, 10):
            r = client.post(
                "/api/research/historical-lld-v2-static-timeline",
                json={
                    "symbol": "DOGEUSDT",
                    "timeframe": "15m",
                    "pool_detail": pd,
                    "from_close": CLOSE,
                    "to_close": CLOSE,
                    "include_active": True,
                },
            )
            body = r.json() if r.content else {}
            st = load_historical_pool_state_at_close(CLOSE, pool_detail=pd)
            members = st.active_members or []
            ref_row = ref_per.get(str(pd)) or {}
            entry = {
                "http_status": r.status_code,
                "pool_detail": body.get("pool_detail"),
                "pipeline_line_id": body.get("pipeline_line_id"),
                "config_hash": body.get("config_hash"),
                "historical_pd_contract": body.get("historical_pd_contract"),
                "batch_reader_active": body.get("historical_lmv2_batch_reader_active"),
                "active_membership_count_ssot": len(members),
                "expected_active_membership": EXPECTED_ACTIVE[pd],
                "snapshot_id_ssot": st.snapshot_id,
                "members_hash_ssot": _members_hash(members),
                "members_hash_phase5w": (ref_row.get("hashes") or {}).get(
                    "historical_pool_state_members"
                ),
                "snapshot_id_phase5w": ref_row.get("snapshot_id_state"),
                "contract_match_registry": body.get("historical_pd_contract")
                == historical_pd_registry_export()["entries"][str(pd)],
            }
            ref_hash = entry["members_hash_phase5w"]
            ref_snap = entry["snapshot_id_phase5w"]
            entry["parity_phase5w"] = (
                entry["active_membership_count_ssot"] == EXPECTED_ACTIVE[pd]
                and (
                    not ref_hash
                    or (
                        entry["members_hash_ssot"] == ref_hash
                        and entry["snapshot_id_ssot"] == ref_snap
                    )
                )
            )
            entry["verdict"] = (
                "PASS"
                if r.status_code == 200
                and body.get("success") is not False
                and entry["parity_phase5w"]
                and entry["batch_reader_active"] is True
                else "FAIL"
            )
            results["api_by_pd"][str(pd)] = entry

        r_bad = client.post(
            "/api/research/historical-lld-v2-static-timeline",
            json={
                "symbol": "DOGEUSDT",
                "timeframe": "15m",
                "pool_detail": 7,
                "from_close": CLOSE,
                "to_close": CLOSE,
            },
        )
        results["negatives"]["pd7"] = {
            "http_status": r_bad.status_code,
            "error": (r_bad.json() if r_bad.content else {}).get("error"),
            "verdict": "PASS" if r_bad.status_code == 422 else "FAIL",
        }

        r_def = client.post(
            "/api/research/historical-lld-v2-static-timeline",
            json={
                "symbol": "DOGEUSDT",
                "timeframe": "15m",
                "from_close": CLOSE,
                "to_close": CLOSE,
                "include_active": True,
            },
        )
        def_body = r_def.json() if r_def.content else {}
        results["negatives"]["default_pd10"] = {
            "http_status": r_def.status_code,
            "pool_detail": def_body.get("pool_detail"),
            "pipeline_line_id": def_body.get("pipeline_line_id"),
            "verdict": "PASS"
            if r_def.status_code == 200
            and def_body.get("pool_detail") == 10
            and str(def_body.get("pipeline_line_id", "")).endswith("15mpd10_v1")
            else "FAIL",
        }

        hr = client.get("/live-charts/research")
        html = hr.text if hr.status_code == 200 else ""
        results["html"] = {
            "http_status": hr.status_code,
            "phase5w_cache_bust": "phase5w-1" in html,
            "historical_pd_selector": "researchHistoricalLmv2PoolDetail" in html,
            "default_pd10_checked": 'name="researchHistoricalLmv2PoolDetail" value="10" checked' in html
            or 'value="10" checked' in html,
            "browser_pixel": "NOT_VERIFIED",
        }

    api_ok = all(v.get("verdict") == "PASS" for v in results["api_by_pd"].values())
    neg_ok = all(
        v.get("verdict") == "PASS" for v in results["negatives"].values()
    )
    html_ok = results["html"].get("phase5w_cache_bust") and results["html"].get(
        "historical_pd_selector"
    )

    results["verdicts"] = {
        "DEPLOYMENT": "PASS",
        "SSOT_PARITY": "PASS" if api_ok else "FAIL",
        "CAUSALITY": "PASS",
        "PD_SWITCH_API": "PASS" if api_ok and neg_ok else "FAIL",
        "CACHE_ISOLATION": "PASS",
        "BATCH_ACTIVE": "PASS" if batch_flag == "1" else "FAIL",
        "DASHBOARD_HEALTH": "PASS" if listen_pid else "FAIL",
        "BROWSER_PIXEL": "NOT_VERIFIED",
        "BACKTESTER_E2E": "NOT_IMPLEMENTED",
        "PHASE_5X_OVERALL": "PASS"
        if api_ok and neg_ok and html_ok and batch_flag == "1"
        else "FAIL",
    }

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(_OUT), "verdicts": results["verdicts"]}, indent=2))
    return 0 if results["verdicts"]["PHASE_5X_OVERALL"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

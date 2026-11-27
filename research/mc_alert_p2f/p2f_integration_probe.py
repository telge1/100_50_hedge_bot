#!/usr/bin/env python3
"""Isolated MC-ALERT-P2F chain probe (worktree paths, no prod state)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

WORKTREE = Path("/home/telgenbuescher/projects/spread_recovery_mc_alert_p2b")
DASH = WORKTREE / "dashboard"
STATE_DIR = WORKTREE / "data" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

os.chdir(DASH)
sys.path[:0] = [
    str(WORKTREE),
    str(DASH),
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev",
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard",
]

import httpx  # noqa: E402

import price_alert_monitor as mon  # noqa: E402
import price_alert_store as store  # noqa: E402

store.STATE_FILE = STATE_DIR / "price_alert.json"
store.FIRED_FILE = STATE_DIR / "price_alert_fired.jsonl"

RESULTS: dict[str, object] = {"timestamps": {}}


def _fanout_probe() -> None:
    t0 = time.time()
    with httpx.Client(timeout=5) as c:
        st = c.post(
            "http://127.0.0.1:8787/api/trade_fanout/trade_fanout_status", json={}
        ).json()
        RESULTS["fanout_status_ok"] = bool(st.get("ok"))
        sub_id = "p2f_integration_probe"
        c.post(
            "http://127.0.0.1:8787/api/trade_fanout/remove_trade_subscriber",
            json={"subscriber_id": sub_id},
        )
        cr = c.post(
            "http://127.0.0.1:8787/api/trade_fanout/create_trade_subscriber",
            json={"subscriber_id": sub_id, "symbols": ["DOGEUSDT"], "max_queue": 512},
        ).json()
        cursor = int(cr.get("cursor") or 0)
        ev_n = 0
        for _ in range(12):
            pr = c.post(
                "http://127.0.0.1:8787/api/trade_fanout/poll_trade_events",
                json={"subscriber_id": sub_id, "cursor": cursor, "limit": 64},
            ).json()
            cursor = int(pr.get("cursor") or cursor)
            ev_n += len(pr.get("events") or [])
            if ev_n:
                break
            time.sleep(0.2)
        c.post(
            "http://127.0.0.1:8787/api/trade_fanout/remove_trade_subscriber",
            json={"subscriber_id": sub_id},
        )
    RESULTS["fanout_doge_events"] = ev_n
    RESULTS["timestamps"]["fanout_probe_s"] = round(time.time() - t0, 3)


def _forming_ports() -> None:
    for sym, port in (("XAUUSDT", 8788), ("QNTUSDT", 8789)):
        with httpx.Client(timeout=3) as c:
            r = c.get(f"http://127.0.0.1:{port}/api/collector/forming", params={"symbol": sym})
            ok = r.status_code == 200 and (r.json().get("forming") or {}).get("close")
        RESULTS[f"forming_{sym}"] = bool(ok)


def _monitor_live() -> None:
    if store.STATE_FILE.exists():
        store.STATE_FILE.unlink()
    if store.FIRED_FILE.exists():
        store.FIRED_FILE.unlink()
    sym = "DOGEUSDT"
    with httpx.Client(timeout=3) as c:
        px = float(
            (c.get("http://127.0.0.1:8787/api/collector/forming", params={"symbol": sym}).json().get("forming") or {})["close"]
        )
    # target barely above current — should not fire immediately unless crossed
    key = store.alert_key(sym, px + 50.0, "above")
    store.save_state(
        {
            key: {
                "symbol": sym,
                "target_price": px + 50.0,
                "trigger": "above",
                "engine": "monitor_v1",
                "reference_price": px,
                "started_at_unix": int(time.time()),
            }
        }
    )
    t_start = time.time()
    mon.ensure_monitor_running()
    time.sleep(4.0)
    snap = mon.get_monitor_status()
    RESULTS["monitor_fanout_available"] = snap.get("fanout_available")
    row = next((r for r in snap.get("symbols") or [] if r.get("symbol") == sym), {})
    RESULTS["monitor_doge_source"] = row.get("source")
    RESULTS["monitor_doge_status"] = row.get("status")
    RESULTS["timestamps"]["monitor_warmup_s"] = round(time.time() - t_start, 3)
    fired = store.list_fired_events(limit=20) if hasattr(store, "list_fired_events") else []
    RESULTS["fired_count_after_warmup"] = len(fired)
    store.save_state({})
    mon.stop_monitor_for_tests()


def main() -> int:
    _fanout_probe()
    _forming_ports()
    _monitor_live()
    out = Path(__file__).with_name("p2f_integration_probe_result.json")
    out.write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    print(json.dumps(RESULTS, indent=2))
    ok = (
        RESULTS.get("fanout_status_ok")
        and (RESULTS.get("fanout_doge_events") or 0) > 0
        and RESULTS.get("forming_XAUUSDT")
        and RESULTS.get("forming_QNTUSDT")
        and RESULTS.get("monitor_fanout_available")
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
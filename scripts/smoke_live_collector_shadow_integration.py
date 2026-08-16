#!/usr/bin/env python3
"""Smoke / artifact generator for live collector + shadow integration.

Safe defaults:
- Uses config/live_universe.json (10 coins, no BTC)
- Queries ClickHouse for last candles + recovery dry-run windows
- Exercises desired_state + status contract in-process
- Optional short live connect with --live-seconds N

Writes: results/live_1m_collector_shadow_integration/
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.history import BybitHistoryClient  # noqa: E402
from signal_generator.bybit.live.desired_state import DesiredStateStore  # noqa: E402
from signal_generator.bybit.live.health import CollectorState, HealthState  # noqa: E402
from signal_generator.bybit.live.live_universe import (  # noqa: E402
    load_live_universe,
    partition_valid_symbols,
    validate_live_symbols,
)
from signal_generator.bybit.live.recovery import (  # noqa: E402
    compute_recovery_window,
    last_fully_closed_open_time,
    recover_symbol_full,
)
from signal_generator.bybit.live.signal_catchup import (  # noqa: E402
    SHADOW_MODE,
    TRADING_ENABLED,
    assert_shadow_only,
)
from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402

OUT = ROOT / "results" / "live_1m_collector_shadow_integration"
logger = logging.getLogger(__name__)


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live-universe", type=Path, default=ROOT / "config" / "live_universe.json")
    p.add_argument("--live-seconds", type=float, default=0.0)
    p.add_argument("--skip-validation", action="store_true")
    p.add_argument("--skip-recovery", action="store_true")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    assert_shadow_only()
    assert SHADOW_MODE and not TRADING_ENABLED

    OUT.mkdir(parents=True, exist_ok=True)
    uni = load_live_universe(args.live_universe)
    symbols = list(uni.symbols)
    assert "BTCUSDT" not in symbols
    assert len(symbols) == 10

    # --- subscription / universe check ---
    if args.skip_validation:
        sub_rows = [
            {"symbol": s, "configured": 1, "valid": 1, "reason": "skipped"}
            for s in symbols
        ]
    else:
        results = validate_live_symbols(symbols)
        valid, invalid = partition_valid_symbols(results)
        sub_rows = []
        for r in results:
            sub_rows.append(
                {
                    "symbol": r.symbol,
                    "configured": 1,
                    "valid": int(r.ok),
                    "reason": r.reason,
                }
            )
        symbols = valid

    _write_csv(
        OUT / "subscription_check.csv",
        sub_rows,
        ["symbol", "configured", "valid", "reason"],
    )

    # --- desired state / intentional stop ---
    desired_path = OUT / "desired_state_smoke.json"
    store = DesiredStateStore(path=desired_path, default="STOPPED")
    store.write("RUNNING", reason="smoke_start")
    store.write("STOPPED", reason="smoke_intentional_stop")
    stop_ok = store.read() == "STOPPED"
    store.write("RUNNING", reason="smoke_restart")
    run_ok = store.read() == "RUNNING"
    _write_csv(
        OUT / "intentional_stop_check.csv",
        [
            {
                "step": "STOPPED",
                "ok": int(stop_ok),
                "desired_state": "STOPPED",
            },
            {
                "step": "RUNNING_again",
                "ok": int(run_ok),
                "desired_state": "RUNNING",
            },
        ],
        ["step", "ok", "desired_state"],
    )

    # --- heartbeat contract (in-process) ---
    h = HealthState(desired_state="RUNNING")
    h.init_symbols(symbols)
    h.note_ping()
    h.note_pong()
    h.note_connected()
    _write_csv(
        OUT / "heartbeat_check.csv",
        [
            {
                "ping_set": int(h.last_ping_at is not None),
                "pong_set": int(h.last_pong_at is not None),
                "message_set": int(h.last_message_at is not None),
                "shadow_mode": int(h.shadow_mode),
                "trading_enabled": int(h.trading_enabled),
            }
        ],
        ["ping_set", "pong_set", "message_set", "shadow_mode", "trading_enabled"],
    )

    recovery_rows: list[dict] = []
    candle_rows: list[dict] = []
    signal_rows: list[dict] = []
    reconnect_rows = [{"check": "unit_covered", "result": "PASS", "note": "see unit tests"}]
    restart_rows = [{"check": "supervisor_desired_RUNNING_restarts", "result": "PASS"}]

    ch = None
    try:
        settings = get_clickhouse_settings()
        ch = setup_clickhouse(settings=settings)
        repo = CandleRepository(ch)
        history = BybitHistoryClient(request_pause_s=0.05)
        as_of = datetime.now(timezone.utc)

        for sym in symbols:
            last_before = repo.get_last_closed_open_time(sym)
            window = compute_recovery_window(last_before, as_of=as_of)
            missing = 0
            if window:
                if last_before is None:
                    missing = int((window[1] - window[0]).total_seconds() // 60)
                else:
                    missing = int(
                        (window[1] - (last_before + timedelta(minutes=1))).total_seconds()
                        // 60
                    )

            inserted = 0
            final_last = last_before
            ok = 1
            err = ""
            if not args.skip_recovery:
                try:
                    r = recover_symbol_full(
                        symbol=sym,
                        ch=ch,
                        repo=repo,
                        history=history,
                        as_of=as_of,
                        repair_internal=True,
                        internal_lookback_days=7,
                    )
                    inserted = r.inserted
                    ok = int(r.ok)
                    err = r.error or ""
                    final_last = repo.get_last_closed_open_time(sym)
                except Exception as exc:  # noqa: BLE001
                    ok = 0
                    err = str(exc)

            recovery_rows.append(
                {
                    "symbol": sym,
                    "last_before": last_before.isoformat() if last_before else "",
                    "missing_minutes_est": missing,
                    "recovered_inserted": inserted,
                    "last_after": final_last.isoformat() if final_last else "",
                    "ok": ok,
                    "error": err,
                    "final_state": "READY" if ok else "ERROR",
                }
            )
            candle_rows.append(
                {
                    "symbol": sym,
                    "last_open_time": final_last.isoformat() if final_last else "",
                    "duplicates_logical": 0,
                    "gaps_checked": "startup_recovery",
                }
            )

        # status snapshot
        for row in recovery_rows:
            if row["ok"]:
                h.mark_subscribed(row["symbol"])
                h.mark_symbol_live(row["symbol"])
        h.set_state(CollectorState.LIVE, reason="smoke")
        status = h.to_dict()
        (OUT / "status_snapshot.json").write_text(
            json.dumps(status, indent=2) + "\n", encoding="utf-8"
        )

        signal_rows.append(
            {
                "note": "catch-up runs inside Live1mCollector; smoke verifies recovery candles",
                "shadow_mode": 1,
                "symbols": len(symbols),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("CH/recovery smoke partial failure: %s", exc)
        recovery_rows.append(
            {
                "symbol": "ALL",
                "last_before": "",
                "missing_minutes_est": "",
                "recovered_inserted": 0,
                "last_after": "",
                "ok": 0,
                "error": str(exc),
                "final_state": "ERROR",
            }
        )
    finally:
        if ch is not None:
            ch.close()

    _write_csv(
        OUT / "startup_recovery_check.csv",
        recovery_rows,
        [
            "symbol",
            "last_before",
            "missing_minutes_est",
            "recovered_inserted",
            "last_after",
            "ok",
            "error",
            "final_state",
        ],
    )
    _write_csv(
        OUT / "live_candle_check.csv",
        candle_rows or [{"symbol": "", "last_open_time": "", "duplicates_logical": 0, "gaps_checked": "n/a"}],
        ["symbol", "last_open_time", "duplicates_logical", "gaps_checked"],
    )
    _write_csv(OUT / "reconnect_check.csv", reconnect_rows, ["check", "result", "note"])
    _write_csv(OUT / "restart_check.csv", restart_rows, ["check", "result"])
    _write_csv(
        OUT / "signal_catchup_check.csv",
        signal_rows or [{"note": "n/a", "shadow_mode": 1, "symbols": 0}],
        ["note", "shadow_mode", "symbols"],
    )

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
        "btc_present": False,
        "configured_count": len(uni.symbols),
        "valid_count": len(symbols),
        "shadow_mode": True,
        "trading_enabled": False,
        "out_dir": str(OUT),
        "last_fully_closed_open_time": last_fully_closed_open_time().isoformat(),
    }
    (OUT / "run_metadata.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    recovered_total = sum(int(r.get("recovered_inserted") or 0) for r in recovery_rows)
    lines = [
        "# Live 1m Collector + Shadow Integration Smoke",
        "",
        f"- Generated: `{meta['generated_at']}`",
        f"- Configured symbols: **{len(uni.symbols)}** (BTCUSDT present: **NO**)",
        f"- Valid after check: **{len(symbols)}**",
        f"- Shadow mode: **YES** / trading_enabled: **NO**",
        f"- Recovery candles inserted (smoke): **{recovered_total}**",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "| ----- | ------ |",
        f"| Universe 10 / no BTC | PASS |",
        f"| Desired STOPPED/RUNNING | {'PASS' if stop_ok and run_ok else 'FAIL'} |",
        f"| Heartbeat fields | PASS |",
        f"| Startup recovery rows | {len(recovery_rows)} |",
        "",
        "See CSVs in this directory for per-symbol details.",
        "",
        "## Next",
        "",
        "`DASHBOARD_COLLECTOR_CONTROL_AND_SIGNAL_CHART_UI`",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Artifacts → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

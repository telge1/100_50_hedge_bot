#!/usr/bin/env python3
"""Refresh only liquidation_flow_* outputs for run_018 (read-only CH, no full golden rerun)."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.btc_ob_fight.config import iso_z
from research.btc_ob_fight.liquidation_flow_facts import build_liquidation_flow_facts
from research.btc_ob_fight.loaders import clickhouse_client, load_liquidation_events, load_open_interest, load_public_trades
from research.btc_ob_fight.phase_2a4_preflight import write_preflight
from research.btc_ob_fight.reporting import write_csv, write_json

RUN_018 = PROJECT_ROOT / "results/btc_ob_fight_cases/20260831T190000Z/run_018"
ANCHOR = datetime(2026, 8, 31, 19, 0, 0, tzinfo=timezone.utc)
WINDOW_START = datetime(2026, 8, 31, 18, 30, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 31, 19, 30, 0, tzinfo=timezone.utc)


def main() -> int:
    if not RUN_018.is_dir():
        print("run_018 missing", file=sys.stderr)
        return 1

    summary = json.loads((RUN_018 / "summary.json").read_text())
    vvah = (summary.get("profile_facts") or {}).get("volume_vah") or 79140.0
    reclaim_rows = list(csv.DictReader((RUN_018 / "reclaim_events.csv").open()))

    cl = clickhouse_client()
    trades, _ = load_public_trades(cl, "BTCUSDT", WINDOW_START, WINDOW_END)
    liq_events, liq_meta = load_liquidation_events(cl, "BTCUSDT", WINDOW_START, WINDOW_END)
    oi_rows = load_open_interest(cl, "BTCUSDT", WINDOW_START, WINDOW_END)

    flow = build_liquidation_flow_facts(
        trades=trades,
        liq_events=liq_events,
        liq_load_meta=liq_meta,
        oi_rows=oi_rows,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        anchor=ANCHOR,
        outer_edge_price=float(vvah) if vvah else None,
        reclaim_events=reclaim_rows,
    )

    write_preflight(RUN_018 / "phase_2a4_liquidation_flow_preflight.json")
    write_json(RUN_018 / "liquidation_flow_summary.json", flow["summary"])
    write_json(RUN_018 / "liquidation_flow_manifest.json", flow["manifest"])
    write_csv(RUN_018 / "liquidation_flow_events.csv", flow["events"])
    write_csv(RUN_018 / "liquidation_public_trade_allocation.csv", flow["allocations"])
    write_csv(RUN_018 / "liquidation_matching_sensitivity.csv", flow["sensitivity"])
    write_csv(RUN_018 / "liquidation_phase_summary.csv", flow["phases"])

    print(json.dumps({"ok": True, "run": str(RUN_018), "contract": flow["summary"]["contract_version"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

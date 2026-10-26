#!/usr/bin/env python3
"""BT-D47 read-only integrated trade forensics."""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D41 = ROOT / "results/phase_bt_d41_pending_lifecycle_v1"
D46 = ROOT / "results/phase_bt_d46_integrated_short_backtester_v1"
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"
OUT = ROOT / "results/phase_bt_d47_trade_forensics_v1"

for p in (
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d42_fill_cancel_chronology_v1",
    ROOT / "tools/phase_bt_d45_execution_contract_v1",
    ROOT / "tools/phase4d_pool_landscape_backtester_v2",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from entry_price_v1 import compute_short_entry_level  # noqa: E402
from fill_chronology_v1 import _parse_utc  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from sell_limit_fill_v1 import evaluate_short_sell_limit_fill  # noqa: E402
from strategy_rules import short_sl  # noqa: E402

OVERLAY_CONTRACT = "bt_d47_integrated_trade_forensics_overlay_v1"
FILL_LABEL = "SIMULATED_TOUCH_FILL_NOT_EXCHANGE_CONFIRMED"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def _period(bar_close: str) -> str:
    c = _parse_utc(bar_close)
    o = c - timedelta(minutes=5)
    return f"{o.strftime('%Y-%m-%dT%H:%M:%SZ')}–{bar_close}"


def _exit_ambiguity(ohlc: dict[str, float], sl: float, tp: float) -> str:
    h, l = float(ohlc["h"]), float(ohlc["l"])
    hit_sl = h >= sl
    hit_tp = l <= tp
    if hit_sl and hit_tp:
        return "UNRESOLVED_BOTH_TOUCHED_SAME_5M_BAR"
    if hit_sl:
        return "RESOLVED_SL"
    if hit_tp:
        return "RESOLVED_TP"
    return "NO_EXIT_ON_BAR"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    positions = list(csv.DictReader((D46 / "simulated_positions.csv").open(encoding="utf-8")))
    events = list(csv.DictReader((D46 / "chronological_events.csv").open(encoding="utf-8")))
    pending_d41 = {r["order_id"]: r for r in csv.DictReader((D41 / "pending_orders.csv").open())}
    lineage = list(csv.DictReader((D46 / "pending_order_lineage.csv").open()))
    lineage_by_child = {r["child_order_id"]: r for r in lineage}

    cfg_path = Path((RUN_DIR / "config_path.txt").read_text().strip())
    config = load_yaml_and_validate(cfg_path)
    sl_pct = float(config["risk"]["sl_above_upper_top_pct"])
    se = config["short_entry"]
    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_excl = bool(config["backtest"].get("end_utc_exclusive"))
    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_excl)

    fills_audit: list[dict[str, Any]] = []
    exits_audit: list[dict[str, Any]] = []
    positions_audit: list[dict[str, Any]] = []
    overlay_records: list[dict[str, Any]] = []

    # Chronology: rescan after close
    close_times: dict[str, str] = {}
    for pos in positions:
        if pos.get("exit_bar"):
            close_times[pos["position_id"]] = pos["exit_bar"]

    for i, pos in enumerate(positions, 1):
        oid = pos["order_id"]
        meta = pending_d41.get(oid, {})
        pool_lo = float(meta.get("pool_lower") or pos["pool_key"].split("|")[0])
        pool_hi = float(meta.get("pool_upper") or pos["pool_key"].split("|")[1])
        origins = meta.get("origin_set_key") or meta.get("origin_ids") or ""
        signal_close = pos["signal_close_utc"]
        active_from = meta.get("earliest_eligible_execution_utc") or ""
        entry_bar = pos["entry_bar_utc"]
        entry_px = float(pos["entry_price"])
        sl = float(pos["sl_price"])
        tp = float(pos["tp_price"])

        exp_entry = compute_short_entry_level(pool_lo, pool_hi, se)
        exp_sl = short_sl(pool_hi, sl_pct)
        parity_ok = abs(exp_entry - entry_px) < 1e-6 or pos["fill_rule_id"] == "CAUSAL_V2_MARKETABLE_AT_OPEN"
        sl_ok = abs(exp_sl - sl) < 1e-5

        ohlc_e = prices.get(entry_bar, {})
        fill_dec = evaluate_short_sell_limit_fill(
            fill_model="causal_sell_limit_v2",
            limit_price=float(meta.get("planned_entry") or entry_px),
            bar_utc=entry_bar,
            order_active_from_utc=active_from or entry_bar,
            ohlc=ohlc_e,
        ) if ohlc_e else None

        parent = lineage_by_child.get(oid, {}).get("parent_order_id", "")
        next_rescan = ""
        if i < len(positions):
            prev_exit = pos.get("exit_bar")
            if prev_exit:
                next_rescan = f"after_{prev_exit}"
        # next allowed: decision close after exit for this position
        exit_bar = pos.get("exit_bar") or ""
        next_allowed = exit_bar if exit_bar else "PILOT_END_STILL_OPEN"

        fill_resolved = "RESOLVED" if fill_dec and fill_dec.filled else "UNRESOLVED"
        if entry_bar < (active_from or entry_bar):
            fill_resolved = "VIOLATION_BEFORE_ACTIVATION"

        fills_audit.append(
            {
                "position_id": pos["position_id"],
                "order_id": oid,
                "fill_bar_close_utc": entry_bar,
                "fill_interval_5m": _period(entry_bar),
                "entry_price": entry_px,
                "fill_rule_id": pos["fill_rule_id"],
                "fill_label": FILL_LABEL,
                "causal_v2_filled": fill_dec.filled if fill_dec else False,
                "causal_v2_rule_id": fill_dec.rule_id if fill_dec else "",
                "ohlc_o": ohlc_e.get("o"),
                "ohlc_h": ohlc_e.get("h"),
                "ohlc_l": ohlc_e.get("l"),
                "ohlc_c": ohlc_e.get("c"),
                "fill_ordering_resolved": fill_resolved,
                "active_from_utc": active_from,
            }
        )

        exit_reason = pos.get("exit_reason") or "OPEN_AT_PILOT_END"
        exit_bar = pos.get("exit_bar") or ""
        exit_px = pos.get("exit_price") or ""
        exit_class = "OPEN"
        if exit_bar and prices.get(exit_bar):
            exit_class = _exit_ambiguity(prices[exit_bar], sl, tp)
        if exit_class.startswith("UNRESOLVED"):
            exit_resolved = "UNRESOLVED"
        elif exit_reason in ("SL", "TP"):
            exit_resolved = "RESOLVED"
        elif exit_reason == "OPEN_AT_PILOT_END":
            exit_resolved = "OPEN"
        else:
            exit_resolved = "N/A"

        exits_audit.append(
            {
                "position_id": pos["position_id"],
                "order_id": oid,
                "exit_bar_close_utc": exit_bar,
                "exit_interval_5m": _period(exit_bar) if exit_bar else "",
                "exit_reason": exit_reason,
                "exit_price": exit_px,
                "status": pos.get("status"),
                "exit_evidence_class": exit_class,
                "exit_ordering_resolved": exit_resolved,
                "same_bar_as_entry": exit_bar == entry_bar,
            }
        )

        positions_audit.append(
            {
                "position_index": i,
                "position_id": pos["position_id"],
                "order_id": oid,
                "parent_order_id": parent,
                "pool_lower": pool_lo,
                "pool_upper": pool_hi,
                "origin_set_key": origins,
                "decision_close_utc": signal_close,
                "pending_activation_utc": active_from,
                "entry_bar_utc": entry_bar,
                "entry_price": entry_px,
                "frozen_sl": sl,
                "frozen_tp": tp,
                "exit_reason": exit_reason,
                "exit_bar_utc": exit_bar,
                "next_rescan_after_utc": next_allowed,
                "parity_entry_recomputed": exp_entry,
                "parity_sl_recomputed": exp_sl,
                "parity_entry_ok": parity_ok,
                "parity_sl_ok": sl_ok,
                "selection_reason": meta.get("selection_reason"),
                "scanner_setup_id": meta.get("scanner_setup_id"),
                "fill_exit_resolved": f"fill={fill_resolved};exit={exit_resolved}",
            }
        )

        overlay_records.append(
            {
                "event_type": "SIMULATED_ENTRY",
                "position_id": pos["position_id"],
                "order_id": oid,
                "side": "SHORT",
                "time_utc": entry_bar,
                "price": entry_px,
                "label": FILL_LABEL,
                "pool_lower": pool_lo,
                "pool_upper": pool_hi,
                "origin_set_key": origins,
                "metadata": {"fill_rule_id": pos["fill_rule_id"]},
            }
        )
        if exit_reason == "SL" and exit_bar:
            overlay_records.append(
                {
                    "event_type": "SIMULATED_EXIT_SL",
                    "position_id": pos["position_id"],
                    "time_utc": exit_bar,
                    "price": exit_px,
                    "label": "SIMULATED_SL_NOT_EXCHANGE_CONFIRMED",
                    "exit_evidence_class": exit_class,
                }
            )
        elif exit_reason == "TP" and exit_bar:
            overlay_records.append(
                {
                    "event_type": "SIMULATED_EXIT_TP",
                    "position_id": pos["position_id"],
                    "time_utc": exit_bar,
                    "price": exit_px,
                    "label": "SIMULATED_TP_NOT_EXCHANGE_CONFIRMED",
                    "exit_evidence_class": exit_class,
                }
            )
        elif exit_reason == "OPEN_AT_PILOT_END":
            overlay_records.append(
                {
                    "event_type": "SIMULATED_POSITION_OPEN",
                    "position_id": pos["position_id"],
                    "time_utc": end,
                    "price": entry_px,
                    "label": "OPEN_AT_PILOT_END_UNRESOLVED_EXIT",
                }
            )

    suppressed = [e for e in events if e.get("event") == "SCANNER_SUPPRESSED"]
    suppressed_audit = [
        {
            "seq": e.get("seq"),
            "decision_close_utc": e.get("decision_close_utc"),
            "reason": e.get("reason"),
            "machine_state": e.get("machine_state"),
            "classification": "suppressed_during_open_position",
        }
        for e in suppressed
    ]

    # Quality descriptors
    sl_hours = defaultdict(int)
    tp_hours = defaultdict(int)
    for p in positions_audit:
        h = _parse_utc(p["entry_bar_utc"]).hour
        ex = p["exit_reason"]
        if ex == "SL":
            sl_hours[h] += 1
        elif ex == "TP":
            tp_hours[h] += 1

    chronology_checks = {
        "simulated_positions": len(positions),
        "simulated_fills_in_events": sum(1 for e in events if e.get("event") == "SIMULATED_FILL"),
        "suppressed_scanner_emits": len(suppressed),
        "expected_suppressed": 66,
        "max_one_position_assumed_by_d46_engine": True,
        "fill_label": FILL_LABEL,
    }

    structural = f"""# BT-D47 structural summary (descriptive)

## Chronology (B)
{json.dumps(chronology_checks, indent=2)}

## Exit audit
- SL cases: {sum(1 for e in exits_audit if e['exit_reason'] == 'SL')}
- TP cases: {sum(1 for e in exits_audit if e['exit_reason'] == 'TP')}
- Open at pilot end: {sum(1 for e in exits_audit if e['exit_reason'] == 'OPEN_AT_PILOT_END')}
- Same-bar entry+exit: {sum(1 for e in exits_audit if e.get('same_bar_as_entry'))}

## SL by entry hour (UTC)
{json.dumps(dict(sl_hours), indent=2)}

## TP by entry hour (UTC)
{json.dumps(dict(tp_hours), indent=2)}

## Pool parity (D)
- All positions checked against recomputed entry (lower edge) and SL (0.2% above upper top).
- See `positions_audit.csv` parity_* columns.

## Quality notes (E) — no optimization
- SL cluster: afternoon UTC hours show multiple quick SLs (13:45–13:55 chain).
- Repeated zone: multiple entries 23:10–01:10 Sep 11–12 around 0.08417–0.08438 band.
- TP cases: fewer; longer hold bars on TP rows in positions CSV.

## Chart overlay
`chart_overlay_events.json` — {len(overlay_records)} markers (entries + exits + open), no scanner boxes.
"""

    chart_bundle = {
        "contract_version": OVERLAY_CONTRACT,
        "symbol": "DOGEUSDT",
        "source": "bt_d46_integrated_short_backtester_v1",
        "fill_disclaimer": FILL_LABEL,
        "plan_only": False,
        "scanner_boxes_included": False,
        "records": overlay_records,
    }

    write_csv(OUT / "positions_audit.csv", positions_audit)
    write_csv(OUT / "fills_audit.csv", fills_audit)
    write_csv(OUT / "exits_audit.csv", exits_audit)
    write_csv(OUT / "suppressed_emits_audit.csv", suppressed_audit)
    (OUT / "chart_overlay_events.json").write_text(json.dumps(chart_bundle, indent=2), encoding="utf-8")
    (OUT / "structural_summary.md").write_text(structural, encoding="utf-8")

    print(json.dumps({"positions": len(positions), "suppressed": len(suppressed), "overlay": len(overlay_records)}, indent=2))


if __name__ == "__main__":
    main()

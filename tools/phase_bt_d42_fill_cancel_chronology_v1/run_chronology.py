#!/usr/bin/env python3
"""BT-D42 fill vs cancel chronology pilot."""

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
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"
OUT = ROOT / "results/phase_bt_d42_fill_cancel_chronology_v1"

for p in (
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d42_fill_cancel_chronology_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from fill_chronology_v1 import (  # noqa: E402
    _parse_utc,
    activation_contract_note,
    analyze_segment,
    build_order_segments,
)
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402

REF_1535 = "2026-09-11T15:35:00Z"


def _load_1m_candles(start_utc: str, end_utc: str) -> tuple[list[dict[str, Any]], str]:
    try:
        dash = ROOT / "backtester/short/dashboard"
        if str(dash) not in sys.path:
            sys.path.insert(0, str(dash))
        from research_charts.clickhouse_source import ClickHouseResearchCandleSource

        src = ClickHouseResearchCandleSource()
        start_dt = _parse_utc(start_utc) - timedelta(hours=2)
        end_dt = _parse_utc(end_utc) + timedelta(hours=2)
        raw = src.get_1m_candles("DOGEUSDT", start=start_dt, end=end_dt, newest_first_limit=False)
        out: list[dict[str, Any]] = []
        for c in raw:
            ot = c.timestamp
            if ot.tzinfo is None:
                from datetime import timezone

                ot = ot.replace(tzinfo=timezone.utc)
            ct = ot + timedelta(minutes=1)
            out.append(
                {
                    "open_time": ot,
                    "close_time": ct,
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                }
            )
        return out, f"clickhouse_1m_{len(out)}_bars"
    except Exception as exc:
        return [], f"unavailable:{type(exc).__name__}"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    events = list(csv.DictReader((D41 / "lifecycle_events.csv").open(encoding="utf-8")))
    pending_rows = list(csv.DictReader((D41 / "pending_orders.csv").open(encoding="utf-8")))
    setup_by_order = {r["order_id"]: r for r in pending_rows}
    unresolved_d41 = list(csv.DictReader((D41 / "unresolved_fill_intervals.csv").open(encoding="utf-8")))

    cfg_path = Path((RUN_DIR / "config_path.txt").read_text().strip())
    config = load_yaml_and_validate(cfg_path)
    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_excl = bool(config["backtest"].get("end_utc_exclusive"))
    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_excl)
    fill_model = str(config["short_entry"].get("pending_limit_fill_model") or "causal_sell_limit_v2")

    candles_1m, source_1m = _load_1m_candles(start, end)

    segments = build_order_segments(events)
    for seg in segments:
        meta = setup_by_order.get(seg.order_id)
        if meta:
            seg.scanner_setup_id = meta.get("scanner_setup_id")
            seg.origin_ids = meta.get("origin_set_key") or seg.origin_ids

    timeline_rows: list[dict[str, Any]] = []
    touch_rows: list[dict[str, Any]] = []
    conflict_rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = defaultdict(int)
    branch_counts: dict[str, int] = defaultdict(int)

    for seg in segments:
        res = analyze_segment(
            seg,
            prices,
            fill_model=fill_model,
            candles_1m=candles_1m if candles_1m else None,
            pilot_end_utc=end if not end_excl else end,
        )
        status_counts[res["chronology_status"]] += 1
        branch_counts[res["fill_branch"]] += 1
        timeline_rows.append({k: v for k, v in res.items() if k != "touch_evidence_rows"})
        for tr in res["touch_evidence_rows"]:
            touch_rows.append(tr)

        if res["end_event_type"] in ("CANCEL", "REPLACE") and not res["bt41_cancel_conditional_on_unfilled"]:
            conflict_rows.append(
                {
                    "order_id": res["order_id"],
                    "decision_close_utc": res["decision_close_utc"],
                    "end_event_type": res["end_event_type"],
                    "end_cancel_reason": res["end_cancel_reason"],
                    "cancel_effective_decision_close_utc": res["cancel_effective_decision_close_utc"],
                    "first_high_touch_bar_5m": res["first_high_touch_bar_5m"],
                    "first_causal_v2_fill_bar_5m": res["first_causal_v2_fill_bar_5m"],
                    "first_touch_1m_close_utc": res["first_touch_1m_close_utc"],
                    "chronology_status": res["chronology_status"],
                    "fill_branch": res["fill_branch"],
                    "note": "BT-D41 cancel/replace may not assume order remained unfilled",
                }
            )

    ref_seg = next(s for s in segments if s.decision_close_utc == REF_1535)
    ref_analysis = analyze_segment(
        ref_seg,
        prices,
        fill_model=fill_model,
        candles_1m=candles_1m if candles_1m else None,
        pilot_end_utc=end,
    )
    ref_bars = []
    for bar in sorted(prices.keys()):
        if ref_seg.pending_active_from_utc <= bar <= (ref_seg.end_decision_close_utc or end):
            o = prices[bar]
            ref_bars.append(
                {
                    "bar_utc": bar,
                    "o": o["o"],
                    "h": o["h"],
                    "l": o["l"],
                    "c": o["c"],
                }
            )

    ref_1535 = {
        "reference_decision_close_utc": REF_1535,
        "order_id": ref_seg.order_id,
        "planned_entry": ref_seg.planned_entry,
        "planned_sl": ref_seg.planned_sl,
        "planned_tp": ref_seg.planned_tp,
        "pending_active_from_utc": ref_seg.pending_active_from_utc,
        "replace_effective_decision_close_utc": ref_seg.end_decision_close_utc,
        "activation_contract": activation_contract_note(),
        "fill_model_reference": fill_model,
        "data_5m_bars_in_window": ref_bars,
        "data_1m_source": source_1m,
        "chronology": {k: v for k, v in ref_analysis.items() if k != "touch_evidence_rows"},
        "touch_evidence_5m": ref_analysis["touch_evidence_rows"],
        "bt41_unresolved_intervals_for_order": [
            u for u in unresolved_d41 if u["order_id"] == ref_seg.order_id
        ],
    }

    possibly_filled = sum(1 for r in timeline_rows if r["fill_branch"] == "POSSIBLY_FILLED")
    conditional_cancel = len(conflict_rows)

    summary = f"""# BT-D42 fill vs cancel chronology summary

## Activation timing (verified, unchanged from BT-D41)
{activation_contract_note()}

## Data sources
- 5m OHLCV: SSOT `load_prices_from_ssot_window` ({len(prices)} bars in pilot window)
- 1m OHLCV: `{source_1m}`
- Fill evidence model (reference only, no simulated trades): `{fill_model}`

## Pilot segments
- Order segments analyzed: **{len(segments)}**
- BT-D41 POTENTIAL_FILL_UNRESOLVED intervals: **{len(unresolved_d41)}**

## Chronology status counts
{json.dumps(dict(status_counts), indent=2)}

## Fill branch
{json.dumps(dict(branch_counts), indent=2)}

## Cancel/replace vs fill evidence
- End events where cancel/replace is **not** conditional on unfilled: **{conditional_cancel}**
- Segments with `POSSIBLY_FILLED` branch: **{possibly_filled}**

## Reference 15:35 UTC
See `reference_1535.json` — replace at **15:40** decision close; first active bar **15:40**.

## Limitations
- No queue priority, liquidity, or guaranteed fill from touch alone
- Same 5m bar as cancel → `EXECUTION_UNRESOLVED` unless 1m ordering resolves
- No SL/TP outcome simulation
"""

    write_csv(OUT / "order_timelines.csv", timeline_rows)
    write_csv(OUT / "touch_evidence.csv", touch_rows)
    write_csv(OUT / "cancel_fill_conflicts.csv", conflict_rows)
    (OUT / "reference_1535.json").write_text(json.dumps(ref_1535, indent=2, default=str), encoding="utf-8")
    (OUT / "summary.md").write_text(summary, encoding="utf-8")

    print(
        json.dumps(
            {
                "segments": len(segments),
                "touch_rows": len(touch_rows),
                "conflicts": len(conflict_rows),
                "status_counts": dict(status_counts),
                "1m_source": source_1m,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

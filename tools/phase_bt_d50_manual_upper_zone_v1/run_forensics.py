#!/usr/bin/env python3
"""BT-D50 read-only manual upper-zone reference forensics (DOGE 2026-09-11)."""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase_bt_d50_manual_upper_zone_v1"
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"
D47 = ROOT / "results/phase_bt_d47_trade_forensics_v1"
D46 = ROOT / "results/phase_bt_d46_integrated_short_backtester_v1"

MANUAL_LO = 0.08410
MANUAL_HI = 0.08436
MANUAL_SL = MANUAL_HI * 1.002

for p in (
    ROOT / "tools/phase_bt_d26_dynamic_upper_pool_selector_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
    ROOT / "tools/phase4d_pool_landscape_backtester_v2",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from entry_price_v1 import compute_short_entry_level  # noqa: E402
from pool_first_signal_v1 import _structural_eligible_upper  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from setup_scanner_v1 import _bars_in_range, measure_upper_gap_pct, snapshot_to_intervals  # noqa: E402
from signal_types_v1 import pool_key  # noqa: E402
from strategy_rules import select_short_tp_depth2, short_sl  # noqa: E402

ORIGIN_TS_RE = re.compile(r":upper:(\d+)$")


def _origin_count(ids: str | None) -> int:
    if not ids:
        return 0
    return len([x for x in str(ids).split("|") if x.strip()])


def _overlaps_manual(lo: float, hi: float) -> bool:
    return lo < MANUAL_HI and hi > MANUAL_LO


def _intervals_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    return lo1 < hi2 and lo2 < hi1


def _manual_tp(snap: dict, entry: float, config: dict) -> dict[str, Any]:
    min_tp_dist = float(config["take_profit"]["min_distance_pct"])
    intervals = snapshot_to_intervals(snap)
    tp_iv, tp_reason = select_short_tp_depth2(entry, intervals, min_tp_dist)
    if not tp_iv:
        return {"tp": None, "tp_reason": tp_reason}
    top = float(tp_iv["top"])
    bottom = float(tp_iv.get("bottom") or tp_iv.get("bottom_price"))
    return {
        "tp": top,
        "tp_bottom": bottom,
        "tp_reason": tp_reason,
        "tp_selection_mode": config["take_profit"]["selection_mode"],
    }


def _load_signal_from_audit(pid: str) -> dict[str, Any]:
    with (D47 / "positions_audit.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["position_id"] == pid:
                return dict(row)
    return {}


def _events_for_position(pid: str) -> list[dict[str, str]]:
    rows = []
    with (D47 / "chart_overlay_events.json").open(encoding="utf-8") as fh:
        doc = json.load(fh)
    for rec in doc.get("records") or []:
        if rec.get("position_id") == pid:
            rows.append(rec)
    return rows


def _landscape_at_bar(
    bar: str,
    *,
    config: dict,
    density,
    prices: dict,
    pool_age: dict[str, int],
) -> dict[str, Any]:
    snap = density.collect_at_close(bar)
    candle = prices[bar]
    close_px = float(candle["c"])
    uppers = snap.get("upper_bands") or []
    manual_bands = []
    all_upper = []
    elig = []
    se = config["short_entry"]
    sl_pct = float(config["risk"]["sl_above_upper_top_pct"])
    min_gap = float(se["upper_gap_min_pct"])

    for b in uppers:
        lo = float(b["lower_price"])
        hi = float(b["upper_price"])
        pk = pool_key(lo, hi)
        ok, fail = _structural_eligible_upper(
            config,
            band=b,
            decision_close_utc=bar,
            candle=candle,
            snapshot=snap,
            pool_age_bars=pool_age,
            require_large_upper_gap=True,
        )
        row = {
            "pool_lower": lo,
            "pool_upper": hi,
            "pool_key": pk,
            "origin_ids": b.get("origin_ids"),
            "origin_set_key": b.get("origin_set_key") or b.get("origin_ids"),
            "active_origin_count": _origin_count(b.get("origin_ids")),
            "structurally_eligible": ok,
            "exclusion_reason": fail or "",
            "overlaps_manual_zone": _overlaps_manual(lo, hi),
            "upper_gap_pct": measure_upper_gap_pct(uppers, hi, min_gap),
            "pool_age_bars": pool_age.get(pk, 0),
            "distance_close_to_pool_lower_pct": (lo - close_px) / close_px * 100.0,
        }
        all_upper.append(row)
        if ok:
            elig.append(row)
        if _overlaps_manual(lo, hi):
            manual_bands.append(row)

    elig.sort(key=lambda r: (r["pool_lower"], r["pool_upper"]))
    lowest = elig[0] if elig else None

    # Union cluster of bands overlapping manual envelope
    cluster_ids = set()
    for i, a in enumerate(manual_bands):
        for j, b in enumerate(manual_bands):
            if _intervals_overlap(
                a["pool_lower"], a["pool_upper"], b["pool_lower"], b["pool_upper"]
            ):
                cluster_ids.add(i)
                cluster_ids.add(j)
    manual_union = {
        "band_count": len(manual_bands),
        "is_single_canonical_pool": len(manual_bands) == 1,
        "is_union_of_neighbors": len(manual_bands) > 1,
        "cluster_member_count": len(cluster_ids),
        "envelope_from_bands": None,
    }
    if manual_bands:
        manual_union["envelope_from_bands"] = {
            "lower": min(b["pool_lower"] for b in manual_bands),
            "upper": max(b["pool_upper"] for b in manual_bands),
        }

    manual_ref = {
        "user_lower": MANUAL_LO,
        "user_upper": MANUAL_HI,
        "user_entry": MANUAL_LO,
        "user_sl": MANUAL_SL,
        "user_tp": _manual_tp(snap, MANUAL_LO, config),
    }

    return {
        "decision_close_utc": bar,
        "close_price": close_px,
        "snapshot_upper_band_count": len(all_upper),
        "structurally_eligible_count": len(elig),
        "lowest_eligible_upper": lowest,
        "manual_zone_bands": manual_bands,
        "manual_zone_analysis": manual_union,
        "manual_reference_levels": manual_ref,
        "all_uppers_near_manual": sorted(
            [r for r in all_upper if r["pool_lower"] < MANUAL_HI + 0.001 and r["pool_upper"] > MANUAL_LO - 0.001],
            key=lambda r: r["pool_lower"],
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    cfg_path = Path((RUN_DIR / "config_path.txt").read_text().strip())
    config = load_yaml_and_validate(cfg_path)
    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_excl = bool(config["backtest"].get("end_utc_exclusive"))
    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_excl)
    bars = _bars_in_range(prices, start, end, end_exclusive=end_excl)
    density = create_density_session(config)

    pool_age: dict[str, int] = {}
    target_bars = [
        "2026-09-11T12:05:00Z",
        "2026-09-11T12:25:00Z",
        "2026-09-11T12:30:00Z",
        "2026-09-11T12:35:00Z",
    ]
    landscapes: dict[str, Any] = {}
    age_snapshots: dict[str, dict[str, int]] = {}

    for bar in bars:
        try:
            density.collect_at_close(bar)
        except KeyError:
            continue
        snap = density.collect_at_close(bar)
        for b in snap.get("upper_bands") or []:
            pk = pool_key(float(b["lower_price"]), float(b["upper_price"]))
            pool_age[pk] = pool_age.get(pk, 0) + 1
        if bar in target_bars:
            age_snapshots[bar] = dict(pool_age)
            landscapes[bar] = _landscape_at_bar(
                bar, config=config, density=density, prices=prices, pool_age=age_snapshots[bar]
            )

    # Signal A: fill 12:25 -> position 78cd59a534af80d5
    sig_a = {
        "user_reference_timestamp_utc": "2026-09-11T12:25:00Z",
        "event_interpretation": "SIMULATED_FILL_AND_SAME_BAR_TP (not scanner decision close)",
        "position_id": "78cd59a534af80d5",
        "order_id": "57c97bbd342e9817bcf7",
        "audit": _load_signal_from_audit("78cd59a534af80d5"),
        "overlay_events": _events_for_position("78cd59a534af80d5"),
        "pending_decision_close_utc": "2026-09-11T12:05:00Z",
        "pending_activation_utc": "2026-09-11T12:10:00Z",
        "landscape_at_fill_bar": landscapes.get("2026-09-11T12:25:00Z"),
        "landscape_at_pending_decision": landscapes.get("2026-09-11T12:05:00Z"),
    }

    # Signal B: fill 12:35
    sig_b = {
        "user_reference_timestamp_utc": "2026-09-11T12:35:00Z",
        "event_interpretation": "SIMULATED_FILL_AND_SAME_BAR_SL",
        "position_id": "aa1a8a9c1bc83efc",
        "order_id": "98356165f844199aa699",
        "audit": _load_signal_from_audit("aa1a8a9c1bc83efc"),
        "overlay_events": _events_for_position("aa1a8a9c1bc83efc"),
        "pending_decision_close_utc": "2026-09-11T12:30:00Z",
        "pending_activation_utc": "2026-09-11T12:35:00Z",
        "landscape_at_fill_bar": landscapes.get("2026-09-11T12:35:00Z"),
        "landscape_at_pending_decision": landscapes.get("2026-09-11T12:30:00Z"),
    }

    manual_doc = {
        "contract_version": "bt_d50_manual_upper_zone_reference_v1",
        "symbol": "DOGEUSDT",
        "timeframe": "5m",
        "pd": "PD1",
        "depth": 2,
        "manual_zone": {"lower": MANUAL_LO, "upper": MANUAL_HI},
        "manual_entry": MANUAL_LO,
        "manual_sl_formula": "upper_edge * 1.002",
        "manual_sl": MANUAL_SL,
        "density_path": "canonical density_bridge_v1 (168h+ warmup via backtest window)",
        "landscapes": landscapes,
    }

    (OUT / "signal_1225.json").write_text(json.dumps(sig_a, indent=2), encoding="utf-8")
    (OUT / "signal_1235.json").write_text(json.dumps(sig_b, indent=2), encoding="utf-8")
    (OUT / "manual_zone_08410_08436.json").write_text(
        json.dumps(manual_doc, indent=2), encoding="utf-8"
    )

    cmp_rows = []
    for label, sig in (("12:25_fill", sig_a), ("12:35_fill", sig_b)):
        aud = sig["audit"]
        ls = sig.get("landscape_at_fill_bar") or {}
        lowest = ls.get("lowest_eligible_upper") or {}
        cmp_rows.append(
            {
                "reference": label,
                "bar_utc": sig["user_reference_timestamp_utc"],
                "position_id": sig["position_id"],
                "actual_pool_lower": aud.get("pool_lower"),
                "actual_pool_upper": aud.get("pool_upper"),
                "actual_entry": aud.get("entry_price"),
                "actual_sl": aud.get("frozen_sl"),
                "actual_tp": aud.get("frozen_tp"),
                "manual_entry": MANUAL_LO,
                "manual_sl": MANUAL_SL,
                "manual_tp": (ls.get("manual_reference_levels") or {}).get("user_tp", {}).get("tp"),
                "lowest_eligible_lower_at_bar": lowest.get("pool_lower"),
                "lowest_eligible_upper_at_bar": lowest.get("pool_upper"),
                "manual_zone_canonical_band_count": (ls.get("manual_zone_analysis") or {}).get(
                    "band_count"
                ),
                "actual_overlaps_manual_zone": _overlaps_manual(
                    float(aud.get("pool_lower") or 0), float(aud.get("pool_upper") or 0)
                ),
            }
        )

    with (OUT / "selected_vs_manual.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(cmp_rows[0].keys()))
        w.writeheader()
        w.writerows(cmp_rows)

    origin_rows = []
    for bar, ls in landscapes.items():
        for b in ls.get("manual_zone_bands") or []:
            origin_rows.append({"decision_close_utc": bar, **b})
    with (OUT / "origin_memberships.csv").open("w", encoding="utf-8", newline="") as fh:
        if origin_rows:
            w = csv.DictWriter(fh, fieldnames=list(origin_rows[0].keys()))
            w.writeheader()
            w.writerows(origin_rows)

    findings = """# BT-D50 structural comparison (auto)

## Key finding — timestamp semantics
- **12:25 UTC** in BT-D46 is the **simulated fill/exit bar** for position `78cd59a5…`, not a new scanner selection.
  Pending was created at **12:05** on pool **0.08384–0.083844** (below the manual 0.08410–0.08436 zone).
- **12:35 UTC** is the **simulated fill/SL bar** for position `aa1a8a9c…`, pending from **12:30** on **0.08408–0.08409**
  (0.00002 below manual lower 0.08410).

## Manual zone vs canonical bands
See `manual_zone_08410_08436.json` for overlapping upper bands at 12:25 and 12:35.

## Ranking
Dynamic selector chooses **lowest structurally eligible `pool_lower`**, not a merged envelope of overlapping bands.
"""
    (OUT / "structural_comparison.md").write_text(findings, encoding="utf-8")
    print("Wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Deep-dive: stuck last_fill=CYCLE_5_SHORT_REDUCE (mild DCOS vs baseline). Analysis only."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .candle_loader import load_candles_for_symbol
from .dynamic_cycle_order_scaling import DynamicCycleOrderScalingConfig, config_from_json_string
from .historical_backtest import normalize_candles, run_historical_backtest
from .simulated_order_book import SyntheticCandle

VARIANT_CONFIGS: dict[str, Path] = {
    "baseline": None,  # type: ignore[assignment]
    "mild_a": Path("research/backtests/configs/dcos_mild_qty/variant_a.json"),
    "mild_b": Path("research/backtests/configs/dcos_mild_qty/variant_b.json"),
    "mild_c": Path("research/backtests/configs/dcos_mild_qty/variant_c.json"),
}


@dataclass
class TradeAnalysis:
    variant: str
    start_index: int
    trade_block_id: str = ""
    last_fill_purpose: str = ""
    final_status: str = ""
    next_required_purpose: str = ""
    final_active_orders: list[str] = field(default_factory=list)
    cycle5_long_add_fill_price: float | None = None
    cycle5_long_add_qty: float | None = None
    cycle5_long_add_qty_before_scaling: float | None = None
    cycle5_qty_factor: float | None = None
    long_qty_before_c5_long_add: float | None = None
    long_qty_after_c5_long_add: float | None = None
    long_avg_before_c5_long_add: float | None = None
    long_avg_after_c5_long_add: float | None = None
    cycle5_short_reduce_trigger: float | None = None
    cycle5_short_reduce_fill_price: float | None = None
    cycle5_short_reduce_qty: float | None = None
    target_profit_usdt: float | None = None
    target_profit_pct: float | None = None
    long_loss_usdt: float | None = None
    required_net: float | None = None
    required_price_move: float | None = None
    sr_submit_candle: int | None = None
    sr_fill_candle: int | None = None
    sr_wait_candles: int | None = None
    min_low_while_sr_active: float | None = None
    max_high_while_sr_active: float | None = None
    sr_miss_distance_bps: float | None = None
    sr_miss_class: str = ""
    cycle6_long_add_placed: bool = False
    cycle6_long_add_trigger: float | None = None
    cycle6_submit_candle: int | None = None
    c6_miss_distance_bps: float | None = None
    c6_miss_class: str = ""
    theoretical_qty_factor_for_sr_touch: float | None = None
    notes: str = ""


def _load_scaling(path: Path | None) -> DynamicCycleOrderScalingConfig | None:
    if path is None:
        return None
    return config_from_json_string(path.read_text())


def _stuck5sr_starts(results_path: Path) -> list[int]:
    payload = json.loads(results_path.read_text())
    out: list[int] = []
    for run in payload.get("runs") or []:
        if run.get("final_status") != "max_candles":
            continue
        last_purpose = str((run.get("last_fill") or {}).get("purpose") or "")
        if last_purpose == "CYCLE_5_SHORT_REDUCE":
            out.append(int(run["start_index"]))
    return sorted(out)


def _find_fill(fills: list[dict[str, Any]], purpose: str) -> dict[str, Any] | None:
    matches = [f for f in fills if (f.get("purpose") or "") == purpose]
    return matches[-1] if matches else None


def _find_fill_before(fills: list[dict[str, Any]], idx: int) -> dict[str, Any] | None:
    if idx <= 0:
        return None
    return fills[idx - 1]


def _index_of_fill(fills: list[dict[str, Any]], purpose: str) -> int | None:
    for i, f in enumerate(fills):
        if (f.get("purpose") or "") == purpose:
            last = i
    return last if any((f.get("purpose") or "") == purpose for f in fills) else None


def _order_submitted(
    orders: list[dict[str, Any]], purpose: str, *, after_candle: int | None = None
) -> dict[str, Any] | None:
    candidates = [
        o
        for o in orders
        if (o.get("purpose") or "") == purpose and o.get("event_type") == "submitted"
        and (after_candle is None or int(o.get("candle_index") or 0) >= after_candle)
    ]
    return candidates[-1] if candidates else None


def _candle_extremes(
    candles: list[SyntheticCandle], start_idx: int, end_idx: int
) -> tuple[float | None, float | None]:
    if start_idx >= len(candles):
        return None, None
    end_idx = min(end_idx, len(candles) - 1)
    min_low: float | None = None
    max_high: float | None = None
    for i in range(start_idx, end_idx + 1):
        c = candles[i]
        low = float(c.low if c.low is not None else c.close)
        high = float(c.high if c.high is not None else c.close)
        min_low = low if min_low is None else min(min_low, low)
        max_high = high if max_high is None else max(max_high, high)
    return min_low, max_high


def _miss_bps_fall_trigger(trigger: float | None, min_low: float | None) -> float | None:
    """Short reduce (fall trigger): positive bps = min low stayed above trigger (miss)."""
    if trigger is None or min_low is None or trigger <= 0:
        return None
    if min_low <= trigger:
        return 0.0
    return (min_low - trigger) / trigger * 10_000.0


def _miss_bps_rise_trigger(trigger: float | None, max_high: float | None) -> float | None:
    """Long add (rise trigger): positive bps = max high stayed below trigger."""
    if trigger is None or max_high is None or trigger <= 0:
        return None
    if max_high >= trigger:
        return 0.0
    return (trigger - max_high) / trigger * 10_000.0


def _classify_miss(bps: float | None) -> str:
    if bps is None:
        return "unknown"
    if bps <= 0:
        return "touched_or_filled"
    if bps < 10:
        return "near_miss_lt_10bps"
    if bps < 25:
        return "near_miss_lt_25bps"
    if bps < 50:
        return "moderate_miss_lt_50bps"
    return "structural_miss_gt_50bps"


def _estimate_theoretical_qty_factor(
    *,
    qty_factor: float | None,
    miss_bps: float | None,
    sr_filled: bool,
) -> float | None:
    """Rough heuristic: if SR filled, factor was sufficient. If miss>0 before end, scale down factor."""
    if qty_factor is None:
        return None
    if sr_filled:
        return qty_factor
    if miss_bps is None or miss_bps <= 0:
        return qty_factor
    # trigger distance scales ~linearly with loss/qty in many cases; invert upscaling
    scale = 1.0 + miss_bps / 10_000.0
    if scale <= 0:
        return None
    return max(0.25, qty_factor / scale)


def analyze_start(
    *,
    variant: str,
    start_index: int,
    candles: list[SyntheticCandle],
    window_candles: int,
    scaling: DynamicCycleOrderScalingConfig | None,
) -> TradeAnalysis:
    subset = candles[start_index : start_index + window_candles + 1]
    global_index_offset = start_index
    result = run_historical_backtest(
        "APTUSDT",
        "long",
        subset,
        max_candles=max(0, window_candles - 1),
        config_source="live",
        fill_model="conservative",
        dynamic_cycle_scaling_config=scaling,
    )
    fills = list(result.fill_log or [])
    orders = list(result.order_log or [])
    row = TradeAnalysis(
        variant=variant,
        start_index=start_index,
        trade_block_id=str((result.final_strategy_state_excerpt or {}).get("trade_block_id") or ""),
        last_fill_purpose=str((result.last_fill or {}).get("purpose") or ""),
        final_status=str(result.final_status or ""),
        next_required_purpose=str(
            (result.final_strategy_state_excerpt or {}).get("next_required_purpose") or ""
        ),
        final_active_orders=list(result.final_active_order_purposes or []),
    )

    c5_la = _find_fill(fills, "CYCLE_5_LONG_ADD")
    c5_sr = _find_fill(fills, "CYCLE_5_SHORT_REDUCE")
    if not c5_la or not c5_sr:
        row.notes = "missing_cycle5_fills"
        return row

    la_idx = next(i for i, f in enumerate(fills) if f.get("purpose") == "CYCLE_5_LONG_ADD")
    prev = _find_fill_before(fills, la_idx)
    la_meta = dict(c5_la.get("metadata_excerpt") or {})
    sr_meta = dict(c5_sr.get("metadata_excerpt") or {})

    row.cycle5_long_add_fill_price = float(c5_la.get("fill_price") or 0) or None
    row.cycle5_long_add_qty = float(c5_la.get("qty") or 0) or None
    row.cycle5_long_add_qty_before_scaling = la_meta.get("planned_cycle_qty_before_scaling")
    row.cycle5_qty_factor = la_meta.get("cycle_qty_factor_used")
    row.long_qty_before_c5_long_add = float(prev.get("long_qty_after") or 0) if prev else None
    row.long_qty_after_c5_long_add = float(c5_la.get("long_qty_after") or 0) or None
    row.long_avg_before_c5_long_add = float(prev.get("long_avg_after") or 0) if prev else None
    row.long_avg_after_c5_long_add = float(c5_la.get("long_avg_after") or 0) or None

    row.cycle5_short_reduce_trigger = float(
        sr_meta.get("trigger_price")
        or sr_meta.get("raw_trigger_price")
        or sr_meta.get("short_tp_guard_original_trigger_price_raw")
        or 0
    ) or None
    row.cycle5_short_reduce_fill_price = float(c5_sr.get("fill_price") or 0) or None
    row.cycle5_short_reduce_qty = float(c5_sr.get("qty") or 0) or None
    row.target_profit_usdt = sr_meta.get("target_profit_usdt")
    row.target_profit_pct = la_meta.get("cycle_target_profit_pct_used")
    row.long_loss_usdt = sr_meta.get("long_loss_usdt")
    row.required_net = sr_meta.get("required_net")
    row.required_price_move = sr_meta.get("required_price_move")

    sr_order = _order_submitted(orders, "CYCLE_5_SHORT_REDUCE", after_candle=int(c5_la.get("candle_index") or 0))
    row.sr_submit_candle = int(sr_order.get("candle_index") or c5_la.get("candle_index") or 0) if sr_order else None
    row.sr_fill_candle = int(c5_sr.get("candle_index") or 0)
    if row.sr_submit_candle is not None and row.sr_fill_candle is not None:
        row.sr_wait_candles = row.sr_fill_candle - row.sr_submit_candle

    if row.sr_submit_candle is not None and row.sr_fill_candle is not None:
        # Wait window: from submit until candle before fill (exclusive of fill touch)
        abs_submit = global_index_offset + row.sr_submit_candle
        abs_pre_fill = global_index_offset + max(row.sr_submit_candle, row.sr_fill_candle - 1)
        min_low, max_high = _candle_extremes(candles, abs_submit, abs_pre_fill)
        row.min_low_while_sr_active = min_low
        row.max_high_while_sr_active = max_high
        row.sr_miss_distance_bps = _miss_bps_fall_trigger(row.cycle5_short_reduce_trigger, min_low)
        row.sr_miss_class = _classify_miss(row.sr_miss_distance_bps)

    row.theoretical_qty_factor_for_sr_touch = _estimate_theoretical_qty_factor(
        qty_factor=float(row.cycle5_qty_factor) if row.cycle5_qty_factor is not None else 1.0,
        miss_bps=row.sr_miss_distance_bps,
        sr_filled=True,
    )

    c6_order = _order_submitted(orders, "CYCLE_6_LONG_ADD", after_candle=row.sr_fill_candle)
    row.cycle6_long_add_placed = c6_order is not None
    if c6_order:
        row.cycle6_long_add_trigger = float(c6_order.get("trigger_price") or 0) or None
        row.cycle6_submit_candle = int(c6_order.get("candle_index") or 0)
        abs_c6 = global_index_offset + row.cycle6_submit_candle
        abs_end = global_index_offset + min(window_candles - 1, len(subset) - 2)
        _, max_high = _candle_extremes(candles, abs_c6 + 1, abs_end)
        row.c6_miss_distance_bps = _miss_bps_rise_trigger(row.cycle6_long_add_trigger, max_high)
        row.c6_miss_class = _classify_miss(row.c6_miss_distance_bps)
    else:
        row.notes = "cycle6_long_add_not_placed_after_c5_sr"

    return row


def _aggregate_miss(rows: list[TradeAnalysis], field: str) -> dict[str, Any]:
    values = [getattr(r, field) for r in rows if getattr(r, field) is not None]
    if not values:
        return {"count": 0}
    sorted_v = sorted(values)
    n = len(sorted_v)

    def pct(p: float) -> float:
        idx = min(n - 1, max(0, int(math.ceil(p * n) - 1)))
        return sorted_v[idx]

    buckets = {"lt_10bps": 0, "lt_25bps": 0, "lt_50bps": 0, "gt_50bps": 0, "touched": 0}
    for v in values:
        if v <= 0:
            buckets["touched"] += 1
        elif v < 10:
            buckets["lt_10bps"] += 1
        elif v < 25:
            buckets["lt_25bps"] += 1
        elif v < 50:
            buckets["lt_50bps"] += 1
        else:
            buckets["gt_50bps"] += 1

    return {
        "count": n,
        "median_bps": statistics.median(sorted_v),
        "p75_bps": pct(0.75),
        "p90_bps": pct(0.90),
        "max_bps": max(sorted_v),
        "buckets": buckets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-candles", type=int, default=5000)
    parser.add_argument("--limit", type=int, default=50000)
    parser.add_argument(
        "--mild-results",
        type=Path,
        default=Path("research/backtests/results/dcos_mild_qty_a_120/APTUSDT_original_hedge_5m_multi_start_results.json"),
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("research/backtests/results/stuck_cycle5_short_reduce_analysis.json"),
    )
    args = parser.parse_args()

    stuck_starts = _stuck5sr_starts(args.mild_results)
    candles = normalize_candles(
        "APTUSDT", load_candles_for_symbol("APTUSDT", timeframe="5m", limit=args.limit)
    )

    all_rows: list[TradeAnalysis] = []
    for start in stuck_starts:
        for variant, cfg_path in VARIANT_CONFIGS.items():
            scaling = _load_scaling(cfg_path)
            all_rows.append(
                analyze_start(
                    variant=variant,
                    start_index=start,
                    candles=candles,
                    window_candles=args.window_candles,
                    scaling=scaling,
                )
            )

    by_variant: dict[str, list[TradeAnalysis]] = {}
    for row in all_rows:
        by_variant.setdefault(row.variant, []).append(row)

    mild_a_rows = [r for r in all_rows if r.variant == "mild_a"]
    payload = {
        "stuck_start_indices": stuck_starts,
        "per_trade": [asdict(r) for r in all_rows],
        "aggregate_sr_miss_bps": {
            variant: _aggregate_miss(rows, "sr_miss_distance_bps") for variant, rows in by_variant.items()
        },
        "aggregate_c6_miss_bps": {
            variant: _aggregate_miss(
                [r for r in rows if r.cycle6_long_add_placed], "c6_miss_distance_bps"
            )
            for variant, rows in by_variant.items()
        },
        "cycle6_placement_rate": {
            variant: {
                "placed": sum(1 for r in rows if r.cycle6_long_add_placed),
                "not_placed": sum(1 for r in rows if not r.cycle6_long_add_placed),
            }
            for variant, rows in by_variant.items()
        },
        "baseline_vs_mild_shift_summary": [],
    }

    for start in stuck_starts:
        base = next(r for r in all_rows if r.start_index == start and r.variant == "baseline")
        mild = next(r for r in all_rows if r.start_index == start and r.variant == "mild_a")
        payload["baseline_vs_mild_shift_summary"].append(
            {
                "start_index": start,
                "baseline_last_fill": base.last_fill_purpose,
                "baseline_active": base.final_active_orders,
                "baseline_c6_placed": base.cycle6_long_add_placed,
                "mild_a_last_fill": mild.last_fill_purpose,
                "mild_a_active": mild.final_active_orders,
                "mild_a_c6_placed": mild.cycle6_long_add_placed,
                "qty_factor_a": mild.cycle5_qty_factor,
                "c5_long_add_qty_base": base.cycle5_long_add_qty,
                "c5_long_add_qty_mild": mild.cycle5_long_add_qty,
                "sr_wait_candles_base": base.sr_wait_candles,
                "sr_wait_candles_mild": mild.sr_wait_candles,
                "sr_miss_bps_base": base.sr_miss_distance_bps,
                "sr_miss_bps_mild": mild.sr_miss_distance_bps,
            }
        )

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, indent=2))
    print(f"Analyzed {len(stuck_starts)} starts x {len(VARIANT_CONFIGS)} variants -> {args.output_json}")
    print("\nCycle-6 placement rate:")
    for variant, stats in payload["cycle6_placement_rate"].items():
        print(f"  {variant}: placed={stats['placed']} not_placed={stats['not_placed']}")
    print("\nSR miss distance (while active, pre-fill) median bps:")
    for variant, agg in payload["aggregate_sr_miss_bps"].items():
        if agg.get("count"):
            print(f"  {variant}: median={agg['median_bps']:.1f} p75={agg['p75_bps']:.1f} buckets={agg['buckets']}")


if __name__ == "__main__":
    main()

"""Causal start-depth selection helpers for Cobertura research audits.

Percentages are decimal fractions (0.02 == 2%).
Fill model for deeper targets (after baseline):
  - Scan candles with timestamp strictly after baseline fill.
  - If candle open <= target: fill at open (gap already through target).
  - Else if candle low <= target: fill at target (limit/trigger touch; never at low).
  - Else: continue.
This matches conservative short-open semantics (no optimistic low fill).
"""

from __future__ import annotations

from typing import Any, Callable


DEPTH_VARIANTS: tuple[tuple[str, float | None], ...] = (
    ("B0", 0.0),
    ("B2", 0.02),
    ("B4", 0.04),
    ("B6", 0.06),
    ("B8", 0.08),
    ("B10", 0.10),
    ("B12", 0.12),
    ("B15", 0.15),
    ("NO_COBERTURA", None),
)

FILL_MODEL = "open_if_gapped_else_target_on_low_touch"


def target_start_price(*, baseline_start_price: float, depth_pct: float) -> float:
    if depth_pct < 0.0:
        raise ValueError("depth_pct must be >= 0")
    px = float(baseline_start_price)
    if px <= 0.0:
        raise ValueError("baseline_start_price must be > 0")
    return px * (1.0 - float(depth_pct))


def achieved_depth_pct(*, baseline_start_price: float, fill_price: float) -> float:
    base = float(baseline_start_price)
    if base <= 0.0:
        raise ValueError("baseline_start_price must be > 0")
    return (base - float(fill_price)) / base


def select_deeper_start_after_baseline(
    candles: list[dict[str, Any]],
    *,
    baseline_fill_ts: Any,
    baseline_fill_price: float,
    depth_pct: float,
    parse_ts: Callable[[Any], Any],
    horizon_end_ts: Any | None = None,
) -> dict[str, Any]:
    """First causal fill at/below target after baseline (no future-low selection)."""
    if float(depth_pct) <= 0.0:
        raise ValueError("use baseline path for depth_pct<=0")
    target = target_start_price(
        baseline_start_price=baseline_fill_price, depth_pct=depth_pct
    )
    base_ts = parse_ts(baseline_fill_ts)
    end_ts = parse_ts(horizon_end_ts) if horizon_end_ts is not None else None
    scan: list[dict[str, Any]] = []

    for i, c in enumerate(candles):
        ts = parse_ts(c["timestamp"])
        if ts <= base_ts:
            continue
        if end_ts is not None and ts > end_ts:
            break
        o = float(c["open"])
        low = float(c["low"])
        row = {
            "candle_index": i,
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "open": o,
            "high": float(c["high"]),
            "low": low,
            "close": float(c["close"]),
            "target": target,
            "open_le_target": o <= target + 1e-12,
            "low_le_target": low <= target + 1e-12,
        }
        scan.append(row)
        if o <= target + 1e-12:
            return {
                "start_reached": True,
                "fill_timestamp": row["timestamp"],
                "fill_price": o,
                "target_price": target,
                "fill_kind": "gap_open_through_target",
                "fill_model": FILL_MODEL,
                "used_low_as_fill": False,
                "candle_index": i,
                "scan_len": len(scan),
                "scan": scan,
            }
        if low <= target + 1e-12:
            return {
                "start_reached": True,
                "fill_timestamp": row["timestamp"],
                "fill_price": float(target),
                "target_price": target,
                "fill_kind": "low_touch_fill_at_target",
                "fill_model": FILL_MODEL,
                "used_low_as_fill": False,
                "candle_index": i,
                "scan_len": len(scan),
                "scan": scan,
            }

    return {
        "start_reached": False,
        "fill_timestamp": None,
        "fill_price": None,
        "target_price": target,
        "fill_kind": None,
        "fill_model": FILL_MODEL,
        "used_low_as_fill": False,
        "candle_index": None,
        "scan_len": len(scan),
        "scan": scan,
    }


def remaining_downside_pct(*, start_price: float, subsequent_min_price: float) -> float:
    sp = float(start_price)
    if sp <= 0.0:
        raise ValueError("start_price must be > 0")
    return (sp - float(subsequent_min_price)) / sp


def distance_from_long_avg_pct(*, long_avg: float, price: float) -> float:
    """(long_avg - price) / long_avg; positive when price below long avg."""
    la = float(long_avg)
    if la <= 0.0:
        raise ValueError("long_avg must be > 0")
    return (la - float(price)) / la


def classify_baseline_case(
    *,
    remaining_downside_after_baseline: float,
    rebound_from_low_pct: float,
    b0_recovered: bool,
    deeper_any_recovered: bool,
    deeper_any_reached: bool,
    deeper_improves_combined: bool,
    deeper_improves_drawdown_only: bool,
    deeper_all_worse_combined: bool,
) -> str:
    """Primary tag from transparent priority rules (first match wins)."""
    rd = float(remaining_downside_after_baseline)
    rb = float(rebound_from_low_pct)
    if not deeper_any_reached and not b0_recovered:
        # B0 always "reaches"; this means no depth target beyond B0 was hit
        pass
    if not deeper_any_reached:
        return "TARGET_START_NOT_REACHED"
    if rd < 0.02:
        return "START_NEAR_LOW"
    if rb < 0.03:
        return "NO_MEANINGFUL_REBOUND"
    if (not b0_recovered) and deeper_any_recovered and rd >= 0.05:
        return "START_LIKELY_TOO_EARLY"
    if (not b0_recovered) and deeper_any_recovered:
        return "DEEPER_START_RECOVERS"
    if deeper_improves_drawdown_only and not deeper_improves_combined:
        return "DEEPER_START_IMPROVES_ONLY_DRAWDOWN"
    if deeper_all_worse_combined:
        return "DEEPER_START_WORSE"
    if (not b0_recovered) and deeper_any_recovered:
        return "DEEPER_START_RECOVERS"
    if rd >= 0.05 and not b0_recovered:
        return "START_LIKELY_TOO_EARLY"
    if deeper_improves_combined:
        return "DEEPER_START_RECOVERS" if deeper_any_recovered and not b0_recovered else "DEEPER_START_IMPROVES_ONLY_DRAWDOWN"
    return "DEEPER_START_WORSE" if deeper_all_worse_combined else "START_NEAR_LOW"

"""Break → armed wait → depth activation → Cobertura handoff helpers.

Activation is measured from structure_break_level (not from a frozen Cobertura
snapshot). TEM inventory at activation is the last fill with timestamp < cutoff.

Fill model (same conservative rule as start_depth deeper starts):
  open <= target → fill at open (gap)
  else low <= target → fill at target (never at low)
"""

from __future__ import annotations

from typing import Any, Callable

from .historical_blocker_fill_replay import fill_before_signal
from .historical_blocker_state_extraction import parse_ts
from .start_depth import FILL_MODEL, target_start_price

# depth_pct None => control / reference variants
BREAK_DEPTH_VARIANTS: tuple[tuple[str, float | None], ...] = (
    ("BREAK_D0", 0.00),
    ("BREAK_D1", 0.01),
    ("BREAK_D2", 0.02),
    ("BREAK_D3", 0.03),
    ("BREAK_D4", 0.04),
    ("BREAK_D5", 0.05),
    ("BREAK_D6", 0.06),
    ("BREAK_D8", 0.08),
    ("BREAK_D10", 0.10),
    ("BREAK_D12", 0.12),
    ("BREAK_D15", 0.15),
    ("BREAK_D20", 0.20),
    ("NO_COBERTURA_AFTER_BREAK", None),
    ("LEGACY_B0_REFERENCE", None),
)

HANDOFF_ORDER_POLICY = (
    "CANCEL_ALL_OPEN_TEM_ORDERS_ON_HANDOFF: reconcile book from fills strictly "
    "before activation_ts; cancel remaining open TEM intents; Cobertura derives "
    "new orders from handed-off inventory only."
)


def activation_target_price(*, structure_break_price: float, depth_pct: float) -> float:
    return target_start_price(
        baseline_start_price=structure_break_price, depth_pct=depth_pct
    )


def select_activation_after_break(
    candles: list[dict[str, Any]],
    *,
    break_available_ts: Any,
    structure_break_price: float,
    depth_pct: float,
    parse_ts_fn: Callable[[Any], Any] | None = None,
    horizon_end_ts: Any | None = None,
) -> dict[str, Any]:
    """First causal activation at/below break_price*(1-depth) after break availability."""
    pts = parse_ts_fn or parse_ts
    target = activation_target_price(
        structure_break_price=structure_break_price, depth_pct=float(depth_pct)
    )
    avail = pts(break_available_ts)
    if avail is None:
        raise ValueError("break_available_ts unparseable")
    end = pts(horizon_end_ts) if horizon_end_ts is not None else None
    scan: list[dict[str, Any]] = []

    for i, c in enumerate(candles):
        ts = pts(c["timestamp"])
        if ts is None or ts < avail:
            continue
        if end is not None and ts > end:
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
        }
        scan.append(row)
        if o <= target + 1e-12:
            return {
                "activation_reached": True,
                "activation_time": row["timestamp"],
                "activation_price": o,
                "activation_target_price": target,
                "activation_fill_reason": "gap_open",
                "fill_model": FILL_MODEL,
                "used_low_as_fill": False,
                "candle_index": i,
                "first_eligible_candle_time": scan[0]["timestamp"] if scan else None,
                "scan": scan,
            }
        if low <= target + 1e-12:
            return {
                "activation_reached": True,
                "activation_time": row["timestamp"],
                "activation_price": float(target),
                "activation_target_price": target,
                "activation_fill_reason": "intrabar_touch",
                "fill_model": FILL_MODEL,
                "used_low_as_fill": False,
                "candle_index": i,
                "first_eligible_candle_time": scan[0]["timestamp"] if scan else None,
                "scan": scan,
            }

    return {
        "activation_reached": False,
        "activation_time": None,
        "activation_price": None,
        "activation_target_price": target,
        "activation_fill_reason": None,
        "fill_model": FILL_MODEL,
        "used_low_as_fill": False,
        "candle_index": None,
        "scan": scan,
    }


def _f(v: Any, default: float | None = None) -> float | None:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def ledger_rows_before_cutoff(
    ledger: list[dict[str, Any]], cutoff_ts: Any, *, strict: bool = True
) -> list[dict[str, Any]]:
    out = []
    for r in ledger:
        ts = r.get("fill_timestamp") or r.get("timestamp")
        if fill_before_signal(ts, cutoff_ts, strict=strict):
            out.append(r)
    return out


def snapshot_from_ledger(
    ledger: list[dict[str, Any]],
    *,
    cutoff_ts: Any,
    trade_id: str,
    coin: str,
) -> dict[str, Any]:
    """True TEM book at cutoff: last fill with fill_timestamp < cutoff."""
    before = ledger_rows_before_cutoff(ledger, cutoff_ts, strict=True)
    after = [
        r
        for r in ledger
        if not fill_before_signal(
            r.get("fill_timestamp") or r.get("timestamp"), cutoff_ts, strict=True
        )
    ]
    if not before:
        return {
            "trade_id": trade_id,
            "coin": coin,
            "cutoff_ts": str(cutoff_ts),
            "has_fills": False,
            "long_qty": 0.0,
            "short_qty": 0.0,
            "long_avg": 0.0,
            "short_avg": 0.0,
            "realized_pnl": 0.0,
            "fills_before_cutoff": 0,
            "fills_at_or_after_cutoff": len(after),
            "last_fill_time": None,
            "last_fill_price": None,
            "bot_state": None,
            "active_cycle_index": None,
            "open_order_count": 0,
        }
    last = before[-1]
    return {
        "trade_id": trade_id,
        "coin": coin,
        "cutoff_ts": parse_ts(cutoff_ts).isoformat()
        if parse_ts(cutoff_ts)
        else str(cutoff_ts),
        "has_fills": True,
        "long_qty": _f(last.get("long_qty_after"), 0.0) or 0.0,
        "short_qty": _f(last.get("short_qty_after"), 0.0) or 0.0,
        "long_avg": _f(last.get("long_avg_after"), 0.0) or 0.0,
        "short_avg": _f(last.get("short_avg_after"), 0.0) or 0.0,
        "realized_pnl": _f(last.get("realized_pnl_cumulative"), 0.0) or 0.0,
        "fills_before_cutoff": len(before),
        "fills_at_or_after_cutoff": len(after),
        "last_fill_time": last.get("fill_timestamp"),
        "last_fill_price": _f(last.get("fill_price")),
        "bot_state": last.get("bot_state_after"),
        "active_cycle_index": last.get("active_cycle_after") or last.get("cycle_index"),
        "open_order_count": int(float(last.get("active_orders_after_count") or 0)),
        "net_qty": _f(last.get("net_qty_after")),
    }


def path_metrics_between(
    ledger: list[dict[str, Any]],
    candles: list[dict[str, Any]],
    *,
    start_ts: Any,
    end_ts: Any,
) -> dict[str, Any]:
    """TEM activity and price path on [start_ts, end_ts)."""
    st = parse_ts(start_ts)
    et = parse_ts(end_ts)
    fills = []
    for r in ledger:
        ts = parse_ts(r.get("fill_timestamp"))
        if ts is None or st is None:
            continue
        if ts < st:
            continue
        if et is not None and ts >= et:
            continue
        fills.append(r)

    def _purp(r: dict[str, Any]) -> str:
        return str(r.get("purpose") or "").upper()

    long_adds = sum(1 for r in fills if "LONG_ADD" in _purp(r))
    short_adds = sum(1 for r in fills if "SHORT_ADD" in _purp(r) or "SHORT_ENTRY" in _purp(r))
    long_red = sum(1 for r in fills if "LONG_REDUCE" in _purp(r))
    short_red = sum(1 for r in fills if "SHORT_REDUCE" in _purp(r))
    exits = sum(1 for r in fills if "EXIT" in _purp(r) or "TP" in _purp(r) or "SL" in _purp(r))
    refills = sum(1 for r in fills if "REFILL" in _purp(r))

    realized0 = None
    realized1 = None
    # realized at start = last fill before start; at end = last fill before end
    before_start = ledger_rows_before_cutoff(ledger, start_ts, strict=True)
    before_end = ledger_rows_before_cutoff(ledger, end_ts, strict=True)
    if before_start:
        realized0 = _f(before_start[-1].get("realized_pnl_cumulative"), 0.0)
    if before_end:
        realized1 = _f(before_end[-1].get("realized_pnl_cumulative"), 0.0)
    realized_delta = None
    if realized0 is not None and realized1 is not None:
        realized_delta = realized1 - realized0

    mn = mx = None
    for c in candles:
        ts = parse_ts(c["timestamp"])
        if ts is None or st is None:
            continue
        if ts < st:
            continue
        if et is not None and ts >= et:
            break
        low = float(c["low"])
        high = float(c["high"])
        mn = low if mn is None else min(mn, low)
        mx = high if mx is None else max(mx, high)

    return {
        "original_bot_fills_after_break": len(fills),
        "original_bot_long_adds_after_break": long_adds,
        "original_bot_short_adds_after_break": short_adds,
        "original_bot_long_reduces_after_break": long_red,
        "original_bot_short_reduces_after_break": short_red,
        "original_bot_refills_after_break": refills,
        "original_bot_exits_after_break": exits,
        "realized_pnl_between_break_and_activation": realized_delta,
        "minimum_price_between_break_and_activation": mn,
        "maximum_price_between_break_and_activation": mx,
    }


def long_short_spread_pct(*, long_avg: float, short_avg: float) -> float | None:
    if long_avg <= 0 or short_avg <= 0:
        return None
    return (float(long_avg) - float(short_avg)) / float(long_avg)


def classify_handoff_case(
    *,
    activation_reached: bool,
    unresolved_break: bool,
    d0_recovered: bool,
    variant_recovered: bool,
    state_improved: bool,
    state_worsened: bool,
    combined_improved_vs_d0: bool,
    combined_worsened_vs_d0: bool,
    only_post_dd_improved: bool,
    shared_be_worsened: bool,
    no_cobertura_best: bool,
    is_d0: bool,
) -> str:
    if unresolved_break:
        return "UNRESOLVED_STRUCTURE_BREAK"
    if no_cobertura_best:
        return "NO_COBERTURA_BEST"
    if not activation_reached and not is_d0:
        return "ACTIVATION_TARGET_NOT_REACHED"
    if is_d0:
        return "IMMEDIATE_HANDOFF_BEST" if d0_recovered else "NO_ROBUST_HANDOFF_DEPTH"
    if state_worsened:
        primary = "ORIGINAL_BOT_WORSENS_STATE_BEFORE_HANDOFF"
    elif state_improved:
        primary = "ORIGINAL_BOT_IMPROVES_STATE_BEFORE_HANDOFF"
    else:
        primary = None
    if (not d0_recovered) and variant_recovered:
        return "DELAYED_HANDOFF_IMPROVES_RECOVERY"
    if shared_be_worsened and combined_worsened_vs_d0:
        return "DELAYED_HANDOFF_WORSENS_SHARED_BE"
    if only_post_dd_improved and not combined_improved_vs_d0:
        return "DELAYED_HANDOFF_ONLY_REDUCES_POST_ACTIVATION_DD"
    if state_improved and combined_improved_vs_d0:
        return "DELAYED_HANDOFF_IMPROVES_STATE"
    if primary:
        return primary
    if combined_improved_vs_d0:
        return "DELAYED_HANDOFF_IMPROVES_STATE"
    return "NO_ROBUST_HANDOFF_DEPTH"

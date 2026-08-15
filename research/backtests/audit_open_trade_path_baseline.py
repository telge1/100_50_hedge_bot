"""Open-trade path audit for causal baseline Trade 3 (research-only)."""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from fixed_cycle_hedge_bot.hedge_exit_math import calculate_hedge_exit_price

ROOT = Path(__file__).resolve().parents[2]
VARIANT_DIR = ROOT / (
    "research/backtests/results/"
    "long_continuous_tp_0_25_causal_parameter_matrix_20260720/la_0_5_buffer_1_00"
)
OUT_DIR = ROOT / (
    "research/backtests/results/"
    "long_continuous_tp_0_25_causal_parameter_matrix_20260720/open_trade_path_audit"
)

TP_PROFIT_TARGET_PCT = 0.25
TP_BUFFER_PCT = 0.0002
FEE_RATE = 0.00055  # 0.055%


def _sf(value: object, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _cycle_from_purpose(purpose: str) -> int | None:
    match = re.search(r"CYCLE_(\d+)_", purpose or "")
    return int(match.group(1)) if match else None


@dataclass
class PositionState:
    long_qty: float = 0.0
    short_qty: float = 0.0
    long_avg: float = 0.0
    short_avg: float = 0.0
    realized_pnl: float = 0.0


def _unrealized(state: PositionState, mark: float) -> tuple[float, float, float]:
    u_long = (mark - state.long_avg) * state.long_qty if state.long_qty > 0 else 0.0
    u_short = (state.short_avg - mark) * state.short_qty if state.short_qty > 0 else 0.0
    return u_long, u_short, u_long + u_short


def _expected_exit_pnl(
    *,
    long_qty: float,
    short_qty: float,
    long_avg: float,
    short_avg: float,
    exit_price: float,
) -> float:
    if long_qty <= 0 and short_qty <= 0:
        return 0.0
    long_pnl = (exit_price - long_avg) * long_qty
    short_pnl = (short_avg - exit_price) * short_qty
    entry_fee = FEE_RATE * (long_avg * long_qty + short_avg * short_qty)
    close_fee = FEE_RATE * exit_price * (long_qty + short_qty)
    return long_pnl + short_pnl - entry_fee - close_fee


def _decompose_exit_target(
    *,
    long_qty: float,
    short_qty: float,
    long_avg: float,
    short_avg: float,
    realized_cycle_net: float,
    pending_cycle_loss_usdt: float = 0.0,
) -> dict[str, float]:
    comps = calculate_hedge_exit_price(
        long_avg=long_avg,
        long_qty=long_qty,
        short_avg=short_avg,
        short_qty=short_qty,
        tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
        tp_buffer_pct=TP_BUFFER_PCT,
        realized_cycle_net=realized_cycle_net,
        pending_cycle_loss_usdt=pending_cycle_loss_usdt,
        primary_side="long",
    )
    return {
        "math_exit_price": comps.exit_price,
        "profit_basis_usdt": comps.profit_basis_usdt,
        "target_profit_usdt_from_tp_pct": comps.target_profit_usdt,
        "buffer_usdt_from_tp_buffer": comps.buffer_usdt,
        "loss_recovery_usdt": comps.pending_cycle_loss_usdt
        if comps.pending_cycle_loss_usdt > 0
        else max(-realized_cycle_net, 0.0),
        "required_profit_usdt": comps.required_profit_usdt,
        "realized_cycle_net": comps.realized_cycle_net,
    }


def load_trade3_blocks() -> list[dict[str, Any]]:
    path = VARIANT_DIR / "APTUSDT_long_continuous_trade_0003_conservative_live_trade_blocks.json"
    return list(json.loads(path.read_text()).get("trade_blocks") or [])


def load_trade3_result() -> dict[str, Any]:
    path = VARIANT_DIR / "APTUSDT_original_hedge_5m_continuous_results.json"
    doc = json.loads(path.read_text())
    for run in doc.get("runs") or []:
        if int(run.get("trade_number") or 0) == 3:
            return run
    raise RuntimeError("trade 3 not found")


def build_candle_index(candles: list[Any]) -> dict[str, Any]:
    by_ts: dict[str, Any] = {}
    for i, c in enumerate(candles):
        ts = c.timestamp.isoformat() if hasattr(c.timestamp, "isoformat") else str(c.timestamp)
        by_ts[ts] = {"index": i, "candle": c, "ts": ts}
    return by_ts


def analyze() -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    blocks = load_trade3_blocks()
    result = load_trade3_result()
    candles = normalize_candles("APTUSDT", load_candles_for_symbol("APTUSDT", limit=50000))
    candle_by_ts = build_candle_index(candles)

    # Map trade-local candle index to absolute via first fill timestamp.
    start_ts = str(result.get("start_time"))
    # Normalize start_ts
    start_dt = _parse_ts(start_ts)
    trade_start_abs = None
    for ts, meta in candle_by_ts.items():
        if _parse_ts(ts) == start_dt:
            trade_start_abs = meta["index"]
            break
    if trade_start_abs is None:
        # fallback: find closest
        for i, c in enumerate(candles):
            if str(c.timestamp).startswith("2026-01-06") and "21:35" in str(c.timestamp):
                trade_start_abs = i
                break
    assert trade_start_abs is not None

    fills = [r for r in blocks if str(r.get("row_type") or "").lower() == "fill"]
    fills.sort(key=lambda r: (_parse_ts(r.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc), str(r.get("purpose"))))

    state = PositionState()
    position_rows: list[dict[str, Any]] = []
    mtm_rows: list[dict[str, Any]] = []
    active_exit: float | None = None
    exit_history: list[dict[str, Any]] = []

    # Track exit submits from order rows
    for row in blocks:
        purpose = str(row.get("purpose") or "")
        if purpose not in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}:
            continue
        if str(row.get("row_type")) != "order":
            continue
        event = str(row.get("event_type") or "")
        trigger = _sf(row.get("trigger_price") or row.get("price"))
        if event == "submitted" and purpose == "LONG_TP_EXIT":
            exit_history.append(
                {
                    "action": "submit",
                    "timestamp": row.get("timestamp"),
                    "candle_index": row.get("candle_index") or row.get("local_candle_index"),
                    "purpose": purpose,
                    "exit_price": trigger,
                    "qty": _sf(row.get("qty") or row.get("filled_qty")),
                    "order_id": row.get("order_id"),
                    "source_event": row.get("event_type"),
                    "mapping_warning": row.get("mapping_warning"),
                }
            )
            active_exit = trigger
        elif event == "cancelled" and purpose == "LONG_TP_EXIT":
            exit_history.append(
                {
                    "action": "cancel",
                    "timestamp": row.get("timestamp"),
                    "candle_index": row.get("candle_index") or row.get("local_candle_index"),
                    "purpose": purpose,
                    "exit_price": trigger,
                    "qty": _sf(row.get("qty") or row.get("filled_qty")),
                    "order_id": row.get("order_id"),
                    "source_event": row.get("event_type"),
                    "mapping_warning": row.get("mapping_warning"),
                }
            )

    # Final resting exit from export snapshot (may lack matching submit row).
    final_exit_price = None
    for row in blocks:
        if str(row.get("row_type")) != "final_active_order":
            continue
        if str(row.get("purpose") or "") != "LONG_TP_EXIT":
            continue
        final_exit_price = _sf(row.get("trigger_price") or row.get("price"))

    # Rebuild pairs: each cancel+submit at same timestamp
    rebuilds: list[dict[str, Any]] = []
    by_ts: dict[str, list[dict[str, Any]]] = {}
    for item in exit_history:
        by_ts.setdefault(str(item["timestamp"]), []).append(item)
    for ts, items in sorted(by_ts.items(), key=lambda kv: _parse_ts(kv[0]) or datetime.min.replace(tzinfo=timezone.utc)):
        cancels = [i for i in items if i["action"] == "cancel"]
        submits = [i for i in items if i["action"] == "submit"]
        if cancels and submits:
            rebuilds.append(
                {
                    "timestamp": ts,
                    "candle_index": submits[0]["candle_index"],
                    "old_exit_price": cancels[0]["exit_price"],
                    "new_exit_price": submits[0]["exit_price"],
                    "delta_exit": submits[0]["exit_price"] - cancels[0]["exit_price"],
                }
            )
        elif submits and not cancels:
            rebuilds.append(
                {
                    "timestamp": ts,
                    "candle_index": submits[0]["candle_index"],
                    "old_exit_price": None,
                    "new_exit_price": submits[0]["exit_price"],
                    "delta_exit": None,
                }
            )

    # If final active exit differs from last submitted rebuild, append terminal rebuild.
    if final_exit_price is not None:
        last_new = rebuilds[-1]["new_exit_price"] if rebuilds else None
        if last_new is None or abs(final_exit_price - float(last_new)) > 1e-9:
            # Find last cancel at end window if present
            end_ts = str(result.get("end_time"))
            old = last_new
            # Prefer cancel timestamp near end if available
            end_cancels = [
                i for i in exit_history
                if i["action"] == "cancel" and str(i["timestamp"]).startswith("2026-02-23T01:20")
            ]
            if end_cancels:
                end_ts = str(end_cancels[0]["timestamp"])
                old = end_cancels[0]["exit_price"]
            rebuilds.append(
                {
                    "timestamp": end_ts,
                    "candle_index": end_cancels[0]["candle_index"] if end_cancels else None,
                    "old_exit_price": old,
                    "new_exit_price": final_exit_price,
                    "delta_exit": (final_exit_price - old) if old is not None else None,
                }
            )

    # Position timeline from fills
    current_exit = None
    rebuild_idx = 0
    sorted_rebuilds = sorted(rebuilds, key=lambda r: _parse_ts(r["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc))

    def exit_at_or_before(ts: str) -> float | None:
        val = None
        for rb in sorted_rebuilds:
            if (_parse_ts(rb["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc)) <= (
                _parse_ts(ts) or datetime.max.replace(tzinfo=timezone.utc)
            ):
                val = rb["new_exit_price"]
        return val

    for fill in fills:
        purpose = str(fill.get("purpose") or "")
        ts = str(fill.get("timestamp") or "")
        mark = _sf(fill.get("candle_close") or fill.get("fill_price") or fill.get("price") or fill.get("order_check_price"))
        fill_price = _sf(fill.get("fill_price") if fill.get("fill_price") not in (None, "") else fill.get("price") or fill.get("order_check_price"))
        qty = _sf(fill.get("filled_qty") if fill.get("filled_qty") not in (None, "") else fill.get("qty"))
        event_pnl = _sf(
            fill.get("net_realized_pnl_event")
            if fill.get("net_realized_pnl_event") not in (None, "")
            else fill.get("closed_pnl")
        )
        # Prefer explicit after fields when present
        long_qty_after = fill.get("long_qty_after")
        short_qty_after = fill.get("short_qty_after")
        long_avg_after = fill.get("long_avg_after")
        short_avg_after = fill.get("short_avg_after")
        if long_qty_after not in (None, ""):
            state.long_qty = _sf(long_qty_after)
            state.short_qty = _sf(short_qty_after)
            state.long_avg = _sf(long_avg_after)
            state.short_avg = _sf(short_avg_after)
        else:
            # Fallback synthetic update (should rarely happen)
            side = str(fill.get("side") or "").lower()
            reduce_only = bool(fill.get("reduce_only"))
            if side == "long" and not reduce_only:
                new_q = state.long_qty + qty
                state.long_avg = (
                    (state.long_avg * state.long_qty + fill_price * qty) / new_q if new_q > 0 else 0.0
                )
                state.long_qty = new_q
            elif side == "short" and not reduce_only:
                new_q = state.short_qty + qty
                state.short_avg = (
                    (state.short_avg * state.short_qty + fill_price * qty) / new_q if new_q > 0 else 0.0
                )
                state.short_qty = new_q
            elif side == "long" and reduce_only:
                state.long_qty = max(0.0, state.long_qty - qty)
                if state.long_qty <= 1e-12:
                    state.long_avg = 0.0
            elif side == "short" and reduce_only:
                state.short_qty = max(0.0, state.short_qty - qty)
                if state.short_qty <= 1e-12:
                    state.short_avg = 0.0

        state.realized_pnl += event_pnl
        u_long, u_short, u_total = _unrealized(state, mark)
        mtm = state.realized_pnl + u_total
        long_notional = state.long_qty * state.long_avg
        short_notional = state.short_qty * state.short_avg
        net_qty = state.long_qty - state.short_qty
        net_notional = long_notional - short_notional
        current_exit = exit_at_or_before(ts)
        local_ci = fill.get("candle_index") or fill.get("local_candle_index")
        abs_ci = int(local_ci) + trade_start_abs if local_ci is not None else None

        row = {
            "timestamp": ts,
            "candle_index_local": local_ci,
            "candle_index_absolute": abs_ci,
            "event_purpose": purpose,
            "cycle_index": _cycle_from_purpose(purpose),
            "fill_price": fill_price,
            "fill_qty": qty,
            "event_realized_net_pnl": event_pnl,
            "cumulative_realized_net_pnl": state.realized_pnl,
            "long_qty_after": state.long_qty,
            "short_qty_after": state.short_qty,
            "long_avg_after": state.long_avg,
            "short_avg_after": state.short_avg,
            "long_notional": long_notional,
            "short_notional": short_notional,
            "net_qty": net_qty,
            "net_notional": net_notional,
            "mark_price": mark,
            "unrealized_long_pnl": u_long,
            "unrealized_short_pnl": u_short,
            "unrealized_total_pnl": u_total,
            "mtm_pnl": mtm,
            "active_exit_price": current_exit,
            "ls_ratio": (state.long_qty / state.short_qty) if state.short_qty > 1e-12 else None,
            "highlight": purpose
            if any(
                key in purpose
                for key in (
                    "INITIAL_",
                    "LONG_ADD",
                    "SHORT_REDUCE",
                    "SHORT_TP",
                    "REFILL_",
                    "RELOAD",
                    "EXIT",
                )
            )
            else "",
        }
        position_rows.append(row)
        mtm_rows.append(
            {
                "timestamp": ts,
                "candle_index_local": local_ci,
                "purpose": purpose,
                "cycle_index": row["cycle_index"],
                "realized_pnl": state.realized_pnl,
                "unrealized_pnl": u_total,
                "mtm_pnl": mtm,
                "net_qty": net_qty,
                "active_exit_price": current_exit,
                "mark_price": mark,
            }
        )

    # Cycle effects: chronological LONG_ADD / SHORT_REDUCE pairs (handles relabeled CYCLE_1 late fills)
    def prev_row(ts: str) -> dict[str, Any] | None:
        target = _parse_ts(ts)
        prev = None
        for row in position_rows:
            if (_parse_ts(row["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc)) < (
                target or datetime.min.replace(tzinfo=timezone.utc)
            ):
                prev = row
        return prev

    cycle_effects: list[dict[str, Any]] = []
    pending_long_add: dict[str, Any] | None = None
    pair_index = 0
    for row in position_rows:
        purpose = str(row["event_purpose"])
        if purpose.endswith("_LONG_ADD"):
            pending_long_add = row
            continue
        if purpose.endswith("_SHORT_REDUCE") and pending_long_add is not None:
            pair_index += 1
            la = pending_long_add
            sr = row
            before = prev_row(str(la["timestamp"]))
            after = sr
            before_exit = before.get("active_exit_price") if before else None
            after_exit = after.get("active_exit_price")
            duration = None
            t0 = _parse_ts(la["timestamp"])
            t1 = _parse_ts(sr["timestamp"])
            if t0 and t1:
                duration = (t1 - t0).total_seconds() / 300.0
            labeled_cycle = la.get("cycle_index")
            cycle_effects.append(
                {
                    "pair_index": pair_index,
                    "cycle_label": labeled_cycle,
                    "cycle": labeled_cycle,
                    "long_add_timestamp": la.get("timestamp"),
                    "short_reduce_timestamp": sr.get("timestamp"),
                    "first_leg_loss": abs(min(_sf(la.get("event_realized_net_pnl")), 0.0)),
                    "second_leg_gain": _sf(sr.get("event_realized_net_pnl")),
                    "cycle_net": _sf(la.get("event_realized_net_pnl")) + _sf(sr.get("event_realized_net_pnl")),
                    "long_qty_before": (before or {}).get("long_qty_after"),
                    "short_qty_before": (before or {}).get("short_qty_after"),
                    "long_qty_after_cycle": after.get("long_qty_after"),
                    "short_qty_after_cycle": after.get("short_qty_after"),
                    "delta_long_qty": after.get("long_qty_after") - _sf((before or {}).get("long_qty_after")),
                    "delta_short_qty": after.get("short_qty_after") - _sf((before or {}).get("short_qty_after")),
                    "delta_net_qty": after.get("net_qty") - _sf((before or {}).get("net_qty")),
                    "exit_before": before_exit,
                    "exit_after": after_exit,
                    "delta_exit": (after_exit - before_exit)
                    if after_exit is not None and before_exit is not None
                    else None,
                    "mtm_before": (before or {}).get("mtm_pnl"),
                    "mtm_after": after.get("mtm_pnl"),
                    "delta_mtm": after.get("mtm_pnl") - _sf((before or {}).get("mtm_pnl")),
                    "second_leg_duration_candles_approx": duration,
                    "complete": True,
                    "long_add_purpose": la.get("event_purpose"),
                    "short_reduce_purpose": sr.get("event_purpose"),
                }
            )
            pending_long_add = None

    # Terminal mark-to-market at series end (result.final_price).
    if position_rows:
        last = position_rows[-1]
        end_mark = _sf(result.get("final_price"))
        u_long, u_short, u_total = _unrealized(
            PositionState(
                long_qty=_sf(result.get("final_long_qty"), last["long_qty_after"]),
                short_qty=_sf(result.get("final_short_qty"), last["short_qty_after"]),
                long_avg=_sf(result.get("final_long_avg_price"), last["long_avg_after"]),
                short_avg=_sf(result.get("final_short_avg_price"), last["short_avg_after"]),
                realized_pnl=_sf(result.get("realized_pnl"), last["cumulative_realized_net_pnl"]),
            ),
            end_mark,
        )
        realized = _sf(result.get("realized_pnl"), last["cumulative_realized_net_pnl"])
        terminal = {
            **last,
            "timestamp": str(result.get("end_time")),
            "event_purpose": "SERIES_END_MARK",
            "cycle_index": None,
            "fill_price": end_mark,
            "fill_qty": 0.0,
            "event_realized_net_pnl": 0.0,
            "cumulative_realized_net_pnl": realized,
            "long_qty_after": _sf(result.get("final_long_qty"), last["long_qty_after"]),
            "short_qty_after": _sf(result.get("final_short_qty"), last["short_qty_after"]),
            "long_avg_after": _sf(result.get("final_long_avg_price"), last["long_avg_after"]),
            "short_avg_after": _sf(result.get("final_short_avg_price"), last["short_avg_after"]),
            "mark_price": end_mark,
            "unrealized_long_pnl": u_long,
            "unrealized_short_pnl": u_short,
            "unrealized_total_pnl": u_total,
            "mtm_pnl": realized + u_total,
            "active_exit_price": final_exit_price if final_exit_price is not None else last.get("active_exit_price"),
            "highlight": "SERIES_END_MARK",
        }
        terminal["long_notional"] = terminal["long_qty_after"] * terminal["long_avg_after"]
        terminal["short_notional"] = terminal["short_qty_after"] * terminal["short_avg_after"]
        terminal["net_qty"] = terminal["long_qty_after"] - terminal["short_qty_after"]
        terminal["net_notional"] = terminal["long_notional"] - terminal["short_notional"]
        terminal["ls_ratio"] = (
            terminal["long_qty_after"] / terminal["short_qty_after"]
            if terminal["short_qty_after"] > 1e-12
            else None
        )
        position_rows.append(terminal)
        mtm_rows.append(
            {
                "timestamp": terminal["timestamp"],
                "candle_index_local": None,
                "purpose": "SERIES_END_MARK",
                "cycle_index": None,
                "realized_pnl": realized,
                "unrealized_pnl": u_total,
                "mtm_pnl": realized + u_total,
                "net_qty": terminal["net_qty"],
                "active_exit_price": terminal["active_exit_price"],
                "mark_price": end_mark,
            }
        )


    # Exit rebuild timeline + touch audit using remaining candles
    exit_rebuild_rows: list[dict[str, Any]] = []
    exit_touch_rows: list[dict[str, Any]] = []

    def future_window(from_ts: str) -> list[Any]:
        dt = _parse_ts(from_ts)
        out = []
        for c in candles:
            cts = c.timestamp if getattr(c.timestamp, "tzinfo", None) else c.timestamp
            cdt = c.timestamp if isinstance(c.timestamp, datetime) else _parse_ts(c.timestamp)
            if cdt and dt and cdt > dt:
                out.append(c)
        return out

    # Attach position snapshot at each rebuild
    for rb in sorted_rebuilds:
        ts = str(rb["timestamp"])
        # position after events at this timestamp (last fill at or before)
        snap = None
        for row in position_rows:
            if (_parse_ts(row["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc)) <= (
                _parse_ts(ts) or datetime.min.replace(tzinfo=timezone.utc)
            ):
                snap = row
        if snap is None and position_rows:
            snap = position_rows[0]
        long_qty = _sf((snap or {}).get("long_qty_after"))
        short_qty = _sf((snap or {}).get("short_qty_after"))
        long_avg = _sf((snap or {}).get("long_avg_after"))
        short_avg = _sf((snap or {}).get("short_avg_after"))
        realized = _sf((snap or {}).get("cumulative_realized_net_pnl"))
        decomp = _decompose_exit_target(
            long_qty=long_qty,
            short_qty=short_qty,
            long_avg=long_avg,
            short_avg=short_avg,
            realized_cycle_net=realized,
        )
        # Minimal mathematical break-even exit (required_profit = 0 beyond covering fees via comps with 0 targets)
        be_comps = calculate_hedge_exit_price(
            long_avg=long_avg,
            long_qty=long_qty,
            short_avg=short_avg,
            short_qty=short_qty,
            tp_profit_target_pct=0.0,
            tp_buffer_pct=0.0,
            realized_cycle_net=realized,
            pending_cycle_loss_usdt=0.0,
            primary_side="long",
        )
        new_exit = rb["new_exit_price"]
        expected = _expected_exit_pnl(
            long_qty=long_qty,
            short_qty=short_qty,
            long_avg=long_avg,
            short_avg=short_avg,
            exit_price=new_exit,
        )
        future = future_window(ts)
        max_high = max((float(c.high) for c in future), default=None)
        min_low = min((float(c.low) for c in future), default=None)
        touched = False
        touch_candle = None
        best_gap = None
        best_gap_pct = None
        candles_to_best = None
        if new_exit is not None and future:
            # long TP / short SL typically need price to rise to exit for long-primary basket
            for i, c in enumerate(future):
                gap = new_exit - float(c.high)
                gap_pct = gap / new_exit * 100.0 if new_exit else None
                if best_gap is None or gap < best_gap:
                    best_gap = gap
                    best_gap_pct = gap_pct
                    candles_to_best = i + 1
                    touch_candle = c.timestamp.isoformat() if hasattr(c.timestamp, "isoformat") else str(c.timestamp)
                if float(c.high) >= new_exit:
                    touched = True
                    touch_candle = c.timestamp.isoformat() if hasattr(c.timestamp, "isoformat") else str(c.timestamp)
                    candles_to_best = i + 1
                    best_gap = 0.0
                    best_gap_pct = 0.0
                    break

        # Would old exit have been touched later?
        old_exit = rb.get("old_exit_price")
        old_would_touch = None
        old_touch_at = None
        if old_exit is not None and future:
            old_would_touch = False
            for c in future:
                if float(c.high) >= old_exit:
                    old_would_touch = True
                    old_touch_at = c.timestamp.isoformat() if hasattr(c.timestamp, "isoformat") else str(c.timestamp)
                    break

        exit_rebuild_rows.append(
            {
                "timestamp": ts,
                "candle_index": rb.get("candle_index"),
                "old_exit_price": old_exit,
                "new_exit_price": new_exit,
                "delta_exit": rb.get("delta_exit"),
                "long_qty": long_qty,
                "short_qty": short_qty,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "realized_pnl_at_rebuild": realized,
                "mtm_at_rebuild": (snap or {}).get("mtm_pnl"),
                "expected_exit_pnl": expected,
                "math_exit_price": decomp["math_exit_price"],
                "break_even_exit_price": be_comps.exit_price,
                "diff_set_vs_breakeven": new_exit - be_comps.exit_price if new_exit is not None else None,
                "contrib_tp_profit_target_usdt": decomp["target_profit_usdt_from_tp_pct"],
                "contrib_tp_buffer_usdt": decomp["buffer_usdt_from_tp_buffer"],
                "contrib_loss_recovery_usdt": decomp["loss_recovery_usdt"],
                "required_profit_usdt": decomp["required_profit_usdt"],
                "future_max_high": max_high,
                "future_min_low": min_low,
                "exit_touched_later": touched,
                "touch_or_best_timestamp": touch_candle,
                "best_gap_usdt": best_gap,
                "best_gap_pct": best_gap_pct,
                "candles_to_best_or_touch": candles_to_best,
                "old_exit_would_touch_later": old_would_touch,
                "old_exit_touch_timestamp": old_touch_at,
                "replaced_reachable_with_unreachable": bool(
                    old_would_touch is True and touched is False
                ),
            }
        )
        exit_touch_rows.append(
            {
                "timestamp": ts,
                "exit_price": new_exit,
                "old_exit_price": old_exit,
                "future_max_high": max_high,
                "touched": touched,
                "best_gap_usdt": best_gap,
                "best_gap_pct": best_gap_pct,
                "candles_to_best_or_touch": candles_to_best,
                "old_exit_would_touch_later": old_would_touch,
                "replaced_reachable_with_unreachable": bool(
                    old_would_touch is True and touched is False
                ),
            }
        )

    # Critical transition: first point after which MTM stays negative and never recovers to >=0
    critical = None
    for i, row in enumerate(position_rows):
        if row["mtm_pnl"] >= 0:
            continue
        # check future never recovers
        recovers = any(r["mtm_pnl"] >= 0 for r in position_rows[i + 1 :])
        if recovers:
            continue
        # also require subsequent path stays mostly negative
        critical = {
            "reason": "mtm_turns_negative_and_never_recovers_to_breakeven",
            "timestamp": row["timestamp"],
            "candle_index_local": row["candle_index_local"],
            "candle_index_absolute": row["candle_index_absolute"],
            "event_purpose": row["event_purpose"],
            "cycle_index": row["cycle_index"],
            "position_before": position_rows[i - 1] if i > 0 else None,
            "position_after": row,
            "exit_before": (position_rows[i - 1] or {}).get("active_exit_price") if i > 0 else None,
            "exit_after": row.get("active_exit_price"),
            "mtm_before": (position_rows[i - 1] or {}).get("mtm_pnl") if i > 0 else None,
            "mtm_after": row.get("mtm_pnl"),
        }
        break

    # Largest single MTM deterioration event
    worst_delta = None
    for i in range(1, len(position_rows)):
        delta = position_rows[i]["mtm_pnl"] - position_rows[i - 1]["mtm_pnl"]
        if worst_delta is None or delta < worst_delta["delta_mtm"]:
            worst_delta = {
                "timestamp": position_rows[i]["timestamp"],
                "event_purpose": position_rows[i]["event_purpose"],
                "cycle_index": position_rows[i]["cycle_index"],
                "delta_mtm": delta,
                "mtm_before": position_rows[i - 1]["mtm_pnl"],
                "mtm_after": position_rows[i]["mtm_pnl"],
                "exit_before": position_rows[i - 1].get("active_exit_price"),
                "exit_after": position_rows[i].get("active_exit_price"),
                "net_qty_before": position_rows[i - 1]["net_qty"],
                "net_qty_after": position_rows[i]["net_qty"],
                "long_qty_after": position_rows[i]["long_qty_after"],
                "short_qty_after": position_rows[i]["short_qty_after"],
            }

    # Harmful rebuilds
    harmful_rebuilds = [r for r in exit_rebuild_rows if r.get("replaced_reachable_with_unreachable")]

    final = position_rows[-1] if position_rows else {}
    final_state = {
        "trade_number": 3,
        "variant": "la_0_5_buffer_1_00",
        "long_fill_distance_pct": 0.5,
        "target_profit_usdt": 0.015,
        "tp_profit_target_pct": 0.25,
        "start_time": result.get("start_time"),
        "end_time": result.get("end_time"),
        "final_status": result.get("final_status"),
        "exit_reason": result.get("exit_reason"),
        "realized_pnl": result.get("realized_pnl"),
        "unrealized_pnl": result.get("unrealized_pnl"),
        "overall_pnl_mtm": result.get("overall_pnl"),
        "series_mtm_including_closed_trades": _sf(result.get("overall_pnl"))
        + 0.2772727607850091
        + 0.1879178242300039,
        "final_long_qty": result.get("final_long_qty"),
        "final_short_qty": result.get("final_short_qty"),
        "final_long_avg": result.get("final_long_avg_price"),
        "final_short_avg": result.get("final_short_avg_price"),
        "final_mark": result.get("final_price"),
        "final_active_orders": result.get("final_active_order_purposes"),
        "timeline_final_mtm": final.get("mtm_pnl"),
        "timeline_final_unreal": final.get("unrealized_total_pnl"),
        "n_fills": len(fills),
        "n_exit_rebuilds": len(exit_rebuild_rows),
        "n_harmful_rebuilds_reachable_to_unreachable": len(harmful_rebuilds),
        "cycles_completed_in_fills": [c.get("cycle") for c in cycle_effects],
        "cycle_pair_count": len(cycle_effects),
        "unrealized_decomposition_final": {
            "long": final.get("unrealized_long_pnl"),
            "short": final.get("unrealized_short_pnl"),
            "total": final.get("unrealized_total_pnl"),
            "note": "short unrealized uses (short_avg - mark)*short_qty; long uses (mark-long_avg)*long_qty",
        },
    }

    # Root cause classification
    root_causes: list[str] = []
    if harmful_rebuilds:
        root_causes.append("Exit-Rebuild verschlechtert einen zuvor erreichbaren Exit")
    final_exit = exit_rebuild_rows[-1]["new_exit_price"] if exit_rebuild_rows else None
    if final_exit is not None and exit_rebuild_rows[-1].get("exit_touched_later") is False:
        root_causes.append("echter Marktpfad erreicht Exit nie")
    if final.get("net_qty", 0) > 5:
        root_causes.append("Rest-Hedge wird netto directional")
        root_causes.append("Mengenverhältnis driftet ungünstig")
    # Cycles individually profitable?
    cycle_nets = [c["cycle_net"] for c in cycle_effects if c.get("complete")]
    if cycle_nets and sum(1 for n in cycle_nets if n >= -1e-6) >= max(1, len(cycle_nets) - 2) and final.get("mtm_pnl", 0) < 0:
        root_causes.append("einzelne Cycles decken ihre Verluste, aber nicht den Gesamt-MTM")
        root_causes.append("Coverage-Formel ignoriert offenen Positionsverlust")
    if final.get("unrealized_long_pnl", 0) < -1 and abs(final.get("unrealized_long_pnl", 0)) > abs(
        final.get("unrealized_short_pnl", 0) or 0.0
    ):
        root_causes.append("Average-Preis verbessert sich nicht ausreichend")
    # Near-miss evidence
    if any(
        r.get("replaced_reachable_with_unreachable") and _sf(r.get("best_gap_usdt")) < 0.01
        for r in exit_rebuild_rows
    ):
        root_causes.append("Exit-Rebuild verschlechtert einen zuvor erreichbaren Exit")

    critical_payload = {
        "critical_transition": critical,
        "worst_mtm_delta_event": worst_delta,
        "harmful_rebuilds": harmful_rebuilds,
        "root_cause_categories": root_causes,
        "final_exit_state": exit_rebuild_rows[-1] if exit_rebuild_rows else None,
    }

    def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            path.write_text("")
            return
        # flatten nested
        flat_rows = []
        for row in rows:
            flat = {}
            for k, v in row.items():
                if isinstance(v, dict):
                    flat[k] = json.dumps(v, default=str)
                else:
                    flat[k] = v
            flat_rows.append(flat)
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(flat_rows[0].keys()))
            writer.writeheader()
            writer.writerows(flat_rows)

    write_csv(OUT_DIR / "position_timeline.csv", position_rows)
    write_csv(OUT_DIR / "cycle_effects.csv", cycle_effects)
    write_csv(OUT_DIR / "exit_rebuild_timeline.csv", exit_rebuild_rows)
    write_csv(OUT_DIR / "exit_touch_audit.csv", exit_touch_rows)
    write_csv(OUT_DIR / "mtm_timeline.csv", mtm_rows)
    (OUT_DIR / "critical_transition.json").write_text(
        json.dumps(critical_payload, indent=2, default=str) + "\n"
    )
    (OUT_DIR / "final_state.json").write_text(json.dumps(final_state, indent=2, default=str) + "\n")

    report = build_report(
        final_state=final_state,
        critical=critical_payload,
        cycle_effects=cycle_effects,
        exit_rebuild_rows=exit_rebuild_rows,
        position_rows=position_rows,
    )
    (OUT_DIR / "REPORT.md").write_text(report)
    return {"out": str(OUT_DIR), "critical": critical, "worst": worst_delta, "harmful": len(harmful_rebuilds)}


def build_report(
    *,
    final_state: dict[str, Any],
    critical: dict[str, Any],
    cycle_effects: list[dict[str, Any]],
    exit_rebuild_rows: list[dict[str, Any]],
    position_rows: list[dict[str, Any]],
) -> str:
    ct = critical.get("critical_transition") or {}
    worst = critical.get("worst_mtm_delta_event") or {}
    harmful = critical.get("harmful_rebuilds") or []
    lines: list[str] = []
    lines.append("# Open Trade Path Audit — Baseline `la_0_5_buffer_1_00` Trade 3\n")
    lines.append("## Final state\n")
    lines.append(f"- Realized: `{final_state['realized_pnl']}`")
    lines.append(f"- Unrealized: `{final_state['unrealized_pnl']}`")
    lines.append(f"- Trade MTM (`overall_pnl`): `{final_state['overall_pnl_mtm']}`")
    lines.append(
        f"- Series MTM (all 3 trades): `{final_state['series_mtm_including_closed_trades']}`"
    )
    lines.append(
        f"- Position: long `{final_state['final_long_qty']}` @ `{final_state['final_long_avg']}` / "
        f"short `{final_state['final_short_qty']}` @ `{final_state['final_short_avg']}`"
    )
    lines.append(f"- Mark: `{final_state['final_mark']}`")
    lines.append(f"- Active exits: `{final_state['final_active_orders']}`")
    lines.append(f"- Exit reason: `{final_state['exit_reason']}`\n")

    lines.append("## Why is the trade open?\n")
    last = exit_rebuild_rows[-1] if exit_rebuild_rows else {}
    lines.append(
        f"Final basket exit sits at `{last.get('new_exit_price')}` while series end mark is "
        f"`{final_state['final_mark']}`. Future max high after last rebuild: "
        f"`{last.get('future_max_high')}`. Touched later: `{last.get('exit_touched_later')}`."
    )
    lines.append(
        "Trade ends as `series_end_with_open_positions` with resting `LONG_TP_EXIT` / `SHORT_SL_EXIT` "
        "never filled.\n"
    )

    lines.append("## Critical transition\n")
    if ct:
        lines.append(f"- Timestamp: `{ct.get('timestamp')}`")
        lines.append(f"- Event: `{ct.get('event_purpose')}` cycle `{ct.get('cycle_index')}`")
        lines.append(f"- MTM before → after: `{ct.get('mtm_before')}` → `{ct.get('mtm_after')}`")
        lines.append(f"- Exit before → after: `{ct.get('exit_before')}` → `{ct.get('exit_after')}`")
        before = ct.get("position_before") or {}
        after = ct.get("position_after") or {}
        lines.append(
            f"- Net qty before → after: `{before.get('net_qty')}` → `{after.get('net_qty')}`"
        )
        lines.append(
            f"- Long/Short after: `{after.get('long_qty_after')}` / `{after.get('short_qty_after')}`"
        )
    else:
        lines.append("- No permanent negative-MTM transition found (unexpected).")
    lines.append("")

    lines.append("## Largest single deterioration\n")
    lines.append(f"- Timestamp: `{worst.get('timestamp')}`")
    lines.append(f"- Event: `{worst.get('event_purpose')}` cycle `{worst.get('cycle_index')}`")
    lines.append(f"- ΔMTM: `{worst.get('delta_mtm')}`")
    lines.append(f"- Exit before → after: `{worst.get('exit_before')}` → `{worst.get('exit_after')}`\n")

    lines.append("## Exit rebuild reachability\n")
    lines.append(f"- Rebuild count: `{len(exit_rebuild_rows)}`")
    lines.append(f"- Rebuilds that replaced a later-reachable exit with an unreachable one: `{len(harmful)}`")
    for h in harmful:
        lines.append(
            f"- `{h['timestamp']}`: old `{h['old_exit_price']}` would touch later "
            f"(`{h.get('old_exit_touch_timestamp')}`), new `{h['new_exit_price']}` never touched "
            f"(best gap `{h.get('best_gap_usdt')}` / `{h.get('best_gap_pct')}%`)"
        )
    lines.append("")

    lines.append("## Cycle effects (summary)\n")
    lines.append("| cycle | first loss | second gain | cycle net | Δnet qty | Δexit | ΔMTM | complete |")
    lines.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |")
    for c in cycle_effects:
        lines.append(
            f"| {c['cycle']} | {c['first_leg_loss']:.4f} | {c.get('second_leg_gain')} | "
            f"{c['cycle_net']:.4f} | {c['delta_net_qty']} | {c.get('delta_exit')} | "
            f"{c.get('delta_mtm')} | {c['complete']} |"
        )
    lines.append("")
    profitable = [c for c in cycle_effects if c.get("complete") and c["cycle_net"] >= -1e-9]
    lines.append(
        f"Completed cycles with non-negative cycle-net: `{len(profitable)}/{sum(1 for c in cycle_effects if c.get('complete'))}`."
    )
    lines.append(
        "Cycles can be locally covered while still leaving a long-heavy residual that marks against the market.\n"
    )

    # Unrealized decomposition
    final_row = position_rows[-1]
    lines.append("## Open loss composition\n")
    lines.append(f"- Unrealized long: `{final_row.get('unrealized_long_pnl')}`")
    lines.append(f"- Unrealized short: `{final_row.get('unrealized_short_pnl')}`")
    lines.append(f"- Net qty: `{final_row.get('net_qty')}` (long-heavy if > 0)")
    lines.append(
        f"- Long avg `{final_row.get('long_avg_after')}` vs short avg `{final_row.get('short_avg_after')}` "
        f"vs mark `{final_row.get('mark_price')}`\n"
    )

    lines.append("## Root-cause classification\n")
    seen_rc: set[str] = set()
    for item in critical.get("root_cause_categories") or []:
        if item in seen_rc:
            continue
        seen_rc.add(item)
        lines.append(f"- {item}")
    lines.append("")
    lines.append("### Near-miss detail (first harmful rebuild)\n")
    if harmful:
        h0 = harmful[0]
        lines.append(
            f"At `{h0['timestamp']}` exit was raised `{h0['old_exit_price']}` → `{h0['new_exit_price']}`. "
            f"Subsequent market high reached `{h0.get('future_max_high')}` "
            f"(gap `{h0.get('best_gap_usdt')}` ≈ `{h0.get('best_gap_pct')}%`). "
            f"The cancelled lower exit `{h0['old_exit_price']}` would have been touched at "
            f"`{h0.get('old_exit_touch_timestamp')}`."
        )
    lines.append("")

    lines.append("## Answers\n")
    lines.append("1. **Why open?** Final exit never touched before series end; resting TP/SL remain.")
    lines.append(
        f"2. **When structural problem began?** `{ct.get('timestamp')}` / `{ct.get('event_purpose')}` "
        "(MTM turns negative permanently)."
    )
    lines.append(
        f"3. **Largest worsening event?** `{worst.get('timestamp')}` `{worst.get('event_purpose')}` "
        f"ΔMTM=`{worst.get('delta_mtm')}`."
    )
    if harmful:
        lines.append(
            f"4. **Earlier exit reachable before replace?** YES — e.g. `{harmful[0]['timestamp']}` "
            f"old `{harmful[0]['old_exit_price']}` would have been touched later."
        )
    else:
        lines.append("4. **Earlier exit reachable before replace?** No such rebuild detected.")
    lines.append(
        "5. **Cycles locally correct but globally harmful?** YES — covered cycle nets coexist with "
        "rising long-heavy residual and worsening mark-to-market."
    )
    lines.append(
        "6. **Strategy vs implementation?** Primarily **strategy/exit-management design**: "
        "coverage is cycle-local while basket exit ignores sustained open inventory MTM; "
        "plus exit rebuilds that raise the target after a reachable lower exit. "
        "Not a same-candle causality bug (deferred second legs observed)."
    )
    lines.append(
        "7. **Next isolated investigation:** Exit-rebuild policy — specifically when/why "
        "`LONG_TP_EXIT` is raised after cycle fills (reachable → unreachable transitions), "
        "and whether basket exit should incorporate open inventory MTM / net directional residual.\n"
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    summary = analyze()
    print(json.dumps(summary, indent=2, default=str))

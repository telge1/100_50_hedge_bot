"""Run-level metrics and reversal-stress outcomes for Cobertura A/B comparison."""

from __future__ import annotations

from typing import Any

from .engine import EngineResult


def _f(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    val = row.get(key)
    if val is None or val == "":
        return float(default)
    return float(val)


def build_equity_curve(result: EngineResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    max_overlay = 0.0
    min_econ = None
    for i, bar in enumerate(result.per_bar_trace):
        ov = _f(bar, "overlay_short_qty") + _f(bar, "overlay_long_qty")
        max_overlay = max(max_overlay, ov)
        econ = _f(bar, "total_exit_economics")
        min_econ = econ if min_econ is None else min(min_econ, econ)
        rows.append(
            {
                "bar_index": i,
                "timestamp": bar.get("timestamp"),
                "close": _f(bar, "close"),
                "total_exit_economics": econ,
                "realized_overlay_pnl": _f(bar, "realized_overlay_pnl"),
                "overlay_open_pnl": _f(bar, "overlay_open_pnl"),
                "core_open_pnl": _f(bar, "core_open_pnl"),
                "overlay_short_qty": _f(bar, "overlay_short_qty"),
                "overlay_short_avg": _f(bar, "overlay_short_avg"),
                "net_qty": _f(bar, "net_qty"),
                "cumulative_fees": _f(bar, "cumulative_fees"),
                "state": bar.get("state"),
            }
        )
    return rows


def compute_policy_metrics(result: EngineResult) -> dict[str, Any]:
    cfg = result.cfg
    ledger = result.ledger
    trace = result.per_bar_trace
    fills = result.fill_events

    add_fills = [f for f in fills if f.get("kind") == "overlay_short_add"]
    tp_closes = [
        f
        for f in fills
        if f.get("kind") in ("overlay_tp_close", "overlay_be_close")
    ]
    tp_partials = [f for f in fills if f.get("kind") == "overlay_tp_partial"]
    tp_events = len(tp_closes) + len(tp_partials)

    max_overlay_qty = 0.0
    max_overlay_notional = 0.0
    max_adverse = None
    for bar in trace:
        ov = _f(bar, "overlay_short_qty") + _f(bar, "overlay_long_qty")
        px = _f(bar, "close")
        max_overlay_qty = max(max_overlay_qty, ov)
        max_overlay_notional = max(max_overlay_notional, ov * px)
        econ = _f(bar, "total_exit_economics")
        max_adverse = econ if max_adverse is None else min(max_adverse, econ)

    # Final economics: last pre-flat gate if recovered, else last bar.
    final_econ = None
    unrealized_at_exit = 0.0
    recovered = result.state in ("RECOVERED", "RECOVERED_BE")
    if recovered:
        for ev in result.order_events:
            if ev.get("event") == "full_exit":
                final_econ = float(ev.get("total_exit_economics_pre"))
                break
        # Unrealized overlay at exit is 0 after full close; report last open overlay
        # MTM from the bar before recovery if available.
        for bar in reversed(trace):
            if bar.get("state") not in ("RECOVERED", "RECOVERED_BE"):
                unrealized_at_exit = _f(bar, "overlay_open_pnl")
                if final_econ is None:
                    final_econ = _f(bar, "total_exit_economics")
                break
    else:
        if trace:
            final_econ = _f(trace[-1], "total_exit_economics")
            unrealized_at_exit = _f(trace[-1], "overlay_open_pnl")

    core_q = min(ledger.core_long.qty, ledger.core_short.qty)
    if core_q > 0:
        locked_final = core_q * (ledger.core_long.avg - ledger.core_short.avg)
        final_long_avg = ledger.core_long.avg
        final_short_avg = ledger.core_short.avg
    else:
        # After full exit, report initial locked loss / last known core avgs from freeze.
        locked_final = result.locked_spread_loss
        final_long_avg = float(cfg.core_long_avg)
        final_short_avg = float(cfg.core_short_avg)

    avg_dist = None
    if final_short_avg > 0:
        avg_dist = (final_long_avg - final_short_avg) / final_short_avg

    hours = result.bars_processed * 5.0 / 60.0
    core_qty = float(cfg.core_qty())
    ratio = (max_overlay_qty / core_qty) if core_qty > 0 else None

    return {
        "variant_id": cfg.run_id or cfg.overlay_exit_policy,
        "overlay_exit_policy": cfg.overlay_exit_policy,
        "individual_tp_pct": cfg.individual_tp_pct
        if cfg.overlay_exit_policy.startswith("individual_tp")
        else None,
        "final_status": result.state,
        "exit_reason": result.exit_reason,
        "final_total_economics_usdt": final_econ,
        "locked_spread_loss_initial_usdt": result.locked_spread_loss,
        "locked_spread_loss_final_usdt": locked_final,
        "realized_overlay_pnl_usdt": ledger.realized_overlay_pnl,
        "unrealized_overlay_pnl_at_exit_usdt": unrealized_at_exit,
        "total_open_fees_usdt": ledger.cumulative_entry_fees,
        "total_close_fees_usdt": ledger.cumulative_close_fees,
        "total_slippage_cost_estimate_usdt": ledger.cumulative_slippage_costs,
        "number_of_adds": len(add_fills),
        "number_of_tp_closes": len(tp_closes),
        "number_of_partial_tp_closes": len(tp_partials),
        "number_of_tp_events": tp_events,
        "number_of_overlay_rounds": result.recovery_rounds,
        "max_overlay_qty": max_overlay_qty,
        "max_overlay_notional_usdt": max_overlay_notional,
        "max_overlay_to_core_ratio": ratio,
        "max_adverse_total_economics_usdt": max_adverse,
        "recovery_duration_bars": result.bars_processed,
        "recovery_duration_hours": hours,
        "unresolved_overlay_qty_at_end": ledger.overlay_short.qty
        + ledger.overlay_long.qty,
        "core_long_qty_final": ledger.core_long.qty,
        "core_short_qty_final": ledger.core_short.qty,
        "net_exposure_final": ledger.net_qty(),
        "final_long_avg": final_long_avg,
        "final_short_avg": final_short_avg,
        "final_long_short_avg_distance_pct": avg_dist,
        "safety_violation_count": int(
            result.integrity.get("safety_violation_count", 0)
        ),
    }


def compute_reversal_stress(result: EngineResult) -> dict[str, Any]:
    """Outcome-only metrics: largest loss after a local low then recovery.

    No lookahead in decisions — this only summarizes the realized path.
    """
    trace = result.per_bar_trace
    empty = {
        "variant_id": result.cfg.run_id or result.cfg.overlay_exit_policy,
        "overlay_exit_policy": result.cfg.overlay_exit_policy,
        "local_low_timestamp": None,
        "local_low_price": None,
        "max_loss_after_low_usdt": None,
        "overlay_qty_at_strongest_reversal": None,
        "realized_overlay_before_reversal_usdt": None,
        "economics_at_return_to_short_avg": None,
        "economics_at_plus_2pct_from_low": None,
        "economics_at_plus_4pct_from_low": None,
        "economics_at_plus_6pct_from_low": None,
    }
    if len(trace) < 3:
        return empty

    # Find global low of close while overlay was open (or entire path).
    low_i = 0
    low_px = _f(trace[0], "close")
    for i, bar in enumerate(trace):
        px = _f(bar, "close")
        if px < low_px:
            low_px = px
            low_i = i

    # After the low, find worst (min) total_exit_economics — "loss after recovery start"
    # and the bar with strongest adverse move after low (max close rebound while econ weak).
    min_econ_after = _f(trace[low_i], "total_exit_economics")
    min_econ_i = low_i
    strongest_rev_i = low_i
    strongest_rev_move = 0.0
    for i in range(low_i, len(trace)):
        econ = _f(trace[i], "total_exit_economics")
        if econ < min_econ_after:
            min_econ_after = econ
            min_econ_i = i
        move = (_f(trace[i], "close") - low_px) / low_px if low_px > 0 else 0.0
        if move > strongest_rev_move:
            strongest_rev_move = move
            strongest_rev_i = i

    loss_after_low = min_econ_after - _f(trace[low_i], "total_exit_economics")

    # Economics when price returns to then-current overlay short avg (first touch after low)
    econ_at_avg = None
    for i in range(low_i, len(trace)):
        avg = _f(trace[i], "overlay_short_avg")
        if avg <= 0:
            continue
        if _f(trace[i], "high") + 1e-12 >= avg:
            econ_at_avg = _f(trace[i], "total_exit_economics")
            break

    def _econ_at_rebound(pct: float) -> float | None:
        target = low_px * (1.0 + pct)
        for i in range(low_i, len(trace)):
            if _f(trace[i], "high") + 1e-12 >= target:
                return _f(trace[i], "total_exit_economics")
        return None

    rev_bar = trace[strongest_rev_i]
    return {
        "variant_id": result.cfg.run_id or result.cfg.overlay_exit_policy,
        "overlay_exit_policy": result.cfg.overlay_exit_policy,
        "individual_tp_pct": result.cfg.individual_tp_pct
        if result.cfg.overlay_exit_policy.startswith("individual_tp")
        else None,
        "local_low_timestamp": trace[low_i].get("timestamp"),
        "local_low_price": low_px,
        "max_loss_after_low_usdt": loss_after_low,
        "worst_economics_after_low_usdt": min_econ_after,
        "worst_economics_after_low_timestamp": trace[min_econ_i].get("timestamp"),
        "overlay_qty_at_strongest_reversal": _f(rev_bar, "overlay_short_qty"),
        "realized_overlay_before_reversal_usdt": _f(
            rev_bar, "realized_overlay_pnl"
        ),
        "strongest_reversal_move_pct": strongest_rev_move,
        "economics_at_return_to_short_avg": econ_at_avg,
        "economics_at_plus_2pct_from_low": _econ_at_rebound(0.02),
        "economics_at_plus_4pct_from_low": _econ_at_rebound(0.04),
        "economics_at_plus_6pct_from_low": _econ_at_rebound(0.06),
    }


def verify_realized_plus_mtm(result: EngineResult, mark: float) -> float:
    """realized_overlay + overlay MTM - open fees paid on remaining should align loosely."""
    ledger = result.ledger
    open_pnls = ledger.open_pnl_at(mark)
    return (
        ledger.realized_overlay_pnl
        + open_pnls["overlay_long_open_pnl"]
        + open_pnls["overlay_short_open_pnl"]
    )

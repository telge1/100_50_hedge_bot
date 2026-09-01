from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any

from .dedup import trade_pnl_usdt
from .simulate import SimResult
from .timeutil import parse_ts


def _win_rate(wins: int, losses: int) -> float | None:
    den = wins + losses
    return None if den == 0 else wins / den


def _profit_factor(gp: float, gl: float) -> float | None:
    if gl == 0:
        return None if gp == 0 else float("inf")
    return gp / abs(gl)


def _bucket() -> dict[str, Any]:
    return {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "open": 0,
        "win_rate": None,
        "gross_profit_usdt": 0.0,
        "gross_loss_usdt": 0.0,
        "realized_pnl_usdt": 0.0,
        "profit_factor": None,
    }


def _add(bucket: dict[str, Any], trade: dict[str, Any]) -> None:
    bucket["trades"] += 1
    oc = trade["outcome"]
    if oc == "WIN":
        bucket["wins"] += 1
        bucket["gross_profit_usdt"] += float(trade["pnl_usdt"])
        bucket["realized_pnl_usdt"] += float(trade["pnl_usdt"])
    elif oc == "LOSS":
        bucket["losses"] += 1
        bucket["gross_loss_usdt"] += float(trade["pnl_usdt"])
        bucket["realized_pnl_usdt"] += float(trade["pnl_usdt"])
    elif oc == "OPEN":
        bucket["open"] += 1
    bucket["win_rate"] = _win_rate(bucket["wins"], bucket["losses"])
    bucket["profit_factor"] = _profit_factor(bucket["gross_profit_usdt"], bucket["gross_loss_usdt"])


def max_drawdown(equity_curve: list[dict[str, Any]]) -> dict[str, Any]:
    peak = None
    max_dd = 0.0
    max_dd_pct = 0.0
    peak_val = None
    trough = None
    for row in equity_curve:
        eq = float(row["realized_equity_usdt"])
        if peak is None or eq > peak:
            peak = eq
        dd = peak - eq
        pct = 0.0 if peak in (None, 0) else dd / peak * 100.0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = pct
            peak_val = peak
            trough = eq
    return {
        "max_drawdown_usdt": round(max_dd, 8),
        "max_drawdown_pct_of_peak_equity": round(max_dd_pct, 8),
        "peak_equity_usdt": peak_val,
        "trough_equity_usdt": trough,
    }


def streak_stats(accepted: list[dict[str, Any]]) -> dict[str, int]:
    closed = [t for t in accepted if t["outcome"] in {"WIN", "LOSS"}]
    closed.sort(key=lambda t: (str(t.get("exit_time") or ""), t["signal_id"]))
    best_w = cur_w = best_l = cur_l = 0
    for t in closed:
        if t["outcome"] == "WIN":
            cur_w += 1
            cur_l = 0
            best_w = max(best_w, cur_w)
        else:
            cur_l += 1
            cur_w = 0
            best_l = max(best_l, cur_l)
    return {"longest_win_streak": best_w, "longest_loss_streak": best_l}


def duration_stats(accepted: list[dict[str, Any]]) -> dict[str, Any]:
    durs = [float(t["duration_seconds"]) for t in accepted if t.get("duration_seconds") is not None]
    if not durs:
        return {"avg_hold_seconds": None, "median_hold_seconds": None, "longest_hold_seconds": None}
    return {
        "avg_hold_seconds": sum(durs) / len(durs),
        "median_hold_seconds": median(durs),
        "longest_hold_seconds": max(durs),
    }


def rate_stats(accepted: list[dict[str, Any]]) -> dict[str, Any]:
    times = [parse_ts(t["entry_time"]) for t in accepted]
    times = [t for t in times if t is not None]
    if len(times) < 1:
        return {"trades_per_day": None, "trades_per_week": None, "trades_per_month": None, "span_days": None}
    span = (max(times) - min(times)).total_seconds() / 86400.0
    span = max(span, 1.0 / 24.0)
    n = len(accepted)
    return {
        "span_days": span,
        "trades_per_day": n / span,
        "trades_per_week": n / span * 7.0,
        "trades_per_month": n / span * 30.437,
    }


def occupancy_summary(occ: dict[int, float], max_slots: int) -> dict[str, Any]:
    total = sum(occ.values()) or 1.0
    avg_open = sum(k * v for k, v in occ.items()) / total
    return {
        "seconds_by_open_count": {str(k): occ.get(k, 0.0) for k in range(max_slots + 1)},
        "avg_open_positions": avg_open,
        "avg_slot_utilization": avg_open / max_slots if max_slots else None,
    }


def breakdowns(accepted: list[dict[str, Any]]) -> dict[str, Any]:
    by_symbol: dict[str, dict[str, Any]] = defaultdict(_bucket)
    by_tf: dict[str, dict[str, Any]] = defaultdict(_bucket)
    by_dir: dict[str, dict[str, Any]] = defaultdict(_bucket)
    by_slot: dict[str, dict[str, Any]] = defaultdict(_bucket)
    by_month: dict[str, dict[str, Any]] = defaultdict(_bucket)
    for t in accepted:
        _add(by_symbol[t["symbol"]], t)
        _add(by_tf[t["timeframe"]], t)
        _add(by_dir[t["direction"]], t)
        _add(by_slot[str(t["slot"])], t)
        ts = parse_ts(t["entry_time"])
        month = ts.strftime("%Y-%m") if ts else "unknown"
        _add(by_month[month], t)
    return {
        "per_symbol": dict(sorted(by_symbol.items())),
        "per_timeframe": dict(sorted(by_tf.items())),
        "per_direction": dict(sorted(by_dir.items())),
        "per_slot": dict(sorted(by_slot.items(), key=lambda kv: int(kv[0]))),
        "per_month": dict(sorted(by_month.items())),
    }


def independent_baseline(rows: list[dict[str, Any]], notional: float) -> dict[str, Any]:
    trades = []
    for row in rows:
        oc = row["outcome"]
        pnl = trade_pnl_usdt(row.get("pnl_pct_gross"), notional) if oc in {"WIN", "LOSS"} else 0.0
        trades.append({**row, "pnl_usdt": pnl, "slot": None})
    b = _bucket()
    for t in trades:
        _add(b, t)
    b["trades"] = len(trades)
    return b


def portfolio_summary(
    sim: SimResult,
    *,
    initial_balance: float,
    max_slots: int,
    independent: dict[str, Any],
) -> dict[str, Any]:
    b = _bucket()
    for t in sim.accepted:
        _add(b, t)
    gp = b["gross_profit_usdt"]
    gl = b["gross_loss_usdt"]
    realized_cash = sim.free_cash
    reserved = sim.reserved
    realized_equity = realized_cash + reserved
    closed = b["wins"] + b["losses"]
    expectancy = None if closed == 0 else b["realized_pnl_usdt"] / closed
    skipped_wins = sum(1 for s in sim.skipped if s.get("outcome") == "WIN")
    skipped_losses = sum(1 for s in sim.skipped if s.get("outcome") == "LOSS")
    return {
        "accepted_trades": len(sim.accepted),
        "skipped_signals": len(sim.skipped),
        "wins": b["wins"],
        "losses": b["losses"],
        "open": b["open"],
        "win_rate": b["win_rate"],
        "gross_profit_usdt": gp,
        "gross_loss_usdt": gl,
        "total_realized_pnl_usdt": b["realized_pnl_usdt"],
        "initial_balance_usdt": initial_balance,
        "realized_cash_usdt": realized_cash,
        "reserved_open_notional_usdt": reserved,
        "realized_equity_usdt": realized_equity,
        "return_on_initial": (realized_equity - initial_balance) / initial_balance if initial_balance else None,
        "profit_factor": b["profit_factor"],
        "expectancy_usdt_per_closed_trade": expectancy,
        "max_concurrent_positions": sim.peak_open,
        "skipped_winners": skipped_wins,
        "skipped_losers": skipped_losses,
        "unrealized_pnl_usdt": None,
        "final_equity_with_unrealized": None,
        **max_drawdown(sim.equity_curve),
        **duration_stats(sim.accepted),
        **streak_stats(sim.accepted),
        **rate_stats(sim.accepted),
        **occupancy_summary(sim.occupancy_seconds, max_slots),
        "skip_reasons": sim.skip_reason_stats,
        "independent_baseline": independent,
        "capacity_delta_trades": independent["trades"] - len(sim.accepted),
        "capacity_delta_pnl_usdt": independent["realized_pnl_usdt"] - b["realized_pnl_usdt"],
    }

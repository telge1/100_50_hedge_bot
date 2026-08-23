"""Causal TP/SL PnL simulation on 1m candles (SL_FIRST on same bar)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

SL_PCT = 0.50
TP_LEVELS = (0.20, 0.30, 0.40, 0.50, 0.60, 0.75)
HORIZON_MAP = {"1h": 60, "2h": 120, "4h": 240}
STRATEGY_IDS = {
    0.20: "TP020_SL050",
    0.30: "TP030_SL050",
    0.40: "TP040_SL050",
    0.50: "TP050_SL050",
    0.60: "TP060_SL050",
    0.75: "TP075_SL050",
}
COST_LEVELS = (0.0, 0.11, 0.15, 0.20)
NOTIONAL_USDT = 1000.0
DD_REF_CAPITAL = 10000.0


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _levels(direction: str, entry_price: float, tp_pct: float, sl_pct: float) -> tuple[float, float]:
    bull = str(direction).upper() == "BULLISH"
    px = float(entry_price)
    if bull:
        return px * (1.0 + tp_pct / 100.0), px * (1.0 - sl_pct / 100.0)
    return px * (1.0 - tp_pct / 100.0), px * (1.0 + sl_pct / 100.0)


def _path(candles_1m: pd.DataFrame, entry_at: datetime, horizon_min: int) -> pd.DataFrame:
    entry_ts = _utc(entry_at)
    end = entry_ts + timedelta(minutes=int(horizon_min))
    if candles_1m is None or candles_1m.empty:
        return pd.DataFrame()
    tcol = pd.to_datetime(candles_1m["open_time"])
    if getattr(tcol.dt, "tz", None) is not None:
        a, b = entry_ts, end
    else:
        a, b = entry_ts.replace(tzinfo=None), end.replace(tzinfo=None)
    mask = (tcol >= pd.Timestamp(a)) & (tcol < pd.Timestamp(b))
    return candles_1m.loc[mask].sort_values("open_time")


def simulate_tpsl_trade(
    candles_1m: pd.DataFrame,
    *,
    direction: str,
    entry_at: str | datetime,
    entry_price: float,
    tp_pct: float,
    sl_pct: float = SL_PCT,
    horizon_min: int,
    require_full_horizon: bool = False,
    incomplete_if_truncated_path: bool = False,
) -> dict[str, Any]:
    """Simulate one isolated trade from entry to first exit or time exit.

    If ``require_full_horizon`` is True and the 1m path does not cover the full
    horizon window, returns ``INCOMPLETE_OUTCOME_HORIZON`` (no premature TIME_EXIT).

    If ``incomplete_if_truncated_path`` is True (canonical frozen strategy), a path that
    ends before ``entry_at + horizon`` is also ``INCOMPLETE_OUTCOME_HORIZON`` even when
    ``require_full_horizon`` is False — never classify truncated data as TIME win/loss.
    """
    bull = str(direction).upper() == "BULLISH"
    entry_ts = _utc(entry_at)
    px = float(entry_price)
    tp_price, sl_price = _levels(direction, px, tp_pct, sl_pct)
    path = _path(candles_1m, entry_ts, horizon_min)
    horizon_end = entry_ts + timedelta(minutes=int(horizon_min))

    base = {
        "entry_at": entry_ts.isoformat(),
        "entry_price": px,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "horizon_min": horizon_min,
        "tp_price": round(tp_price, 8),
        "sl_price": round(sl_price, 8),
        "coverage": "EMPTY" if path.empty else "OK",
    }
    if path.empty or px <= 0:
        return {
            **base,
            "exit_at": None,
            "exit_price": None,
            "exit_reason": "COVERAGE_MISSING",
            "same_bar_conflict": False,
            "bars_held": 0,
            "duration_minutes": 0,
            "gross_return_pct": None,
            "include_in_primary_pnl": False,
        }

    if require_full_horizon and len(path) < int(horizon_min):
        return {
            **base,
            "exit_at": None,
            "exit_price": None,
            "exit_reason": "INCOMPLETE_OUTCOME_HORIZON",
            "same_bar_conflict": False,
            "bars_held": int(len(path)),
            "duration_minutes": 0,
            "gross_return_pct": None,
            "include_in_primary_pnl": False,
            "coverage": "INCOMPLETE_HORIZON",
        }

    exit_at = exit_price = None
    exit_reason = "TIME_EXIT"
    same_bar = False
    bars_held = 0

    for _, row in path.iterrows():
        bars_held += 1
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        ts = pd.Timestamp(row["open_time"]).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)

        if bull:
            sl_hit = low <= sl_price
            tp_hit = high >= tp_price
        else:
            sl_hit = high >= sl_price
            tp_hit = low <= tp_price

        if sl_hit and tp_hit:
            exit_reason = "SL_EXIT"
            exit_price = sl_price
            exit_at = ts
            same_bar = True
            break
        if sl_hit:
            exit_reason = "SL_EXIT"
            exit_price = sl_price
            exit_at = ts
            break
        if tp_hit:
            exit_reason = "TP_EXIT"
            exit_price = tp_price
            exit_at = ts
            break

    if exit_at is None:
        last = path.iloc[-1]
        last_open = pd.Timestamp(last["open_time"]).to_pydatetime()
        if last_open.tzinfo is None:
            last_open = last_open.replace(tzinfo=timezone.utc)
        else:
            last_open = last_open.astimezone(timezone.utc)
        # Premature data end inside horizon → incomplete
        truncated = (last_open + timedelta(minutes=1)) < horizon_end
        if (require_full_horizon or incomplete_if_truncated_path) and truncated:
            return {
                **base,
                "exit_at": None,
                "exit_price": None,
                "exit_reason": "INCOMPLETE_OUTCOME_HORIZON",
                "same_bar_conflict": False,
                "bars_held": bars_held,
                "duration_minutes": 0,
                "gross_return_pct": None,
                "include_in_primary_pnl": False,
                "coverage": "INCOMPLETE_HORIZON",
            }
        exit_at = last_open
        exit_price = float(last["close"])
        exit_reason = "TIME_EXIT"

    if bull:
        gross = (float(exit_price) - px) / px * 100.0
    else:
        gross = (px - float(exit_price)) / px * 100.0

    duration = (exit_at - entry_ts).total_seconds() / 60.0 if exit_at else 0.0
    return {
        **base,
        "exit_at": exit_at.isoformat() if exit_at else None,
        "exit_price": round(float(exit_price), 8),
        "exit_reason": exit_reason,
        "same_bar_conflict": same_bar,
        "bars_held": bars_held,
        "duration_minutes": round(duration, 3),
        "gross_return_pct": round(gross, 6),
        "include_in_primary_pnl": True,
    }


def apply_costs(trade: dict[str, Any], roundtrip_cost_pct: float, *, funding_pnl_usdt: float = 0.0) -> dict[str, Any]:
    gross = trade.get("gross_return_pct")
    if gross is None:
        return {
            **trade,
            "roundtrip_cost_pct": roundtrip_cost_pct,
            "net_return_pct": None,
            "notional_usdt": NOTIONAL_USDT,
            "gross_pnl_usdt": None,
            "costs_usdt": None,
            "funding_pnl_usdt": funding_pnl_usdt,
            "net_pnl_usdt": None,
        }
    net = float(gross) - float(roundtrip_cost_pct)
    gross_usdt = NOTIONAL_USDT * float(gross) / 100.0
    costs_usdt = NOTIONAL_USDT * float(roundtrip_cost_pct) / 100.0
    net_usdt = gross_usdt - costs_usdt + float(funding_pnl_usdt)
    return {
        **trade,
        "roundtrip_cost_pct": roundtrip_cost_pct,
        "net_return_pct": round(net, 6),
        "notional_usdt": NOTIONAL_USDT,
        "gross_pnl_usdt": round(gross_usdt, 6),
        "costs_usdt": round(costs_usdt, 6),
        "funding_pnl_usdt": round(funding_pnl_usdt, 6),
        "net_pnl_usdt": round(net_usdt, 6),
    }


def aggregate_strategy_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [t for t in trades if t.get("net_pnl_usdt") is not None]
    n = len(valid)
    if n == 0:
        return {"n_trades": 0}

    gross_pnls = [float(t["gross_pnl_usdt"]) for t in valid]
    net_pnls = [float(t["net_pnl_usdt"]) for t in valid]
    gross_wins = sum(1 for x in gross_pnls if x > 0)
    net_wins = sum(1 for x in net_pnls if x > 0)
    net_losses = sum(1 for x in net_pnls if x < 0)
    net_flat = sum(1 for x in net_pnls if x == 0)

    gross_profit = sum(x for x in gross_pnls if x > 0)
    gross_loss = abs(sum(x for x in gross_pnls if x < 0))
    net_profit = sum(x for x in net_pnls if x > 0)
    net_loss = abs(sum(x for x in net_pnls if x < 0))

    avg_win = (sum(x for x in net_pnls if x > 0) / net_wins) if net_wins else 0.0
    avg_loss = (abs(sum(x for x in net_pnls if x < 0)) / net_losses) if net_losses else 0.0
    payoff = (avg_win / avg_loss) if avg_loss > 0 else None
    breakeven_wr = (avg_loss / (avg_win + avg_loss) * 100.0) if payoff and (avg_win + avg_loss) > 0 else None
    obs_wr = net_wins / n * 100.0

    # drawdown on reference capital
    ordered = sorted(valid, key=lambda t: (t.get("entry_at", ""), t.get("candidate_id", "")))
    equity = DD_REF_CAPITAL
    peak = equity
    max_dd_usdt = 0.0
    max_dd_pct = 0.0
    loss_streak = max_loss_streak = 0
    for t in ordered:
        equity += float(t["net_pnl_usdt"])
        peak = max(peak, equity)
        dd = peak - equity
        max_dd_usdt = max(max_dd_usdt, dd)
        max_dd_pct = max(max_dd_pct, dd / DD_REF_CAPITAL * 100.0)
        if float(t["net_pnl_usdt"]) < 0:
            loss_streak += 1
            max_loss_streak = max(max_loss_streak, loss_streak)
        else:
            loss_streak = 0

    durations = [float(t.get("duration_minutes") or 0) for t in valid]
    return {
        "n_trades": n,
        "tp_exit": sum(1 for t in valid if t.get("exit_reason") == "TP_EXIT"),
        "sl_exit": sum(1 for t in valid if t.get("exit_reason") == "SL_EXIT"),
        "time_exit": sum(1 for t in valid if t.get("exit_reason") == "TIME_EXIT"),
        "same_bar_conflicts": sum(1 for t in valid if t.get("same_bar_conflict")),
        "gross_wins": gross_wins,
        "net_wins": net_wins,
        "net_losses": net_losses,
        "net_flat": net_flat,
        "gross_winrate": round(gross_wins / n, 6),
        "net_winrate": round(net_wins / n, 6),
        "gross_pnl_pct": round(sum(gross_pnls) / NOTIONAL_USDT * 100.0, 6),
        "net_pnl_pct": round(sum(net_pnls) / NOTIONAL_USDT * 100.0, 6),
        "gross_pnl_usdt": round(sum(gross_pnls), 6),
        "costs_usdt": round(sum(float(t.get("costs_usdt") or 0) for t in valid), 6),
        "funding_pnl_usdt": round(sum(float(t.get("funding_pnl_usdt") or 0) for t in valid), 6),
        "net_pnl_usdt": round(sum(net_pnls), 6),
        "avg_net_pnl_usdt": round(sum(net_pnls) / n, 6),
        "median_net_pnl_usdt": round(float(pd.Series(net_pnls).median()), 6),
        "profit_factor_gross": round(gross_profit / gross_loss, 6) if gross_loss > 0 else None,
        "profit_factor_net": round(net_profit / net_loss, 6) if net_loss > 0 else None,
        "max_drawdown_usdt": round(max_dd_usdt, 6),
        "max_drawdown_pct_ref10k": round(max_dd_pct, 6),
        "max_loss_streak": max_loss_streak,
        "avg_duration_minutes": round(sum(durations) / n, 3),
        "median_duration_minutes": round(float(pd.Series(durations).median()), 3),
        "avg_win_usdt": round(avg_win, 6),
        "avg_loss_usdt": round(avg_loss, 6),
        "payoff_ratio": round(payoff, 6) if payoff else None,
        "breakeven_winrate_pct": round(breakeven_wr, 6) if breakeven_wr else None,
        "winrate_minus_breakeven_pct": round(obs_wr - breakeven_wr, 6) if breakeven_wr else None,
    }

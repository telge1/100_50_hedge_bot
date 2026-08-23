"""Causal 1m TP/SL backtest helpers (research-only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def simulate_tpsl_trade(
    candles_1m: pd.DataFrame,
    *,
    direction: str,
    entry_at: datetime | str,
    entry_price: float,
    tp_pct: float,
    sl_pct: float,
    horizon_minutes: int,
    fee_roundtrip_pct: float,
) -> dict[str, Any]:
    """
    Walk 1m bars after entry. Same-bar TP+SL → SL_FIRST.
    Horizon exit at last available 1m close within horizon.
    Fee applied once as full roundtrip cost on notional (pct points).
    """
    bull = str(direction).upper() == "BULLISH"
    entry_ts = _utc(entry_at)
    px = float(entry_price)
    if px <= 0:
        return {"status": "INVALID_ENTRY", "net_pnl_pct": 0.0}

    if bull:
        tp = px * (1.0 + tp_pct / 100.0)
        sl = px * (1.0 - sl_pct / 100.0)
    else:
        tp = px * (1.0 - tp_pct / 100.0)
        sl = px * (1.0 + sl_pct / 100.0)

    horizon_end = entry_ts + timedelta(minutes=int(horizon_minutes))
    df = candles_1m.copy()
    if df.empty:
        return {"status": "NO_CANDLES", "net_pnl_pct": -float(fee_roundtrip_pct)}
    tcol = pd.to_datetime(df["open_time"])
    if getattr(tcol.dt, "tz", None) is not None:
        tcol = tcol.dt.tz_convert("UTC")
        entry_naive = entry_ts
        end_naive = horizon_end
    else:
        entry_naive = entry_ts.replace(tzinfo=None)
        end_naive = horizon_end.replace(tzinfo=None)
    # strictly after entry open: use bars with open_time >= entry_at
    mask = (tcol >= pd.Timestamp(entry_naive)) & (tcol < pd.Timestamp(end_naive))
    path = df.loc[mask].sort_values("open_time")
    if path.empty:
        return {
            "status": "NO_PATH",
            "exit_reason": "NO_PATH",
            "entry_price": px,
            "exit_price": None,
            "gross_pnl_pct": 0.0,
            "fee_pct": float(fee_roundtrip_pct),
            "net_pnl_pct": -float(fee_roundtrip_pct),
            "hold_minutes": 0.0,
            "win": False,
        }

    exit_price = None
    exit_reason = None
    exit_at = None
    for _, row in path.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        ts = pd.Timestamp(row["open_time"]).to_pydatetime()
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        hit_tp = high >= tp if bull else low <= tp
        hit_sl = low <= sl if bull else high >= sl
        if hit_tp and hit_sl:
            exit_price = sl
            exit_reason = "SL_FIRST"
            exit_at = ts
            break
        if hit_sl:
            exit_price = sl
            exit_reason = "SL"
            exit_at = ts
            break
        if hit_tp:
            exit_price = tp
            exit_reason = "TP"
            exit_at = ts
            break

    if exit_price is None:
        last = path.iloc[-1]
        exit_price = float(last["close"])
        exit_reason = "HORIZON"
        exit_at = pd.Timestamp(last["open_time"]).to_pydatetime()
        if getattr(exit_at, "tzinfo", None) is None:
            exit_at = exit_at.replace(tzinfo=timezone.utc)

    if bull:
        gross = (exit_price - px) / px * 100.0
    else:
        gross = (px - exit_price) / px * 100.0
    fee = float(fee_roundtrip_pct)
    net = gross - fee
    hold = max(0.0, (_utc(exit_at) - entry_ts).total_seconds() / 60.0) if exit_at else 0.0
    return {
        "status": "OK",
        "exit_reason": exit_reason,
        "entry_price": px,
        "exit_price": exit_price,
        "entry_at": entry_ts.isoformat(),
        "exit_at": _utc(exit_at).isoformat() if exit_at else None,
        "gross_pnl_pct": round(gross, 6),
        "fee_pct": fee,
        "net_pnl_pct": round(net, 6),
        "hold_minutes": hold,
        "win": net > 0,
        "tp_price": tp,
        "sl_price": sl,
    }


def summarize_trade_pnl(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "n_trades": 0,
            "n_wins": 0,
            "n_losses": 0,
            "winrate": None,
            "gross_pnl_pct": 0.0,
            "fee_pct_total": 0.0,
            "net_pnl_pct": 0.0,
            "net_expectancy": None,
            "profit_factor": None,
            "max_drawdown_pct": 0.0,
            "max_loss_streak": 0,
            "avg_hold_minutes": None,
        }
    nets = [float(t["net_pnl_pct"]) for t in trades]
    gross = [float(t["gross_pnl_pct"]) for t in trades]
    fees = [float(t["fee_pct"]) for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
    for n in nets:
        eq += n
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        if n <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    sum_win = sum(wins) if wins else 0.0
    sum_loss = abs(sum(losses)) if losses else 0.0
    pf = (sum_win / sum_loss) if sum_loss > 0 else (float("inf") if sum_win > 0 else None)
    holds = [float(t.get("hold_minutes") or 0) for t in trades]
    return {
        "n_trades": len(trades),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "winrate": round(len(wins) / len(trades), 6),
        "gross_pnl_pct": round(sum(gross), 6),
        "fee_pct_total": round(sum(fees), 6),
        "net_pnl_pct": round(sum(nets), 6),
        "net_expectancy": round(sum(nets) / len(trades), 6),
        "profit_factor": (round(pf, 6) if pf is not None and pf != float("inf") else pf),
        "max_drawdown_pct": round(max_dd, 6),
        "max_loss_streak": max_streak,
        "avg_hold_minutes": round(sum(holds) / len(holds), 3) if holds else None,
    }

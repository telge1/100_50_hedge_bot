"""Public-trade impact intervals around zone contact."""

from __future__ import annotations

from typing import Any

import pandas as pd

from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import MISSING

INTERVALS_MS = {
    "pre_60s": (-60_000, 0),
    "pre_30s": (-30_000, 0),
    "pre_10s": (-10_000, 0),
    "contact": (0, 5_000),
    "post_10s": (0, 10_000),
    "post_30s": (0, 30_000),
    "post_60s": (0, 60_000),
    "post_120s": (0, 120_000),
    "post_300s": (0, 300_000),
}


def _slice(trades: pd.DataFrame, t0: int, t1: int) -> pd.DataFrame:
    if trades.empty:
        return trades
    return trades[(trades["ts_ms"] >= t0) & (trades["ts_ms"] < t1)]


def summarize_trades(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {
            "buy_notional": 0.0,
            "sell_notional": 0.0,
            "delta": 0.0,
            "trade_count": 0,
            "largest_buy": 0.0,
            "largest_sell": 0.0,
            "largest_trade": 0.0,
        }
    buy = trades[trades["side"].astype(str).str.lower().isin(["buy", "b"])]
    sell = trades[trades["side"].astype(str).str.lower().isin(["sell", "s"])]
    bn = float(buy["notional"].sum()) if not buy.empty else 0.0
    sn = float(sell["notional"].sum()) if not sell.empty else 0.0
    return {
        "buy_notional": bn,
        "sell_notional": sn,
        "delta": bn - sn,
        "trade_count": int(len(trades)),
        "largest_buy": float(buy["notional"].max()) if not buy.empty else 0.0,
        "largest_sell": float(sell["notional"].max()) if not sell.empty else 0.0,
        "largest_trade": float(trades["notional"].max()),
    }


def price_progress(mids: list[tuple[int, float]], t0: int, t1: int) -> float | None:
    """Signed mid change over [t0,t1]."""
    if not mids:
        return None
    before = [m for ts, m in mids if ts <= t0]
    after = [m for ts, m in mids if ts <= t1]
    if not before or not after:
        return None
    return after[-1] - before[-1]


def impact_rows(
    *,
    window_id: str,
    contact_ts_ms: int | None,
    trades: pd.DataFrame,
    mids: list[tuple[int, float]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if contact_ts_ms is None:
        for name in INTERVALS_MS:
            rows.append(
                {
                    "window_id": window_id,
                    "interval": name,
                    "contact_ts_ms": MISSING,
                    "status": "NO_CONTACT",
                    "buy_notional": MISSING,
                    "sell_notional": MISSING,
                    "delta": MISSING,
                    "trade_count": MISSING,
                    "largest_trade": MISSING,
                    "price_progress": MISSING,
                    "progress_per_buy_notional": MISSING,
                    "progress_per_sell_notional": MISSING,
                    "impact_compression": MISSING,
                }
            )
        return rows

    for name, (a, b) in INTERVALS_MS.items():
        t0 = contact_ts_ms + a
        t1 = contact_ts_ms + b
        if b <= a:
            t1 = contact_ts_ms + max(b, 1)
        sub = _slice(trades, t0, t1)
        s = summarize_trades(sub)
        prog = price_progress(mids, t0, t1)
        buy_n = s["buy_notional"]
        sell_n = s["sell_notional"]
        ppb = (prog / buy_n) if prog is not None and buy_n > 0 else None
        pps = (prog / sell_n) if prog is not None and sell_n > 0 else None
        # compression: large aggressive notional, small |progress|
        agg = max(buy_n, sell_n)
        compression = None
        if prog is not None and agg > 0:
            compression = abs(prog) / (agg / 1e6)  # price per $1m aggression
        rows.append(
            {
                "window_id": window_id,
                "interval": name,
                "contact_ts_ms": contact_ts_ms,
                "status": "OK",
                "buy_notional": s["buy_notional"],
                "sell_notional": s["sell_notional"],
                "delta": s["delta"],
                "trade_count": s["trade_count"],
                "largest_trade": s["largest_trade"],
                "largest_buy": s["largest_buy"],
                "largest_sell": s["largest_sell"],
                "price_progress": prog if prog is not None else MISSING,
                "progress_per_buy_notional": ppb if ppb is not None else MISSING,
                "progress_per_sell_notional": pps if pps is not None else MISSING,
                "impact_compression": compression if compression is not None else MISSING,
            }
        )
    return rows


def classify_flow_mechanism(
    *,
    attack_side: str,  # BUY attacking asks / SELL attacking bids
    wall_side: str,  # ASK or BID
    buy_n: float,
    sell_n: float,
    wall_notional_before: float | None,
    wall_notional_after: float | None,
    wall_present_after: bool,
    price_held_beyond: bool,
    consumed_estimate: float | None,
) -> str:
    """
    ASK_DEFENSE / BID_DEFENSE / ASK_ABSORPTION / BID_ABSORPTION / LIQUIDITY_PULL / UNDETERMINED
    """
    if wall_notional_before is None:
        return "UNDETERMINED"
    disappeared = (wall_notional_after is None or wall_notional_after <= 0.15 * wall_notional_before) and not wall_present_after
    aggressive = buy_n if wall_side == "ASK" else sell_n
    if disappeared and (consumed_estimate is None or aggressive < 0.35 * wall_notional_before):
        return "LIQUIDITY_PULL"
    if wall_side == "ASK":
        if price_held_beyond and consumed_estimate is not None and consumed_estimate >= 0.5 * wall_notional_before:
            return "ASK_ABSORPTION"
        if buy_n > sell_n and wall_present_after and not price_held_beyond:
            return "ASK_DEFENSE"
    if wall_side == "BID":
        if price_held_beyond and consumed_estimate is not None and consumed_estimate >= 0.5 * wall_notional_before:
            return "BID_ABSORPTION"
        if sell_n > buy_n and wall_present_after and not price_held_beyond:
            return "BID_DEFENSE"
    return "UNDETERMINED"

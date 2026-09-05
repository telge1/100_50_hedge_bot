"""Book lookup + PnL helpers for entry timing V1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.timeutil import ensure_utc, iso_z
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.entry_timing_contracts import (
    ACCEPTANCE_TO_TRADE_SIDE,
)
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


def trade_side_from_acceptance(acceptance_state: str) -> str:
    side = ACCEPTANCE_TO_TRADE_SIDE.get(str(acceptance_state))
    if side is None:
        raise ValueError(f"unsupported acceptance_state={acceptance_state!r}")
    return side


@dataclass(frozen=True)
class BookQuote:
    ts: datetime
    best_bid: float
    best_ask: float
    mid: float
    source: str = "ob200_sample"


def sample_to_quote(s: SampleRow) -> Optional[BookQuote]:
    if s.best_bid is None or s.best_ask is None or s.mid is None:
        return None
    if s.best_bid <= 0 or s.best_ask <= 0 or s.mid <= 0:
        return None
    if s.best_ask < s.best_bid:
        return None
    ts = datetime.fromtimestamp(s.ts_ms / 1000.0, tz=timezone.utc)
    return BookQuote(ts=ts, best_bid=float(s.best_bid), best_ask=float(s.best_ask), mid=float(s.mid))


def first_quote_at_or_after(
    samples: list[SampleRow],
    *,
    legal_ts: datetime,
    max_lookup_seconds: float,
) -> tuple[Optional[BookQuote], str]:
    """First causal OB200 quote with ts >= legal_ts within lookup window."""
    legal_ts = ensure_utc(legal_ts)
    legal_ms = int(legal_ts.timestamp() * 1000)
    end_ms = legal_ms + int(max_lookup_seconds * 1000)
    # binary search leftmost sample with ts_ms >= legal_ms
    lo, hi = 0, len(samples)
    while lo < hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms < legal_ms:
            lo = mid + 1
        else:
            hi = mid
    i = lo
    while i < len(samples) and samples[i].ts_ms <= end_ms:
        q = sample_to_quote(samples[i])
        if q is not None:
            if q.ts < legal_ts:
                i += 1
                continue
            return q, "OK"
        i += 1
    if lo >= len(samples) or samples[lo].ts_ms > end_ms:
        return None, "ENTRY_UNAVAILABLE" if True else "UNAVAILABLE"
    return None, "UNAVAILABLE"


def apply_entry_price(
    *,
    side: str,
    quote: BookQuote,
    extra_slippage_bps: float,
) -> dict[str, float]:
    """LONG pays ask (+slip); SHORT sells bid (-slip)."""
    spread_bps = (quote.best_ask - quote.best_bid) / quote.mid * 1e4
    if side == "LONG":
        raw = quote.best_ask
        px = raw * (1.0 + extra_slippage_bps / 1e4)
    else:
        raw = quote.best_bid
        px = raw * (1.0 - extra_slippage_bps / 1e4)
    return {
        "entry_bid": quote.best_bid,
        "entry_ask": quote.best_ask,
        "entry_mid": quote.mid,
        "spread_bps": spread_bps,
        "raw_entry_price": raw,
        "executable_entry_price": px,
    }


def apply_exit_price(
    *,
    side: str,
    quote: BookQuote,
    extra_slippage_bps: float,
) -> dict[str, float]:
    """LONG exits bid (-slip); SHORT covers ask (+slip)."""
    spread_bps = (quote.best_ask - quote.best_bid) / quote.mid * 1e4
    if side == "LONG":
        raw = quote.best_bid
        px = raw * (1.0 - extra_slippage_bps / 1e4)
    else:
        raw = quote.best_ask
        px = raw * (1.0 + extra_slippage_bps / 1e4)
    return {
        "exit_bid": quote.best_bid,
        "exit_ask": quote.best_ask,
        "exit_mid": quote.mid,
        "exit_spread_bps": spread_bps,
        "raw_exit_price": raw,
        "executable_exit_price": px,
    }


def gross_return(side: str, entry_px: float, exit_px: float) -> float:
    if side == "LONG":
        return exit_px / entry_px - 1.0
    return entry_px / exit_px - 1.0


def mid_to_mid_return(side: str, entry_mid: float, exit_mid: float) -> float:
    return gross_return(side, entry_mid, exit_mid)


def trade_economics(
    *,
    side: str,
    entry_mid: float,
    exit_mid: float,
    raw_entry: float,
    raw_exit: float,
    exec_entry: float,
    exec_exit: float,
    entry_fee_rate: float,
    exit_fee_rate: float,
    notional_usdt: float,
) -> dict[str, float]:
    g_mid = mid_to_mid_return(side, entry_mid, exit_mid)
    g_exec = gross_return(side, exec_entry, exec_exit)
    # fee on notional at entry/exit prices (fraction of notional)
    entry_fee = entry_fee_rate
    exit_fee = exit_fee_rate
    # spread+slip embedded in exec vs mid path:
    # cost relative to mid-to-mid
    total_fee = entry_fee + exit_fee
    net = g_exec - total_fee
    # decompose: executable gross already includes spread+slip vs mid
    spread_slip_cost = g_mid - g_exec  # positive if exec worse than mid
    return {
        "mid_to_mid_return": g_mid,
        "executable_gross_return": g_exec,
        "spread_slip_vs_mid": spread_slip_cost,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "total_fee": total_fee,
        "total_trading_cost": spread_slip_cost + total_fee,
        "net_return": net,
        "gross_pnl_usdt": g_exec * notional_usdt,
        "net_pnl_usdt": net * notional_usdt,
        "required_move_bps_for_break_even": (spread_slip_cost + total_fee) * 1e4,
        "gross_move_bps": g_exec * 1e4,
        "net_move_bps": net * 1e4,
    }


def path_mfe_mae(
    samples: list[SampleRow],
    *,
    side: str,
    entry_ts: datetime,
    entry_px: float,
    horizon_end: datetime,
) -> dict[str, Any]:
    """MFE/MAE from executable entry using mid path (diagnostic)."""
    entry_ts = ensure_utc(entry_ts)
    horizon_end = ensure_utc(horizon_end)
    t0 = int(entry_ts.timestamp() * 1000)
    t1 = int(horizon_end.timestamp() * 1000)
    mfe = 0.0
    mae = 0.0
    t_mfe = None
    t_mae = None
    lo, hi = 0, len(samples)
    while lo < hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms < t0:
            lo = mid + 1
        else:
            hi = mid
    i = lo
    while i < len(samples) and samples[i].ts_ms <= t1:
        m = samples[i].mid
        if m and m > 0 and entry_px > 0:
            if side == "LONG":
                r = m / entry_px - 1.0
            else:
                r = entry_px / m - 1.0
            if r > mfe:
                mfe = r
                t_mfe = samples[i].ts_ms
            if r < mae:
                mae = r
                t_mae = samples[i].ts_ms
        i += 1
    return {
        "mfe": mfe,
        "mae": mae,
        "time_to_mfe_s": None if t_mfe is None else (t_mfe - t0) / 1000.0,
        "time_to_mae_s": None if t_mae is None else (t_mae - t0) / 1000.0,
    }

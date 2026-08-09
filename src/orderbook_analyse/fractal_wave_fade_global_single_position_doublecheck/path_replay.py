"""Independent 1m path exit reconstruction (does not import global_engine)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_signal_confluence_db import TF_RANK, TPSL_BY_TF
from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck import (
    FEE_PCT,
    PCT_TOL,
    PRICE_TOL,
    STRATEGY_MAX_HOLD_BY_TF,
)


def ts_utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def tpsl(tf: str) -> tuple[float, float]:
    tp, sl = TPSL_BY_TF[tf]
    return float(tp), float(sl)


def tp_sl_prices(side: str, entry: float, tp_pct: float, sl_pct: float) -> tuple[float, float]:
    if side == "LONG":
        return entry * (1.0 + tp_pct / 100.0), entry * (1.0 - sl_pct / 100.0)
    return entry * (1.0 - tp_pct / 100.0), entry * (1.0 + sl_pct / 100.0)


def gross_from_prices(side: str, entry: float, exit_px: float) -> float:
    if side == "LONG":
        return (exit_px / entry - 1.0) * 100.0
    return (entry / exit_px - 1.0) * 100.0  # equiv (entry-exit)/entry * 100 when same


def gross_dir(side: str, entry: float, exit_px: float) -> float:
    """Frozen engine semantics: SHORT = (entry - exit) / entry * 100."""
    if side == "LONG":
        return (exit_px / entry - 1.0) * 100.0
    return (entry - exit_px) / entry * 100.0


def parse_upgrade_sequence(seq: str) -> list[str]:
    parts = [p.strip() for p in str(seq).split("->") if p.strip()]
    return parts


@dataclass
class MinuteBook:
    times: np.ndarray  # datetime64[ns] naive UTC
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray

    @classmethod
    def from_df(cls, df: pd.DataFrame) -> "MinuteBook":
        ts = pd.to_datetime(df["timestamp"], utc=True)
        # store as naive ns for fast searchsorted (UTC wall)
        times = ts.dt.tz_localize(None).to_numpy(dtype="datetime64[ns]")
        return cls(
            times=times.copy(),
            opens=df["open"].astype(float).to_numpy().copy(),
            highs=df["high"].astype(float).to_numpy().copy(),
            lows=df["low"].astype(float).to_numpy().copy(),
            closes=df["close"].astype(float).to_numpy().copy(),
        )

    def index_at(self, t: pd.Timestamp) -> int:
        tt = np.datetime64(ts_utc(t).tz_localize(None).to_datetime64())
        i = int(np.searchsorted(self.times, tt, side="left"))
        if i < 0 or i >= len(self.times) or self.times[i] != tt:
            return -1
        return i

    def first_after(self, t: pd.Timestamp) -> int:
        tt = np.datetime64(ts_utc(t).tz_localize(None).to_datetime64())
        i = int(np.searchsorted(self.times, tt, side="right"))
        return i if 0 <= i < len(self.times) else -1


def scan_bar_sl_first(
    side: str,
    entry: float,
    high: float,
    low: float,
    tp_pct: float,
    sl_pct: float,
) -> tuple[str | None, float | None, bool]:
    """Single-bar SL_FIRST. Returns reason, gross_pct, ambiguous."""
    if side == "LONG":
        hit_tp = high >= entry * (1.0 + tp_pct / 100.0)
        hit_sl = low <= entry * (1.0 - sl_pct / 100.0)
    else:
        hit_tp = low <= entry * (1.0 - tp_pct / 100.0)
        hit_sl = high >= entry * (1.0 + sl_pct / 100.0)
    if hit_sl and hit_tp:
        return "SL", -float(sl_pct), True
    if hit_sl:
        return "SL", -float(sl_pct), False
    if hit_tp:
        return "TP", float(tp_pct), False
    return None, None, False


def exit_price_from_gross(side: str, entry: float, gross: float) -> float:
    if side == "LONG":
        return entry * (1.0 + gross / 100.0)
    return entry * (1.0 - gross / 100.0)


@dataclass
class UpgradeEvent:
    tf: str
    available_at: pd.Timestamp
    apply_at_entry_time: pd.Timestamp  # T0 1m open when upgrade applies


def replay_trade_path(
    *,
    side: str,
    entry_time: pd.Timestamp,
    entry_price: float,
    first_tf: str,
    upgrade_events: list[UpgradeEvent],
    book: MinuteBook,
    max_hold_min: int,
    forced_conflict_exit_i: int | None = None,
) -> dict[str, Any]:
    """
    Independent chronological replay with causal ladder upgrades.
    Upgrades apply starting at apply_at_entry_time bar (inclusive for that bar's OHLC
    after upgrade is known at that T0 — matching engine: upgrade processed at signal
    event before same-bar TP/SL scan).
    """
    ei = book.index_at(entry_time)
    if ei < 0:
        return {"ok": False, "error": "entry_bar_missing"}

    # Build timeline of (start_i, tp, sl) segments
    plan_tf = first_tf
    tp, sl = tpsl(first_tf)
    # sort upgrades by apply time
    ups = sorted(upgrade_events, key=lambda u: ts_utc(u.apply_at_entry_time))
    segments: list[tuple[int, int, float, float, str]] = []  # start, end_excl, tp, sl, tf

    cur_i = ei
    for u in ups:
        ui = book.index_at(u.apply_at_entry_time)
        if ui < 0 or ui < ei:
            continue
        if TF_RANK[u.tf] <= TF_RANK[plan_tf]:
            continue
        if ui > cur_i:
            segments.append((cur_i, ui, tp, sl, plan_tf))
        tp, sl = tpsl(u.tf)
        plan_tf = u.tf
        cur_i = ui

    end_hold = ei
    # progressive max-hold from original entry (extends on each upgrade)
    hold = STRATEGY_MAX_HOLD_BY_TF[first_tf]
    plan_tf2 = first_tf
    for u in ups:
        if TF_RANK[u.tf] > TF_RANK[plan_tf2]:
            hold = max(hold, STRATEGY_MAX_HOLD_BY_TF[u.tf])
            plan_tf2 = u.tf
    t_end = book.times[ei] + np.timedelta64(int(hold), "m")
    end_hold = min(len(book.times) - 1, max(ei, int(np.searchsorted(book.times, t_end, side="right") - 1)))

    segments.append((cur_i, end_hold + 1, tp, sl, plan_tf))

    same_bar_both = 0
    sl_first_ok = 0
    retro_viol = 0

    # Retroactive check: for each upgrade, ensure no segment applies new ladder before apply_i
    for u in ups:
        ui = book.index_at(u.apply_at_entry_time)
        ntp, nsl = tpsl(u.tf)
        for s0, s1, stp, ssl, stf in segments:
            if s1 <= ui:
                # segment entirely before upgrade — must not already be upgraded tf
                if TF_RANK.get(stf, -1) >= TF_RANK[u.tf] and stf == u.tf and s0 < ui:
                    retro_viol += 1

    reason = None
    exit_i = None
    gross = None
    amb = False
    highest = first_tf

    for s0, s1, stp, ssl, stf in segments:
        highest = stf if TF_RANK[stf] >= TF_RANK[highest] else highest
        for i in range(max(s0, ei), min(s1, end_hold + 1)):
            if forced_conflict_exit_i is not None and i == forced_conflict_exit_i:
                reason = "HIGHER_TF_CONFLICT"
                exit_i = i
                gross = gross_dir(side, entry_price, float(book.opens[i]))
                break
            r, g, a = scan_bar_sl_first(
                side, entry_price, float(book.highs[i]), float(book.lows[i]), stp, ssl
            )
            if a:
                same_bar_both += 1
                if r == "SL":
                    sl_first_ok += 1
            if r is not None:
                reason, exit_i, gross, amb = r, i, g, a
                break
        if reason is not None:
            break

    if reason is None:
        # timeout at end_hold
        reason = "TIMEOUT"
        exit_i = end_hold
        gross = gross_dir(side, entry_price, float(book.closes[end_hold]))

    assert exit_i is not None and gross is not None
    exit_px = (
        float(book.opens[exit_i])
        if reason == "HIGHER_TF_CONFLICT"
        else exit_price_from_gross(side, entry_price, float(gross))
        if reason in ("TP", "SL")
        else float(book.closes[exit_i])
    )
    # For TP/SL, engine uses theoretical price from gross, not candle extreme
    if reason in ("TP", "SL"):
        exit_px = exit_price_from_gross(side, entry_price, float(gross))

    return {
        "ok": True,
        "exit_reason": reason,
        "exit_i": int(exit_i),
        "exit_time": ts_utc(pd.Timestamp(book.times[exit_i])),
        "exit_price": float(exit_px),
        "gross_return_pct": float(gross),
        "net_return_pct": float(gross) - FEE_PCT,
        "highest_tf": highest,
        "same_bar_both_hit": int(same_bar_both),
        "sl_first_correct": int(sl_first_ok),
        "retroactive_upgrade_violations": int(retro_viol),
        "ambiguous_sl_first": bool(amb),
        "max_hold_min_used": int(hold),
        "entry_i": int(ei),
    }


def match_upgrades_from_signals(
    signals: pd.DataFrame,
    *,
    symbol: str,
    side: str,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
    first_tf: str,
    expected_seq: list[str],
    book: MinuteBook,
) -> list[UpgradeEvent]:
    """Pick causal same-symbol same-side higher-TF signals while open; build upgrade list."""
    if signals.empty:
        return []
    et0 = ts_utc(entry_time)
    xt = ts_utc(exit_time)
    df = signals[
        (signals["symbol"].astype(str) == symbol)
        & (signals["side"].astype(str) == side)
    ].copy()
    if df.empty:
        return []
    df["confirmation_available_at"] = pd.to_datetime(df["confirmation_available_at"], utc=True)
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    # upgrades apply at their T0 while position open, strictly after trade entry
    df = df[(df["entry_time"] > et0) & (df["entry_time"] <= xt)].sort_values("entry_time")
    plan = first_tf
    out: list[UpgradeEvent] = []
    expected_higher = expected_seq[1:] if len(expected_seq) > 1 else []
    exp_i = 0
    for _, row in df.iterrows():
        tf = str(row["signal_tf"])
        if TF_RANK[tf] <= TF_RANK[plan]:
            continue
        # optional: prefer matching expected sequence order
        if expected_higher and exp_i < len(expected_higher):
            # allow skip if signal matches later expected? stick to first higher each time
            pass
        out.append(
            UpgradeEvent(
                tf=tf,
                available_at=ts_utc(row["confirmation_available_at"]),
                apply_at_entry_time=ts_utc(row["entry_time"]),
            )
        )
        plan = tf
        exp_i += 1
    return out

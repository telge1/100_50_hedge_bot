"""Causal path simulation: structural SL vs confirmed EMA-cross exit. No TPs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence

import pandas as pd

from pool_order_plan_v1.candles import ensure_utc

from .config import FEE_PCT, RATCHET_VARIANT, STATIC_VARIANT
from .ema_regime import confirmed_strong_crosses, indicators_for_bars
from .pool_bias import cluster_rows
from .protection import select_thin_protection, sl_from_cluster
from .tf_bars import causal_tf_prefix


def _pnl(direction: str, entry: float, exit_px: float) -> float:
    if direction == "LONG":
        return (exit_px - entry) / entry * 100.0
    return (entry - exit_px) / entry * 100.0


def simulate_path(
    *,
    executed_direction: str,
    entry_time,
    entry_price: float,
    initial_sl: float,
    one_minute: pd.DataFrame,
    five_minute: pd.DataFrame,
    all_pools: list,
    signal_tf_bars: pd.DataFrame,
    variant: str,
    window_end,
    ema_exit_kind: str,
) -> dict[str, Any]:
    et = ensure_utc(entry_time)
    end = ensure_utc(window_end)
    direction = executed_direction.upper()
    sl = float(initial_sl)
    ratchet_steps: list[dict[str, Any]] = []
    pending_sl: float | None = None
    pending_from: datetime | None = None

    m1 = one_minute.copy()
    m1["open_time"] = pd.to_datetime(m1["open_time"], utc=True)
    after = m1.loc[m1["open_time"] >= pd.Timestamp(et)].sort_values("open_time")

    tf = causal_tf_prefix(signal_tf_bars, et)
    # indicators on growing prefix as TF bars close after entry
    full_tf = signal_tf_bars.copy()
    full_tf["close_time"] = pd.to_datetime(full_tf["close_time"], utc=True)
    full_tf["timestamp"] = pd.to_datetime(full_tf["timestamp"], utc=True)

    five = five_minute.copy()
    five["close_time"] = pd.to_datetime(five["close_time"], utc=True)
    five["timestamp"] = pd.to_datetime(five["timestamp"], utc=True)

    def ema_exit_fill(as_of: datetime) -> tuple[datetime, float] | None:
        prefix = full_tf.loc[full_tf["close_time"] <= pd.Timestamp(as_of)]
        if prefix.empty or len(prefix) < 22:
            return None
        inds = indicators_for_bars(prefix["close"].tolist(), prefix["high"].tolist(), prefix["low"].tolist())
        events = confirmed_strong_crosses(inds)
        hits = [e for e in events if e["kind"] == ema_exit_kind]
        if not hits:
            return None
        idx = int(hits[-1]["index"])
        confirm_close = ensure_utc(prefix.iloc[idx]["close_time"])
        if confirm_close <= et:
            return None
        nxt = full_tf.loc[full_tf["timestamp"] > prefix.iloc[idx]["timestamp"]]
        if nxt.empty:
            return None
        fill_open = ensure_utc(nxt.iloc[0]["timestamp"])
        fill_px = float(nxt.iloc[0]["open"])
        if fill_open > as_of:
            return None
        return fill_open, fill_px

    last_seen = et
    for _, bar in after.iterrows():
        ot = ensure_utc(bar["open_time"])
        if ot >= end:
            break
        ct = ot + timedelta(minutes=1)
        high = float(bar["high"])
        low = float(bar["low"])
        o = float(bar["open"])

        if pending_sl is not None and pending_from is not None and ot >= pending_from:
            sl = pending_sl
            pending_sl = None
            pending_from = None

        if variant == RATCHET_VARIANT:
            closed_5 = five.loc[(five["close_time"] <= pd.Timestamp(ot)) & (five["close_time"] > pd.Timestamp(et))]
            if not closed_5.empty:
                last5_close = ensure_utc(closed_5.iloc[-1]["close_time"])
                if last5_close > last_seen:
                    from pool_order_plan_v1.pool_snapshot import snapshot_pools

                    snap = snapshot_pools(all_pools, last5_close)
                    clusters = cluster_rows(snap, entry_price)
                    prot = select_thin_protection(clusters, entry=entry_price, executed_direction=direction)
                    if prot is not None:
                        cand = sl_from_cluster(prot, executed_direction=direction, entry=entry_price)
                        new_sl = float(cand["sl_price"])
                        improved = (new_sl > sl + 1e-12) if direction == "LONG" else (new_sl < sl - 1e-12)
                        if improved:
                            pending_sl = new_sl
                            pending_from = last5_close  # earliest next 5m: already closed; apply from next 1m after close
                            ratchet_steps.append(
                                {
                                    "effective_from": last5_close.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "sl_price": new_sl,
                                    "cluster": prot,
                                }
                            )
                    last_seen = last5_close

        sl_hit = (low <= sl) if direction == "LONG" else (high >= sl)
        ema_fill = ema_exit_fill(ct)

        if sl_hit:
            exit_px = sl
            gross = _pnl(direction, entry_price, exit_px)
            fees = FEE_PCT
            return {
                "outcome": "SL",
                "exit_reason": "STRUCTURAL_POOL_SL",
                "exit_time": ot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "exit_price": exit_px,
                "gross_pnl_pct": gross,
                "fees_pct": fees,
                "net_pnl_pct": gross - fees,
                "sl_price_final": sl,
                "ratchet_steps": ratchet_steps,
                "variant": variant,
            }

        if ema_fill is not None:
            fill_t, fill_px = ema_fill
            if fill_t <= ct:
                gross = _pnl(direction, entry_price, fill_px)
                fees = FEE_PCT
                return {
                    "outcome": "EMA_CROSS",
                    "exit_reason": ema_exit_kind,
                    "exit_time": fill_t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "exit_price": fill_px,
                    "gross_pnl_pct": gross,
                    "fees_pct": fees,
                    "net_pnl_pct": gross - fees,
                    "sl_price_final": sl,
                    "ratchet_steps": ratchet_steps,
                    "variant": variant,
                }

    return {
        "outcome": "OPEN",
        "exit_reason": "WINDOW_END_OPEN",
        "exit_time": None,
        "exit_price": None,
        "gross_pnl_pct": None,
        "fees_pct": None,
        "net_pnl_pct": None,
        "sl_price_final": sl,
        "ratchet_steps": ratchet_steps,
        "variant": variant,
    }


def simulate_baseline(
    *,
    direction: str,
    entry_time,
    entry_price: float,
    sl: float | None,
    tp: float | None,
    one_minute: pd.DataFrame,
    window_end,
) -> dict[str, Any]:
    if sl is None:
        return {"outcome": "NO_SL", "gross_pnl_pct": None, "net_pnl_pct": None, "fees_pct": None}
    et = ensure_utc(entry_time)
    end = ensure_utc(window_end)
    m1 = one_minute.copy()
    m1["open_time"] = pd.to_datetime(m1["open_time"], utc=True)
    after = m1.loc[m1["open_time"] >= pd.Timestamp(et)].sort_values("open_time")
    d = direction.upper()
    for _, bar in after.iterrows():
        ot = ensure_utc(bar["open_time"])
        if ot >= end:
            break
        high = float(bar["high"])
        low = float(bar["low"])
        sl_hit = (low <= float(sl)) if d == "LONG" else (high >= float(sl))
        tp_hit = False
        if tp is not None:
            tp_hit = (high >= float(tp)) if d == "LONG" else (low <= float(tp))
        if sl_hit and tp_hit:
            sl_hit = True
            tp_hit = False
        if sl_hit:
            px = float(sl)
            g = _pnl(d, entry_price, px)
            return {
                "outcome": "SL",
                "exit_reason": "BASELINE_SL",
                "exit_time": ot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "gross_pnl_pct": g,
                "fees_pct": FEE_PCT,
                "net_pnl_pct": g - FEE_PCT,
            }
        if tp_hit:
            px = float(tp)
            g = _pnl(d, entry_price, px)
            return {
                "outcome": "TP",
                "exit_reason": "BASELINE_TP",
                "exit_time": ot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "gross_pnl_pct": g,
                "fees_pct": FEE_PCT,
                "net_pnl_pct": g - FEE_PCT,
            }
    return {"outcome": "OPEN", "exit_reason": "WINDOW_END_OPEN", "gross_pnl_pct": None, "fees_pct": None, "net_pnl_pct": None}

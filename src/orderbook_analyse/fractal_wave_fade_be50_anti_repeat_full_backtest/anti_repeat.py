"""SAME_SIDE anti-repeat filter with opposite-wave structural reset."""

from __future__ import annotations

from typing import Any

import pandas as pd

from orderbook_analyse.fractal_parent_lower_tf_quality_db.db_build import build_waves_from_db
from orderbook_analyse.fractal_signal_confluence_db import ENV_FILE, SIGNAL_TFS
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")


def build_wave_reset_index(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """
    Per symbol: completed waves (all signal TFs) with end_available_at + direction.
    Reset after SHORT SL requires a DOWN wave end after SL.
    Reset after LONG SL requires an UP wave end after SL.
    """
    load_env_file(ENV_FILE)
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        for tf in SIGNAL_TFS:
            print(f"[waves] {sym} {tf} …", flush=True)
            w = build_waves_from_db(sym, tf)
            if w is None or w.empty:
                continue
            for _, r in w.iterrows():
                rows.append(
                    {
                        "symbol": sym,
                        "timeframe": tf,
                        "direction": str(r["direction"]),
                        "end_available_at": _utc(r["end_available_at"]),
                    }
                )
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    df = df.sort_values("end_available_at").reset_index(drop=True)
    return {str(sym): g.reset_index(drop=True) for sym, g in df.groupby("symbol")}


def wave_reset_available(
    wave_index: dict[str, pd.DataFrame],
    *,
    symbol: str,
    blocked_side: str,
    sl_exit: pd.Timestamp,
    asof: pd.Timestamp,
    prefer_tf: str | None = None,
) -> dict[str, Any] | None:
    """Intervening opposite wave completed in (sl_exit, asof]. Prefer SL TF when possible."""
    g = wave_index.get(symbol)
    if g is None or g.empty:
        return None
    need_dir = "DOWN" if blocked_side == "SHORT" else "UP"
    sl_exit = _utc(sl_exit)
    asof = _utc(asof)
    m = g[
        (g["direction"] == need_dir)
        & (g["end_available_at"] > sl_exit)
        & (g["end_available_at"] <= asof)
    ]
    if prefer_tf:
        m = m[m["timeframe"] == prefer_tf]
    if m.empty:
        return None
    r = m.iloc[0]
    return {
        "reset_time": r["end_available_at"],
        "reset_direction": need_dir,
        "reset_tf": r["timeframe"],
        "reset_source": "wave_end",
        "reset_reason": "OPPOSITE_WAVE_COMPLETED",
    }


def apply_anti_repeat(
    be50_trades: pd.DataFrame,
    wave_index: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    """Walk frozen BE50 trades; SAME_SIDE_BLOCK after true SL until opposite-wave reset."""
    df = be50_trades.sort_values(["seq", "trade_id"]).reset_index(drop=True).copy()
    blocks: dict[str, dict[str, Any]] = {}
    kept_rows: list[dict[str, Any]] = []
    blocked_rows: list[dict[str, Any]] = []
    reset_events: list[dict[str, Any]] = []
    post_sl_signals: list[dict[str, Any]] = []
    last_sl: dict[tuple[str, str], dict[str, Any]] = {}

    for _, tr in df.iterrows():
        sym = str(tr["symbol"])
        side = str(tr["side"])
        entry = _utc(tr["entry_time"])
        be_reason = str(tr["be50_reason"])
        base_reason = str(tr["baseline_reason"])
        tid = int(tr["trade_id"])
        first_tf = str(tr.get("first_signal_tf", ""))

        key = (sym, side)
        if key in last_sl:
            prev = last_sl[key]
            hours = (entry - _utc(prev["exit"])).total_seconds() / 3600.0
            post_sl_signals.append(
                {
                    "trade_id": tid,
                    "symbol": sym,
                    "side": side,
                    "entry_time": entry,
                    "hours_since_sl": hours,
                    "prev_sl_trade_id": prev["trade_id"],
                    "be50_reason": be_reason,
                    "baseline_reason": base_reason,
                    "be50_net_pct": float(tr["be50_net_pct"]),
                    "baseline_net_pct": float(tr["baseline_net_pct"]),
                }
            )

        blk = blocks.get(sym)
        if blk is not None and blk["side"] == side:
            reset = wave_reset_available(
                wave_index,
                symbol=sym,
                blocked_side=side,
                sl_exit=blk["sl_exit"],
                asof=entry,
                prefer_tf=blk.get("sl_tf"),
            )
            if reset is not None:
                reset_events.append(
                    {
                        "symbol": sym,
                        "blocked_side": side,
                        "sl_trade_id": blk["sl_trade_id"],
                        "sl_exit_time": blk["sl_exit"],
                        "cleared_before_trade_id": tid,
                        "cleared_at_entry": entry,
                        **reset,
                    }
                )
                del blocks[sym]
            else:
                hours = (entry - _utc(blk["sl_exit"])).total_seconds() / 3600.0
                blocked_rows.append(
                    {
                        "trade_id": tid,
                        "seq": int(tr["seq"]),
                        "timestamp": entry,
                        "symbol": sym,
                        "side": side,
                        "original_baseline_outcome": base_reason,
                        "original_be50_outcome": be_reason,
                        "original_baseline_net_pct": float(tr["baseline_net_pct"]),
                        "original_be50_net_pct": float(tr["be50_net_pct"]),
                        "prev_sl_trade_id": blk["sl_trade_id"],
                        "prev_sl_exit_time": blk["sl_exit"],
                        "hours_since_prev_sl": hours,
                        "same_wave_proxy": True,
                        "same_side": True,
                        "reset_already_occurred": False,
                        "block_reason": "BLOCK_REPEAT_SAME_SIDE_NO_OPPOSITE_WAVE_RESET",
                    }
                )
                continue

        kept_rows.append(dict(tr))

        if be_reason == "SL":
            blocks[sym] = {
                "side": side,
                "sl_trade_id": tid,
                "sl_exit": _utc(tr["be50_exit_time"]),
                "sl_entry": entry,
                "sl_tf": first_tf,
            }
            last_sl[(sym, side)] = {
                "trade_id": tid,
                "exit": _utc(tr["be50_exit_time"]),
            }

    return {
        "kept": pd.DataFrame(kept_rows),
        "blocked": pd.DataFrame(blocked_rows),
        "resets": pd.DataFrame(reset_events),
        "post_sl_signals": pd.DataFrame(post_sl_signals),
    }

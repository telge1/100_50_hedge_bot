"""Load evaluation outcomes and signal metadata. SELECT-only candles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import EVAL_DIR, FEE_PP, JOB_DIR

from research.stoch_fade_filter_tests.zec_5m_exhaustion.io import (
    connect_readonly,
    iso_z,
    load_jsonl,
    load_symbol_1m,
    to_utc,
)


def find_signals_jsonl(symbol: str) -> Path:
    matches = sorted((JOB_DIR / "coin_runs" / symbol).glob("*/signals.jsonl"))
    if not matches:
        raise FileNotFoundError(f"SIGNALS_MISSING:{symbol}")
    return matches[0]


def load_coin_trades(symbol: str) -> pd.DataFrame:
    outcomes = load_jsonl(EVAL_DIR / "coin_runs" / symbol / "outcomes.jsonl")
    signals = load_jsonl(find_signals_jsonl(symbol))
    by_id = {str(r["signal_id"]): r for r in signals}
    rows = []
    for rec in outcomes:
        sid = str(rec["signal_id"])
        sig = by_id.get(sid, {})
        try:
            meta = json.loads(sig.get("metadata") or "{}")
        except json.JSONDecodeError:
            meta = {}
        wave = meta.get("wave_end_price")
        if wave is None:
            wave = rec.get("wave_end_price")
        rows.append(
            {
                "signal_id": sid,
                "setup_id": rec.get("setup_id"),
                "symbol": rec.get("symbol") or symbol,
                "timeframe": rec.get("timeframe"),
                "direction": str(rec.get("direction") or "").upper(),
                "outcome": str(rec.get("outcome") or "").upper(),
                "is_open": bool(rec.get("is_open") or rec.get("outcome") == "OPEN"),
                "entry_time": to_utc(rec["entry_time"]),
                "exit_time": to_utc(rec["exit_time"]) if rec.get("exit_time") else pd.NaT,
                "entry_price": float(rec["entry_price"]),
                "tp_price": float(rec["tp_price"]),
                "sl_price": float(rec.get("initial_sl_price") or rec.get("sl_price")),
                "exit_price": rec.get("exit_price"),
                "exit_reason": rec.get("exit_reason"),
                "hold_seconds": rec.get("duration_seconds"),
                "pnl_pct_gross": rec.get("pnl_pct_gross"),
                "wave_end_price": None if wave is None else float(wave),
                "end_ts": rec.get("end_ts"),
                "end_available_at": rec.get("end_available_at"),
                "recognition_ts": rec.get("recognition_ts"),
                "confirmation_available_at": rec.get("confirmation_available_at"),
            }
        )
    frame = pd.DataFrame(rows)
    frame["pnl_pct_gross"] = pd.to_numeric(frame["pnl_pct_gross"], errors="coerce")
    frame["pnl_pct_net"] = frame["pnl_pct_gross"] - FEE_PP
    frame.loc[frame["outcome"] == "OPEN", "pnl_pct_net"] = np.nan
    return frame.sort_values(["entry_time", "signal_id"]).reset_index(drop=True)

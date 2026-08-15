"""R0 / R1 / R2 confirmation stages for level-absorption events."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.liquidity_sweep_reclaim.reclaim import r1_same_candle, reclaim_close
from research.regime_scanner.liquidity_sweep_reclaim.sweep import measure_sweep
from research.regime_scanner.orderflow_absorption_level.config import LevelAbsorptionConfig
from research.regime_scanner.oi_price_delta_pattern.features import _contiguous


def _lsr_side(level_side: str) -> str:
    # LSR: long = support reclaim from below; short = resistance reclaim from above
    return "long" if level_side == "support" else "short"


def _expected_close_ok(level_side: str, level_price: float, close: float) -> bool:
    if level_side == "support":
        return close >= level_price
    return close <= level_price


def confirmation_r0(event: dict[str, Any]) -> dict[str, Any]:
    start = int(event["event_start_index"])
    return {
        **event,
        "confirmation_type": "R0",
        "confirmation_id": f"{event['event_id']}|R0",
        "entry_eligible_index": start,
        "entry_eligible_timestamp": event["event_start_timestamp"],
        "confirmation_bar_index": start,
        "confirmation_ok": True,
        "confirmation_reason": "raw_level_absorption",
    }


def confirmation_r1(
    df: pd.DataFrame,
    event: dict[str, Any],
    cfg: LevelAbsorptionConfig,
) -> dict[str, Any] | None:
    """Rejection: wick tests/pierces level; close finishes on expected side."""
    if event.get("level_price") is None or event.get("no_level") or event.get("far_from_level"):
        return None
    level_side = str(event["level_side"])
    level_price = float(event["level_price"])
    lsr = _lsr_side(level_side)
    start = int(event["event_start_index"])
    end = int(event["event_end_index"])
    seq = df["sequence_id"].to_numpy()
    ts = df["bucket_start"].to_numpy()
    for i in range(start, end + 1):
        if i > start and not _contiguous(seq, ts, start, i):
            break
        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])
        open_ = float(df["open"].iloc[i])
        close = float(df["close"].iloc[i])
        atr = float(df["atr_14"].iloc[i - 1]) if i >= 1 else float(df["atr_14"].iloc[i])
        if not np.isfinite(atr) or atr <= 0:
            continue
        sweep = measure_sweep(
            side=lsr,
            level=level_price,
            high=high,
            low=low,
            open_=open_,
            close=close,
            atr=atr,
        )
        wick_test = sweep is not None
        if not wick_test:
            # also allow wick touch without full beyond (test)
            if level_side == "support" and low <= level_price <= high:
                wick_test = True
            if level_side == "resistance" and low <= level_price <= high:
                wick_test = True
        if not wick_test:
            continue
        if not _expected_close_ok(level_side, level_price, close):
            continue
        # Prefer LSR same-candle reclaim when swept beyond
        if sweep is not None and not r1_same_candle(
            side=lsr, level=level_price, swept=True, close=close
        ):
            # still accept close-on-side after wick test
            pass
        return {
            **event,
            "confirmation_type": "R1",
            "confirmation_id": f"{event['event_id']}|R1",
            "entry_eligible_index": i,
            "entry_eligible_timestamp": str(df["bucket_start"].iloc[i]),
            "confirmation_bar_index": i,
            "confirmation_ok": True,
            "confirmation_reason": "rejection_wick_close_side",
        }
    return None


def confirmation_r2(
    df: pd.DataFrame,
    event: dict[str, Any],
    cfg: LevelAbsorptionConfig,
) -> dict[str, Any] | None:
    """Break + reclaim within 1–3 bars after event start."""
    if event.get("level_price") is None or event.get("no_level") or event.get("far_from_level"):
        return None
    level_side = str(event["level_side"])
    level_price = float(event["level_price"])
    lsr = _lsr_side(level_side)
    start = int(event["event_start_index"])
    n = len(df)
    seq = df["sequence_id"].to_numpy()
    ts = df["bucket_start"].to_numpy()
    # Find first break beyond level in [start, start+3]
    break_i: int | None = None
    for i in range(start, min(n, start + 4)):
        if i > start and not _contiguous(seq, ts, start, i):
            return None
        close = float(df["close"].iloc[i])
        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])
        if level_side == "support" and (low < level_price or close < level_price):
            break_i = i
            break
        if level_side == "resistance" and (high > level_price or close > level_price):
            break_i = i
            break
    if break_i is None:
        return None
    # Reclaim within 1–3 bars after break
    for j in range(break_i, min(n, break_i + 4)):
        if j > break_i and not _contiguous(seq, ts, break_i, j):
            return None
        bars_since = j - break_i
        if bars_since < 1 and j == break_i:
            # same-bar reclaim allowed
            close = float(df["close"].iloc[j])
            if reclaim_close(side=lsr, level=level_price, close=close):
                return {
                    **event,
                    "confirmation_type": "R2",
                    "confirmation_id": f"{event['event_id']}|R2",
                    "entry_eligible_index": j,
                    "entry_eligible_timestamp": str(df["bucket_start"].iloc[j]),
                    "confirmation_bar_index": j,
                    "confirmation_ok": True,
                    "confirmation_reason": "break_reclaim_same_bar",
                    "break_index": break_i,
                }
            continue
        if bars_since > 3:
            break
        close = float(df["close"].iloc[j])
        if reclaim_close(side=lsr, level=level_price, close=close):
            return {
                **event,
                "confirmation_type": "R2",
                "confirmation_id": f"{event['event_id']}|R2",
                "entry_eligible_index": j,
                "entry_eligible_timestamp": str(df["bucket_start"].iloc[j]),
                "confirmation_bar_index": j,
                "confirmation_ok": True,
                "confirmation_reason": f"break_reclaim_{bars_since}_bars",
                "break_index": break_i,
            }
    return None


def build_confirmation_events(
    df: pd.DataFrame,
    events: list[dict[str, Any]],
    cfg: LevelAbsorptionConfig,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        if "R0" in cfg.confirmations:
            out.append(confirmation_r0(ev))
        if "R1" in cfg.confirmations:
            r1 = confirmation_r1(df, ev, cfg)
            if r1 is not None:
                out.append(r1)
        if "R2" in cfg.confirmations:
            r2 = confirmation_r2(df, ev, cfg)
            if r2 is not None:
                out.append(r2)
    return out

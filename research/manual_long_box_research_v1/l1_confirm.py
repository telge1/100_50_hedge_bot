"""L1 confirmation modes — semantics from touch_watcher_research_v3an/watcher_fsm.py."""

from __future__ import annotations

from typing import Any, Literal

ConfirmationMode = Literal["A1", "A2", "A3"]


def confirm_a1(c: dict[str, Any], entry_level: float) -> bool:
    return float(c["close"]) > entry_level


def confirm_a2(c: dict[str, Any], entry_level: float, prev_close: float) -> bool:
    return float(c["close"]) > entry_level and float(c["close"]) > prev_close


def confirm_a3(
    c: dict[str, Any], entry_level: float, prev_close: float, min_gain_pct: float
) -> bool:
    if float(c["close"]) <= entry_level:
        return False
    rel = (float(c["close"]) - prev_close) / prev_close * 100.0
    if rel < float(min_gain_pct) - 1e-9:
        return False
    return True


def evaluate_confirmation(
    mode: ConfirmationMode,
    candle: dict[str, Any],
    entry_level: float,
    prev_close: float,
    *,
    min_gain_pct: float = 0.20,
) -> bool:
    if mode == "A1":
        return confirm_a1(candle, entry_level)
    if mode == "A2":
        return confirm_a2(candle, entry_level, prev_close)
    if mode == "A3":
        return confirm_a3(candle, entry_level, prev_close, min_gain_pct)
    raise ValueError(f"unsupported confirmation mode: {mode!r}")

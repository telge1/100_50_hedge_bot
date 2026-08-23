"""Candidate evaluation for multicoin frozen validation — shared canonical engine."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from ..shared_strategy.candidates import detect_candidates_for_scopes, evaluate_candidates_canonical
from ..shared_strategy.semantics import ENTRY_RULE, MULTICOIN_DETECTION_SCOPES

# Re-export for tests / callers
__all__ = [
    "ENTRY_RULE",
    "MODE_IDS_NEEDED",
    "detect_modes_for_coin",
    "evaluate_candidates_multicoin",
    "modes_for_multicoin",
]

MODE_IDS_NEEDED = tuple(sorted({m for _, m in MULTICOIN_DETECTION_SCOPES}))


def modes_for_multicoin() -> list[dict[str, Any]]:
    from ..mfe_runner import build_mode_catalog

    catalog = {m["mode_id"]: m for m in build_mode_catalog()}
    return [catalog[m] for m in MODE_IDS_NEEDED]


def evaluate_candidates_multicoin(
    raw_list: list[dict[str, Any]],
    *,
    df: pd.DataFrame,
    candles_1m: pd.DataFrame,  # kept for call-site compatibility; unused (TF-next-open)
    symbol: str,
    timeframe: str,
    window_start: datetime,
    window_end: datetime,
    trades_1m,
    ob_1m,
    oi_1m,
    liq,
    window_report: dict[str, Any] | None,
    mode_id: str,
) -> list[dict[str, Any]]:
    """Thin wrapper → shared canonical evaluator (original XRP entry semantics)."""
    del candles_1m  # entry uses signal-TF next open, not 1m scan
    return evaluate_candidates_canonical(
        raw_list,
        df=df,
        symbol=symbol,
        timeframe=timeframe,
        window_start=window_start,
        window_end=window_end,
        trades_1m=trades_1m,
        ob_1m=ob_1m,
        oi_1m=oi_1m,
        liq=liq,
        window_report=window_report,
        mode_id=mode_id,
    )


def detect_modes_for_coin(
    *,
    df_by_tf: dict[str, pd.DataFrame],
    candles_1m: pd.DataFrame,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    trades_1m,
    ob_1m,
    oi_1m,
    liq,
    window_report: dict[str, Any] | None,
    timeframes: tuple[str, ...] = ("5m", "15m"),
) -> list[dict[str, Any]]:
    del candles_1m, timeframes
    return detect_candidates_for_scopes(
        df_by_tf=df_by_tf,
        symbol=symbol,
        window_start=window_start,
        window_end=window_end,
        trades_1m=trades_1m,
        ob_1m=ob_1m,
        oi_1m=oi_1m,
        liq=liq,
        window_report=window_report,
        scopes=MULTICOIN_DETECTION_SCOPES,
    )

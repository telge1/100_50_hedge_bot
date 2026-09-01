"""Frozen BLOCK_5M_EXHAUSTED_IN_TRADE_DIRECTION definition.

Copied from the ZEC causal trade-context analysis. Do not retune.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import STOCH_HIGH, STOCH_LOW

RULE_ID = "BLOCK_5M_EXHAUSTED_IN_TRADE_DIRECTION"
FEATURE_NAME = "stoch_exhausted_in_trade_direction"
SOURCE_COLUMNS = (
    "tf_5m_stoch_exhausted_in_trade_direction",
    "ltf_5m_exhausted",
)

# Exact snapshot-time implementation from
# research/stoch_fade_trade_context_analysis/pipeline.py snapshot_row().
SOURCE_CODE = """
k = row["stoch_k"]
exhausted = bool(k < STOCH_LOW) if str(direction).upper() == "SHORT" else bool(k > STOCH_HIGH) if pd.notna(k) else False
if pd.isna(k):
    exhausted = False
"""

SOURCE_DICTIONARY = {
    "indicators.stoch": "Gold Wilder RSI 14, StochRSI 14, K SMA 3, D SMA 3.",
    "causality.rule": "For entry T, last fully closed bar per TF with available_at <= T.",
    "causality.htf": "Last bucket whose close_time <= T. Running 4h candle forbidden.",
    "stoch_phase": {
        "OVERSOLD": "K < 20 and not turning up.",
        "OVERSOLD_TURNING_UP": "K < 20 and bullish cross or K and D rising.",
        "BULL_MOMENTUM": "K > D and 20 <= K <= 80.",
        "OVERBOUGHT": "K > 80 and not turning down.",
        "OVERBOUGHT_TURNING_DOWN": "K > 80 and bearish cross or K and D falling.",
        "BEAR_MOMENTUM": "K < D and 20 <= K <= 80.",
        "NEUTRAL": "Else, including missing K/D.",
    },
}


def is_missing_k(stoch_k: object) -> bool:
    if stoch_k is None:
        return True
    try:
        if pd.isna(stoch_k):
            return True
    except (TypeError, ValueError):
        return True
    if isinstance(stoch_k, (float, np.floating)) and not np.isfinite(stoch_k):
        return True
    return False


def stoch_exhausted_in_trade_direction(direction: str, stoch_k: object) -> bool:
    """Block flag from last closed 5m StochRSI %K only.

    LONG: K > 80 (already extended up).
    SHORT: K < 20 (already extended down).
    Missing K: False (do not invent a block).
    K == 20 or K == 80 is not exhausted (strict inequality, matching source).
    D and phase turning flags are NOT part of this flag.
    """
    if is_missing_k(stoch_k):
        return False
    k = float(stoch_k)
    side = str(direction).upper()
    if side == "SHORT":
        return bool(k < STOCH_LOW)
    return bool(k > STOCH_HIGH)


def rule_manifest() -> dict[str, Any]:
    return {
        "rule_id": RULE_ID,
        "feature_name": FEATURE_NAME,
        "source_columns": list(SOURCE_COLUMNS),
        "source_analysis": "results/stoch_fade_trade_context_analysis/ZECUSDT_94d0cfbfb2da4c829dc0d95588dc052d",
        "copied_source_code": SOURCE_CODE.strip(),
        "copied_feature_dictionary": SOURCE_DICTIONARY,
        "stoch_low": STOCH_LOW,
        "stoch_high": STOCH_HIGH,
        "long_block": "last fully closed 5m StochRSI K > 80",
        "short_block": "last fully closed 5m StochRSI K < 20",
        "missing_k": False,
        "uses_d": False,
        "uses_phase_turning": False,
        "uses_running_5m_bar": False,
        "uses_future_bars": False,
        "no_parameter_search": True,
        "no_combo_with_4h_trend": True,
        "entry_time_unchanged": True,
        "tp_sl_unchanged": True,
        "confirmation": "cross_recognition",
        "exit": "NO_BE50 / full_1m_scan / SL_FIRST / no max-hold",
    }

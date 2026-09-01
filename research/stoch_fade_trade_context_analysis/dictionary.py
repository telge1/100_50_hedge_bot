"""Feature dictionary for the ZEC causal trade-context export."""

from __future__ import annotations

from typing import Any

FEATURE_DICTIONARY: dict[str, Any] = {
    "identity": {
        "signal_id": "Canonical evaluation signal id. SIGNAL_VIEW primary key.",
        "setup_id": "Wave setup UUID5.",
        "generation_key": "symbol|tf|direction|type|wave_end_open.",
        "timeframe": "Signal timeframe 15m/30m/1h/4h.",
        "direction": "LONG or SHORT.",
        "entry_time": "First 1m open strictly after confirmation_available_at.",
        "split": "Chronological 60/20/20 development/validation/test by entry_time. Not used for thresholds.",
    },
    "views": {
        "SIGNAL_VIEW": "Every ZEC evaluation outcome is kept. No silent drop.",
        "EXECUTION_DIAGNOSTIC_VIEW": "Extra overlap/duplicate flags only. Outcomes unchanged.",
    },
    "causality": {
        "rule": "For entry T, last fully closed bar per TF with available_at <= T.",
        "1m": "Bar open T-1m, close T.",
        "htf": "Last bucket whose close_time <= T. Running 4h candle forbidden.",
        "hard_fail": "available_at > entry_time raises LOOKAHEAD.",
        "source_bar_open": "Open of the last closed source bar.",
        "source_bar_close": "Close/available_at of that bar.",
        "available_at_le_entry": "Must be true for every used snapshot.",
    },
    "indicators": {
        "stoch": "Gold Wilder RSI 14, StochRSI 14, K SMA 3, D SMA 3.",
        "ema": "Gold ewm(span, adjust=False, min_periods=span). Extra EMA50/EMA200 for analysis only.",
        "atr": "Wilder ATR 14 on the TF.",
        "missing": "Warm-up NaNs are kept visible. Not imputed.",
    },
    "stoch_phase": {
        "OVERSOLD": "K < 20 and not turning up.",
        "OVERSOLD_TURNING_UP": "K < 20 and bullish cross or K and D rising.",
        "BULL_MOMENTUM": "K > D and 20 <= K <= 80.",
        "OVERBOUGHT": "K > 80 and not turning down.",
        "OVERBOUGHT_TURNING_DOWN": "K > 80 and bearish cross or K and D falling.",
        "BEAR_MOMENTUM": "K < D and 20 <= K <= 80.",
        "NEUTRAL": "Else, including missing K/D.",
    },
    "ema_trend": {
        "STRONG_BULL": "close > EMA20 > EMA50 > EMA200 and 3-bar slopes of 20/50/200 > 0. Requires EMA200.",
        "BULL": "close > EMA20 > EMA50 and EMA20 3-bar slope > 0.",
        "STRONG_BEAR": "close < EMA20 < EMA50 < EMA200 and 3-bar slopes of 20/50/200 < 0. Requires EMA200.",
        "BEAR": "close < EMA20 < EMA50 and EMA20 3-bar slope < 0.",
        "NEUTRAL": "Otherwise when EMA20/50 exist.",
        "MISSING": "EMA20 or EMA50 warm-up incomplete.",
    },
    "structure": {
        "hh_ll": "Causal two-bar comparison only. No future-confirmed pivots.",
        "breakout_closed": "Close > prior-20 high excluding the current bar.",
        "range20_pos": "(price - low20) / (high20 - low20) on last closed bar.",
        "room_to_target": "Distance from entry to 4h 20-bar support (SHORT) or resistance (LONG).",
        "htf_support_before_short_tp": "4h 20-bar low sits between entry and SHORT TP.",
        "htf_resistance_before_long_tp": "4h 20-bar high sits between entry and LONG TP.",
    },
    "pre_entry_path": {
        "scope": "Wave-end A available_at -> entry. 1m data with open_time < entry only.",
        "tp_consumed_frac": "Aligned A-to-entry percent move / signal TP percent.",
        "not_an_outcome": "Uses only prices known at or before entry.",
    },
    "outcome_path": {
        "scope": "Entry 1m through exit 1m. Never used as an entry feature.",
        "pnl_pct_net_0_11pp": "Gross percent minus 0.11 percentage points round-trip fee.",
    },
    "overlap": {
        "exact_entry_duplicate": "Another ZEC trade shares the same entry_time.",
        "higher_tf_would_win": "Among same-entry duplicates, a higher TF exists.",
        "overlaps_previous_trade": "At least one earlier ZEC trade still open at this entry.",
        "overlap_same_direction": "An overlapping open trade has the same direction.",
        "overlap_opposite_direction": "An overlapping open trade has the opposite direction.",
        "number_of_open_zec_trades_at_entry": "Count of earlier still-open ZEC trades.",
    },
}

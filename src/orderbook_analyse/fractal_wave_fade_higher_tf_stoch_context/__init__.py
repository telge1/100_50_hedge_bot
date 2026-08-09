"""Higher-TF Stoch context analysis for validated global-single trades (read-only)."""

from __future__ import annotations

from pathlib import Path

from orderbook_analyse.fractal_cycle_wave_analysis import (
    STOCH_HIGH_K,
    STOCH_LOW_K,
)
from orderbook_analyse.fractal_signal_confluence_db import ENV_FILE, TF_RANK

__all__ = [
    "AUDIT_VERSION",
    "ENV_FILE",
    "STOCH_LOW_K",
    "STOCH_HIGH_K",
    "TF_RANK",
]

AUDIT_VERSION = "fractal_wave_fade_higher_tf_stoch_context_v1"
REF_TRADES = Path("results/fractal_wave_fade_global_single_position_db/trades.csv")
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_higher_tf_stoch_context")

SYMBOLS = ("APTUSDT", "DOGEUSDT")
PRIMARY_TFS = ("15m", "30m", "1h", "4h")
ALL_SNAP_TFS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
HIGHER_SIGNAL_TFS = {
    "15m": ("30m", "1h", "4h"),
    "30m": ("1h", "4h"),
    "1h": ("4h",),
    "4h": (),
}

K_BUCKETS = (
    (0, 10, "0-10"),
    (10, 20, "10-20"),
    (20, 40, "20-40"),
    (40, 60, "40-60"),
    (60, 80, "60-80"),
    (80, 90, "80-90"),
    (90, 100.0001, "90-100"),
)

DEFINITIONS_DOC = f"""
Higher-TF Stoch context analysis ({AUDIT_VERSION})

Purpose: descriptive analysis only. No strategy change / filters / reoptimization.

Trades: fixed list from {REF_TRADES} (validated global single-position backtest).
Market data: MySQL market_candles only via {ENV_FILE}.

Stoch RSI (frozen fractal_cycle_wave_analysis):
  RSI 14 / Stoch length 14 / K smooth 3 / D smooth 3
  Zones: LOW if K < {STOCH_LOW_K}, HIGH if K > {STOCH_HIGH_K}, else MID

Causality:
  For entry_time T, each TF uses the last CLOSED candle with available_at
  (= close_time) <= T. No open HTF bar.

Turn state (last closed bar):
  UP_TURN = bullish K/D cross on that bar
  DOWN_TURN = bearish K/D cross
  NO_TURN otherwise

Wave direction:
  Last completed Stoch wave (segment_stoch_waves) with end_available_at <= T.
  Also ongoing_dir from last K/D cross before T when available.

Relative state (descriptive, side-aware):
  LONG: LOW | TURNING_UP_FROM_LOW | FALLING_TOWARD_LOW | MID | HIGH | FALLING_FROM_HIGH | OTHER
  SHORT: mirrored (HIGH / TURNING_DOWN_FROM_HIGH / RISING_TOWARD_HIGH / …)

Analytical LONG higher-TF support (a priori, NOT outcome-optimized):
  zone == LOW OR turn == UP_TURN
Analytical SHORT higher-TF support:
  zone == HIGH OR turn == DOWN_TURN

higher_tf_support_count = count of higher signal TFs (vs first_signal_tf) that support.
"""

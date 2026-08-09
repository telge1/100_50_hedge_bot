"""All completed Stoch-wave fade audit (APTUSDT)."""

from __future__ import annotations

AUDIT_VERSION = "fractal_all_wave_fade_v1"
SYMBOL = "APTUSDT"
MIN_SAMPLE = 30
VERY_SMALL = 10
ROUNDTRIP_FEE_PCT = 0.11

WAVE_DIR = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_cycle_wave_analysis_apt"
)

TRADING_TFS = ("5m", "15m", "30m", "1h", "4h")

HORIZONS_BY_TF: dict[str, tuple[int, ...]] = {
    "5m": (5, 15, 30, 60, 120),
    "15m": (15, 30, 60, 120, 240),
    "30m": (30, 60, 120, 240, 480),
    "1h": (60, 120, 240, 480, 720),
    "4h": (240, 480, 720, 1440),
}

MAIN_HORIZON_BY_TF: dict[str, int] = {
    "5m": 30,
    "15m": 60,
    "30m": 120,
    "1h": 240,
    "4h": 720,
}

EDGE_DELAYS_BY_TF: dict[str, tuple[int, ...]] = {
    "5m": (0, 1, 3, 5, 10),
    "15m": (0, 1, 3, 5, 10, 15),
    "30m": (0, 5, 10, 15, 30),
    "1h": (0, 5, 15, 30, 60),
    "4h": (0, 15, 30, 60, 120),
}

DURATION_BUCKETS = (
    ("1-2", 1, 2),
    ("3-4", 3, 4),
    ("5-8", 5, 8),
    ("9-16", 9, 16),
    (">16", 17, 10_000),
)

RSI_BUCKETS = (
    ("lt40", None, 40),
    ("40_50", 40, 50),
    ("50_60", 50, 60),
    ("gt60", 60, None),
)

EXTRA_WAVE_COLS = [
    "n_bars",
    "favorable_move_pct",
    "adverse_move_pct",
    "rsi_end",
    "rsi_delta",
    "rsi_start",
]

METHOD_DOC = """
All completed Stoch waves fade:
  UP wave end   -> SHORT (expect DOWN)
  DOWN wave end -> LONG  (expect UP)
Known at end_available_at; entry = first 1m open STRICTLY AFTER.
Failure mask only as comparison group (existing local_failure_mask).
No threshold optimization; RSI/EMA/zones are context only.
Fixed main horizons per TF. APT in-sample only.
"""

__all__ = ["AUDIT_VERSION", "SYMBOL", "TRADING_TFS", "MAIN_HORIZON_BY_TF"]

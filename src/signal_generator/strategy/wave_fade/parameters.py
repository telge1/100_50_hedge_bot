"""Frozen Wave-Fade BE50 parameters — values copied from freeze commit f16ae32.

Source packages (orderbook_analyse @ f16ae32):
- fractal_cycle_wave_analysis
- fractal_signal_confluence_db
- fractal_wave_fade_strategy_backtest_db
- fractal_wave_fade_global_single_position_db
- fractal_wave_fade_be50_july_2026
"""

from __future__ import annotations

from pathlib import Path

BASELINE_LABEL = "fractal_wave_fade_be50_frozen_baseline_v1"
SOURCE_REPO = "/home/telgenbuescher/projects/orderbook_analyse"
SOURCE_COMMIT = "f16ae32da38da86f39e75b09c63c31f62d11996b"
SOURCE_TREE = "78633c9f001b5c3716fbae2bbbb53e9e0bca9a05"

SIGNAL_TFS: tuple[str, ...] = ("15m", "30m", "1h", "4h")
COVERAGE_TFS: tuple[str, ...] = ("1m", "15m", "30m", "1h", "4h")

TF_RANK: dict[str, int] = {"15m": 0, "30m": 1, "1h": 2, "4h": 3}
TF_BAR_MIN: dict[str, int] = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}

# Indicator params (fractal_cycle_wave_analysis)
RSI_LENGTH = 14
STOCH_RSI_LENGTH = 14
STOCH_K_SMOOTH = 3
STOCH_D_SMOOTH = 3
STOCH_LOW_K = 20.0
STOCH_HIGH_K = 80.0
CCI_LENGTH = 20
EMA_SPANS: tuple[int, ...] = (9, 20, 100, 400)

# Wave segmentation
MIN_WAVE_BARS = 3
INEFFICIENT_ABS_PRICE_PCT = 0.02
MIN_ABS_STOCH_DELTA = 10.0

# APT in-sample cutoff for frozen Q4 efficiency edges
APT_IS_END = "2026-08-08T10:21:00+00:00"
EFF_EDGE_SYMBOL = "APTUSDT"  # edges are APT-IS only; not refit per coin

# Fixed pair windows (minutes) — a priori
PAIR_WINDOW_MIN: dict[tuple[str, str], int] = {
    ("15m", "30m"): 30,
    ("30m", "1h"): 60,
    ("1h", "4h"): 240,
    ("15m", "1h"): 60,
    ("15m", "4h"): 240,
    ("30m", "4h"): 240,
}

TPSL_BY_TF: dict[str, tuple[float, float]] = {
    "15m": (1.0, 1.0),
    "30m": (2.0, 1.5),
    "1h": (2.0, 1.5),
    "4h": (4.0, 2.0),
}
TPSL_EXTRA_4H: tuple[float, float] = (6.0, 3.0)

STRATEGY_MAX_HOLD_BY_TF: dict[str, int] = {
    "15m": 24 * 60,
    "30m": 48 * 60,
    "1h": 72 * 60,
    "4h": 10 * 24 * 60,
}

PRIMARY_FEE = 0.11
SLIP_FEE = 0.13
STRESS_FEE = 0.15
FEE_PCT = PRIMARY_FEE

# Intrabar (freeze engine._scan_exit)
INTRABAR_POLICY = "SL_FIRST"  # same-bar TP+SL → SL wins

# Position / selection (freeze global_single)
POSITION_MODE = "GLOBAL_SINGLE"
ENTRY_SELECTION = "FIRST_CLUSTER_ENTRY"
UPGRADE_POLICY = "P5A"
CONFLICT_EXIT = True

# BE50
BE_FRAC = 0.50
BE50_PARTIAL_TP = False
BE50_SL_TO_ENTRY = True
BE50_FULL_ORIGINAL_TP_REMAINS = True

# Cashout / reimbursement (equity management from freeze manifest)
CASHOUT_RATE = 0.30
REIMBURSEMENT_COVERAGE = 1.0

# Columns required by annotate_waves_df (from freeze WAVE_COLS + EXTRA_WAVE_COLS)
WAVE_COLS: list[str] = [
    "direction",
    "stoch_k_start",
    "stoch_k_end",
    "stoch_delta",
    "stoch_zone_start",
    "stoch_zone_end",
    "directional_efficiency",
    "signed_price_move_pct",
    "price_move_pct",
    "rsi_start",
    "rsi_end",
    "rsi_delta",
    "rsi_end_gt_50",
    "rsi_end_lt_50",
    "price_vs_ema20_end",
    "ema9_vs_ema20_end",
    "ema100_end",
    "ema400_end",
    "inefficient_flag",
    "end_available_at",
    "start_available_at",
]
EXTRA_WAVE_COLS: list[str] = [
    "n_bars",
    "favorable_move_pct",
    "adverse_move_pct",
    "rsi_end",
    "rsi_delta",
    "rsi_start",
]

PACKAGE_DATA_DIR = Path(__file__).resolve().parent / "data"
FROZEN_EFF_EDGES_PATH = PACKAGE_DATA_DIR / "frozen_eff_edges_apt_is.json"

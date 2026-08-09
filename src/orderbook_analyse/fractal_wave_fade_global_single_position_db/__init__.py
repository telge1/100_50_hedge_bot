"""Global single-position wave-fade backtest (MySQL SoT; frozen strategy params)."""

from __future__ import annotations

from pathlib import Path

from orderbook_analyse.fractal_signal_confluence_db import (  # noqa: F401
    APT_IS_END,
    ENV_FILE,
    FEE_PCT,
    SIGNAL_TFS,
    TF_RANK,
    TPSL_BY_TF,
)
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db import (  # noqa: F401
    PRIMARY_FEE,
    SLIP_FEE,
    STRATEGY_MAX_HOLD_BY_TF,
    STRESS_FEE,
)

AUDIT_VERSION = "fractal_wave_fade_global_single_position_db_v1"

SYMBOLS = ("APTUSDT", "DOGEUSDT")  # alphabetical for stable defaults; both used
COVERAGE_TFS = ("1m", "15m", "30m", "1h", "4h")

START_EQUITY = 1000.0
EQUITY_FRACTIONS = (0.25, 0.50, 1.00)

OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_global_single_position_db")

TIE_BREAK_DOC = (
    "Event sort: (1) entry_available_at ascending, "
    "(2) signal_available_at (=confirmation_available_at) ascending, "
    "(3) higher TF first (4h>1h>30m>15m), "
    "(4) alphabetical symbol ascending. "
    "No performance-based preference."
)

DEFINITIONS_DOC = f"""
Global single-position wave-fade backtest ({AUDIT_VERSION})

Source of truth: MySQL market_candles only (ENV_FILE).
No ClickHouse / CSV / Feather inputs / downloads.

Strategy parameters FROZEN (identical to fractal_wave_fade_strategy_backtest_db):
  Wave-end fade UP→SHORT / DOWN→LONG; Tier A = TREND_ALIGNED + Q4;
  clusters / pair windows / T0 entry / P5A ladder / conflict / SL_FIRST / fees.

ONLY change vs prior strategy backtest:
  OLD: max 1 open position PER SYMBOL (DOGE and APT may both be open).
  NEW: max 1 open position GLOBALLY across all symbols.

While OPEN:
  - same-symbol same-side higher TF → P5A upgrade (no new position)
  - same-symbol opposite higher TF in pair_window → HIGHER_TF_CONFLICT exit
  - other-symbol signals → SUPPRESSED_WHILE_POSITION_OPEN (no upgrade, no queue)

After EXIT → FLAT. Next entry only if new_entry_available_at > exit_time (strict).
Signals whose entry fell while OPEN are discarded forever (not queued).

{TIE_BREAK_DOC}

Fees: primary 0.11%; stress 0.13% / 0.15%.
Equity simulation: start {START_EQUITY} USDT; fractions 25%/50%/100% of current equity
per trade (normalized strategy equity, no added leverage).
Symbols: DOGEUSDT + APTUSDT on common full MySQL coverage of 1m/15m/30m/1h/4h.
BTCUSDT excluded (1m coverage does not overlap current HTF history).
"""

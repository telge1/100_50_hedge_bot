"""Full chronological wave-fade cluster strategy backtest (MySQL SoT)."""

from __future__ import annotations

from orderbook_analyse.fractal_signal_confluence_db import (  # noqa: F401
    APT_IS_END,
    ENV_FILE,
    FEE_PCT,
    SIGNAL_TFS,
    SYMBOLS,
    TF_RANK,
    TPSL_BY_TF,
    TPSL_EXTRA_4H,
)

AUDIT_VERSION = "fractal_wave_fade_strategy_backtest_db_v1"

# Strategy safety max-hold (minutes) from original entry; extends with highest TF reached
STRATEGY_MAX_HOLD_BY_TF = {
    "15m": 24 * 60,
    "30m": 48 * 60,
    "1h": 72 * 60,
    "4h": 10 * 24 * 60,
}

PRIMARY_FEE = 0.11
SLIP_FEE = 0.13  # 0.11 + 0.02 slippage
STRESS_FEE = 0.15

START_EQUITY = 100.0
UNIT_SIZE = 1.0

DEFINITIONS_DOC = """
Full chronological wave-fade strategy backtest (frozen defs)

Source: MySQL market_candles. No CSV inputs / downloads / new thresholds.

Signals (15m/30m/1h/4h): UP-wave end → SHORT; DOWN → LONG.
Known at end_available_at; T0 = first 1m open strictly after.
Tier A: TREND_ALIGNED (H4 EMA) + Q4 efficiency (APT-IS frozen edges).

Clusters / pair windows: identical to fractal_signal_confluence_db
(and fractal_dynamic_cluster_upgrade_db). Reconstructed from DB waves.

Entry: FIRST_CLUSTER_ENTRY — first valid signal of a new cluster, at T0.
Max one open position per symbol. No second entry in same cluster.
Same-side signal while open → suppress new entry; if higher TF → P5A upgrade.
Cross-symbol: DOGE and BTC may be open simultaneously.

Initial TPSL by first signal TF (frozen):
  15m TP1/SL1; 30m TP2/SL1.5; 1h TP2/SL1.5; 4h TP4/SL2
  (+ sensitivity 4h TP6/SL3)

P5A_FULL_UPGRADE: while open, same-side higher TF at its T0 upgrades TP and SL
to that TF plan; max-hold extends to STRATEGY_MAX_HOLD of highest TF from entry.

Conflict exit (frozen C1/C3 semantics): while open, higher-TF opposite signal
within frozen pair_window of entry confirmation → close at that signal's T0 open.
No reverse trade from the conflict event.

Execution: 1m OHLC, SL_FIRST if both touched. Fees roundtrip on close.
Primary fee 0.11%; sensitivity 0.13% (+0.02 slip); stress 0.15%.
Size: 1.0 unit/trade, additive equity from 100, no compounding.

Timeouts (safety, from original entry):
  15m-origin 24h; 30m 48h; 1h 72h; 4h 10d — extended on upgrade.
"""

"""Walk-forward / TRUE-OOS validation of frozen wave-fade strategy."""

from __future__ import annotations

from orderbook_analyse.fractal_signal_confluence_db import APT_IS_END, ENV_FILE, SYMBOLS, TPSL_BY_TF
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db import (
    PRIMARY_FEE,
    SLIP_FEE,
    STRESS_FEE,
    STRATEGY_MAX_HOLD_BY_TF,
)

AUDIT_VERSION = "fractal_wave_fade_walkforward_validation_db_v1"

# Frozen development cutoff: APT-IS quartile edges + all strategy research used data ≤ this
DEVELOPMENT_DATA_END = APT_IS_END  # 2026-08-08T10:21:00+00:00

# Exact frozen primary strategy fingerprint (must match strategy backtest)
FROZEN_STRATEGY = {
    "signal_tfs": ("15m", "30m", "1h", "4h"),
    "entry": "FIRST_CLUSTER_ENTRY",
    "tier": "TIER_A",
    "upgrade": "P5A_FULL_UPGRADE",
    "conflict_exit": True,
    "tpsl_by_tf": dict(TPSL_BY_TF),
    "primary_4h": (4.0, 2.0),
    "max_hold_by_tf": dict(STRATEGY_MAX_HOLD_BY_TF),
    "fee_primary": PRIMARY_FEE,
    "fee_stress": (SLIP_FEE, STRESS_FEE),
    "extra_4h_primary": False,
    "unit_size": 1.0,
    "sl_first": True,
}

DEFINITIONS_DOC = """
Walk-forward + honest TRUE-OOS validation of FROZEN wave-fade strategy.

Strategy fingerprint identical to fractal_wave_fade_strategy_backtest_db:
  TFs 15m/30m/1h/4h wave-end fade; Tier A; FIRST_CLUSTER_ENTRY;
  frozen confluence clusters; P5A full TP/SL upgrade; conflict exit retained;
  TPSL 15m 1/1, 30m 2/1.5, 1h 2/1.5, 4h 4/2; fees 0.11% (+0.13/0.15 stress);
  max 1 position/symbol; 1m SL_FIRST; STRATEGY_MAX_HOLD as strategy package.

DEVELOPMENT_DATA_END = APT_IS_END (quartile edge freeze + research history end).
TRUE_OOS = trades with entry_time > DEVELOPMENT_DATA_END only.
If coverage after cutoff is insufficient → TRUE_OOS_COVERAGE_INSUFFICIENT
(no fake OOS). Walk-forward = TEMPORAL_HOLDOUT / WALK_FORWARD_STABILITY
on known history with identical frozen rules — not claimed as unseen OOS.

No re-optimization. No new filters. MySQL market_candles SoT.
"""

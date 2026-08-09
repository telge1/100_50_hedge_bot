"""Causal paper / forward runner for frozen wave-fade cluster strategy v1."""

from __future__ import annotations

from pathlib import Path

from orderbook_analyse.fractal_signal_confluence_db import (  # noqa: F401
    ENV_FILE,
    SIGNAL_TFS,
    TF_RANK,
    TPSL_BY_TF,
)
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db import (
    PRIMARY_FEE,
    STRATEGY_MAX_HOLD_BY_TF,
)

AUDIT_VERSION = "fractal_wave_fade_forward_paper_v1"
STRATEGY_VERSION = "wave_fade_cluster_v1"

DEFAULT_PAPER_START = "2026-08-08T19:30:00+00:00"
DEFAULT_OUT_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/fractal_wave_fade_forward_paper"
)

PRIMARY_SYMBOLS = ("DOGEUSDT",)
OPTIONAL_SYMBOLS = ("BTCUSDT",)

# Stale if latest 1m older than this in FORWARD --once mode
STALE_AGE_MINUTES = 180

DEFINITIONS_DOC = """
Frozen paper/forward runner — wave_fade_cluster_v1

Identical to fractal_wave_fade_strategy_backtest_db primary:
  Tier-A, FIRST_CLUSTER_ENTRY, P5A full upgrade, conflict exit (configurable),
  TPSL 15m 1/1, 30m 2/1.5, 1h 2/1.5, 4h 4/2, fee 0.11%, SL_FIRST,
  STRATEGY_MAX_HOLD 24h/48h/72h/10d, max 1 position/symbol.

PAPER_START: no forward PnL before this instant; prior bars = warmup only.
REPLAY = DB history after PAPER_START already present at runner build time.
TRUE_FORWARD = signal_time >= forward_capture_start (set once at first start).
No re-optimization after forward_capture_start.
"""

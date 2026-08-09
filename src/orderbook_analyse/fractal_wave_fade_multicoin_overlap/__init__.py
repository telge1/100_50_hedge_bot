"""Multi-coin idle-fill / overlap simulation (APT + DOGE, frozen trades only)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_multicoin_overlap_v1"
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_multicoin_overlap")
REF_GLOBAL_TRADES = Path("results/fractal_wave_fade_global_single_position_db/trades.csv")
REF_GLOBAL_SUMMARY = Path("results/fractal_wave_fade_global_single_position_db/summary.json")
INDEPENDENT_CACHE = OUT_DIR_DEFAULT / "independent_trades_per_symbol.csv"

SYMBOLS = ("APTUSDT", "DOGEUSDT")
PRIMARY_FEE = 0.11

DEFINITIONS_DOC = f"""
Multi-coin overlap / idle-fill audit ({AUDIT_VERSION})

Independent trades: per-symbol max-1 Wave-Fade (frozen P5A / Tier A / fees),
regenerated with the same engine as OLD_PER_SYMBOL_MAX1 in
fractal_wave_fade_global_single_position_db (common window APT∩DOGE).

Global reference: {REF_GLOBAL_TRADES} (max 1 position across symbols).

Intervals: [entry_time, exit_time) in UTC — entry_time is T0 (first 1m open
strictly after confirmation_available_at). No lookahead / no signal shift.

Shared-slot: candidates = independent trades; max_concurrent=1; blocked forever
if entry falls while slot busy. Tie-break variants: APT_FIRST / DOGE_FIRST.

Parallel: max_concurrent=2 ≡ both independent streams (OLD per-symbol mode).

Capital models M1–M3 use identical total capital base (unit notional);
M3 scales each concurrent trade to 50% while both are open.
"""

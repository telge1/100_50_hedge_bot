"""BE50 + SAME_SIDE anti-repeat after true SL (research filter on frozen trade list)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_be50_anti_repeat_full_backtest_v1"
FROZEN_DIR = Path("results/fractal_wave_fade_be50_frozen_baseline")
BE50_DIR = Path("results/fractal_wave_fade_be50_full_backtest")
GLOBAL_DIR = Path("results/fractal_wave_fade_global_single_position_db")
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_be50_anti_repeat_full_backtest")

FEE_PCT = 0.11
CASHOUT_RATE = 0.30
COVERAGE_RATE = 1.00
START_ACTIVE = 1000.0
START_RESERVE = 0.0

# Known BE50 reference (frozen)
REF_BE50_END = 5474667077.849328
REF_BE50_MAX_DD = -15.13428703864581
REF_BE50_SL_STREAK = 6
REF_BE50_GE3 = 51
REF_BE50_GE5 = 1
REF_BE50_GE10_DD = 6

LARGE_DD_WINDOWS = [
    {
        "episode": "2023-08",
        "peak": "2023-08-10",
        "recovery": "2023-09-08",
        "be50_dd": -12.64,
    },
    {
        "episode": "2024-07",
        "peak": "2024-07-02",
        "recovery": "2024-07-23",
        "be50_dd": -13.91,
    },
    {
        "episode": "2025-07/08",
        "peak": "2025-07-30",
        "recovery": "2025-08-03",
        "be50_dd": -11.00,
    },
    {
        "episode": "2025-11/12",
        "peak": "2025-11-26",
        "recovery": "2025-12-18",
        "be50_dd": -10.73,
    },
    {
        "episode": "2026-01",
        "peak": "2026-01-09",
        "recovery": "2026-01-27",
        "be50_dd": -15.13,
    },
    {
        "episode": "2026-03/04",
        "peak": "2026-03-26",
        "recovery": "2026-05-01",
        "be50_dd": -10.71,
    },
]

DEFINITIONS_DOC = f"""
{AUDIT_VERSION}

Phase-1 frozen BE50 baseline: {FROZEN_DIR}
Anti-repeat applied as eligibility filter on the frozen BE50 trade sequence
(same entries/order as exit-only BE50). No live strategy change.

Rule: SAME_SIDE_BLOCK after true SL only (not BE/TP).
Reset (causal): intervening opposite stochastic wave completed on the symbol
(DOWN after SHORT SL / UP after LONG SL), preferring the SL first_signal_tf.
Wave ends from MySQL build_waves_from_db across signal TFs.

Limitation: suppressed signals are not re-queued into new entries; other-symbol
trades already present in the frozen sequence remain. This isolates anti-repeat
from schedule reshuffles caused by early BE exits.
"""

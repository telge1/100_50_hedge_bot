"""Idle / wait-time analysis between global-single-position trades."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_idle_time_analysis_v1"
REF_TRADES = Path("results/fractal_wave_fade_global_single_position_db/trades.csv")
OUT_DIR_DEFAULT = Path("results/fractal_wave_fade_idle_time_analysis")

DEFINITIONS_DOC = f"""
Idle time analysis ({AUDIT_VERSION})

Input: {REF_TRADES} (validated global-single trades only).
No new strategy / signals / reoptimization.

idle_minutes = current_entry_time - previous_exit_time
(chronological by entry_time; requires entry > previous exit).

Time in market = sum(holding intervals).
Flat idle = sum(inter-trade gaps).
Total span = last exit - first entry (or first exit if earlier).
"""

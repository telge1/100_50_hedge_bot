"""Build failure / non-failure wave episodes per timeframe."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_phase_failure.events import (
    load_waves,
    local_failure_mask,
)
from orderbook_analyse.fractal_failure_multitimeframe import SYMBOL, WAVE_DIR


def annotate_waves(tf: str, wave_dir: Path | str = WAVE_DIR) -> pd.DataFrame:
    """All completed waves for TF with failure flags + previous-wave fields."""
    w = load_waves(tf, wave_dir)
    fail_up, fail_dn = local_failure_mask(w)
    out = w.copy()
    out["timeframe"] = tf
    out["symbol"] = SYMBOL
    out["is_failed"] = (fail_up | fail_dn).to_numpy()
    out["failure_type"] = np.where(
        fail_up,
        "FAILED_UP_WAVE",
        np.where(fail_dn, "FAILED_DOWN_WAVE", "NON_FAILED"),
    )
    # Reversal hypothesis used for baselines and failures alike:
    # UP wave -> expect DOWN; DOWN wave -> expect UP
    out["expected_reversal"] = np.where(out["direction"].astype(str) == "UP", "DOWN", "UP")
    out["side"] = np.where(out["expected_reversal"] == "DOWN", "SHORT", "LONG")
    out["confirmation_available_at"] = out["end_available_at"]
    # previous opposite-wave asymmetry fields already on load_waves
    out["prev_is_opposite"] = (
        out["prev_direction"].astype(str) != out["direction"].astype(str)
    ) & out["prev_direction"].notna()
    out["eff_gap_prev_minus_cur"] = (
        out["prev_directional_efficiency"].astype(float)
        - out["directional_efficiency"].astype(float)
    )
    out["prev_more_efficient"] = (
        out["prev_is_opposite"]
        & np.isfinite(out["prev_directional_efficiency"].astype(float))
        & np.isfinite(out["directional_efficiency"].astype(float))
        & (
            out["prev_directional_efficiency"].astype(float)
            > out["directional_efficiency"].astype(float)
        )
    )
    return out.reset_index(drop=True)


def failure_events(waves: pd.DataFrame) -> pd.DataFrame:
    return waves[waves["is_failed"]].copy().reset_index(drop=True)

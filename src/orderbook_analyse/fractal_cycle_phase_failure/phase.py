"""Cycle-phase labels from existing Stoch wave zone + direction."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_phase_failure import PHASES


def _eq(s: pd.Series, val: str) -> pd.Series:
    return s.astype(str) == val


def cycle_phase_from_wave(df: pd.DataFrame) -> pd.Series:
    """Map direction × stoch_zone_end → phase (existing LOW/MID/HIGH only)."""
    direction = df["direction"].astype(str)
    zone = df["stoch_zone_end"].astype(str)
    out = pd.Series("NA", index=df.index, dtype=object)
    out[(_eq(direction, "UP")) & (_eq(zone, "LOW"))] = "LOW_UP"
    out[(_eq(direction, "UP")) & (_eq(zone, "MID"))] = "MID_UP"
    out[(_eq(direction, "UP")) & (_eq(zone, "HIGH"))] = "HIGH_UP"
    out[(_eq(direction, "DOWN")) & (_eq(zone, "HIGH"))] = "HIGH_DOWN"
    out[(_eq(direction, "DOWN")) & (_eq(zone, "MID"))] = "MID_DOWN"
    out[(_eq(direction, "DOWN")) & (_eq(zone, "LOW"))] = "LOW_DOWN"
    return out


def turning_flags(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Optional turning markers from zone path within the wave."""
    direction = df["direction"].astype(str)
    zs = df["stoch_zone_start"].astype(str)
    ze = df["stoch_zone_end"].astype(str)
    turning_up = _eq(direction, "UP") & _eq(zs, "LOW") & ze.isin(["MID", "HIGH"])
    turning_down = _eq(direction, "DOWN") & _eq(zs, "HIGH") & ze.isin(["MID", "LOW"])
    return turning_up, turning_down


def early_late_bucket(phase: pd.Series, *, side_up: bool) -> pd.Series:
    """EARLY/LATE relative to UP or DOWN cycle for conditioning tests."""
    out = pd.Series("OTHER", index=phase.index, dtype=object)
    if side_up:
        out[phase.isin(["LOW_UP", "MID_UP"])] = "EARLY_UP"
        out[phase == "HIGH_UP"] = "LATE_UP"
    else:
        out[phase.isin(["HIGH_DOWN", "MID_DOWN"])] = "EARLY_DOWN"
        out[phase == "LOW_DOWN"] = "LATE_DOWN"
    return out


def rsi_bucket(rsi: pd.Series) -> pd.Series:
    x = rsi.astype(float)
    out = pd.Series("na", index=x.index, dtype=object)
    out[x < 40] = "lt40"
    out[(x >= 40) & (x < 50)] = "40_50"
    out[(x >= 50) & (x < 60)] = "50_60"
    out[x >= 60] = "gt60"
    out[~np.isfinite(x)] = "na"
    return out

"""Path metrics and directional MFE/MAE (nonnegative magnitudes)."""

from __future__ import annotations

from typing import Any

import numpy as np


def return_bps(endpoint: float, anchor: float) -> float:
    return (endpoint - anchor) / anchor * 1e4


def path_extremes_bps(
    mids: list[float],
    available_ms_from_anchor: list[float],
    *,
    anchor: float,
) -> dict[str, Any]:
    if not mids:
        return {
            "max_up_bps": None,
            "max_down_bps": None,
            "time_to_max_up_ms": None,
            "time_to_max_down_ms": None,
            "realized_range_bps": None,
            "path_volatility_bps": None,
        }
    rets = [(m - anchor) / anchor * 1e4 for m in mids]
    raw_max = max(rets)
    raw_min = min(rets)
    max_up = float(max(0.0, raw_max))
    max_down = float(min(0.0, raw_min))
    # time to extremes: prefer first index achieving the reported extreme
    if max_up > 0:
        i_up = next(i for i, r in enumerate(rets) if r >= max_up - 1e-15)
    else:
        i_up = int(np.argmax(rets))
    if max_down < 0:
        i_dn = next(i for i, r in enumerate(rets) if r <= max_down + 1e-15)
    else:
        i_dn = int(np.argmin(rets))
    # 1s incremental returns for path vol
    vol = None
    if len(mids) >= 2:
        step = [return_bps(mids[i], mids[i - 1]) for i in range(1, len(mids))]
        vol = float(np.std(step, ddof=0))
    return {
        "max_up_bps": max_up,
        "max_down_bps": max_down,
        "time_to_max_up_ms": float(available_ms_from_anchor[i_up]),
        "time_to_max_down_ms": float(available_ms_from_anchor[i_dn]),
        "realized_range_bps": float(max_up - max_down),
        "path_volatility_bps": vol,
    }


def directional_mfe_mae(
    direction_hint: str,
    *,
    max_up_bps: float | None,
    max_down_bps: float | None,
) -> tuple[float | None, float | None, list[str]]:
    """Return (mfe_bps, mae_bps, quality_flags) as nonnegative magnitudes."""
    d = (direction_hint or "").upper()
    flags: list[str] = []
    if d in {"CONFLICTING", "UNCLEAR", "NEUTRAL", ""}:
        flags.append("MFE_MAE_UNDEFINED_DIRECTION")
        return None, None, flags
    if max_up_bps is None or max_down_bps is None:
        return None, None, flags
    if d == "BULLISH":
        return float(max_up_bps), float(max(0.0, -max_down_bps)), flags
    if d == "BEARISH":
        return float(max(0.0, -max_down_bps)), float(max_up_bps), flags
    flags.append("MFE_MAE_UNDEFINED_DIRECTION")
    return None, None, flags

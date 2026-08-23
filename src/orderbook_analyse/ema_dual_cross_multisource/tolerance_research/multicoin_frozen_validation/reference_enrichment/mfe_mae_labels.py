"""Outcome path labels: MFE/MAE from entry→exit 1m path (never used as features)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from .causality import as_utc, normalize_direction


def compute_mfe_mae_labels(
    candles_1m: pd.DataFrame | None,
    trade: dict[str, Any],
) -> dict[str, Any]:
    """Excursion labels between entry_at and exit_at inclusive on 1m bars.

    Uses only the trade's own path (not other trades). Missing path → nulls.
    """
    from . import constants as C

    prefix = C.LABEL_PREFIX
    out = {
        f"{prefix}mfe_pct": None,
        f"{prefix}mae_pct": None,
        f"{prefix}mfe_usdt": None,
        f"{prefix}mae_usdt": None,
        f"{prefix}mfe_mae_coverage": "MISSING",
    }
    entry_at = trade.get("entry_at")
    exit_at = trade.get("exit_at")
    entry_price = trade.get("entry_price")
    direction = trade.get("direction")
    if entry_at is None or exit_at is None or entry_price is None or candles_1m is None or candles_1m.empty:
        return out
    try:
        px = float(entry_price)
        if px <= 0:
            return out
        ent = as_utc(entry_at)
        ex = as_utc(exit_at)
        df = candles_1m.copy()
        df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
        path = df[(df["open_time"] >= pd.Timestamp(ent)) & (df["open_time"] <= pd.Timestamp(ex))]
        if path.empty:
            out[f"{prefix}mfe_mae_coverage"] = "EMPTY_PATH"
            return out
        high = float(path["high"].max())
        low = float(path["low"].min())
        side = normalize_direction(str(direction))
        if side == "LONG":
            mfe_pct = (high - px) / px * 100.0
            mae_pct = (low - px) / px * 100.0
        else:
            mfe_pct = (px - low) / px * 100.0
            mae_pct = (px - high) / px * 100.0
        notional = float(trade.get("notional_usdt") or C.REF_NOTIONAL)
        out[f"{prefix}mfe_pct"] = mfe_pct
        out[f"{prefix}mae_pct"] = mae_pct
        out[f"{prefix}mfe_usdt"] = mfe_pct / 100.0 * notional
        out[f"{prefix}mae_usdt"] = mae_pct / 100.0 * notional
        out[f"{prefix}mfe_mae_coverage"] = "OK"
    except Exception:
        out[f"{prefix}mfe_mae_coverage"] = "ERROR"
    return out

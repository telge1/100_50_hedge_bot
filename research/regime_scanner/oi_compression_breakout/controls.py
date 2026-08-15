"""Diagnostic control samples (no future outcomes used for matching)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.oi_compression_breakout.boxes import FrozenBox


def sample_controls(
    df: pd.DataFrame,
    boxes: list[FrozenBox],
    oi_feat_by_box_id: dict[str, dict[str, Any]],
    *,
    rng: np.random.Generator | None = None,
) -> list[dict[str, Any]]:
    """Build diagnostic control rows (counts/labels only in smoke).

    C1: boxes with oi_change_pct <= 0 (subset of O0)
    C2: OI buildup (chg>0) with no breakout within max wait — filled later by audit
    C3: wide ranges (width_atr > 2.0) at analogous confirms — skipped if none
    C4: random timestamps coin-matched
    C5: volume up during box but oi_change_pct <= 0
    """
    rng = rng or np.random.default_rng(42)
    rows: list[dict[str, Any]] = []

    for b in boxes:
        feat = oi_feat_by_box_id.get(b.box_id) or {}
        chg = feat.get("oi_change_pct")
        if chg is not None and chg <= 0:
            rows.append(
                {
                    "control": "C1",
                    "box_id": b.box_id,
                    "symbol": b.symbol,
                    "bucket_start": b.confirm_bucket,
                    "box_length": b.box_length,
                    "box_width_atr": b.box_width_atr,
                    "oi_change_pct": chg,
                }
            )

        # C5: volume build without OI build
        vol = df["total_volume"].to_numpy(dtype=float) if "total_volume" in df.columns else None
        if vol is not None and chg is not None and chg <= 0:
            v0 = float(vol[b.start_i])
            v1 = float(vol[b.confirm_i])
            if np.isfinite(v0) and v0 > 0 and np.isfinite(v1) and (v1 / v0 - 1.0) > 0:
                rows.append(
                    {
                        "control": "C5",
                        "box_id": b.box_id,
                        "symbol": b.symbol,
                        "bucket_start": b.confirm_bucket,
                        "volume_change_pct": float(v1 / v0 - 1.0),
                        "oi_change_pct": chg,
                    }
                )

    # C4 random confirms
    n = len(df)
    if n > 100 and boxes:
        picks = rng.choice(n, size=min(len(boxes), 50), replace=False)
        for i in picks:
            rows.append(
                {
                    "control": "C4",
                    "symbol": str(df["symbol"].iloc[int(i)]),
                    "bucket_start": str(df["bucket_start"].iloc[int(i)]),
                    "confirm_i": int(i),
                }
            )
    return rows

"""OI features inside frozen boxes and fixed O0–O4 groups (parent/subset)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from research.regime_scanner.oi_compression_breakout.boxes import FrozenBox
from research.regime_scanner.oi_compression_breakout.config import (
    BOX_DRIFT_TIGHT,
    OI_PCTL_MIN_HISTORY,
    OI_STEP_RATIO_MIN,
    candidate_id,
)


def compute_oi_features(df, box: FrozenBox) -> dict[str, Any]:
    """OI path on [start_i, confirm_i] inclusive (same sequence already verified)."""
    oi = df["open_interest"].to_numpy(dtype=float)
    seg = oi[box.start_i : box.confirm_i + 1]
    if len(seg) < 2 or not np.isfinite(seg).all() or seg[0] <= 0:
        return {"valid_oi": False}

    diffs = np.diff(seg)
    pos = diffs > 0
    neg = diffs < 0
    # longest positive run
    longest = 0
    cur = 0
    for d in diffs:
        if d > 0:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0

    x = np.arange(len(seg), dtype=float)
    # linear slope + R^2
    if len(seg) >= 3:
        coef = np.polyfit(x, seg, 1)
        slope = float(coef[0])
        pred = np.polyval(coef, x)
        ss_res = float(np.sum((seg - pred) ** 2))
        ss_tot = float(np.sum((seg - np.mean(seg)) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    else:
        slope = float(seg[-1] - seg[0])
        r2 = np.nan

    oi_change_pct = float(seg[-1] / seg[0] - 1.0)
    oi_mean = float(np.mean(seg))
    return {
        "valid_oi": True,
        "oi_start": float(seg[0]),
        "oi_end": float(seg[-1]),
        "oi_min": float(np.min(seg)),
        "oi_max": float(np.max(seg)),
        "oi_mean": oi_mean,
        "oi_change_abs": float(seg[-1] - seg[0]),
        "oi_change_pct": oi_change_pct,
        "positive_oi_step_ratio": float(np.mean(pos)) if len(pos) else np.nan,
        "negative_oi_step_ratio": float(np.mean(neg)) if len(pos) else np.nan,
        "longest_positive_oi_run": int(longest),
        "oi_slope": slope,
        "oi_r2": float(r2) if r2 == r2 else np.nan,
        "oi_end_vs_mean": float(seg[-1] / oi_mean - 1.0) if oi_mean > 0 else np.nan,
    }


def assign_oi_groups(
    boxes: list[FrozenBox],
    oi_feats: list[dict[str, Any]],
    *,
    min_history: int = OI_PCTL_MIN_HISTORY,
) -> list[dict[str, Any]]:
    """Causal per-coin O2 threshold from prior boxes only; emit one row per OI group membership.

    O0 is always emitted (parent). O1–O4 are subsets (additional rows tagged oi_group).
    """
    # process in confirm order per symbol
    order = sorted(range(len(boxes)), key=lambda i: (boxes[i].symbol, boxes[i].confirm_i, boxes[i].box_length, boxes[i].quality))
    history: dict[str, list[float]] = defaultdict(list)
    rows: list[dict[str, Any]] = []

    for i in order:
        box = boxes[i]
        feat = oi_feats[i]
        base = {
            "box_id": box.box_id,
            "physical_id": box.physical_id,
            "symbol": box.symbol,
            "sequence_id": box.sequence_id,
            "box_length": box.box_length,
            "quality": box.quality,
            "box_confirm_timestamp": box.confirm_bucket,
            "box_drift_ratio": box.box_drift_ratio,
            **{k: feat.get(k) for k in feat},
        }
        if not feat.get("valid_oi"):
            continue

        chg = float(feat["oi_change_pct"])
        pos_ratio = float(feat.get("positive_oi_step_ratio") or 0.0)
        hist = history[box.symbol]
        o2 = False
        o2_threshold = np.nan
        insufficient = len(hist) < min_history
        if not insufficient:
            o2_threshold = float(np.percentile(hist, 75))
            o2 = chg >= o2_threshold

        membership = ["O0"]
        if chg > 0:
            membership.append("O1")
        if o2:
            membership.append("O2")
        if pos_ratio >= OI_STEP_RATIO_MIN and chg > 0:
            membership.append("O3")
        if (o2 or (pos_ratio >= OI_STEP_RATIO_MIN and chg > 0)) and box.box_drift_ratio <= BOX_DRIFT_TIGHT:
            membership.append("O4")

        for g in membership:
            rows.append(
                {
                    **base,
                    "oi_group": g,
                    "oi_p75_threshold": o2_threshold,
                    "insufficient_warmup": bool(insufficient),
                    "is_parent_O0": g == "O0",
                    "candidate_id": candidate_id(
                        physical_id=box.physical_id,
                        box_length=box.box_length,
                        quality=box.quality,
                        oi_group=g,
                    ),
                }
            )

        # update history AFTER assigning (exclude current from own threshold)
        history[box.symbol].append(chg)

    return rows

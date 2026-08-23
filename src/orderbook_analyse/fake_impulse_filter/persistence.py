from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from .states import Side
from .thresholds import ResearchExploreParams, DEFAULT_RESEARCH


@dataclass
class ImpulseMetrics:
    """Causal impulse path metrics from start_idx using only samples <= end of each horizon.

    Outcomes beyond decision time are stored separately by the caller for labeling.
    """

    side: Side
    start_price: float
    impulse_ext_price: float | None
    impulse_move: float | None
    persistence_ok: dict[int, bool] = field(default_factory=dict)
    same_dir_flow_frac: dict[int, float] = field(default_factory=dict)
    same_dir_imb_frac: dict[int, float] = field(default_factory=dict)
    consecutive_same_dir: int = 0
    giveback_ratio: dict[int, float | None] = field(default_factory=dict)
    in_range_flag: bool | None = None
    dist_to_range_high: float | None = None
    dist_to_range_low: float | None = None


def compute_impulse_metrics(
    prices: Sequence[float],
    flow_sign: Sequence[float],
    imb_sign: Sequence[float],
    start_idx: int,
    side: Side,
    range_high: float | None = None,
    range_low: float | None = None,
    sample_seconds: int = 1,
    params: ResearchExploreParams = DEFAULT_RESEARCH,
) -> ImpulseMetrics:
    """Compute persistence/giveback using only indices >= start_idx.

    prices/flow_sign/imb_sign are aligned causal series (e.g. 1s).
    flow_sign: +1 buy-dominant, -1 sell-dominant, 0 unknown
    imb_sign: +1 bid-heavy, -1 ask-heavy, 0 unknown
    """
    n = len(prices)
    if start_idx < 0 or start_idx >= n or not np.isfinite(prices[start_idx]):
        return ImpulseMetrics(side=side, start_price=float("nan"), impulse_ext_price=None, impulse_move=None)

    p0 = float(prices[start_idx])
    want = 1 if side == Side.LONG else -1

    # consecutive same-dir from start
    consec = 0
    for i in range(start_idx, n):
        fs = flow_sign[i] if i < len(flow_sign) else 0
        if fs == want:
            consec += 1
        else:
            break

    persistence_ok: dict[int, bool] = {}
    same_flow: dict[int, float] = {}
    same_imb: dict[int, float] = {}
    giveback: dict[int, float | None] = {}
    ext_price = p0
    impulse_move = 0.0

    for h in params.persistence_horizons_s:
        end = min(n - 1, start_idx + max(1, h // sample_seconds))
        window = range(start_idx, end + 1)
        # extreme in direction
        if side == Side.LONG:
            ext = max(float(prices[i]) for i in window if np.isfinite(prices[i]))
            move = ext / p0 - 1.0
            # giveback from extreme to last
            last = float(prices[end])
            gb = (ext - last) / (ext - p0) if ext > p0 else None
        else:
            ext = min(float(prices[i]) for i in window if np.isfinite(prices[i]))
            move = 1.0 - ext / p0
            last = float(prices[end])
            gb = (last - ext) / (p0 - ext) if ext < p0 else None

        if h == params.confirm_persist_s or abs(move) >= abs(impulse_move):
            ext_price = ext
            impulse_move = move

        flow_hits = [1.0 for i in window if i < len(flow_sign) and flow_sign[i] == want]
        imb_hits = [1.0 for i in window if i < len(imb_sign) and imb_sign[i] == want]
        denom = max(1, end - start_idx + 1)
        same_flow[h] = sum(flow_hits) / denom
        same_imb[h] = sum(imb_hits) / denom
        persistence_ok[h] = (
            move >= params.min_impulse_move
            and same_flow[h] >= 0.5
            and consec >= min(params.min_confirm_samples_same_dir, denom)
        )
        giveback[h] = gb

    # also fill giveback horizons not in persistence list
    for h in params.giveback_horizons_s:
        if h in giveback:
            continue
        end = min(n - 1, start_idx + max(1, h // sample_seconds))
        window = range(start_idx, end + 1)
        if side == Side.LONG:
            ext = max(float(prices[i]) for i in window if np.isfinite(prices[i]))
            last = float(prices[end])
            giveback[h] = (ext - last) / (ext - p0) if ext > p0 else None
        else:
            ext = min(float(prices[i]) for i in window if np.isfinite(prices[i]))
            last = float(prices[end])
            giveback[h] = (last - ext) / (p0 - ext) if ext < p0 else None

    in_range = None
    d_hi = d_lo = None
    if range_high is not None and range_low is not None and range_high > range_low:
        d_hi = (range_high - p0) / p0
        d_lo = (p0 - range_low) / p0
        # impulse extreme still inside prior range
        if ext_price is not None:
            in_range = range_low <= ext_price <= range_high

    return ImpulseMetrics(
        side=side,
        start_price=p0,
        impulse_ext_price=ext_price,
        impulse_move=impulse_move,
        persistence_ok=persistence_ok,
        same_dir_flow_frac=same_flow,
        same_dir_imb_frac=same_imb,
        consecutive_same_dir=consec,
        giveback_ratio=giveback,
        in_range_flag=in_range,
        dist_to_range_high=d_hi,
        dist_to_range_low=d_lo,
    )


def outcome_mfe_mae(
    prices: Sequence[float],
    highs: Sequence[float] | None,
    lows: Sequence[float] | None,
    start_idx: int,
    side: Side,
    horizons_s: Sequence[int],
    sample_seconds: int = 1,
) -> dict[str, float | None]:
    """Label-only outcomes; must not feed live decision."""
    n = len(prices)
    if start_idx < 0 or start_idx >= n:
        return {}
    p0 = float(prices[start_idx])
    hi = highs if highs is not None else prices
    lo = lows if lows is not None else prices
    out: dict[str, float | None] = {}
    for h in horizons_s:
        end = min(n - 1, start_idx + max(1, h // sample_seconds))
        window = range(start_idx + 1, end + 1)
        if not window:
            out[f"mfe_{h}s"] = None
            out[f"mae_{h}s"] = None
            continue
        if side == Side.LONG:
            mfe = max(0.0, max(float(hi[i]) for i in window) / p0 - 1.0)
            mae = max(0.0, 1.0 - min(float(lo[i]) for i in window) / p0)
        else:
            mfe = max(0.0, 1.0 - min(float(lo[i]) for i in window) / p0)
            mae = max(0.0, max(float(hi[i]) for i in window) / p0 - 1.0)
        out[f"mfe_{h}s"] = mfe
        out[f"mae_{h}s"] = mae
    return out

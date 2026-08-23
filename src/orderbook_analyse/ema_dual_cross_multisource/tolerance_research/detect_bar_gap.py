"""M0 strict sync + M1 BAR_GAP_SYNC detectors (research-only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from ...cluster_sweep_research.ema_features import required_warmup_bars
from ..config import EMA_DUAL_CROSS_DEFAULTS, EmaDualCrossConfig
from ..ema_candidate import (
    _band_metrics,
    _ema_vals,
    attach_atr,
    detect_cross_events,
    make_candidate_id,
)
from ..models import CandidateType, Direction
from .episode_id import make_cross_episode_id


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _passes_sync_quality(metrics: dict[str, Any], cfg: EmaDualCrossConfig) -> tuple[bool, str | None]:
    """Same compression / flat filters as production sync path (ema_candidate.py)."""
    if metrics.get("max_band_gap_pct_lookback") is not None:
        if metrics["max_band_gap_pct_lookback"] > cfg.band_compression_pct * 2.5:
            return False, "REJECTED_BAND_ALREADY_EXPANDED"
    compressed = True
    if metrics.get("ema_9_20_gap_pct") is not None and metrics["ema_9_20_gap_pct"] > cfg.band_compression_pct:
        compressed = False
    if metrics.get("ema_9_20_gap_atr") is not None and metrics["ema_9_20_gap_atr"] > cfg.band_compression_atr:
        compressed = False
    if not compressed:
        return False, "REJECTED_BAND_ALREADY_EXPANDED"
    if metrics.get("flat_slopes"):
        return False, "REJECTED_FLAT_NO_IMPULSE"
    return True, None


def detect_strict_sync_baseline(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    cfg: EmaDualCrossConfig | None = None,
) -> list[dict[str, Any]]:
    """M0: production detect_cross_events, sync candidates only."""
    cfg = cfg or EMA_DUAL_CROSS_DEFAULTS
    valid, _rejected = detect_cross_events(df, symbol=symbol, timeframe=timeframe, cfg=cfg)
    out: list[dict[str, Any]] = []
    for raw in valid:
        if str(raw.get("candidate_type")) != CandidateType.SYNCHRONOUS_DUAL_EMA_CROSS.value:
            continue
        row = dict(raw)
        metrics = dict(row.get("ema_metrics") or {})
        metrics["exact_gap"] = 0
        metrics["first_leg"] = "BOTH"
        metrics["first_cross_bar"] = int(row["bar_index"])
        metrics["second_cross_bar"] = int(row["bar_index"])
        metrics["same_candle_cross"] = True
        metrics["cross_lag_bars"] = 0
        row["ema_metrics"] = metrics
        row["mode_family"] = "STRICT_SYNC"
        row["exact_gap"] = 0
        row["first_leg"] = "BOTH"
        row["first_cross_bar"] = int(row["bar_index"])
        row["cross_episode_id"] = make_cross_episode_id(
            symbol=symbol,
            timeframe=timeframe,
            direction=str(row["direction"]),
            first_cross_bar=int(row["bar_index"]),
            first_leg="BOTH",
        )
        out.append(row)
    return out


@dataclass
class _Pending:
    first_bar: int
    first_leg: str  # EMA9 | EMA20
    direction: str


def detect_bar_gap_sync(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    max_gap: int,
    cfg: EmaDualCrossConfig | None = None,
) -> list[dict[str, Any]]:
    """
    M1 BAR_GAP_SYNC: emit when second leg confirms within max_gap bars.

    - Gap 0: both cross same bar (requires sync_prev & sync_now) — parity with M0 quality rules
    - Gap 1..max_gap: signal only at second cross close; never backdated to first
    - Opposite cross while pending invalidates the sequence
    - Pending expires if second leg not confirmed within max_gap
    """
    if max_gap < 0:
        raise ValueError("max_gap must be >= 0")
    cfg = cfg or EMA_DUAL_CROSS_DEFAULTS
    work = attach_atr(df.sort_values("open_time").reset_index(drop=True), cfg.atr_period)
    warm = required_warmup_bars(cfg.ema_slow, 20)
    pending: dict[str, _Pending | None] = {"BULLISH": None, "BEARISH": None}
    valid: list[dict[str, Any]] = []

    def _emit(
        *,
        i: int,
        direction: str,
        exact_gap: int,
        first_bar: int,
        first_leg: str,
        prev: pd.Series,
        cur: pd.Series,
        metrics: dict[str, Any],
    ) -> None:
        ok, _rej = _passes_sync_quality(metrics, cfg)
        if not ok:
            return
        ts = _utc(pd.Timestamp(cur["open_time"]).to_pydatetime().replace(tzinfo=timezone.utc))
        sm = dict(metrics)
        sm["exact_gap"] = exact_gap
        sm["first_leg"] = first_leg
        sm["first_cross_bar"] = first_bar
        sm["second_cross_bar"] = i
        sm["cross_lag_bars"] = exact_gap
        sm["same_candle_cross"] = exact_gap == 0
        cid = make_candidate_id(symbol, timeframe, direction, ts, f"M1G{exact_gap}")
        ep = make_cross_episode_id(
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            first_cross_bar=first_bar,
            first_leg=first_leg,
        )
        valid.append(
            {
                "candidate_id": cid,
                "symbol": symbol,
                "timeframe": timeframe,
                "direction": direction,
                "candidate_type": CandidateType.SYNCHRONOUS_DUAL_EMA_CROSS.value,
                "candidate_at": ts,
                "bar_index": i,
                "ema_before": _ema_vals(prev),
                "ema_after": _ema_vals(cur),
                "ema_metrics": sm,
                "reason_codes": ["VALID_BAR_GAP_SYNC" if exact_gap > 0 else "VALID_SYNCHRONOUS_CROSS"],
                "mode_family": "BAR_GAP_SYNC" if exact_gap > 0 else "STRICT_SYNC",
                "exact_gap": exact_gap,
                "first_leg": first_leg,
                "first_cross_bar": first_bar,
                "cross_episode_id": ep,
            }
        )

    for i in range(max(warm, 1), len(work) - 1):
        prev, cur = work.iloc[i - 1], work.iloc[i]
        if any(pd.isna(prev[k]) or pd.isna(cur[k]) for k in ("ema_9", "ema_20", "ema_59")):
            continue
        p9, p20, p59 = float(prev["ema_9"]), float(prev["ema_20"]), float(prev["ema_59"])
        c9, c20, c59 = float(cur["ema_9"]), float(cur["ema_20"]), float(cur["ema_59"])
        metrics = _band_metrics(work, i, cfg)

        for direction, bull in ((Direction.BULLISH, True), (Direction.BEARISH, False)):
            d = direction.value
            if bull:
                cross9 = p9 <= p59 and c9 > c59
                cross20 = p20 <= p59 and c20 > c59
                sync_prev = p9 <= p59 and p20 <= p59
                sync_now = c9 > c59 and c20 > c59
                inv9 = p9 >= p59 and c9 < c59
                inv20 = p20 >= p59 and c20 < c59
            else:
                cross9 = p9 >= p59 and c9 < c59
                cross20 = p20 >= p59 and c20 < c59
                sync_prev = p9 >= p59 and p20 >= p59
                sync_now = c9 < c59 and c20 < c59
                inv9 = p9 <= p59 and c9 > c59
                inv20 = p20 <= p59 and c20 > c59

            pend = pending[d]
            if pend is not None:
                if i - pend.first_bar > max_gap:
                    pending[d] = None
                    pend = None
                elif inv9 or inv20:
                    pending[d] = None
                    pend = None

            # Gap-0 same-bar dual cross
            if cross9 and cross20 and sync_prev and sync_now:
                if max_gap >= 0:
                    _emit(
                        i=i,
                        direction=d,
                        exact_gap=0,
                        first_bar=i,
                        first_leg="BOTH",
                        prev=prev,
                        cur=cur,
                        metrics=metrics,
                    )
                pending[d] = None
                continue

            # Complete pending with second leg
            pend = pending[d]
            if pend is not None:
                need20 = pend.first_leg == "EMA9"
                got_second = cross20 if need20 else cross9
                # both crossing on completion bar without sync_prev is still valid for gap>0
                if got_second or (cross9 and cross20):
                    gap = i - pend.first_bar
                    if gap <= 0:
                        pending[d] = None
                        continue
                    if gap > max_gap:
                        pending[d] = None
                        continue
                    if not sync_now:
                        pending[d] = None
                        continue
                    _emit(
                        i=i,
                        direction=d,
                        exact_gap=gap,
                        first_bar=pend.first_bar,
                        first_leg=pend.first_leg,
                        prev=prev,
                        cur=cur,
                        metrics=metrics,
                    )
                    pending[d] = None
                    continue

            # Start new pending (only if max_gap allows stagger)
            if max_gap >= 1:
                if cross9 and not cross20:
                    pending[d] = _Pending(first_bar=i, first_leg="EMA9", direction=d)
                elif cross20 and not cross9:
                    pending[d] = _Pending(first_bar=i, first_leg="EMA20", direction=d)

    return valid

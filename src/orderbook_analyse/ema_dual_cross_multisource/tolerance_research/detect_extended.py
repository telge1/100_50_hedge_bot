"""M2 / M4 / M5 research detectors + M3 cohesion filter (research-only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

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
from .detect_bar_gap import _passes_sync_quality, _utc
from .episode_id import make_cross_episode_id


def _side_bull(e: float, e59: float) -> bool:
    return e > e59


def _side_bear(e: float, e59: float) -> bool:
    return e < e59


def apply_cohesion_filter(
    candidates: list[dict[str, Any]],
    *,
    max_ema9_20_atr: float,
    source_mode_id: str,
) -> list[dict[str, Any]]:
    """M3: keep candidates whose |ema9-ema20|/ATR at decision bar ≤ threshold."""
    out: list[dict[str, Any]] = []
    for raw in candidates:
        metrics = raw.get("ema_metrics") or {}
        gap_atr = metrics.get("ema_9_20_gap_atr")
        after = raw.get("ema_after") or {}
        if gap_atr is None:
            e9, e20, atr = after.get("ema_9"), after.get("ema_20"), after.get("atr")
            if e9 is None or e20 is None or atr is None or float(atr) <= 0:
                continue
            gap_atr = abs(float(e9) - float(e20)) / float(atr)
        if float(gap_atr) > float(max_ema9_20_atr):
            continue
        row = dict(raw)
        row["cohesion_atr_max"] = float(max_ema9_20_atr)
        row["cohesion_source_mode"] = source_mode_id
        row["mode_family"] = "COHESION_FILTER"
        out.append(row)
    return out


def detect_price_distance_sync(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    atr_thresh: float,
    cfg: EmaDualCrossConfig | None = None,
    max_first_leg_age: int = 5,
) -> list[dict[str, Any]]:
    """
    M2: one EMA fully on signal side of EMA59; the other still not fully crossed
    but |second - EMA59| / ATR ≤ atr_thresh at decision bar close.
    Signal never backdated to the first cross.
    """
    cfg = cfg or EMA_DUAL_CROSS_DEFAULTS
    work = attach_atr(df.sort_values("open_time").reset_index(drop=True), cfg.atr_period)
    warm = required_warmup_bars(cfg.ema_slow, 20)
    last_full_cross: dict[str, dict[str, int | None]] = {
        "BULLISH": {"EMA9": None, "EMA20": None},
        "BEARISH": {"EMA9": None, "EMA20": None},
    }
    valid: list[dict[str, Any]] = []
    seen_ep: set[str] = set()

    for i in range(max(warm, 1), len(work) - 1):
        prev, cur = work.iloc[i - 1], work.iloc[i]
        if any(pd.isna(prev[k]) or pd.isna(cur[k]) for k in ("ema_9", "ema_20", "ema_59", "atr")):
            continue
        p9, p20, p59 = float(prev["ema_9"]), float(prev["ema_20"]), float(prev["ema_59"])
        c9, c20, c59 = float(cur["ema_9"]), float(cur["ema_20"]), float(cur["ema_59"])
        atr = float(cur["atr"])
        if atr <= 0:
            continue
        metrics = _band_metrics(work, i, cfg)

        for direction, bull in ((Direction.BULLISH, True), (Direction.BEARISH, False)):
            d = direction.value
            if bull:
                cross9 = p9 <= p59 and c9 > c59
                cross20 = p20 <= p59 and c20 > c59
                on9, on20 = c9 > c59, c20 > c59
                inv9 = p9 >= p59 and c9 < c59
                inv20 = p20 >= p59 and c20 < c59
            else:
                cross9 = p9 >= p59 and c9 < c59
                cross20 = p20 >= p59 and c20 < c59
                on9, on20 = c9 < c59, c20 < c59
                inv9 = p9 <= p59 and c9 > c59
                inv20 = p20 <= p59 and c20 > c59

            if cross9:
                last_full_cross[d]["EMA9"] = i
            if cross20:
                last_full_cross[d]["EMA20"] = i
            if inv9:
                last_full_cross[d]["EMA9"] = None
            if inv20:
                last_full_cross[d]["EMA20"] = None

            # Both fully on side → not M2 (that is sync / gap territory)
            if on9 and on20:
                continue
            # Need exactly one fully on signal side
            if on9 == on20:
                continue

            if on9 and not on20:
                first_leg, second_e = "EMA9", c20
                first_bar = last_full_cross[d]["EMA9"]
            else:
                first_leg, second_e = "EMA20", c9
                first_bar = last_full_cross[d]["EMA20"]

            if first_bar is None:
                continue
            age = i - int(first_bar)
            if age < 0 or age > int(max_first_leg_age):
                continue
            dist_atr = abs(float(second_e) - c59) / atr
            if dist_atr > float(atr_thresh):
                continue
            # M2: current-bar cohesion + non-flat; lookback expansion is often
            # violated by construction (one leg already crossed).
            gap_atr = metrics.get("ema_9_20_gap_atr")
            if gap_atr is not None and float(gap_atr) > cfg.band_compression_atr * 2.0:
                continue
            if metrics.get("flat_slopes"):
                continue
            if bull:
                s9, s20 = cur.get("ema_9_slope_1"), cur.get("ema_20_slope_1")
                if pd.notna(s9) and pd.notna(s20) and float(s9) <= 0 and float(s20) <= 0:
                    continue
            else:
                s9, s20 = cur.get("ema_9_slope_1"), cur.get("ema_20_slope_1")
                if pd.notna(s9) and pd.notna(s20) and float(s9) >= 0 and float(s20) >= 0:
                    continue

            ts = _utc(pd.Timestamp(cur["open_time"]).to_pydatetime().replace(tzinfo=timezone.utc))
            ep = make_cross_episode_id(
                symbol=symbol,
                timeframe=timeframe,
                direction=d,
                first_cross_bar=int(first_bar),
                first_leg=str(first_leg),
            )
            if ep in seen_ep:
                continue
            seen_ep.add(ep)
            sm = dict(metrics)
            sm["exact_gap"] = None
            sm["price_distance_atr"] = dist_atr
            sm["atr_thresh"] = float(atr_thresh)
            sm["first_leg"] = first_leg
            sm["first_cross_bar"] = int(first_bar)
            sm["second_cross_bar"] = i
            valid.append(
                {
                    "candidate_id": make_candidate_id(symbol, timeframe, d, ts, f"M2A{atr_thresh}"),
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "direction": d,
                    "candidate_type": CandidateType.SYNCHRONOUS_DUAL_EMA_CROSS.value,
                    "candidate_at": ts,
                    "bar_index": i,
                    "ema_before": _ema_vals(prev),
                    "ema_after": _ema_vals(cur),
                    "ema_metrics": sm,
                    "reason_codes": ["VALID_PRICE_DISTANCE_SYNC"],
                    "mode_family": "PRICE_DISTANCE_SYNC",
                    "exact_gap": None,
                    "first_leg": first_leg,
                    "first_cross_bar": int(first_bar),
                    "cross_episode_id": ep,
                    "atr_thresh": float(atr_thresh),
                }
            )
    return valid


def detect_touch_and_expand(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    touch_atr: float,
    expand_bars: int,
    cfg: EmaDualCrossConfig | None = None,
) -> list[dict[str, Any]]:
    """
    M4: both fast EMAs near EMA59 and tight; then expand_bars consecutive bars
    moving away from EMA59 in the same direction. Signal at last expansion bar.
    """
    if expand_bars < 1:
        raise ValueError("expand_bars must be >= 1")
    cfg = cfg or EMA_DUAL_CROSS_DEFAULTS
    work = attach_atr(df.sort_values("open_time").reset_index(drop=True), cfg.atr_period)
    warm = required_warmup_bars(cfg.ema_slow, 20)
    valid: list[dict[str, Any]] = []
    seen_ep: set[str] = set()

    def _dist(e: float, e59: float, atr: float) -> float:
        return abs(e - e59) / atr

    def _signed(e: float, e59: float, bull: bool) -> float:
        return (e - e59) if bull else (e59 - e)

    for i in range(max(warm, expand_bars), len(work) - 1):
        cur = work.iloc[i]
        if any(pd.isna(cur[k]) for k in ("ema_9", "ema_20", "ema_59", "atr")):
            continue
        atr = float(cur["atr"])
        if atr <= 0:
            continue
        # Touch window starts expand_bars bars before decision (inclusive chain)
        start = i - expand_bars + 1
        touch_i = start  # require touch at beginning of expansion window
        trow = work.iloc[touch_i]
        if any(pd.isna(trow[k]) for k in ("ema_9", "ema_20", "ema_59", "atr")):
            continue
        tatr = float(trow["atr"])
        if tatr <= 0:
            continue
        te9, te20, te59 = float(trow["ema_9"]), float(trow["ema_20"]), float(trow["ema_59"])
        if (
            _dist(te9, te59, tatr) > touch_atr
            or _dist(te20, te59, tatr) > touch_atr
            or abs(te9 - te20) / tatr > touch_atr
        ):
            continue

        for direction, bull in ((Direction.BULLISH, True), (Direction.BEARISH, False)):
            d = direction.value
            ok_chain = True
            prev_s9 = prev_s20 = None
            for j in range(start, i + 1):
                row = work.iloc[j]
                if j > 0:
                    prow = work.iloc[j - 1]
                else:
                    ok_chain = False
                    break
                if any(pd.isna(row[k]) or pd.isna(prow[k]) for k in ("ema_9", "ema_20", "ema_59")):
                    ok_chain = False
                    break
                e9, e20, e59 = float(row["ema_9"]), float(row["ema_20"]), float(row["ema_59"])
                p9, p20, p59 = float(prow["ema_9"]), float(prow["ema_20"]), float(prow["ema_59"])
                s9 = row.get("ema_9_slope_1")
                s20 = row.get("ema_20_slope_1")
                if pd.isna(s9) or pd.isna(s20):
                    ok_chain = False
                    break
                # no opposite cross during confirmation
                if bull:
                    if (p9 >= p59 and e9 < e59) or (p20 >= p59 and e20 < e59):
                        ok_chain = False
                        break
                    if float(s9) <= 0 or float(s20) <= 0:
                        ok_chain = False
                        break
                else:
                    if (p9 <= p59 and e9 > e59) or (p20 <= p59 and e20 > e59):
                        ok_chain = False
                        break
                    if float(s9) >= 0 or float(s20) >= 0:
                        ok_chain = False
                        break
                s9v, s20v = _signed(e9, e59, bull), _signed(e20, e59, bull)
                if prev_s9 is not None:
                    if s9v < prev_s9 or s20v < prev_s20:
                        ok_chain = False
                        break
                prev_s9, prev_s20 = s9v, s20v
            if not ok_chain:
                continue
            # final distances should be expanding away (signed gap >= 0 preferred)
            e9, e20, e59 = float(cur["ema_9"]), float(cur["ema_20"]), float(cur["ema_59"])
            if _signed(e9, e59, bull) < 0 and _signed(e20, e59, bull) < 0:
                # both still on wrong side after expand — still allowed if distances grew from touch
                pass

            metrics = _band_metrics(work, i, cfg)
            ts = _utc(pd.Timestamp(cur["open_time"]).to_pydatetime().replace(tzinfo=timezone.utc))
            first_bar = touch_i
            ep = make_cross_episode_id(
                symbol=symbol,
                timeframe=timeframe,
                direction=d,
                first_cross_bar=first_bar,
                first_leg=f"TOUCH{touch_atr}_EXP{expand_bars}",
            )
            if ep in seen_ep:
                continue
            seen_ep.add(ep)
            sm = dict(metrics)
            sm["touch_atr"] = float(touch_atr)
            sm["expand_bars"] = int(expand_bars)
            sm["first_leg"] = "TOUCH"
            sm["first_cross_bar"] = first_bar
            sm["second_cross_bar"] = i
            valid.append(
                {
                    "candidate_id": make_candidate_id(symbol, timeframe, d, ts, f"M4T{touch_atr}E{expand_bars}"),
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "direction": d,
                    "candidate_type": CandidateType.COMPRESSED_EMA59_REBOUND.value,
                    "candidate_at": ts,
                    "bar_index": i,
                    "ema_before": _ema_vals(work.iloc[i - 1]),
                    "ema_after": _ema_vals(cur),
                    "ema_metrics": sm,
                    "reason_codes": ["VALID_TOUCH_AND_EXPAND"],
                    "mode_family": "TOUCH_AND_EXPAND",
                    "exact_gap": None,
                    "first_leg": "TOUCH",
                    "first_cross_bar": first_bar,
                    "cross_episode_id": ep,
                    "touch_atr": float(touch_atr),
                    "expand_bars": int(expand_bars),
                }
            )
    return valid


def detect_compressed_rebound_only(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    cfg: EmaDualCrossConfig | None = None,
) -> list[dict[str, Any]]:
    """M5: production rebound path with sync disabled (research cfg only)."""
    from dataclasses import replace

    base = cfg or EMA_DUAL_CROSS_DEFAULTS
    research_cfg = replace(base, enable_sync_cross=False, enable_compressed_rebound=True)
    valid, _ = detect_cross_events(df, symbol=symbol, timeframe=timeframe, cfg=research_cfg)
    out: list[dict[str, Any]] = []
    for raw in valid:
        if str(raw.get("candidate_type")) != CandidateType.COMPRESSED_EMA59_REBOUND.value:
            continue
        row = dict(raw)
        metrics = dict(row.get("ema_metrics") or {})
        metrics["first_leg"] = "REBOUND"
        metrics["first_cross_bar"] = int(row["bar_index"])
        metrics["second_cross_bar"] = int(row["bar_index"])
        row["ema_metrics"] = metrics
        row["mode_family"] = "COMPRESSED_REBOUND"
        row["exact_gap"] = None
        row["first_leg"] = "REBOUND"
        row["first_cross_bar"] = int(row["bar_index"])
        row["cross_episode_id"] = make_cross_episode_id(
            symbol=symbol,
            timeframe=timeframe,
            direction=str(row["direction"]),
            first_cross_bar=int(row["bar_index"]),
            first_leg="REBOUND",
        )
        out.append(row)
    return out

"""Level lifecycle: invalidation, touches, replacements (no retroactive edits)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.htf_pivot_level_preview.config import (
    INVALIDATION_BOTH,
    INVALIDATION_CLOSE_BREAK_ONLY,
    INVALIDATION_REPLACEMENT_ONLY,
    TOUCH_ATR_ZONE,
    TOUCH_CLOSE_DISTANCE,
    TOUCH_WICK,
    HtfPivotPreviewConfig,
)
from research.regime_scanner.htf_pivot_level_preview.htf_bars import prepare_5m_ohlcv, sequence_segments_5m
from research.regime_scanner.htf_pivot_level_preview.pivots import build_htf_pivot_levels_for_segment


def _ts(v: Any) -> pd.Timestamp:
    t = pd.Timestamp(v)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _iso(ts: Any) -> str:
    return _ts(ts).isoformat().replace("+00:00", "Z")


def _bar_close_time(bucket_start: Any) -> pd.Timestamp:
    """5m bucket_start is open; bar is closed at open+5m."""
    return _ts(bucket_start) + pd.Timedelta(minutes=5)


def is_touch(
    *,
    side: str,
    level_price: float,
    high: float,
    low: float,
    close: float,
    atr: float | None,
    cfg: HtfPivotPreviewConfig,
) -> bool:
    tol = float(cfg.tick_tolerance)
    if cfg.touch_mode == TOUCH_WICK:
        if side == "support":
            return low <= level_price + tol
        return high >= level_price - tol
    if cfg.touch_mode == TOUCH_CLOSE_DISTANCE:
        return abs(close - level_price) <= tol
    if cfg.touch_mode == TOUCH_ATR_ZONE:
        atr_v = float(atr) if atr is not None and np.isfinite(atr) and atr > 0 else float("nan")
        if not np.isfinite(atr_v):
            return False
        band = cfg.touch_atr_mult * atr_v
        if side == "support":
            return low <= level_price + band
        return high >= level_price - band
    return False


def apply_lifecycle(
    raw_levels: list[dict[str, Any]],
    candles_5m: pd.DataFrame,
    cfg: HtfPivotPreviewConfig,
) -> list[dict[str, Any]]:
    """Apply close-break / replacement invalidation and touch counts on 5m closes.

    No past visible_from or invalidation timestamps are rewritten.
    Touches before visible_from do not count.
    Close-break uses the close of a fully finished 5m bar (known at bar close time).
    """
    if not raw_levels:
        return []
    frame = prepare_5m_ohlcv(candles_5m)
    if frame.empty:
        return deepcopy(raw_levels)

    # Sort births by visible_from
    levels = sorted(deepcopy(raw_levels), key=lambda r: (_ts(r["visible_from_timestamp"]), r["level_id"]))

    # Track active by (source_type, timeframe, side) for replacement
    active_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    finalized: list[dict[str, Any]] = []

    # First pass: replacement at birth time (when new level becomes visible)
    mode = cfg.invalidation_mode
    for lv in levels:
        key = (str(lv["source_type"]), str(lv["timeframe"]), str(lv["side"]))
        if mode in (INVALIDATION_REPLACEMENT_ONLY, INVALIDATION_BOTH):
            prev = active_by_key.get(key)
            if prev is not None and prev.get("active"):
                # end previous at this level's visible_from (not retroactive before)
                prev["active"] = False
                if prev.get("invalidated_at") is None:
                    prev["invalidated_at"] = lv["visible_from_timestamp"]
                    prev["invalidation_reason"] = "replacement"
                    prev["replacement_level_id"] = lv["level_id"]
                elif prev.get("invalidation_reason") == "close_break":
                    # already broken; still record replacement id diagnostically
                    prev["replacement_level_id"] = lv["level_id"]
        active_by_key[key] = lv
        finalized.append(lv)

    # Index for close-break / touches: walk 5m bars once
    by_id = {r["level_id"]: r for r in finalized}
    atr = None
    if "atr_14" in frame.columns:
        atr_arr = frame["atr_14"].to_numpy(dtype=float)
    else:
        atr_arr = np.full(len(frame), np.nan)

    # Pre-parse timestamps once (hot path); dense TFs multiply level×bar cost.
    vis_ts = [_ts(lv["visible_from_timestamp"]) for lv in finalized]
    inv_ts = [_ts(lv["invalidated_at"]) if lv.get("invalidated_at") is not None else None for lv in finalized]

    ts_arr = pd.to_datetime(frame["timestamp"], utc=True)
    high_arr = frame["high"].to_numpy(dtype=float)
    low_arr = frame["low"].to_numpy(dtype=float)
    close_arr = frame["close"].to_numpy(dtype=float)

    for i in range(len(frame)):
        bar_open = pd.Timestamp(ts_arr.iloc[i])
        if bar_open.tzinfo is None:
            bar_open = bar_open.tz_localize("UTC")
        else:
            bar_open = bar_open.tz_convert("UTC")
        bar_close_t = bar_open + pd.Timedelta(minutes=5)
        high = float(high_arr[i])
        low = float(low_arr[i])
        close = float(close_arr[i])
        atr_i = float(atr_arr[i - 1]) if i >= 1 else float(atr_arr[i])

        for j, lv in enumerate(finalized):
            vis = vis_ts[j]
            if bar_close_t <= vis:
                continue
            inv = inv_ts[j]
            if inv is not None and inv < bar_close_t and lv.get("invalidation_reason") == "close_break":
                continue
            if inv is not None and inv <= bar_open and lv.get("invalidation_reason") == "replacement":
                continue

            still_active = lv.get("active", True)
            if still_active or (inv is not None and inv >= bar_close_t):
                active_now = bool(lv.get("active", True))
                if inv is not None and inv <= bar_close_t:
                    active_now = False
                if active_now and is_touch(
                    side=str(lv["side"]),
                    level_price=float(lv["level_price"]),
                    high=high,
                    low=low,
                    close=close,
                    atr=atr_i,
                    cfg=cfg,
                ):
                    lv["touch_count"] = int(lv.get("touch_count") or 0) + 1
                    if not lv.get("first_touch_timestamp"):
                        lv["first_touch_timestamp"] = _iso(bar_close_t)

            if mode in (INVALIDATION_CLOSE_BREAK_ONLY, INVALIDATION_BOTH) and lv.get("active", True):
                if inv is not None and inv < bar_close_t:
                    continue
                price = float(lv["level_price"])
                broke = (lv["side"] == "support" and close < price) or (
                    lv["side"] == "resistance" and close > price
                )
                if broke:
                    rep_at = lv.get("invalidated_at")
                    if (
                        lv.get("invalidation_reason") == "replacement"
                        and rep_at is not None
                        and _ts(rep_at) < bar_close_t
                    ):
                        continue
                    lv["active"] = False
                    lv["invalidated_at"] = _iso(bar_close_t)
                    lv["invalidation_reason"] = "close_break"
                    inv_ts[j] = bar_close_t

    # Clean internal flags; ensure inactive have invalidated_at
    for lv in finalized:
        if not lv.get("active", True) and lv.get("invalidated_at") is None:
            lv["invalidated_at"] = lv["visible_from_timestamp"]
            lv["invalidation_reason"] = lv.get("invalidation_reason") or "inactive"
        if lv.get("invalidated_at") is not None:
            lv["active"] = False
    return finalized


def build_external_and_protected(
    candles_5m: pd.DataFrame,
    *,
    symbol: str,
    cfg: HtfPivotPreviewConfig,
) -> list[dict[str, Any]]:
    """Optional families via existing causal builders (adapter; no absorption changes)."""
    from research.regime_scanner.orderflow_absorption.config import AbsorptionConfig
    from research.regime_scanner.orderflow_absorption.features import enrich_frame
    from research.regime_scanner.orderflow_absorption_level.config import LevelAbsorptionConfig
    from research.regime_scanner.orderflow_absorption_level.levels_build import (
        build_external_swing_levels,
        build_protected_levels,
    )

    out: list[dict[str, Any]] = []
    if not cfg.include_external_swing and not cfg.include_protected:
        return out

    frame = prepare_5m_ohlcv(candles_5m)
    if "bucket_start" not in frame.columns:
        frame["bucket_start"] = frame["timestamp"]
    if "symbol" not in frame.columns:
        frame["symbol"] = symbol
    if "sequence_id" not in frame.columns:
        frame["sequence_id"] = 0

    abs_cfg = AbsorptionConfig()
    enriched = enrich_frame(frame, abs_cfg)
    lacfg = LevelAbsorptionConfig(
        level_types=tuple(
            x
            for x, on in (
                ("external_swing", cfg.include_external_swing),
                ("protected", cfg.include_protected),
            )
            if on
        ),
        pivot_left=cfg.external_pivot_left,
        pivot_right=cfg.external_pivot_right,
        protected_variant=cfg.protected_variant,
    )

    for seq_id, g in enriched.groupby("sequence_id", sort=True):
        local = g.sort_values("bucket_start").reset_index(drop=True)
        local["bar_index"] = range(len(local))
        raw: list[dict[str, Any]] = []
        if cfg.include_external_swing:
            raw.extend(
                build_external_swing_levels(
                    local, symbol=symbol, sequence_id=seq_id, cfg=lacfg
                )
            )
        if cfg.include_protected:
            raw.extend(
                build_protected_levels(local, symbol=symbol, sequence_id=seq_id, cfg=lacfg)
            )
        for r in raw:
            conf_i = int(r["confirmation_index"])
            # visible from next bar after confirmation (strict <) → use bar close of conf bar
            # inventory uses confirmation_index as HTF/5m index; visible at confirmation close
            if 0 <= conf_i < len(local):
                conf_open = local["bucket_start"].iloc[conf_i]
                visible = _iso(_bar_close_time(conf_open))
                pivot_ts = r.get("extreme_timestamp") or (
                    _iso(local["bucket_start"].iloc[int(r["extreme_index"])])
                    if r.get("extreme_index") is not None
                    else visible
                )
            else:
                visible = r.get("confirmation_timestamp") or ""
                pivot_ts = r.get("extreme_timestamp") or visible
            inv_at = None
            if r.get("invalidated_at") is not None:
                ii = int(r["invalidated_at"])
                if 0 <= ii < len(local):
                    inv_at = _iso(_bar_close_time(local["bucket_start"].iloc[ii]))
            out.append(
                {
                    "level_id": r["level_id"],
                    "symbol": symbol,
                    "source_type": str(r["level_type"]),
                    "timeframe": "5m",
                    "side": r["side"],
                    "level_price": float(r["level_price"]),
                    "pivot_index": r.get("extreme_index"),
                    "confirmation_index": conf_i,
                    "created_index": conf_i,
                    "pivot_timestamp": pivot_ts,
                    "confirmation_timestamp": visible,
                    "visible_from_timestamp": visible,
                    "invalidated_at": inv_at,
                    "invalidation_reason": r.get("invalidation_reason"),
                    "replacement_level_id": None,
                    "active": inv_at is None,
                    "touch_count": 0,
                    "sequence_id": seq_id,
                    "repaint_safe": True,
                }
            )
    return out


def build_all_levels(
    candles_5m: pd.DataFrame,
    *,
    symbol: str,
    cfg: HtfPivotPreviewConfig,
) -> list[dict[str, Any]]:
    """Full inventory for one symbol: HTF pivots (+ optional external/protected)."""
    segments = sequence_segments_5m(candles_5m)
    raw: list[dict[str, Any]] = []
    for seg in segments:
        seq = seg["sequence_id"].iloc[0] if "sequence_id" in seg.columns else 0
        raw.extend(
            build_htf_pivot_levels_for_segment(
                seg, symbol=symbol, cfg=cfg, sequence_id=seq
            )
        )
    # lifecycle on full frame (HTF only first)
    htf_done = apply_lifecycle(raw, candles_5m, cfg)
    extra = build_external_and_protected(candles_5m, symbol=symbol, cfg=cfg)
    # touch counts for extra on same path
    if extra:
        extra = apply_lifecycle(extra, candles_5m, cfg)
    return sorted(
        htf_done + extra,
        key=lambda r: (
            str(r["source_type"]),
            str(r["side"]),
            str(r["visible_from_timestamp"]),
            str(r["level_id"]),
        ),
    )


def replay_levels_stepwise(
    candles_5m: pd.DataFrame,
    *,
    symbol: str,
    cfg: HtfPivotPreviewConfig,
    step: int = 48,
) -> list[dict[str, Any]]:
    """Expanding-window rebuild; used to assert no past visible_from mutation."""
    frame = prepare_5m_ohlcv(candles_5m)
    if frame.empty:
        return []
    last: list[dict[str, Any]] = []
    for end in range(max(step, 50), len(frame) + 1, step):
        last = build_all_levels(frame.iloc[:end], symbol=symbol, cfg=cfg)
    return last


def assert_no_visible_from_rewrite(
    earlier: list[dict[str, Any]],
    later: list[dict[str, Any]],
) -> None:
    """Levels present in earlier inventory must keep the same visible_from in later."""
    later_map = {r["level_id"]: r for r in later}
    for r in earlier:
        if r["level_id"] in later_map:
            assert later_map[r["level_id"]]["visible_from_timestamp"] == r["visible_from_timestamp"]
            assert later_map[r["level_id"]]["level_price"] == r["level_price"]

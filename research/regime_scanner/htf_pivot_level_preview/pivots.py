"""Causal HTF pivot detection with visible_from at confirmation bar close."""

from __future__ import annotations

from typing import Any

import pandas as pd

from research.regime_scanner.config import RegimeScannerConfig
from research.regime_scanner.htf_pivot_level_preview.config import (
    LTF_LOOKBACK_TIMEFRAMES,
    HtfPivotPreviewConfig,
    level_id,
)
from research.regime_scanner.htf_pivot_level_preview.htf_bars import build_closed_htf_bars
from research.regime_scanner.swings import find_confirmed_pivots


def _iso(ts: Any) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.isoformat().replace("+00:00", "Z")


def pivots_on_htf_frame(
    htf: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    source_type: str,
    left: int,
    right: int,
    sequence_id: Any = 0,
) -> list[dict[str, Any]]:
    """Detect pivots on closed HTF bars; visible_from = confirming bar's decision_time.

    A pivot at HTF index p becomes known only after HTF bar p+right fully closes.
    find_confirmed_pivots uses confirmation_index = p+right with timestamp = bar open;
    we shift visibility to decision_time (bar close) so unfinished HTF bars never confirm.
    """
    if htf.empty or len(htf) < left + right + 1:
        return []
    candles = htf.copy()
    if "timestamp" not in candles.columns:
        raise ValueError("htf frame requires timestamp")
    cfg = RegimeScannerConfig(pivot_left=left, pivot_right=right)
    pivots = find_confirmed_pivots(candles, config=cfg, pivot_left=left, pivot_right=right)
    out: list[dict[str, Any]] = []
    for p in pivots:
        conf_i = int(p.confirmation_index)
        pivot_i = int(p.pivot_index)
        side = "resistance" if p.pivot_type == "high" else "support"
        # confirmation completes at close of confirming HTF bar
        if "decision_time" in candles.columns:
            visible_from = pd.Timestamp(candles["decision_time"].iloc[conf_i])
        else:
            tf_m = int(candles["tf_minutes"].iloc[0]) if "tf_minutes" in candles.columns else 0
            visible_from = pd.Timestamp(candles["timestamp"].iloc[conf_i]) + pd.Timedelta(minutes=tf_m)
        pivot_ts = pd.Timestamp(candles["timestamp"].iloc[pivot_i])
        conf_ts = visible_from
        price = float(p.price)
        conf_iso = _iso(conf_ts)
        lid = level_id(
            symbol=symbol,
            source_type=source_type,
            timeframe=timeframe,
            side=side,
            confirmation_timestamp=conf_iso,
            level_price=price,
        )
        out.append(
            {
                "level_id": lid,
                "symbol": symbol,
                "source_type": source_type,
                "timeframe": timeframe,
                "side": side,
                "level_price": price,
                "pivot_index": pivot_i,
                "confirmation_index": conf_i,
                "created_index": conf_i,
                "pivot_timestamp": _iso(pivot_ts),
                "confirmation_timestamp": conf_iso,
                "visible_from_timestamp": conf_iso,
                "invalidated_at": None,
                "invalidation_reason": None,
                "replacement_level_id": None,
                "active": True,
                "touch_count": 0,
                "sequence_id": sequence_id,
                "repaint_safe": True,
            }
        )
    return out


def build_htf_pivot_levels_for_segment(
    candles_5m: pd.DataFrame,
    *,
    symbol: str,
    cfg: HtfPivotPreviewConfig,
    sequence_id: Any = 0,
    end_wall: pd.Timestamp | None = None,
) -> list[dict[str, Any]]:
    levels: list[dict[str, Any]] = []
    frame_full = candles_5m
    for tf in cfg.htf_timeframes:
        spec = cfg.htf_spec(tf)
        frame = frame_full
        # Dense TFs densify near price; restrict detection window for speed/noise.
        if tf in LTF_LOOKBACK_TIMEFRAMES and int(getattr(cfg, "ltf_lookback_days", 0) or 0) > 0:
            prepared = frame_full.copy()
            if "timestamp" not in prepared.columns and "bucket_start" in prepared.columns:
                prepared["timestamp"] = prepared["bucket_start"]
            prepared["timestamp"] = pd.to_datetime(prepared["timestamp"], utc=True)
            if not prepared.empty:
                cutoff = prepared["timestamp"].max() - pd.Timedelta(days=int(cfg.ltf_lookback_days))
                frame = prepared[prepared["timestamp"] >= cutoff].reset_index(drop=True)
        htf = build_closed_htf_bars(frame, minutes=spec["minutes"], end_wall=end_wall)
        levels.extend(
            pivots_on_htf_frame(
                htf,
                symbol=symbol,
                timeframe=tf,
                source_type=cfg.source_type_for_tf(tf),
                left=spec["left"],
                right=spec["right"],
                sequence_id=sequence_id,
            )
        )
    return levels

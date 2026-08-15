"""Event deduplication for level-conditioned absorption states."""
from __future__ import annotations

import math

import hashlib
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.orderflow_absorption_level.config import LevelAbsorptionConfig
from research.regime_scanner.oi_price_delta_pattern.features import _contiguous


def make_event_id(
    *,
    symbol: str,
    pattern: str,
    flow_rule: str,
    lookback: int,
    level_id: str | None,
    event_start_timestamp: str,
) -> str:
    raw = f"{symbol}|{pattern}|{flow_rule}|{lookback}|{level_id or 'NO_LEVEL'}|{event_start_timestamp}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _in_zone(row: dict[str, Any], max_d: float) -> bool:
    if bool(row.get("no_level")):
        return False
    if bool(row.get("far_from_level")):
        return False
    d = row.get("distance_atr")
    if d is None or d != d:
        return False
    return float(d) <= max_d


def _pattern_true(row: dict[str, Any] | None) -> bool:
    return row is not None


def build_absorption_level_events(
    df: pd.DataFrame,
    anchor_assignments: list[dict[str, Any]],
    *,
    patterns: tuple[str, ...],
    cfg: LevelAbsorptionConfig,
) -> list[dict[str, Any]]:
    """Collapse consecutive same pattern×level zone bars into physical events."""
    if not anchor_assignments:
        return []

    # Index: (pattern, flow, lookback, anchor_i) -> row
    by_key: dict[tuple[str, str, int], dict[int, dict[str, Any]]] = {}
    for row in anchor_assignments:
        pattern = str(row["pattern"])
        if pattern not in patterns:
            continue
        fr = str(row.get("flow_rule") or "")
        if fr not in cfg.flow_rules:
            continue
        lb = int(row.get("lookback") or 0)
        if lb not in cfg.lookbacks:
            continue
        key = (pattern, fr, lb)
        by_key.setdefault(key, {})[int(row["anchor_index"])] = row

    seq = df["sequence_id"].to_numpy() if "sequence_id" in df.columns else np.zeros(len(df))
    ts = df["bucket_start"].to_numpy()
    n = len(df)
    events: list[dict[str, Any]] = []
    cooldown = int(cfg.event_cooldown_bars)
    max_d = float(cfg.max_distance_atr)

    for (pattern, flow_rule, lookback), by_i in sorted(by_key.items()):
        direction = "bullish" if pattern == "A4" else "bearish"
        active: dict[str, Any] | None = None
        cooldown_until = -1
        anchors_buf: list[dict[str, Any]] = []

        def _flush(end_i: int, reason: str) -> None:
            nonlocal active, anchors_buf
            if active is None:
                return
            dists = []
            for anchor in anchors_buf:
                value = anchor.get("distance_atr")
                if value is None:
                    continue
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    dists.append(value)

            start_i = int(active["event_start_index"])
            end_i = max(start_i, int(end_i))
            start_ts = str(df["bucket_start"].iloc[start_i])
            end_ts = str(df["bucket_start"].iloc[end_i])
            lid = active.get("level_id")
            eid = make_event_id(
                symbol=str(active["symbol"]),
                pattern=pattern,
                flow_rule=flow_rule,
                lookback=lookback,
                level_id=str(lid) if lid else None,
                event_start_timestamp=start_ts,
            )
            events.append(
                {
                    "event_id": eid,
                    "symbol": active["symbol"],
                    "sequence_id": active["sequence_id"],
                    "pattern": pattern,
                    "direction": direction,
                    "flow_rule": flow_rule,
                    "lookback": lookback,
                    "level_id": lid,
                    "level_type": active.get("level_type"),
                    "level_side": active.get("side"),
                    "level_price": active.get("level_price"),
                    "event_start_index": start_i,
                    "event_end_index": end_i,
                    "event_start_timestamp": start_ts,
                    "event_end_timestamp": end_ts,
                    "anchor_count": len(anchors_buf),
                    "first_anchor_index": int(anchors_buf[0]["anchor_index"]) if anchors_buf else start_i,
                    "last_anchor_index": int(anchors_buf[-1]["anchor_index"]) if anchors_buf else end_i,
                    "min_distance_atr": float(min(dists)) if dists else None,
                    "median_distance_atr": float(np.median(dists)) if dists else None,
                    "distance_bucket_at_entry": active.get("distance_bucket"),
                    "confluent": bool(active.get("confluent")),
                    "entry_eligible_index": start_i,
                    "entry_eligible_timestamp": start_ts,
                    "confirmation_type": "R0",
                    "event_end_reason": reason,
                    "no_level": bool(active.get("no_level")),
                    "far_from_level": bool(active.get("far_from_level")),
                    "atr_reference": active.get("atr_reference"),
                    "anchor_price": active.get("anchor_price"),
                }
            )
            active = None
            anchors_buf = []

        indices = sorted(by_i.keys())
        # Walk all bars in range of anchors to detect gaps / pattern edges
        if not indices:
            continue
        i_lo, i_hi = indices[0], indices[-1]
        prev_true = False
        for i in range(i_lo, i_hi + 1):
            row = by_i.get(i)
            true_now = row is not None
            in_zone = _in_zone(row, max_d) if row is not None else False
            gap = i > 0 and not _contiguous(seq, ts, i - 1, i)

            if gap and active is not None:
                _flush(i - 1, "sequence_gap")
                cooldown_until = i + cooldown
                prev_true = False
                continue

            if i < cooldown_until and active is None:
                prev_true = true_now
                continue

            if true_now and row is not None:
                start_new = False
                if not prev_true:
                    start_new = True
                elif active is not None:
                    # zone re-entry or level_id change
                    same_level = (active.get("level_id") == row.get("level_id")) or (
                        active.get("no_level") and row.get("no_level")
                    )
                    was_zone = not bool(active.get("no_level")) and not bool(active.get("far_from_level"))
                    if in_zone and not was_zone and not bool(row.get("no_level")):
                        start_new = True
                    if not same_level and in_zone:
                        start_new = True
                    # far/no_level treatment events: still allow continuous pattern streak
                    if active.get("no_level") or active.get("far_from_level"):
                        if row.get("no_level") or row.get("far_from_level"):
                            start_new = False
                        elif in_zone:
                            start_new = True
                else:
                    start_new = True

                if start_new:
                    if active is not None:
                        _flush(i - 1, "new_event")
                    active = {
                        "symbol": row["symbol"],
                        "sequence_id": int(seq[i]) if np.isscalar(seq[i]) else seq[i],
                        "level_id": row.get("level_id"),
                        "level_type": row.get("level_type"),
                        "side": row.get("side"),
                        "level_price": row.get("level_price"),
                        "distance_bucket": row.get("distance_bucket"),
                        "confluent": row.get("confluent"),
                        "no_level": row.get("no_level"),
                        "far_from_level": row.get("far_from_level"),
                        "atr_reference": row.get("atr_reference"),
                        "anchor_price": row.get("anchor_price"),
                        "event_start_index": i,
                    }
                    anchors_buf = [row]
                else:
                    if active is not None:
                        anchors_buf.append(row)
                        # update zone membership flags to current
                        active["no_level"] = row.get("no_level")
                        active["far_from_level"] = row.get("far_from_level")
                        active["level_id"] = row.get("level_id")
                        active["level_type"] = row.get("level_type")
                        active["side"] = row.get("side")
                        active["level_price"] = row.get("level_price")
                        active["confluent"] = bool(active.get("confluent")) or bool(row.get("confluent"))
            else:
                # pattern false
                if active is not None:
                    # end if left zone or was already no_level/far event
                    if bool(active.get("no_level")) or bool(active.get("far_from_level")):
                        _flush(i - 1, "pattern_false")
                        cooldown_until = i + cooldown
                    else:
                        # still in event if we were in zone — without pattern, end when zone left
                        # without current row we cannot measure zone; end event
                        _flush(i - 1, "pattern_false_zone_exit")
                        cooldown_until = i + cooldown

            prev_true = true_now

        if active is not None:
            _flush(indices[-1], "stream_end")

    events.sort(key=lambda e: (e["symbol"], e["pattern"], e["event_start_index"], e["event_id"]))
    return events

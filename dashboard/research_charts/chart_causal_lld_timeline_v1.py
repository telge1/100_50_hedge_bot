"""Causal LLD landscape timeline (ui_selected membership, prefix-safe, no COMMON break filter)."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import datetime
from typing import Any

from .chart_scanner_pools_v1 import _ensure_utc, _parse_decision_close

CAUSAL_LLD_LANDSCAPE_LAYER = "causal_lld_landscape_v1"
CAUSAL_LLD_LANDSCAPE_NAMESPACE = "CAUSAL_LLD_LANDSCAPE"

_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_CACHE_MAX = 32


def _cache_key(
    symbol: str,
    timeframe: str,
    from_close: str,
    to_close: str,
    config_version: int,
) -> str:
    return "|".join(
        [
            str(symbol or "").upper(),
            str(timeframe or ""),
            "causal_lld",
            str(from_close or ""),
            str(to_close or ""),
            str(int(config_version)),
        ]
    )


def get_cached_causal_lld(key: str) -> dict[str, Any] | None:
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    return None


def store_cached_causal_lld(key: str, payload: dict[str, Any]) -> None:
    _CACHE[key] = payload
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def clear_causal_lld_cache() -> None:
    _CACHE.clear()


def _zone_colors(side: str) -> tuple[str, str]:
    is_lower = side == "lower"
    color = "#00bcd4" if is_lower else "#ec4079"
    fill = "#00bcd433" if is_lower else "#ec407933"
    return color, fill


def _break_unix(row: dict[str, Any]) -> int | None:
    break_at = row.get("break_at")
    if break_at is None:
        return None
    return int(_ensure_utc(break_at).timestamp())


def _resolve_end_ts(
    deselection_unix: int,
    row: dict[str, Any],
    *,
    segment_start_unix: int,
) -> tuple[int, str]:
    """End at break only when it falls inside [segment_start, deselection]."""
    ba = _break_unix(row)
    start = int(segment_start_unix)
    end = int(deselection_unix)
    if ba is not None and start <= ba <= end:
        return ba, "broken"
    return end, "deselected"


def _canonical_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for seg in sorted(
        segments,
        key=lambda s: (
            s.get("pool_id") or "",
            int(s.get("segment_index") or 0),
            int(s.get("start_timestamp") or 0),
        ),
    ):
        out.append(
            {
                "pool_id": seg["pool_id"],
                "segment_index": int(seg["segment_index"]),
                "side": seg["side"],
                "lower_price": float(seg["lower_price"]),
                "upper_price": float(seg["upper_price"]),
                "start_timestamp": int(seg["start_timestamp"]),
                "end_timestamp": int(seg["end_timestamp"]),
                "end_reason": seg.get("end_reason"),
            }
        )
    return out


def segments_hash(segments: list[dict[str, Any]]) -> str:
    payload = json.dumps(_canonical_segments(segments), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _visible_ui_rows(ui_selected: list[dict], decision_close: datetime) -> list[dict]:
    dc = _ensure_utc(decision_close)
    return [r for r in ui_selected if _ensure_utc(r["known"]) <= dc]


def _open_segment(
    row: dict,
    segment_index: int,
    timeframe: str,
    decision_close: datetime,
) -> dict[str, Any]:
    known = row["known"]
    source = row.get("source")
    known_ts = int(_ensure_utc(known).timestamp())
    dc_ts = int(_ensure_utc(decision_close).timestamp())
    start_ts = max(known_ts, dc_ts)
    return {
        "pool_id": str(row["pool_id"]),
        "segment_index": segment_index,
        "side": str(row["side"]),
        "lower": float(row["bottom"]),
        "upper": float(row["top"]),
        "known": known,
        "break_at": row.get("break_at"),
        "source": source,
        "start_ts": start_ts,
        "last_row": row,
        "source_timeframe": timeframe,
    }


def _finalize_segment(seg: dict[str, Any], end_ts: int, end_reason: str) -> dict[str, Any]:
    known = seg["known"]
    break_at = seg.get("break_at")
    source = seg.get("source")
    out: dict[str, Any] = {
        "pool_id": seg["pool_id"],
        "segment_index": int(seg["segment_index"]),
        "side": seg["side"],
        "lower_price": float(seg["lower"]),
        "upper_price": float(seg["upper"]),
        "start_timestamp": int(seg["start_ts"]),
        "end_timestamp": int(end_ts),
        "known_at": _ensure_utc(known).isoformat() if known else None,
        "break_at": _ensure_utc(break_at).isoformat() if break_at else None,
        "end_reason": end_reason,
        "source_timeframe": seg.get("source_timeframe") or "15m",
    }
    if isinstance(source, datetime):
        out["source_timestamp"] = _ensure_utc(source).isoformat()
    return out


def _clip_segment_to_window(
    seg: dict[str, Any],
    from_unix: int,
    to_unix: int,
) -> dict[str, Any] | None:
    start = int(seg["start_timestamp"])
    end = int(seg["end_timestamp"])
    if end < from_unix or start > to_unix:
        return None
    clipped_start = max(start, from_unix)
    clipped_end = min(end, to_unix)
    if clipped_end < clipped_start:
        return None
    out = dict(seg)
    out["start_timestamp"] = clipped_start
    out["end_timestamp"] = clipped_end
    return out


def _segment_to_zone(seg: dict[str, Any]) -> dict[str, Any]:
    pid = str(seg["pool_id"])
    seg_idx = int(seg["segment_index"])
    side = str(seg["side"])
    color, fill = _zone_colors(side)
    lower = float(seg["lower_price"])
    upper = float(seg["upper_price"])
    direction = "support" if side == "lower" else "resistance"
    return {
        "id": f"causal-lld:{pid}:{seg_idx}",
        "pool_id": pid,
        "segment_index": seg_idx,
        "namespace": CAUSAL_LLD_LANDSCAPE_NAMESPACE,
        "type": "zone",
        "side": side,
        "lower_price": lower,
        "upper_price": upper,
        "bottom_price": lower,
        "top_price": upper,
        "start_timestamp": int(seg["start_timestamp"]),
        "end_timestamp": int(seg["end_timestamp"]),
        "style": {"color": color, "fillColor": fill, "lineWidth": 2},
        "known_at": seg.get("known_at"),
        "break_at": seg.get("break_at"),
        "end_reason": seg.get("end_reason"),
        "source_timeframe": seg.get("source_timeframe"),
        "metadata": {
            "source": "causal_lld_landscape",
            "direction": direction,
            "pool_id": pid,
            "segment_index": seg_idx,
            "side": side,
            "known_at": seg.get("known_at"),
            "break_at": seg.get("break_at"),
            "end_reason": seg.get("end_reason"),
            "source_timestamp": seg.get("source_timestamp"),
        },
    }


def visible_pool_ids_at(
    zones: list[dict[str, Any]],
    decision_close_unix: int,
) -> set[str]:
    """Pool ids whose causal segment covers decision_close (window_end inclusive)."""
    t = int(decision_close_unix)
    out: set[str] = set()
    for z in zones:
        start = int(z["start_timestamp"])
        end = int(z["end_timestamp"])
        reason = z.get("end_reason") or (z.get("metadata") or {}).get("end_reason")
        if t < start:
            continue
        if reason == "window_end_active":
            if t <= end:
                out.add(str(z["pool_id"]))
        elif t < end:
            out.add(str(z["pool_id"]))
    return out


def chart_causal_lld_timeline(
    symbol: str,
    from_close: datetime | str,
    to_close: datetime | str,
    *,
    timeframe: str = "15m",
    cache: dict[str, Any] | None = None,
    use_response_cache: bool = True,
) -> dict[str, Any]:
    from bot.e1r_live_scanner.config import ensure_runtime_paths

    ensure_runtime_paths()
    from dashboard.research_charts.frozen_lld_config_v1 import (
        FROZEN_LLD_CONFIG_VERSION,
        HISTORY_WEEKS,
        PANE_FROM_UTC,
        frozen_lld_config_for_timeframe_v1,
    )
    from dashboard.research_charts.lld_research_kernel import (
        candle_stamps,
        load_pane_candles,
        scanner_pools_for_index,
    )
    from pool_scan.clock import bar_close

    sym = str(symbol or "").strip().upper()
    tf = str(timeframe or "15m").strip()
    if tf != "15m":
        raise ValueError("causal lld landscape is 15m only")

    from_dt = _parse_decision_close(from_close)
    to_dt = _parse_decision_close(to_close)
    if from_dt > to_dt:
        raise ValueError("from_close must be <= to_close")

    cfg_version = int(FROZEN_LLD_CONFIG_VERSION)
    cache_key = _cache_key(sym, tf, from_dt.isoformat(), to_dt.isoformat(), cfg_version)
    if use_response_cache:
        hit = get_cached_causal_lld(cache_key)
        if hit is not None:
            return hit

    packed, candles = load_pane_candles(
        sym,
        tf,
        from_unix=int(PANE_FROM_UTC.timestamp()),
        to_unix=int(to_dt.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    if not bool(packed.get("strict_complete_buckets")):
        raise ValueError(f"strict_complete_buckets=False for {sym} {tf}")

    stamps = candle_stamps(candles)
    cfg = frozen_lld_config_for_timeframe_v1(tf)
    lld_cache = cache if cache is not None else {}

    indices: list[int] = []
    decision_closes: list[datetime] = []
    for i, open_ts in enumerate(stamps):
        dc = bar_close(open_ts, tf)
        if from_dt <= dc <= to_dt:
            indices.append(i)
            decision_closes.append(dc)

    if not indices:
        raise ValueError("no 15m decision bars in [from_close, to_close] on pane")

    from_unix = int(from_dt.timestamp())
    to_unix = int(to_dt.timestamp())

    active: dict[str, dict[str, Any]] = {}
    segment_index_next: dict[str, int] = {}
    closed: list[dict[str, Any]] = []
    prev_visible: set[str] = set()

    for i, dc in zip(indices, decision_closes):
        _available, ui_selected, _proof = scanner_pools_for_index(
            candles, tf, i, cfg, cache=lld_cache
        )
        visible_rows = _visible_ui_rows(ui_selected, dc)
        row_map = {str(r["pool_id"]): r for r in visible_rows}
        visible_ids = set(row_map)
        dc_unix = int(dc.timestamp())

        for pid in list(active.keys()):
            if pid not in visible_ids:
                continue
            row = row_map[pid]
            seg = active[pid]
            new_lo, new_hi = float(row["bottom"]), float(row["top"])
            if (float(seg["lower"]), float(seg["upper"])) == (new_lo, new_hi):
                seg["last_row"] = row
                if row.get("break_at") is not None:
                    seg["break_at"] = row.get("break_at")
                continue
            end_ts, _ = _resolve_end_ts(
                dc_unix, seg["last_row"], segment_start_unix=int(seg["start_ts"])
            )
            closed.append(_finalize_segment(active.pop(pid), end_ts, "bounds_changed"))
            seg_idx = segment_index_next[pid]
            segment_index_next[pid] = seg_idx + 1
            active[pid] = _open_segment(row, seg_idx, tf, dc)

        for pid in prev_visible - visible_ids:
            if pid not in active:
                continue
            seg = active.pop(pid)
            end_ts, end_reason = _resolve_end_ts(
                dc_unix, seg["last_row"], segment_start_unix=int(seg["start_ts"])
            )
            closed.append(_finalize_segment(seg, end_ts, end_reason))

        for pid in visible_ids:
            if pid not in active:
                continue
            row = row_map[pid]
            active[pid]["last_row"] = row
            if row.get("break_at") is not None:
                active[pid]["break_at"] = row.get("break_at")

        for pid in visible_ids - prev_visible:
            if pid in active:
                continue
            row = row_map[pid]
            seg_idx = segment_index_next.get(pid, 0)
            segment_index_next[pid] = seg_idx + 1
            active[pid] = _open_segment(row, seg_idx, tf, dc)

        prev_visible = visible_ids

    for seg in list(active.values()):
        start = int(seg["start_ts"])
        ba = _break_unix(seg["last_row"])
        if ba is not None and start <= ba <= to_unix:
            closed.append(_finalize_segment(seg, ba, "broken"))
        else:
            closed.append(_finalize_segment(seg, to_unix, "window_end_active"))

    clipped_segments: list[dict[str, Any]] = []
    for seg in closed:
        clipped = _clip_segment_to_window(seg, from_unix, to_unix)
        if clipped is not None:
            clipped_segments.append(clipped)

    zones = [_segment_to_zone(s) for s in clipped_segments]
    pool_ids = sorted({z["pool_id"] for z in zones})
    payload: dict[str, Any] = {
        "layer": CAUSAL_LLD_LANDSCAPE_LAYER,
        "namespace": CAUSAL_LLD_LANDSCAPE_NAMESPACE,
        "symbol": sym,
        "timeframe": tf,
        "from_close": from_dt.isoformat(),
        "to_close": to_dt.isoformat(),
        "config_version": cfg_version,
        "pool_count": len(pool_ids),
        "segment_count": len(zones),
        "segments_hash": segments_hash(clipped_segments),
        "zones": zones,
    }
    if use_response_cache:
        store_cached_causal_lld(cache_key, payload)
    return payload


def ui_selected_ids_at_index(
    candles: list,
    timeframe: str,
    index: int,
    decision_close: datetime,
    config=None,
    *,
    cache: dict[str, Any] | None = None,
) -> set[str]:
    """Visible ui_selected pool ids at decision_close (prefix-safe membership)."""
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index

    _available, ui, _ = scanner_pools_for_index(
        candles, timeframe, index, config, cache=cache
    )
    return {str(r["pool_id"]) for r in _visible_ui_rows(ui, decision_close)}

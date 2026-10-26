"""Read-only COMMON pool lifecycle timeline for Research Charts (frozen scanner path)."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from .chart_scanner_pools_v1 import _ensure_utc, _parse_decision_close
from .short_pool_oracle_v1 import parse_decision_iso

SCANNER_COMMON_TIMELINE_LAYER = "scanner_pools_common_timeline_v1"
SCANNER_COMMON_TIMELINE_NAMESPACE = "SCANNER_COMMON_TIMELINE"

_TIMELINE_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_TIMELINE_CACHE_MAX = 32


def _timeline_cache_key(
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
            str(from_close or ""),
            str(to_close or ""),
            str(int(config_version)),
        ]
    )


def get_cached_timeline(key: str) -> dict[str, Any] | None:
    if key in _TIMELINE_CACHE:
        _TIMELINE_CACHE.move_to_end(key)
        return _TIMELINE_CACHE[key]
    return None


def store_cached_timeline(key: str, payload: dict[str, Any]) -> None:
    _TIMELINE_CACHE[key] = payload
    _TIMELINE_CACHE.move_to_end(key)
    while len(_TIMELINE_CACHE) > _TIMELINE_CACHE_MAX:
        _TIMELINE_CACHE.popitem(last=False)


def clear_timeline_cache() -> None:
    _TIMELINE_CACHE.clear()


def _zone_colors(side: str) -> tuple[str, str]:
    is_lower = side == "lower"
    color = "#1e88e5" if is_lower else "#fb8c00"
    fill = "#1e88e533" if is_lower else "#fb8c0033"
    return color, fill


def _end_reason_for_removal(last_row: dict, end_ts_unix: int) -> str:
    break_at = last_row.get("break_at")
    if break_at is not None:
        ba = int(_ensure_utc(break_at).timestamp())
        if ba <= end_ts_unix:
            return "broken"
    return "deselected"


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


def _segment_to_zone(seg: dict[str, Any], timeframe: str) -> dict[str, Any]:
    pid = str(seg["pool_id"])
    seg_idx = int(seg["segment_index"])
    side = str(seg["side"])
    color, fill = _zone_colors(side)
    direction = "support" if side == "lower" else "resistance"
    zone_id = f"scanner-common-timeline:{pid}:{seg_idx}"
    known = seg.get("known_at")
    break_at = seg.get("break_at")
    lower = float(seg["lower_price"])
    upper = float(seg["upper_price"])
    return {
        "id": zone_id,
        "pool_id": pid,
        "segment_index": seg_idx,
        "namespace": SCANNER_COMMON_TIMELINE_NAMESPACE,
        "type": "zone",
        "side": side,
        "lower_price": lower,
        "upper_price": upper,
        "bottom_price": lower,
        "top_price": upper,
        "start_timestamp": int(seg["start_timestamp"]),
        "end_timestamp": int(seg["end_timestamp"]),
        "style": {"color": color, "fillColor": fill, "lineWidth": 2},
        "known_at": known,
        "break_at": break_at,
        "end_reason": seg.get("end_reason"),
        "source_timeframe": timeframe,
        "metadata": {
            "source": "scanner_common_timeline",
            "direction": direction,
            "status": "active",
            "pool_id": pid,
            "segment_index": seg_idx,
            "end_reason": seg.get("end_reason"),
        },
    }


def _segment_active_at(
    start_ts: int,
    end_ts: int,
    end_reason: str | None,
    decision_close_unix: int,
) -> bool:
    t = int(decision_close_unix)
    if t < int(start_ts):
        return False
    if end_reason == "window_end_active":
        return t <= int(end_ts)
    return t < int(end_ts)


def active_pool_ids_at(zones: list[dict[str, Any]], decision_close_unix: int) -> set[str]:
    """Pool ids active at decision_close (matches snapshot membership semantics)."""
    out: set[str] = set()
    for z in zones:
        reason = z.get("end_reason") or (z.get("metadata") or {}).get("end_reason")
        if _segment_active_at(
            int(z["start_timestamp"]),
            int(z["end_timestamp"]),
            reason,
            decision_close_unix,
        ):
            out.add(str(z["pool_id"]))
    return out


def chart_scanner_pool_timeline(
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
        raise ValueError("scanner common timeline is 15m only")

    from_dt = _parse_decision_close(from_close)
    to_dt = _parse_decision_close(to_close)
    if from_dt > to_dt:
        raise ValueError("from_close must be <= to_close")

    cfg_version = int(FROZEN_LLD_CONFIG_VERSION)
    cache_key = _timeline_cache_key(
        sym, tf, from_dt.isoformat(), to_dt.isoformat(), cfg_version
    )
    if use_response_cache:
        hit = get_cached_timeline(cache_key)
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

    active: dict[str, dict[str, Any]] = {}
    segment_index_next: dict[str, int] = {}
    closed: list[dict[str, Any]] = []
    prev_avail: set[str] = set()
    to_unix = int(to_dt.timestamp())

    def finalize_open(seg: dict[str, Any], end_ts: int, end_reason: str) -> dict[str, Any]:
        known = seg["known"]
        break_at = seg.get("break_at")
        return {
            "pool_id": seg["pool_id"],
            "segment_index": seg["segment_index"],
            "side": seg["side"],
            "lower_price": float(seg["lower"]),
            "upper_price": float(seg["upper"]),
            "start_timestamp": int(seg["start_ts"]),
            "end_timestamp": int(end_ts),
            "known_at": _ensure_utc(known).isoformat() if known else None,
            "break_at": _ensure_utc(break_at).isoformat() if break_at else None,
            "end_reason": end_reason,
            "source_timeframe": tf,
        }

    for i, dc in zip(indices, decision_closes):
        available, _ui, _proof = scanner_pools_for_index(
            candles, tf, i, cfg, cache=lld_cache
        )
        avail_ids = {str(r["pool_id"]) for r in available}
        row_map = {str(r["pool_id"]): r for r in available}
        end_ts = int(dc.timestamp())

        for pid in prev_avail - avail_ids:
            seg = active.pop(pid)
            closed.append(
                finalize_open(
                    seg,
                    end_ts,
                    _end_reason_for_removal(seg["last_row"], end_ts),
                )
            )

        for pid in avail_ids - prev_avail:
            row = row_map[pid]
            known = row["known"]
            start_ts = int(_ensure_utc(known).timestamp())
            seg_idx = segment_index_next.get(pid, 0)
            segment_index_next[pid] = seg_idx + 1
            active[pid] = {
                "pool_id": pid,
                "segment_index": seg_idx,
                "side": row["side"],
                "lower": float(row["bottom"]),
                "upper": float(row["top"]),
                "known": known,
                "break_at": row.get("break_at"),
                "start_ts": start_ts,
                "last_row": row,
            }

        for pid in avail_ids & prev_avail:
            row = row_map[pid]
            seg = active[pid]
            seg["last_row"] = row
            seg["lower"] = float(row["bottom"])
            seg["upper"] = float(row["top"])
            if row.get("break_at") is not None:
                seg["break_at"] = row.get("break_at")

        prev_avail = avail_ids

    for seg in list(active.values()):
        closed.append(finalize_open(seg, to_unix, "window_end_active"))

    zones = [_segment_to_zone(s, tf) for s in closed]
    pool_ids = sorted({z["pool_id"] for z in zones})
    payload: dict[str, Any] = {
        "layer": SCANNER_COMMON_TIMELINE_LAYER,
        "namespace": SCANNER_COMMON_TIMELINE_NAMESPACE,
        "symbol": sym,
        "timeframe": tf,
        "from_close": from_dt.isoformat(),
        "to_close": to_dt.isoformat(),
        "config_version": cfg_version,
        "pool_count": len(pool_ids),
        "segment_count": len(zones),
        "segments_hash": segments_hash(closed),
        "zones": zones,
    }
    if use_response_cache:
        store_cached_timeline(cache_key, payload)
    return payload


def timeline_parity_at(
    timeline_payload: dict[str, Any],
    symbol: str,
    decision_iso: str,
    *,
    timeframe: str = "15m",
) -> dict[str, Any]:
    """Compare timeline coverage at T with direct COMMON snapshot."""
    from bot.common_causal_pools_v1 import common_causal_pool_universe

    decision = parse_decision_iso(decision_iso)
    dc_unix = int(_ensure_utc(decision).timestamp())
    zones = timeline_payload.get("zones") or []
    from_close = parse_decision_iso(str(timeline_payload["from_close"]))
    to_close = parse_decision_iso(str(timeline_payload["to_close"]))
    if not (from_close <= decision <= to_close):
        raise ValueError("decision_iso outside timeline window")

    timeline_ids = active_pool_ids_at(zones, dc_unix)
    snap = common_causal_pool_universe(symbol, decision, timeframe=timeframe)
    snap_ids = {str(r["pool_id"]) for r in snap.available}
    return {
        "decision_close": decision.isoformat(),
        "timeline_count": len(timeline_ids),
        "snapshot_count": len(snap_ids),
        "ids_match": timeline_ids == snap_ids,
        "only_timeline": sorted(timeline_ids - snap_ids),
        "only_snapshot": sorted(snap_ids - timeline_ids),
        "rows_hash": snap.rows_hash,
    }

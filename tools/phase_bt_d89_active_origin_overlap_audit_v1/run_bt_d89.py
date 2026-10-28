#!/usr/bin/env python3
"""BT-D89: ACTIVE origin overlap forensics (read-only CH + July D8 cache)."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D8 = ROOT / "tools/phase_bt_d8_canonical_density_cache_v1"
D87_TOOL = ROOT / "tools/phase_bt_d87_weak_upper_pool_analysis_v1"
DASH = ROOT / "backtester/short/dashboard"
sys.path.insert(0, str(D8))
sys.path.insert(0, str(D87_TOOL))
sys.path.insert(0, str(DASH))

from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402
from run_bt_d87 import (  # noqa: E402
    BUNDLE,
    LEDGER_PATH,
    USER_FILL_SET,
    _active_uppers,
    _f,
    _origin_set,
    _parse_utc,
)
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402

OUT = ROOT / "results/phase_bt_d89_active_origin_overlap_audit_v1"
PIPELINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
CONFIG_HASH = "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9"
TABLE_PUBLISH = "research_lmv2_active_snapshot_publish_v1"
TABLE_CATALOG = "research_lmv2_snapshot_active_pool_v1"
JULY1 = datetime(2026, 7, 1, tzinfo=timezone.utc)
PUBLISH_MIN = datetime(2026, 5, 1, tzinfo=timezone.utc)
WARMUP_HOURS = 168
OVERLAP_MIN = 0.5


def _overlap_frac(ref_lo: float, ref_hi: float, lo: float, hi: float) -> float:
    inter = max(0.0, min(ref_hi, hi) - max(ref_lo, lo))
    ref_w = max(ref_hi - ref_lo, 1e-15)
    return inter / ref_w


def _match_band(
    uppers: list[dict[str, Any]],
    *,
    ref_key: str,
    ref_lo: float,
    ref_hi: float,
) -> tuple[dict[str, Any] | None, str, float]:
    for u in uppers:
        if u["pool_key"] == ref_key:
            return u, "exact_pool_key", 1.0
    best = None
    best_ov = 0.0
    for u in uppers:
        ov = _overlap_frac(ref_lo, ref_hi, u["lower_f"], u["upper_f"])
        if ov > best_ov:
            best_ov = ov
            best = u
    if best is not None and best_ov >= OVERLAP_MIN:
        return best, "price_overlap", best_ov
    return None, "none", 0.0


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _floor_5m(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    minute = (dt.minute // 5) * 5
    return dt.replace(minute=minute, second=0, microsecond=0)


def _iter_closes_backward(end: datetime, start: datetime) -> list[str]:
    out: list[str] = []
    cur = _floor_5m(end)
    start = _floor_5m(start)
    while cur >= start:
        out.append(_iso(cur))
        cur -= timedelta(minutes=5)
    return out


def _segment_epoch_hours_before(signal: datetime, origin_id: str) -> float | None:
    try:
        ts = int(str(origin_id).rsplit(":", 1)[-1])
        seg = datetime.fromtimestamp(ts, tz=timezone.utc)
        return (signal - seg).total_seconds() / 3600.0
    except (ValueError, OSError):
        return None


def _epoch_suffix(pool_id: str) -> str:
    return str(pool_id).rsplit(":", 1)[-1]


def _origin_id_ch_candidates(origin_id: str) -> list[str]:
    """D8 density origin_ids omit pool_type; CH catalog uses e.g. ...:micro:upper:epoch."""
    oid = str(origin_id).strip()
    parts = oid.split(":")
    if len(parts) == 5 and parts[0] == "lldv2":
        return [oid, f"{parts[0]}:{parts[1]}:{parts[2]}:micro:{parts[3]}:{parts[4]}"]
    return [oid]


def _logical_pool_key(pool_id: str) -> str:
    return _epoch_suffix(pool_id)


class ChPublishIndex:
    def __init__(self) -> None:
        import clickhouse_connect

        cfg = load_clickhouse_config()
        self._db = cfg.database
        client = clickhouse_connect.get_client(**cfg.connect_kwargs())
        try:
            rows = client.query(
                f"""
                SELECT decision_close, snapshot_id
                FROM {self._db}.{TABLE_PUBLISH} FINAL
                WHERE pipeline_line_id = {{lid:String}}
                  AND config_hash = {{ch:String}}
                  AND publish_complete = 1
                  AND timeframe = '5m'
                  AND pool_detail = 1
                ORDER BY decision_close ASC
                """,
                parameters={"lid": PIPELINE, "ch": CONFIG_HASH},
            ).result_rows
        finally:
            client.close()
        self.close_to_sid: dict[str, str] = {}
        for dc, sid in rows:
            if dc.tzinfo is None:
                dc = dc.replace(tzinfo=timezone.utc)
            self.close_to_sid[_iso(dc)] = str(sid)
        self.first_close = min(self.close_to_sid) if self.close_to_sid else None
        self.last_close = max(self.close_to_sid) if self.close_to_sid else None
        self.count = len(self.close_to_sid)


@lru_cache(maxsize=12000)
def _catalog_rows(snapshot_id: str) -> tuple[tuple[str, str, float, float], ...]:
    import clickhouse_connect

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        rows = client.query(
            f"""
            SELECT pool_id, side, bottom_price, top_price, known_at
            FROM {cfg.database}.{TABLE_CATALOG}
            WHERE snapshot_id = {{sid:String}}
            """,
            parameters={"sid": snapshot_id},
        ).result_rows
    finally:
        client.close()
    out: list[tuple[str, str, float, float]] = []
    for pool_id, side, bot, top, known in rows:
        out.append((str(pool_id), str(side).lower(), float(bot), float(top)))
    return tuple(out)


def _known_at_for_pool_ids(snapshot_id: str, origin_ids: set[str], signal: datetime) -> dict[str, str]:
    import clickhouse_connect

    if not origin_ids:
        return {}
    candidates: list[str] = []
    for oid in origin_ids:
        candidates.extend(_origin_id_ch_candidates(oid))
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        rows = client.query(
            f"""
            SELECT pool_id, known_at
            FROM {cfg.database}.{TABLE_CATALOG}
            WHERE snapshot_id = {{sid:String}}
              AND pool_id IN {{pids:Array(String)}}
            """,
            parameters={"sid": snapshot_id, "pids": sorted(set(candidates))},
        ).result_rows
    finally:
        client.close()
    out: dict[str, str] = {}
    for pid, known in rows:
        if known is None:
            continue
        if known.tzinfo is None:
            known = known.replace(tzinfo=timezone.utc)
        if known > signal:
            continue
        out[_epoch_suffix(str(pid))] = _iso(known)
    return out


def _region_upper_logical_origins_ch(
    snapshot_id: str, ref_lo: float, ref_hi: float
) -> set[str]:
    logical: set[str] = set()
    for pool_id, side, bot, top in _catalog_rows(snapshot_id):
        if side != "upper":
            continue
        inter = max(0.0, min(ref_hi, top) - max(ref_lo, bot))
        if inter <= 0:
            continue
        logical.add(_logical_pool_key(pool_id))
    return logical


@lru_cache(maxsize=9500)
def _d8_origin_set_at_close(
    close: str, ref_key: str, ref_lo: float, ref_hi: float
) -> tuple[int, str, str]:
    snap = _D8.collect_at_close(close)
    uppers = _active_uppers(snap)
    matched, method, _ = _match_band(uppers, ref_key=ref_key, ref_lo=ref_lo, ref_hi=ref_hi)
    if not matched:
        return 0, "none", ""
    oids = str(matched.get("origin_ids") or "")
    return len(_origin_set(oids)), method, oids


def analyze_trade(
    trade: dict[str, Any],
    *,
    user_selected: bool,
    ch: ChPublishIndex,
) -> dict[str, Any]:
    signal = str(trade["signal_close_utc"])
    signal_dt = _parse_utc(signal)
    window_start = max(PUBLISH_MIN, signal_dt - timedelta(hours=WARMUP_HOURS))
    ref_key = str(trade["pool_key"])
    ref_lo, ref_hi = _f(ref_key.split("|")[0]), _f(ref_key.split("|")[1])

    n_orig, match_method, origin_ids = _d8_origin_set_at_close(signal, ref_key, ref_lo, ref_hi)
    band_origins = _origin_set(origin_ids)

    sid_signal = ch.close_to_sid.get(signal)
    known_at_map: dict[str, str] = {}
    if sid_signal and band_origins:
        known_at_map = _known_at_for_pool_ids(sid_signal, band_origins, signal_dt)

    known_ages_h: list[float] = []
    for oid in band_origins:
        ka = known_at_map.get(_epoch_suffix(oid))
        if ka:
            known_ages_h.append((signal_dt - _parse_utc(ka)).total_seconds() / 3600.0)
    seg_ages_h = [a for a in (_segment_epoch_hours_before(signal_dt, o) for o in band_origins) if a is not None]

    closes = _iter_closes_backward(signal_dt, window_start)
    bars_depth2_ge2 = 0
    bars_tracked = 0
    bars_pre_july = 0
    bars_july_d8 = 0
    origin_change_bars = 0
    prev_origins: set[str] | None = None
    prev_logical_region: set[str] | None = None
    region_change_bars = 0

    for close in reversed(closes):  # chronological
        close_dt = _parse_utc(close)
        in_july = close_dt >= JULY1
        if in_july and close in ch.close_to_sid:
            n, _, oids = _d8_origin_set_at_close(close, ref_key, ref_lo, ref_hi)
            if n >= 2:
                bars_depth2_ge2 += 1
            if n > 0:
                bars_tracked += 1
                cur = _origin_set(oids)
                if prev_origins is not None and cur != prev_origins:
                    origin_change_bars += 1
                prev_origins = cur
            bars_july_d8 += 1
        else:
            bars_pre_july += 1
            sid = ch.close_to_sid.get(close)
            if not sid:
                continue
            region = _region_upper_logical_origins_ch(sid, ref_lo, ref_hi)
            if len(region) >= 2:
                bars_depth2_ge2 += 1
            if region:
                bars_tracked += 1
                if prev_logical_region is not None and region != prev_logical_region:
                    region_change_bars += 1
                prev_logical_region = region

    ws = _iso(_floor_5m(window_start))
    sid_ws = ch.close_to_sid.get(ws)
    origins_at_window_start: set[str] = set()
    window_start_source = "missing_publish"
    if sid_ws:
        if _parse_utc(ws) >= JULY1:
            _, _, ows = _d8_origin_set_at_close(ws, ref_key, ref_lo, ref_hi)
            origins_at_window_start = {_epoch_suffix(o) for o in _origin_set(ows)}
            window_start_source = "d8_depth2_band"
        else:
            origins_at_window_start = _region_upper_logical_origins_ch(sid_ws, ref_lo, ref_hi)
            window_start_source = "ch_upper_region_proxy"

    inter = len({_epoch_suffix(o) for o in band_origins} & origins_at_window_start)
    union = len({_epoch_suffix(o) for o in band_origins} | origins_at_window_start)
    jaccard_ws = inter / union if union else None

    cluster_neighbors = 0
    if sid_signal:
        for pool_id, side, bot, top in _catalog_rows(sid_signal):
            if side != "upper":
                continue
            if _epoch_suffix(pool_id) in {_epoch_suffix(o) for o in band_origins}:
                continue
            center = (bot + top) / 2.0
            entry = float(trade["entry_price"])
            if abs(center - entry) / entry <= 0.0005:
                cluster_neighbors += 1

    hours_known_min = min(known_ages_h) if known_ages_h else None
    hours_seg_min = min(seg_ages_h) if seg_ages_h else None
    persistent = (
        bars_tracked > 0
        and bars_depth2_ge2 / max(bars_tracked, 1) >= 0.5
        and (hours_known_min or 0) >= 24
    )
    fresh_unstable = (
        (hours_seg_min is not None and hours_seg_min < 1.0 and bars_depth2_ge2 <= 3)
        or (bars_tracked <= 3 and bars_depth2_ge2 <= 3)
    )

    return {
        "user_selected_weak": user_selected,
        "fill_time_utc": trade["fill_time_utc"],
        "signal_close_utc": signal,
        "exit_reason": trade.get("exit_reason"),
        "pool_key_at_signal": ref_key,
        "origin_ids_at_signal": origin_ids,
        "match_confidence_at_signal": match_method,
        "contributing_origin_count_at_signal": n_orig,
        "window_start_utc": ws,
        "window_start_origin_source": window_start_source,
        "window_bars_total": len(closes),
        "window_bars_pre_july": bars_pre_july,
        "window_bars_july_d8_exact": bars_july_d8,
        "bars_with_depth2_ge2_in_region": bars_depth2_ge2,
        "bars_with_any_overlap_track": bars_tracked,
        "fraction_window_bars_depth2_ge2": round(bars_depth2_ge2 / len(closes), 6) if closes else None,
        "origin_membership_change_bars_d8": origin_change_bars,
        "region_logical_change_bars_ch_pre_july": region_change_bars,
        "origin_jaccard_signal_vs_window_start": round(jaccard_ws, 4) if jaccard_ws is not None else None,
        "ch_known_at_verified_count": len(known_at_map),
        "ch_known_at_min_hours_before_signal": round(hours_known_min, 4) if hours_known_min is not None else None,
        "ch_known_at_max_hours_before_signal": round(max(known_ages_h), 4) if known_ages_h else None,
        "segment_epoch_min_hours_before_signal": round(hours_seg_min, 4) if hours_seg_min is not None else None,
        "segment_vs_known_at_delta_hours_min": (
            round(hours_known_min - hours_seg_min, 4)
            if hours_known_min is not None and hours_seg_min is not None
            else None
        ),
        "adjacent_upper_neighbors_50bp_at_signal": cluster_neighbors,
        "label_persistent_overlap_heuristic": persistent,
        "label_fresh_unstable_overlap_heuristic": fresh_unstable,
        "coverage_note": (
            "July closes: exact depth2 band origin_ids from D8 cache. "
            "Pre-July closes: CH ACTIVE upper pools overlapping price region "
            "(logical pool_id count >=2 proxy, not exact depth2 clustering)."
        ),
    }


def _summ(vals: list[Any]) -> dict[str, Any]:
    xs = [float(v) for v in vals if v is not None]
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "min": min(xs),
        "max": max(xs),
        "mean": round(statistics.mean(xs), 6),
        "median": round(statistics.median(xs), 6),
    }


def main() -> int:
    global _D8
    OUT.mkdir(parents=True, exist_ok=True)
    ch = ChPublishIndex()
    if ch.count < 1000:
        print("BLOCKED: insufficient CH publish rows", ch.count)
        return 2

    provenance = {
        "clickhouse_database_tables": {
            "publish": TABLE_PUBLISH,
            "active_pool_catalog": TABLE_CATALOG,
        },
        "pipeline_line_id": PIPELINE,
        "config_hash": CONFIG_HASH,
        "timeframe": "5m",
        "pool_detail": 1,
        "publish_row_count": ch.count,
        "publish_first_close": ch.first_close,
        "publish_last_close": ch.last_close,
        "density_depth2_exact": "results/phase_bt_d85_july_2026_no_pause_research_v1/cache_bundle/",
        "concepts_distinguished": {
            "ch_known_at": "research_lmv2_snapshot_active_pool_v1.known_at at published snapshot",
            "segment_epoch_in_origin_id": "5m candle-open epoch suffix in lldv2 pool_id string",
        "d8_origin_ids": "exact unique-origin depth2 band membership at decision close",
        "d8_vs_ch_pool_id": "D8 origin_id strings omit :micro: pool_type segment present in CH catalog pool_id",
        "ch_pre_july_region": "ACTIVE upper catalog overlap with entry band prices (epoch-key proxy)",
        },
    }
    (OUT / "source_provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")

    _D8 = ValidatedDensityCacheSessionV1(BUNDLE)
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    rows = [
        analyze_trade(t, user_selected=t["fill_time_utc"] in USER_FILL_SET, ch=ch)
        for t in ledger
    ]

    selected = [r for r in rows if r["user_selected_weak"]]
    comparison = [r for r in rows if not r["user_selected_weak"]]

    keys = [
        "bars_with_depth2_ge2_in_region",
        "fraction_window_bars_depth2_ge2",
        "bars_with_any_overlap_track",
        "ch_known_at_min_hours_before_signal",
        "segment_epoch_min_hours_before_signal",
        "origin_jaccard_signal_vs_window_start",
        "origin_membership_change_bars_d8",
        "window_bars_pre_july",
        "adjacent_upper_neighbors_50bp_at_signal",
    ]
    summary = {
        "baseline": "BT-D88 8b80eed3",
        "selected_n": len(selected),
        "comparison_n": len(comparison),
        "persistent_heuristic_selected": sum(1 for r in selected if r["label_persistent_overlap_heuristic"]),
        "persistent_heuristic_comparison": sum(1 for r in comparison if r["label_persistent_overlap_heuristic"]),
        "fresh_heuristic_selected": sum(1 for r in selected if r["label_fresh_unstable_overlap_heuristic"]),
        "fresh_heuristic_comparison": sum(1 for r in comparison if r["label_fresh_unstable_overlap_heuristic"]),
        "metrics_selected": {k: _summ([r.get(k) for r in selected]) for k in keys},
        "metrics_comparison": {k: _summ([r.get(k) for r in comparison]) for k in keys},
    }
    (OUT / "group_comparison.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    fields = list(rows[0].keys())
    with (OUT / "per_trade_origin_overlap.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"Wrote {OUT} ch_publish={ch.count} d8_misses={_d8_origin_set_at_close.cache_info().misses}")
    return 0


if __name__ == "__main__":
    _D8: ValidatedDensityCacheSessionV1
    raise SystemExit(main())

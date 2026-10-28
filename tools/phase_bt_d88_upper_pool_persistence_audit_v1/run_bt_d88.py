#!/usr/bin/env python3
"""BT-D88: Past-only upper pool persistence & origin evolution (read-only)."""

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
sys.path.insert(0, str(D8))
sys.path.insert(0, str(D87_TOOL))

from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402

# Reuse BT-D87 cohort definitions
from run_bt_d87 import (  # noqa: E402
    BT85,
    BUNDLE,
    LEDGER_PATH,
    USER_FILL_SET,
    _active_uppers,
    _f,
    _origin_set,
    _parse_utc,
    _pool_key,
)

OUT = ROOT / "results/phase_bt_d88_upper_pool_persistence_audit_v1"
WARMUP_HOURS = 168
BUNDLE_START = "2026-07-01T00:00:00Z"
OVERLAP_TRACK_MIN = 0.5
SUDDEN_GAP_BARS = 12  # descriptive: 1h without tracked band before signal
SUDDEN_NEAR_BARS = 3


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iter_5m_closes_backward(end: datetime, start: datetime) -> list[str]:
    out: list[str] = []
    cur = end
    while cur >= start:
        out.append(_iso(cur))
        cur -= timedelta(minutes=5)
    return out


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
    if best is not None and best_ov >= OVERLAP_TRACK_MIN:
        return best, "price_overlap", best_ov
    return None, "none", 0.0


def _origin_segment_utc(origin_id: str) -> str | None:
    try:
        ts = int(str(origin_id).rsplit(":", 1)[-1])
        return _iso(datetime.fromtimestamp(ts, tz=timezone.utc))
    except (ValueError, OSError):
        return None


def _origin_ages_hours_at_signal(signal_dt: datetime, origin_ids: str) -> list[float]:
    ages: list[float] = []
    for oid in _origin_set(origin_ids):
        seg = _origin_segment_utc(oid)
        if not seg:
            continue
        seg_dt = _parse_utc(seg)
        ages.append((signal_dt - seg_dt).total_seconds() / 3600.0)
    return ages


@lru_cache(maxsize=9500)
def _upper_band_summaries(decision_close: str) -> tuple[tuple[str, float, float, int, str], ...]:
    snap = _SESSION.collect_at_close(decision_close)
    rows = []
    for u in _active_uppers(snap):
        rows.append(
            (
                u["pool_key"],
                u["lower_f"],
                u["upper_f"],
                int(u["unique_origin_count"]),
                str(u["origin_ids"]),
            )
        )
    return tuple(rows)


def _uppers_from_summary(summary: tuple[tuple[str, float, float, int, str], ...]) -> list[dict[str, Any]]:
    out = []
    for pk, lo, hi, n, oids in summary:
        out.append(
            {
                "pool_key": pk,
                "lower_f": lo,
                "upper_f": hi,
                "width_f": hi - lo,
                "center_f": (lo + hi) / 2.0,
                "unique_origin_count": n,
                "origin_ids": oids,
            }
        )
    return out


def _nearby_upper_count(uppers: list[dict[str, Any]], entry: float, bp: float = 0.0005) -> int:
    return sum(1 for u in uppers if abs(u["center_f"] - entry) / entry <= bp)


def analyze_trade_persistence(trade: dict[str, Any], *, user_selected: bool) -> dict[str, Any]:
    signal = str(trade["signal_close_utc"])
    fill = str(trade["fill_time_utc"])
    signal_dt = _parse_utc(signal)
    bundle_start_dt = _parse_utc(BUNDLE_START)
    window_start_dt = max(bundle_start_dt, signal_dt - timedelta(hours=WARMUP_HOURS))
    window_limited_by = "168h_warmup"
    if window_start_dt <= bundle_start_dt:
        window_limited_by = "bundle_start_july_1"

    ref_snap = _upper_band_summaries(signal)
    ref_uppers = _uppers_from_summary(ref_snap)
    ref_key = str(trade["pool_key"])
    parts = ref_key.split("|")
    ref_lo, ref_hi = _f(parts[0]), _f(parts[1])
    ref_match, ref_method, _ = _match_band(ref_uppers, ref_key=ref_key, ref_lo=ref_lo, ref_hi=ref_hi)
    entry = float(trade["entry_price"])

    closes = _iter_5m_closes_backward(signal_dt, window_start_dt)
    track: list[dict[str, Any]] = []

    for close in closes:
        uppers = _uppers_from_summary(_upper_band_summaries(close))
        matched, method, ov = _match_band(uppers, ref_key=ref_key, ref_lo=ref_lo, ref_hi=ref_hi)
        track.append(
            {
                "close": close,
                "matched": matched is not None,
                "method": method,
                "overlap": ov,
                "pool_key": matched["pool_key"] if matched else None,
                "origins": matched["origin_ids"] if matched else None,
                "n_origins": matched["unique_origin_count"] if matched else None,
                "nearby_50bp": _nearby_upper_count(uppers, entry),
            }
        )

    matched_bars = [t for t in track if t["matched"]]
    exact_bars = [t for t in track if t["pool_key"] == ref_key]

    first_track_close = matched_bars[-1]["close"] if matched_bars else None
    first_exact_close = exact_bars[-1]["close"] if exact_bars else None
    persistence_bars_tracked = len(matched_bars)
    persistence_bars_exact = len(exact_bars)

    boundary_changes = 0
    origin_churn_events = 0
    prev_key = None
    prev_origins: set[str] | None = None
    for t in reversed(matched_bars):  # chronological
        if prev_key is not None and t["pool_key"] != prev_key:
            boundary_changes += 1
        cur_o = _origin_set(t["origins"] or "")
        if prev_origins is not None and cur_o != prev_origins:
            origin_churn_events += 1
        prev_key = t["pool_key"]
        prev_origins = cur_o

    bars_before_signal = len(track) - 1
    gap_before_match = 0
    for t in track[1:]:  # exclude signal bar, walk backward
        if t["matched"]:
            break
        gap_before_match += 1
    sudden_appearance = gap_before_match >= SUDDEN_GAP_BARS and persistence_bars_tracked <= SUDDEN_NEAR_BARS

    ages_h = _origin_ages_hours_at_signal(signal_dt, ref_match["origin_ids"] if ref_match else "")
    first_origins = _origin_set(matched_bars[-1]["origins"]) if matched_bars else set()
    signal_origins = _origin_set(ref_match["origin_ids"]) if ref_match else set()
    jaccard_first_to_signal = None
    if first_origins or signal_origins:
        inter = len(first_origins & signal_origins)
        union = len(first_origins | signal_origins)
        jaccard_first_to_signal = inter / union if union else None

    qualifying_bars = sum(1 for t in matched_bars if (t["n_origins"] or 0) >= 2)
    nearby_mean = (
        statistics.mean(t["nearby_50bp"] for t in matched_bars) if matched_bars else None
    )

    return {
        "user_selected_weak": user_selected,
        "fill_time_utc": fill,
        "signal_close_utc": signal,
        "exit_reason": trade.get("exit_reason"),
        "entry_price": entry,
        "pool_key_at_signal": ref_key,
        "origin_ids_at_signal": ref_match["origin_ids"] if ref_match else None,
        "match_confidence_at_signal": ref_method,
        "window_start_utc": _iso(window_start_dt),
        "window_limit_note": window_limited_by,
        "history_bars_in_window": len(closes),
        "persistence_bars_price_tracked": persistence_bars_tracked,
        "persistence_bars_exact_pool_key": persistence_bars_exact,
        "first_tracked_close_in_window": first_track_close,
        "first_exact_pool_key_close_in_window": first_exact_close,
        "hours_signal_minus_first_tracked": (
            (signal_dt - _parse_utc(first_track_close)).total_seconds() / 3600.0
            if first_track_close
            else None
        ),
        "boundary_pool_key_changes_while_tracked": boundary_changes,
        "origin_membership_change_bars": origin_churn_events,
        "sudden_appearance_heuristic": sudden_appearance,
        "gap_unmatched_bars_immediately_before_signal": gap_before_match,
        "qualifying_depth2_bars_in_track": qualifying_bars,
        "min_origin_age_hours_at_signal": min(ages_h) if ages_h else None,
        "max_origin_age_hours_at_signal": max(ages_h) if ages_h else None,
        "mean_origin_age_hours_at_signal": round(statistics.mean(ages_h), 4) if ages_h else None,
        "origin_jaccard_first_tracked_vs_signal": jaccard_first_to_signal,
        "nearby_upper_50bp_mean_while_tracked": (
            round(nearby_mean, 4) if nearby_mean is not None else None
        ),
        "nearby_upper_50bp_at_signal": track[0]["nearby_50bp"] if track else None,
        "bars_before_signal_in_window": bars_before_signal,
    }


def _summ(vals: list[float | int | None]) -> dict[str, Any]:
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
    global _SESSION
    OUT.mkdir(parents=True, exist_ok=True)
    _SESSION = ValidatedDensityCacheSessionV1(BUNDLE)
    manifest = _SESSION.manifest
    provenance = {
        "bundle": str(BUNDLE.relative_to(ROOT)),
        "entry_count": manifest.get("entry_count"),
        "density_method": manifest.get("density_method"),
        "pool_detail": manifest.get("pool_detail"),
        "min_unique_origins": manifest.get("min_unique_origins"),
        "pipeline_line_id": manifest.get("required_pipeline_line_id"),
        "decision_close_first": manifest.get("decision_close_first"),
        "decision_close_last": manifest.get("decision_close_last"),
        "clickhouse_used": False,
        "clickhouse_note": (
            "Origin segment start times parsed from lldv2 origin_ids epoch suffix; "
            "band persistence from July D8 cache snapshots only (read-only)."
        ),
        "pre_july_1_blind_spot": (
            "Bundle covers 2026-07-01..2026-07-31 only; persistence before July 1 "
            "is not observable in this audit."
        ),
    }
    (OUT / "source_provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")

    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for trade in ledger:
        user = trade["fill_time_utc"] in USER_FILL_SET
        rows.append(analyze_trade_persistence(trade, user_selected=user))

    selected = [r for r in rows if r["user_selected_weak"]]
    comparison = [r for r in rows if not r["user_selected_weak"]]

    keys = [
        "persistence_bars_price_tracked",
        "persistence_bars_exact_pool_key",
        "hours_signal_minus_first_tracked",
        "boundary_pool_key_changes_while_tracked",
        "origin_membership_change_bars",
        "mean_origin_age_hours_at_signal",
        "min_origin_age_hours_at_signal",
        "qualifying_depth2_bars_in_track",
        "nearby_upper_50bp_mean_while_tracked",
        "gap_unmatched_bars_immediately_before_signal",
    ]
    summary = {
        "baseline": "BT-D87 031ea67e / BT-D85 434fbbd8",
        "matching_methodology": {
            "reference_band": "ledger pool_key at signal_close_utc",
            "historical_track": f"ACTIVE upper with exact pool_key OR >={OVERLAP_TRACK_MIN:.0%} price overlap with reference",
            "identity": "pool_key may change on merge/split; price_tracked chain allows key changes",
            "past_only": "only closes <= signal_close_utc; window max(168h, bundle_start)",
        },
        "metrics_selected": {k: _summ([r.get(k) for r in selected]) for k in keys},
        "metrics_comparison": {k: _summ([r.get(k) for r in comparison]) for k in keys},
        "sudden_appearance_selected": sum(1 for r in selected if r["sudden_appearance_heuristic"]),
        "sudden_appearance_comparison": sum(1 for r in comparison if r["sudden_appearance_heuristic"]),
        "unavailable": [
            "full_lifetime_builder_timeline_pre_bake",
            "notional_at_origin",
            "exact_merge_split_event_log_ssot",
            "chart_render_persistence",
        ],
    }
    (OUT / "comparison_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    fields = list(rows[0].keys())
    with (OUT / "per_trade_persistence.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    info = _upper_band_summaries.cache_info()
    print(
        f"Wrote {OUT} trades={len(rows)} "
        f"snapshot_loads={info.misses} cache_hits={info.hits} unique_closes_cached={info.currsize}"
    )
    return 0


if __name__ == "__main__":
    _SESSION: ValidatedDensityCacheSessionV1
    raise SystemExit(main())

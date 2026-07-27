"""Full-history orderbook analysis — Phase 0/1–5 (segments, replay, market, walls, patterns).

Read-only ClickHouse. Phase 5 produces descriptive pattern candidates only
(no trading signals, no forward outcomes).
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import orjson

from orderbook_analyse.dynamic_wall_detector import (
    PROJECT_ROOT,
    ReadOnlyClickHouse,
    _ensure_aware,
    connect_readonly,
    parse_utc,
    utc_now,
    write_csv,
)
from decimal import Decimal

from orderbook_analyse.market_bars import (
    LIQUIDATION_BAR_HEADERS,
    LIQUIDATION_EVENT_HEADERS,
    OI_HEADERS,
    PRICE_BAR_HEADERS,
    PRICE_SUMMARY_HEADERS,
    TIMELINE_HEADERS,
    TRADEFLOW_HEADERS,
    check_market_context_integrity,
    decide_combined_analysis,
    decide_phase3_market,
    parse_bar_timeframes,
    phase3_output_files,
    quadrant_counts,
    run_market_context,
)
from orderbook_analyse.wall_history import (
    TIMELINE_WALL_EXTRA_HEADERS,
    WALL_CANDIDATE_HEADERS,
    WALL_CLUSTER_HEADERS,
    WALL_ERROR_HEADERS,
    WALL_OBSERVATION_HEADERS,
    WALL_SEGMENT_SUMMARY_HEADERS,
    WALL_SEQUENCE_HEADERS,
    WALL_TRANSITION_HEADERS,
    WallHistoryParams,
    check_wall_history_integrity,
    decide_full_analysis,
    decide_phase4_wall,
    parse_wall_resolutions,
    phase4_output_files,
    run_wall_history,
)

from orderbook_analyse.pattern_candidates import (
    CANDIDATE_HEADERS,
    FEATURE_HEADERS,
    INTEGRITY_ERROR_HEADERS,
    PATTERN_TIMELINE_EXTRA_HEADERS,
    SUMMARY_SEGMENT_HEADERS,
    SUMMARY_SYMBOL_HEADERS,
    SUMMARY_TYPE_HEADERS,
    TRANSITION_CONTEXT_HEADERS,
    PatternCandidateError,
    PatternParams,
    check_pattern_integrity,
    decide_phase5_patterns,
    phase5_output_files,
    run_pattern_candidates,
    validate_pattern_params,
)
from orderbook_analyse.replay_segmentation import (
    SegmentationResult,
    discover_replay_segments,
    load_orderbook_messages,
    segmentation_integrity_checks,
)
from orderbook_analyse.segment_replay import (
    decide_phase2,
    replay_all_segments,
)

logger = logging.getLogger(__name__)

OUTPUT_FILES = (
    "REPORT.md",
    "summary.json",
    "integrity.json",
    "data_inventory.csv",
    "data_quality.csv",
    "snapshot_inventory.csv",
    "replay_segments.csv",
    "replay_gaps.csv",
    "config.json",
)

PHASE2_OUTPUT_FILES = (
    "segment_replay_results.csv",
    "segment_book_end_states.csv",
    "segment_replay_errors.csv",
    "segment_replay_samples.csv",
)

TABLE_SPECS: tuple[tuple[str, str], ...] = (
    ("orderbook_deltas", "exchange_ts"),
    ("public_trades", "trade_ts"),
    ("ticker_samples", "exchange_ts"),
    ("liquidations", "liquidation_ts"),
    ("recorder_health", "event_ts"),
)


@dataclass
class FullHistoryParams:
    symbol: str
    start: datetime | None = None
    end: datetime | None = None
    output_dir: Path | None = None
    segment_minutes_min: float = 5.0
    min_snapshot_levels_per_side: int = 150
    run_segment_replay: bool = False
    run_market_context: bool = False
    run_wall_history: bool = False
    run_pattern_candidates: bool = False
    warmup_seconds: int = 300
    replay_sample_interval: int = 60
    bar_timeframes: str = "1m,5m"
    tiny_liquidation_notional: float = 1.0
    max_bar_range_pct: float = 20.0
    max_oi_open_close_ratio: float = 100.0
    wall_sample_interval: int = 60
    wall_warmup_seconds: int = 300
    wall_resolutions: str = "5,10,20,50"
    wall_distance_max_pct: float = 5.0
    wall_multiple_min: float = 3.0
    wall_percentile_min: float = 90.0
    wall_depth_share_min: float = 0.01
    wall_local_radius: int = 5
    wall_cluster_max_gap_buckets: int = 1
    wall_match_distance_bps: float = 10.0
    wall_test_distance_bps: float = 5.0
    wall_break_distance_bps: float = 5.0
    wall_min_age_seconds: float = 60.0
    wall_notional_change_threshold_pct: float = 20.0
    wall_output_mode: str = "candidates"
    pattern_timeframe: str = "1m"
    pattern_lookback_bars: int = 5
    pattern_min_wall_age_sec: float = 120.0
    pattern_min_wall_samples: int = 2
    pattern_near_distance_bps: float = 100.0
    pattern_strong_wall_multiple: float = 3.0
    pattern_dominant_depth_share: float = 0.05
    pattern_delta_ratio_threshold: float = 0.20
    pattern_oi_change_threshold_pct: float = 0.10
    pattern_price_change_threshold_pct: float = 0.05
    pattern_wall_growth_threshold_pct: float = 20.0
    pattern_wall_imbalance_threshold: float = 0.5
    pattern_cooldown_bars: int = 3
    pattern_output_mode: str = "all"
    log_level: str = "INFO"


def write_csv_headered(
    path: Path, rows: Sequence[Mapping[str, Any]], headers: Sequence[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(headers), extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in headers})


def discover_symbol_time_range(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
) -> dict[str, Any]:
    """Min/max timestamps per table and global analysis bounds."""
    tables: dict[str, dict[str, Any]] = {}
    for table, ts_col in TABLE_SPECS:
        rows = db.query(
            f"""
            SELECT
                min({ts_col}) AS first_ts,
                max({ts_col}) AS last_ts,
                count() AS row_count
            FROM {table}
            WHERE symbol = %(symbol)s
            """,
            parameters={"symbol": symbol},
        ).result_rows
        first_ts = rows[0][0] if rows else None
        last_ts = rows[0][1] if rows else None
        count = int(rows[0][2] or 0) if rows else 0
        if first_ts is not None:
            first_ts = _ensure_aware(first_ts)
        if last_ts is not None:
            last_ts = _ensure_aware(last_ts)
        tables[table] = {
            "timestamp_column": ts_col,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "row_count": count,
        }

    starts = [t["first_ts"] for t in tables.values() if t["first_ts"] is not None]
    ends = [t["last_ts"] for t in tables.values() if t["last_ts"] is not None]
    analysis_start = min(starts) if starts else None
    analysis_end = max(ends) if ends else None
    ob = tables["orderbook_deltas"]
    return {
        "symbol": symbol,
        "tables": tables,
        "analysis_start": analysis_start,
        "analysis_end": analysis_end,
        "orderbook_start": ob["first_ts"],
        "orderbook_end": ob["last_ts"],
        "has_any_data": bool(starts),
        "has_orderbook": int(ob["row_count"] or 0) > 0,
    }


def clip_range(
    available_start: datetime | None,
    available_end: datetime | None,
    *,
    start: datetime | None,
    end: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """Intersect requested window with available data."""
    if available_start is None or available_end is None:
        return None, None
    lo = available_start
    hi = available_end
    if start is not None:
        lo = max(lo, _ensure_aware(start))
    if end is not None:
        hi = min(hi, _ensure_aware(end))
    if lo > hi:
        return None, None
    return lo, hi


def build_data_inventory(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    range_info: Mapping[str, Any],
    analysis_start: datetime | None,
    analysis_end: datetime | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    tables = range_info["tables"]

    for table, ts_col in TABLE_SPECS:
        info = tables[table]
        base = {
            "symbol": symbol,
            "table_name": table,
            "timestamp_column": ts_col,
            "first_ts": None if info["first_ts"] is None else info["first_ts"].isoformat(),
            "last_ts": None if info["last_ts"] is None else info["last_ts"].isoformat(),
            "row_count": info["row_count"],
            "distinct_message_count": None,
            "snapshot_message_count": None,
            "delta_message_count": None,
            "notes": "",
        }
        if table != "orderbook_deltas" or analysis_start is None or analysis_end is None:
            if table == "orderbook_deltas":
                base["notes"] = "no orderbook window"
            rows.append(base)
            continue

        extra = db.query(
            """
            SELECT
                count() AS level_rows,
                uniqExact((exchange_ts, update_id, cross_sequence, message_type)) AS msg_count,
                uniqExactIf(
                    (exchange_ts, update_id, cross_sequence, message_type),
                    message_type = 'snapshot'
                ) AS snap_msgs,
                uniqExactIf(
                    (exchange_ts, update_id, cross_sequence, message_type),
                    message_type = 'delta'
                ) AS delta_msgs,
                uniqExact(update_id) AS uniq_update_ids,
                uniqExact(cross_sequence) AS uniq_cross_sequences
            FROM orderbook_deltas
            WHERE symbol = %(symbol)s
              AND exchange_ts >= %(start)s
              AND exchange_ts <= %(end)s
            """,
            parameters={
                "symbol": symbol,
                "start": analysis_start,
                "end": analysis_end,
            },
        ).result_rows[0]
        base["row_count"] = int(extra[0] or 0)
        base["distinct_message_count"] = int(extra[1] or 0)
        base["snapshot_message_count"] = int(extra[2] or 0)
        base["delta_message_count"] = int(extra[3] or 0)
        base["notes"] = (
            f"level_rows={int(extra[0] or 0)}; "
            f"uniq_update_ids={int(extra[4] or 0)}; "
            f"uniq_cross_sequences={int(extra[5] or 0)}; "
            f"window={analysis_start.isoformat()}..{analysis_end.isoformat()}"
        )
        rows.append(base)
    return rows


def load_recorder_health_summary(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    start: datetime | None,
    end: datetime | None,
) -> dict[str, int]:
    counts = {
        "STARTED": 0,
        "SUBSCRIBED": 0,
        "HEARTBEAT": 0,
        "RECONNECT": 0,
        "SEQUENCE_ERROR": 0,
        "INSERT_ERROR": 0,
        "STOPPED": 0,
    }
    if start is None or end is None:
        return counts
    rows = db.query(
        """
        SELECT event_type, count() AS c
        FROM recorder_health
        WHERE symbol = %(symbol)s
          AND event_ts >= %(start)s
          AND event_ts <= %(end)s
        GROUP BY event_type
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    ).result_rows
    for event_type, c in rows:
        key = str(event_type)
        if key in counts:
            counts[key] = int(c or 0)
        else:
            counts[key] = int(c or 0)
    return counts


def decide_phase01(
    *,
    has_any_data: bool,
    has_orderbook: bool,
    segment_count: int,
    replayable_count: int,
    gap_count: int,
    integrity_ok: bool,
) -> str:
    if not integrity_ok:
        return "FULL_HISTORY_ANALYSIS_FAILED"
    if not has_any_data:
        return "FULL_HISTORY_ANALYSIS_FAILED"
    if not has_orderbook:
        return "FULL_HISTORY_ANALYSIS_PARTIAL"
    if segment_count == 0 and replayable_count == 0:
        return "FULL_HISTORY_ANALYSIS_PARTIAL"
    if gap_count > 0 or replayable_count < segment_count:
        return "FULL_HISTORY_SEGMENT_DISCOVERY_COMPLETE_WITH_GAPS"
    return "FULL_HISTORY_SEGMENT_DISCOVERY_COMPLETE"


def build_data_quality(
    *,
    inventory: Sequence[Mapping[str, Any]],
    seg: SegmentationResult,
    health: Mapping[str, int],
    orderbook_start: datetime | None,
    orderbook_end: datetime | None,
) -> list[dict[str, Any]]:
    ob = next((r for r in inventory if r["table_name"] == "orderbook_deltas"), {})
    replayable = [s for s in seg.segments if s.is_replayable]
    discarded = [s for s in seg.segments if not s.is_replayable]
    replayable_dur = sum(s.duration_sec for s in replayable)
    if orderbook_start and orderbook_end:
        span = max((orderbook_end - orderbook_start).total_seconds(), 0.0)
    else:
        span = 0.0
    coverage = (replayable_dur / span * 100.0) if span > 0 else 0.0

    def row(metric: str, value: Any, status: str = "ok", details: str = "") -> dict[str, Any]:
        return {"metric": metric, "value": value, "status": status, "details": details}

    return [
        row("orderbook_rows", ob.get("row_count")),
        row("orderbook_message_count", ob.get("distinct_message_count")),
        row("complete_snapshots", seg.complete_snapshot_count),
        row("incomplete_snapshots", seg.incomplete_snapshot_count),
        row("update_id_gaps", seg.update_id_gap_events),
        row("backwards_sequences", seg.backwards_sequence_count),
        row("replayable_segments", len(replayable)),
        row("discarded_segments", len(discarded)),
        row("total_orderbook_duration_sec", span),
        row("replayable_duration_sec", replayable_dur),
        row(
            "coverage_pct",
            round(coverage, 6),
            details="sum(replayable segment duration) / orderbook time span",
        ),
        row("recorder_reconnect_count", health.get("RECONNECT", 0)),
        row("recorder_sequence_error_count", health.get("SEQUENCE_ERROR", 0)),
        row("recorder_insert_error_count", health.get("INSERT_ERROR", 0)),
        row("gap_rows", len(seg.gaps)),
        row("segment_rows", len(seg.segments)),
    ]


def render_report(
    *,
    decision: str,
    symbol: str,
    analysis_start: datetime | None,
    analysis_end: datetime | None,
    inventory: Sequence[Mapping[str, Any]],
    seg: SegmentationResult,
    quality: Sequence[Mapping[str, Any]],
    health: Mapping[str, int],
    coverage_pct: float,
    limitations: Sequence[str],
    replay_stats: Mapping[str, Any] | None = None,
    replay_results: Sequence[Mapping[str, Any]] | None = None,
    end_states: Sequence[Mapping[str, Any]] | None = None,
    warmup_seconds: int | None = None,
    market_stats: Mapping[str, Any] | None = None,
    market_coverage: Mapping[str, Any] | None = None,
    price_summary: Mapping[str, Any] | None = None,
    quadrant_summary: Mapping[str, int] | None = None,
    wall_stats: Mapping[str, Any] | None = None,
    wall_segment_summaries: Sequence[Mapping[str, Any]] | None = None,
    pattern_stats: Mapping[str, Any] | None = None,
) -> str:
    lines = [
        "# Full History Orderbook Analysis — Segment Discovery (Phase 0/1)",
        "",
        f"**Decision:** `{decision}`",
        "",
        f"1. Entscheidung: `{decision}`",
        f"2. Symbol: `{symbol}`",
        f"3. Analysefenster: `{None if analysis_start is None else analysis_start.isoformat()}` → "
        f"`{None if analysis_end is None else analysis_end.isoformat()}`",
        "",
        "## 4. Dateninventar",
        "",
    ]
    for row in inventory:
        lines.append(
            f"- `{row['table_name']}` ({row['timestamp_column']}): "
            f"rows={row['row_count']} first={row['first_ts']} last={row['last_ts']} "
            f"msgs={row.get('distinct_message_count')} "
            f"snap={row.get('snapshot_message_count')} delta={row.get('delta_message_count')}"
        )
    lines += [
        "",
        "## 5–6. Orderbook Messages / Snapshots",
        "",
        f"- Messages: siehe data_inventory",
        f"- Complete snapshots: {seg.complete_snapshot_count}",
        f"- Incomplete snapshots: {seg.incomplete_snapshot_count}",
        "",
        "## 7–8. Gaps und Sequenzanomalien",
        "",
        f"- Update-ID gap events: {seg.update_id_gap_events}",
        f"- Gap rows: {len(seg.gaps)}",
        f"- Backwards sequences: {seg.backwards_sequence_count}",
        "",
    ]
    for g in seg.gaps[:20]:
        lines.append(
            f"- {g.gap_id}: {g.reason} {g.previous_update_id}→{g.next_update_id} "
            f"missing={g.missing_update_count} recovered={g.recovered_at_snapshot_ts}"
        )
    if len(seg.gaps) > 20:
        lines.append(f"- … {len(seg.gaps) - 20} weitere Gaps in replay_gaps.csv")
    lines += [
        "",
        "## 9–10. Segmente",
        "",
        f"- Segmente gesamt: {len(seg.segments)}",
        f"- Replayable: {sum(1 for s in seg.segments if s.is_replayable)}",
        f"- Verworfen (insufficient_duration etc.): "
        f"{sum(1 for s in seg.segments if not s.is_replayable)}",
        "",
    ]
    for s in seg.segments[:30]:
        lines.append(
            f"- {s.segment_id}: replayable={s.is_replayable} "
            f"{s.segment_start_ts.isoformat()}→{s.segment_end_ts.isoformat()} "
            f"u={s.bootstrap_update_id}→{s.last_update_id} "
            f"dur={s.duration_sec:.1f}s end={s.end_reason}"
        )
    if len(seg.segments) > 30:
        lines.append(f"- … {len(seg.segments) - 30} weitere Segmente in replay_segments.csv")
    lines += [
        "",
        "## 11. Coverage",
        "",
        f"- Replaybare Abdeckung: **{coverage_pct:.4f}%**",
        "- Definition: `sum(replayable segment duration) / full orderbook time span`",
        "",
        "## 12. Recorder Health",
        "",
    ]
    for k, v in health.items():
        lines.append(f"- {k}: {v}")

    if replay_stats is not None:
        lines += [
            "",
            "## Phase 2 — Segment Replay Smoke",
            "",
            f"1. Replaybare Segmente: {replay_stats.get('segments_replayable')}",
            f"2. Tatsächlich abgespielt (ok+failed): {replay_stats.get('segments_replayed')}",
            f"3. Erfolgreich: {replay_stats.get('segments_replay_ok')}; "
            f"fehlgeschlagen: {replay_stats.get('segments_replay_failed')}",
            f"4. Messages geladen (gesamt): {replay_stats.get('messages_loaded_total')}; "
            f"Level-Zeilen: {replay_stats.get('level_rows_loaded_total')}",
            f"5. Replay-Laufzeit gesamt: {replay_stats.get('replay_runtime_sec_total')} s",
            f"6. Warm-up: {warmup_seconds}s — Feature-Emission erst danach "
            f"(`feature_emission_start_ts`); Replay selbst läuft ab Bootstrap.",
            f"7. Segmente ohne Post-Warm-up-Fenster: {replay_stats.get('segments_no_post_warmup')}",
            f"8. Invariants ok (alle erfolgreichen): {replay_stats.get('replay_invariants_ok')}",
            "",
            "### Replay results",
            "",
        ]
        for r in (replay_results or [])[:40]:
            lines.append(
                f"- {r.get('segment_id')}: {r.get('replay_status')} "
                f"u={r.get('actual_last_update_id')} "
                f"runtime={r.get('runtime_sec')}s "
                f"rows={r.get('events_or_level_rows_loaded')} "
                f"err={r.get('error_message')}"
            )
        lines += ["", "### Endzustände", ""]
        for e in (end_states or [])[:40]:
            lines.append(
                f"- {e.get('segment_id')}: bid={e.get('best_bid')} ask={e.get('best_ask')} "
                f"mid={e.get('mid_price')} spread_bps={e.get('spread_bps')} "
                f"levels={e.get('active_levels')}"
            )
        lines += [
            "",
            "### Speicher / Performance",
            "",
            "- Phase 2 lädt Segment-Events via bestehendes `load_events` in den Speicher.",
            "- Bei sehr großen Segmenten: Warnung ab 5M Level-Rows; Chunking erst Phase 2b.",
        ]

    if market_stats is not None:
        ps = price_summary or {}
        lines += [
            "",
            "## Phase 3 — Market Context Aggregation",
            "",
            f"- Status ok: {market_stats.get('market_context_ok')}",
            f"- Bar timeframes: {market_stats.get('bar_timeframes')}",
            f"- Preis: {ps.get('start_price')} → {ps.get('end_price')} "
            f"({ps.get('net_change_pct')}%) high={ps.get('high_price')} low={ps.get('low_price')}",
            f"- Spread avg/median bps: {ps.get('average_spread_bps')} / {ps.get('median_spread_bps')}",
            f"- 1m bar extremes: +{ps.get('largest_positive_1m_bar_pct')}% / "
            f"{ps.get('largest_negative_1m_bar_pct')}%",
            f"- 5m bar extremes: +{ps.get('largest_positive_5m_bar_pct')}% / "
            f"{ps.get('largest_negative_5m_bar_pct')}%",
            f"- Max drawdown / run-up: {ps.get('maximum_drawdown_pct')}% / "
            f"{ps.get('maximum_runup_pct')}%",
            f"- Trades notional: total={market_stats.get('trade_total_notional')} "
            f"buy={market_stats.get('trade_buy_notional')} sell={market_stats.get('trade_sell_notional')} "
            f"delta={market_stats.get('trade_delta_notional')}",
            f"- VWAP: {ps.get('vwap')}",
            f"- OI: {market_stats.get('oi_start')} → {market_stats.get('oi_end')} "
            f"({market_stats.get('oi_change_pct')}%)",
            f"- Liquidations: events={market_stats.get('liquidation_event_count')} "
            f"notional={market_stats.get('liquidation_total_notional')} "
            f"tiny={market_stats.get('tiny_liquidation_count')} "
            f"(threshold={market_stats.get('tiny_liquidation_notional')})",
            "",
            "### Datenabdeckung je Tabelle",
            "",
        ]
        for table in ("ticker_samples", "public_trades", "liquidations"):
            cov = (market_coverage or {}).get(table) or {}
            lines.append(
                f"- `{table}`: first={cov.get('first_ts')} last={cov.get('last_ts')} "
                f"rows={cov.get('row_count')}"
            )
        lines += [
            "",
            "### OI context quadrants (descriptive)",
            "",
        ]
        for q, c in sorted((quadrant_summary or {}).items()):
            lines.append(f"- {q}: {c}")
        lines += [
            "",
            "### Timeline outputs",
            "",
            f"- price_bars / tradeflow / oi / liquidation_bars / analysis_timeline "
            f"for {market_stats.get('bar_timeframes')}",
            "- Join: ticker-based LEFT JOIN (trades-only buckets excluded from timeline).",
            "- No walls, patterns, or long/short signals in Phase 3.",
            "- No forward liquidation price reaction (`price_change_1m_after` not computed).",
        ]

    if wall_stats is not None:
        lines += [
            "",
            "## Phase 4 — Wall History",
            "",
            f"- Status ok: {wall_stats.get('wall_history_ok')}",
            f"- Sample interval: {wall_stats.get('wall_sample_interval_sec')}s; "
            f"warmup: {wall_stats.get('wall_warmup_seconds')}s",
            f"- Preferred resolution: {wall_stats.get('preferred_resolution')}",
            f"- Segments ok/failed: {wall_stats.get('wall_segments_ok')}/"
            f"{wall_stats.get('wall_segments_failed')} "
            f"(replayable total {wall_stats.get('wall_segments_total')})",
            f"- Samples / observations / clusters / sequences / transitions: "
            f"{wall_stats.get('wall_samples_total')} / "
            f"{wall_stats.get('wall_observations_total')} / "
            f"{wall_stats.get('wall_clusters_total')} / "
            f"{wall_stats.get('wall_sequences_total')} / "
            f"{wall_stats.get('wall_transitions_total')}",
            f"- Bid/Ask sequences: {wall_stats.get('bid_wall_sequences')}/"
            f"{wall_stats.get('ask_wall_sequences')}",
            f"- Tested / broken / disappeared-before-test: "
            f"{wall_stats.get('tested_wall_sequences')} / "
            f"{wall_stats.get('broken_wall_sequences')} / "
            f"{wall_stats.get('disappeared_before_test_sequences')}",
            f"- Timeline-with-walls rows 1m/5m: "
            f"{wall_stats.get('timeline_with_walls_rows_1m')}/"
            f"{wall_stats.get('timeline_with_walls_rows_5m')}",
            f"- Price path source for test/break: {wall_stats.get('price_path_source')}",
            f"- Runtime total: {wall_stats.get('wall_history_runtime_sec_total')}s",
            "",
            "### Per-segment wall status",
            "",
        ]
        for s in (wall_segment_summaries or [])[:40]:
            lines.append(
                f"- {s.get('segment_id')}: {s.get('wall_history_status')} "
                f"samples={s.get('wall_sample_count')} "
                f"obs_bid/ask={s.get('bid_observation_count')}/{s.get('ask_observation_count')} "
                f"seq_bid/ask={s.get('bid_sequence_count')}/{s.get('ask_sequence_count')}"
            )
        lines += [
            "",
            "### Phase 4 limitations",
            "",
            "- No long/short signals, entries, MFE/MAE, or forward returns.",
            "- Cancel vs execution is unknown; only disappeared/reduced/increased/persisted/moved.",
            "- No spoofing claims. MERGED/SPLIT not implemented (MATCH_LOST/DISAPPEARED used).",
            "- Wall state never crosses gaps or segment boundaries.",
        ]

    lines += [
        "",
        "## 13. Einschränkungen",
        "",
    ]
    for lim in limitations:
        lines.append(f"- {lim}")
    lines += [
        "",
        "## 14. Nächster Implementierungsschritt",
        "",
        "- Phase 6 (optional): Forward-Outcome-/Evaluation-Layer getrennt von Pattern-Kandidaten "
        "(nur nach expliziter Freigabe; nicht Teil von Phase 5).",
        "",
        "## Data quality (compact)",
        "",
    ]
    for q in quality:
        lines.append(f"- {q['metric']}={q['value']} ({q['status']}) {q.get('details') or ''}")
    lines.append("")

    if pattern_stats is not None:
        tops = pattern_stats.get("top_pattern_types") or []
        top_txt = ", ".join(
            f"{t.get('pattern_type')}={t.get('count')}" for t in tops[:8]
        ) or "none"
        lines += [
            "",
            "## Phase 5 — Pattern Candidates (descriptive only)",
            "",
            f"- Status ok: {pattern_stats.get('pattern_candidates_ok')}",
            f"- Timeframe: {pattern_stats.get('pattern_timeframe')}; "
            f"lookback: {pattern_stats.get('pattern_lookback_bars')} bars",
            f"- Candidate count: {pattern_stats.get('pattern_candidate_count')}",
            f"- Pattern types / families: {pattern_stats.get('pattern_type_count')} / "
            f"{pattern_stats.get('pattern_family_count')}",
            f"- Segments with patterns: {pattern_stats.get('pattern_segments_count')}",
            f"- Data complete/incomplete: {pattern_stats.get('pattern_data_complete_count')} / "
            f"{pattern_stats.get('pattern_data_incomplete_count')}",
            f"- Wall lifecycle: {pattern_stats.get('pattern_wall_lifecycle_count')}",
            f"- Price/delta divergences: {pattern_stats.get('pattern_price_delta_count')}",
            f"- Price/OI: {pattern_stats.get('pattern_price_oi_count')}",
            f"- Wall+flow: {pattern_stats.get('pattern_wall_flow_count')}",
            f"- Liquidation context: {pattern_stats.get('pattern_liquidation_count')}",
            f"- Absorption candidates: {pattern_stats.get('pattern_absorption_candidate_count')}",
            f"- Wall-failure candidates: {pattern_stats.get('pattern_wall_failure_candidate_count')}",
            f"- Integrity errors: {pattern_stats.get('pattern_integrity_error_count')}",
            f"- Runtime: {pattern_stats.get('pattern_runtime_sec')}s",
            f"- Most frequent types: {top_txt}",
            "",
            "### Phase 5 limitations",
            "",
            "- No forward outcomes, MFE/MAE, hit-rate, or expected value.",
            "- No long/short signals, entries, exits, or strategy PnL.",
            "- No cancel-vs-execution or spoofing claims.",
            "- Absorption is not proven; candidates are descriptive only.",
            "- No cross-coin validation without operator smokes.",
            "- BTC Phase 4/5 may still be pending depending on operator runs.",
            "",
        ]

    return "\n".join(lines)


def default_output_dir(symbol: str) -> Path:
    day = utc_now().strftime("%Y%m%d")
    return PROJECT_ROOT / "results" / f"full_history_{symbol}_{day}"


def run_full_history_phase01(
    *,
    params: FullHistoryParams,
    db: ReadOnlyClickHouse | None = None,
) -> dict[str, Any]:
    own_db = db is None
    client = db or connect_readonly()
    try:
        range_info = discover_symbol_time_range(client, symbol=params.symbol)
        analysis_start, analysis_end = clip_range(
            range_info["analysis_start"],
            range_info["analysis_end"],
            start=params.start,
            end=params.end,
        )
        # Orderbook window: prefer clipped orderbook span within analysis window
        ob_start, ob_end = clip_range(
            range_info["orderbook_start"],
            range_info["orderbook_end"],
            start=params.start or analysis_start,
            end=params.end or analysis_end,
        )

        out_dir = params.output_dir or default_output_dir(params.symbol)
        out_dir.mkdir(parents=True, exist_ok=True)

        wall_history_auto_enabled_replay = False
        pattern_auto_enabled_wall = False
        pattern_auto_enabled_market = False
        if params.run_pattern_candidates:
            if not params.run_wall_history:
                params.run_wall_history = True
                pattern_auto_enabled_wall = True
            if not params.run_market_context:
                params.run_market_context = True
                pattern_auto_enabled_market = True
        if params.run_wall_history and not params.run_segment_replay:
            params.run_segment_replay = True
            wall_history_auto_enabled_replay = True

        limitations = [
            "Coverage uses orderbook span, not union of all tables (TTL differences possible).",
            "Message-level continuity; level rows are aggregated in ClickHouse.",
            "Incomplete snapshots never bootstrap a segment.",
        ]
        if params.run_segment_replay:
            limitations.append(
                "Phase 2: segment replay smoke only; no wall/pattern/signal analysis."
            )
            limitations.append(
                "Segment events loaded via existing load_events into memory; "
                "chunking deferred to Phase 2b if segments exceed soft threshold."
            )
            limitations.append(
                "load_events end param may lose sub-second precision on DateTime64(3); "
                "Phase 2 queries end+1s then filters strictly by segment update_id/ts."
            )
        else:
            limitations.append(
                "Phase 0/1 only: inventory + segment discovery; no wall/pattern/signal analysis."
            )
        if params.run_market_context:
            limitations.append(
                "Phase 3: market context bars only; ticker-based timeline LEFT JOIN; "
                "no walls/patterns/signals."
            )
            limitations.append(
                "Per-table TTL may differ; bars only for overlapping analysis window."
            )
            limitations.append(
                "Liquidation forward metrics (price_change_1m_after) not computed in Phase 3."
            )
        if params.run_wall_history:
            limitations.append(
                "Phase 4: wall observations/lifecycle only; no long/short signals, "
                "no cancel-vs-execution, no spoofing claims, no MFE/MAE."
            )
            limitations.append(
                "MERGED/SPLIT wall transitions not implemented; unmatched walls end as DISAPPEARED."
            )
            limitations.append(
                "Wall test/break price path source: segment_replay_mid_high_low."
            )
            if wall_history_auto_enabled_replay:
                limitations.append(
                    "--run-wall-history auto-enabled --run-segment-replay as dependency."
                )
            if not params.run_market_context:
                limitations.append(
                    "Market context not requested; wall timeline join skipped (partial coverage)."
                )
        if params.run_pattern_candidates:
            limitations.append(
                "Phase 5: descriptive pattern candidates only; no signals, no MFE/MAE, "
                "no forward returns, no profitability claims."
            )
            limitations.append(
                "Absorption/pulling labels are technical pattern names; "
                "cancel-vs-execution is unknown."
            )
            if pattern_auto_enabled_wall:
                limitations.append(
                    "--run-pattern-candidates auto-enabled --run-wall-history as dependency."
                )
            if pattern_auto_enabled_market:
                limitations.append(
                    "--run-pattern-candidates auto-enabled --run-market-context as dependency."
                )
            if wall_history_auto_enabled_replay:
                limitations.append(
                    "--run-pattern-candidates dependency chain auto-enabled --run-segment-replay."
                )

        inventory = build_data_inventory(
            client,
            symbol=params.symbol,
            range_info=range_info,
            analysis_start=ob_start,
            analysis_end=ob_end,
        )

        if ob_start is None or ob_end is None or not range_info["has_orderbook"]:
            seg = SegmentationResult()
            health = load_recorder_health_summary(
                client,
                symbol=params.symbol,
                start=analysis_start,
                end=analysis_end,
            )
            quality = build_data_quality(
                inventory=inventory,
                seg=seg,
                health=health,
                orderbook_start=ob_start,
                orderbook_end=ob_end,
            )
            integrity = {
                "ok": True,
                "errors": [],
                "warnings": ["no orderbook data in selected window"],
                "clickhouse_writes": 0,
                "readonly": True,
            }
            decision = decide_phase01(
                has_any_data=range_info["has_any_data"],
                has_orderbook=False,
                segment_count=0,
                replayable_count=0,
                gap_count=0,
                integrity_ok=True,
            )
            coverage_pct = 0.0
        else:
            logger.info(
                "Loading orderbook messages %s .. %s",
                ob_start.isoformat(),
                ob_end.isoformat(),
            )
            messages = load_orderbook_messages(
                client, symbol=params.symbol, start=ob_start, end=ob_end
            )
            logger.info("Loaded %s orderbook messages", len(messages))
            seg = discover_replay_segments(
                messages,
                symbol=params.symbol,
                min_snapshot_levels_per_side=params.min_snapshot_levels_per_side,
                segment_minutes_min=params.segment_minutes_min,
                analysis_end=ob_end,
            )
            health = load_recorder_health_summary(
                client, symbol=params.symbol, start=ob_start, end=ob_end
            )
            quality = build_data_quality(
                inventory=inventory,
                seg=seg,
                health=health,
                orderbook_start=ob_start,
                orderbook_end=ob_end,
            )
            integ = segmentation_integrity_checks(
                seg,
                min_snapshot_levels_per_side=params.min_snapshot_levels_per_side,
            )
            integrity = {
                **integ,
                "clickhouse_writes": 0,
                "readonly": True,
                "replayable_segment_count": sum(1 for s in seg.segments if s.is_replayable),
                "segment_count": len(seg.segments),
                "gap_count": len(seg.gaps),
                "snapshot_inventory_count": len(seg.snapshots),
                "complete_snapshots": seg.complete_snapshot_count,
                "incomplete_snapshots": seg.incomplete_snapshot_count,
            }
            coverage_pct = float(
                next(q["value"] for q in quality if q["metric"] == "coverage_pct")
            )
            decision = decide_phase01(
                has_any_data=range_info["has_any_data"],
                has_orderbook=True,
                segment_count=len(seg.segments),
                replayable_count=sum(1 for s in seg.segments if s.is_replayable),
                gap_count=len(seg.gaps),
                integrity_ok=bool(integ.get("ok")),
            )

        replayable = [s for s in seg.segments if s.is_replayable]
        replayable_dur = sum(s.duration_sec for s in replayable)
        if ob_start and ob_end:
            span = max((ob_end - ob_start).total_seconds(), 0.0)
        else:
            span = 0.0

        phase01_decision = decision
        replay_bundle: dict[str, Any] | None = None
        replay_stats: dict[str, Any] = {
            "replay_requested": bool(params.run_segment_replay),
            "segments_total": len(seg.segments),
            "segments_replayable": len(replayable),
            "segments_replayed": 0,
            "segments_replay_ok": 0,
            "segments_replay_failed": 0,
            "segments_no_post_warmup": 0,
            "messages_loaded_total": 0,
            "level_rows_loaded_total": 0,
            "replay_runtime_sec_total": 0.0,
            "replay_invariants_ok": True,
        }
        replay_result_rows: list[dict[str, Any]] = []
        end_state_rows: list[dict[str, Any]] = []
        error_rows: list[dict[str, Any]] = []
        sample_rows: list[dict[str, Any]] = []

        if params.run_segment_replay and seg.segments:
            logger.info(
                "Phase 2: replaying %s segments (warmup=%ss sample=%ss)",
                len(seg.segments),
                params.warmup_seconds,
                params.replay_sample_interval,
            )
            replay_bundle = replay_all_segments(
                client,
                symbol=params.symbol,
                segments=seg.segments,
                warmup_seconds=params.warmup_seconds,
                sample_interval_seconds=params.replay_sample_interval,
            )
            replay_stats.update(replay_bundle["stats"])
            replay_stats["replay_requested"] = True
            for w in replay_bundle.get("warnings") or []:
                integrity.setdefault("warnings", []).append(w)
                limitations.append(w)
            decision = decide_phase2(
                phase01_decision=phase01_decision,
                gap_count=len(seg.gaps),
                stats=replay_stats,
            )
            replay_result_rows = [r.to_row() for r in replay_bundle["results"]]
            end_state_rows = [e.to_row() for e in replay_bundle["end_states"]]
            error_rows = list(replay_bundle["errors"])
            sample_rows = list(replay_bundle["samples"])
            phase2_integrity = _phase2_integrity_checks(
                segments=seg.segments,
                results=replay_bundle["results"],
                end_states=replay_bundle["end_states"],
                errors=error_rows,
                samples=sample_rows,
                stats=replay_stats,
            )
            integrity["phase2"] = phase2_integrity
            if not phase2_integrity.get("ok"):
                integrity["ok"] = False
                integrity.setdefault("errors", []).extend(
                    phase2_integrity.get("errors") or []
                )
        elif params.run_segment_replay:
            decision = "FULL_HISTORY_SEGMENT_REPLAY_FAILED"
            integrity.setdefault("warnings", []).append(
                "segment replay requested but no segments discovered"
            )

        replay_decision = decision if params.run_segment_replay else None
        market_bundle: Any | None = None
        market_decision: str | None = None
        market_stats: dict[str, Any] = {
            "market_context_requested": bool(params.run_market_context),
            "market_context_ok": False,
            "bar_timeframes": [],
        }
        bar_timeframes: list[str] = []
        if params.run_market_context:
            bar_timeframes = parse_bar_timeframes(params.bar_timeframes)
            market_stats["bar_timeframes"] = bar_timeframes

        if params.run_market_context:
            logger.info(
                "Phase 3: market context aggregation timeframes=%s",
                bar_timeframes,
            )
            market_bundle = run_market_context(
                client,
                symbol=params.symbol,
                analysis_start=analysis_start,
                analysis_end=analysis_end,
                table_ranges=range_info["tables"],
                timeframes=bar_timeframes,
                tiny_liquidation_notional=Decimal(str(params.tiny_liquidation_notional)),
                max_bar_range_pct=float(params.max_bar_range_pct),
                max_oi_open_close_ratio=float(params.max_oi_open_close_ratio),
            )
            market_stats.update(market_bundle.stats)
            for w in market_bundle.warnings:
                integrity.setdefault("warnings", []).append(w)
                limitations.append(w)
            has_partial = bool(market_bundle.warnings) or any(
                (market_bundle.coverage.get(t) or {}).get("first_ts") is None
                for t in ("ticker_samples", "public_trades", "liquidations")
            )
            market_decision = decide_phase3_market(
                ok=bool(market_bundle.ok),
                has_partial_coverage=has_partial,
            )
            phase3_integ = check_market_context_integrity(
                price_bars=market_bundle.price_bars,
                tradeflow_bars=market_bundle.tradeflow_bars,
                timelines=market_bundle.timelines,
                stats=market_stats,
                max_bar_range_pct=float(params.max_bar_range_pct),
                max_oi_open_close_ratio=float(params.max_oi_open_close_ratio),
                price_summary=market_bundle.price_summary,
            )
            integrity["phase3"] = phase3_integ
            if not phase3_integ.get("ok"):
                integrity["ok"] = False
                integrity.setdefault("errors", []).extend(
                    phase3_integ.get("errors") or []
                )
            if params.run_segment_replay:
                decision = decide_combined_analysis(
                    run_replay=True,
                    run_market=True,
                    phase01_decision=phase01_decision,
                    replay_decision=replay_decision,
                    market_decision=market_decision,
                    gap_count=len(seg.gaps),
                )
            else:
                decision = market_decision

        wall_bundle: Any | None = None
        wall_decision: str | None = None
        wall_stats: dict[str, Any] = {
            "wall_history_requested": bool(params.run_wall_history),
            "wall_history_ok": False,
        }
        if params.run_wall_history:
            logger.info(
                "Phase 4: wall history sample=%ss warmup=%ss resolutions=%s",
                params.wall_sample_interval,
                params.wall_warmup_seconds,
                params.wall_resolutions,
            )
            wall_params = WallHistoryParams(
                sample_interval_sec=int(params.wall_sample_interval),
                warmup_seconds=int(params.wall_warmup_seconds),
                resolutions_bps=tuple(parse_wall_resolutions(params.wall_resolutions)),
                distance_max_pct=float(params.wall_distance_max_pct),
                wall_multiple_min=float(params.wall_multiple_min),
                percentile_min=float(params.wall_percentile_min),
                depth_share_min=float(params.wall_depth_share_min),
                local_radius=int(params.wall_local_radius),
                cluster_max_gap_buckets=int(params.wall_cluster_max_gap_buckets),
                match_distance_bps=float(params.wall_match_distance_bps),
                test_distance_bps=float(params.wall_test_distance_bps),
                break_distance_bps=float(params.wall_break_distance_bps),
                min_age_seconds=float(params.wall_min_age_seconds),
                notional_change_threshold_pct=float(params.wall_notional_change_threshold_pct),
                output_mode=str(params.wall_output_mode),
            )
            replay_ok_ids = None
            if replay_bundle is not None:
                replay_ok_ids = {
                    r.segment_id
                    for r in replay_bundle["results"]
                    if str(r.replay_status).startswith("REPLAY_OK")
                }
            timelines = market_bundle.timelines if market_bundle is not None else None
            wall_bundle = run_wall_history(
                client,
                symbol=params.symbol,
                segments=seg.segments,
                gaps=seg.gaps,
                params=wall_params,
                timelines=timelines,
                replay_ok_segment_ids=replay_ok_ids,
            )
            wall_stats.update(wall_bundle.stats)
            for w in wall_bundle.warnings:
                integrity.setdefault("warnings", []).append(w)
                limitations.append(w)
            has_fail = int(wall_stats.get("wall_segments_failed") or 0) > 0
            has_ok = int(wall_stats.get("wall_segments_ok") or 0) > 0
            wall_decision = decide_phase4_wall(
                ok=bool(wall_bundle.ok),
                gap_count=len(seg.gaps),
                has_failures=has_fail,
                has_success=has_ok,
            )
            phase4_integ = check_wall_history_integrity(
                observations=wall_bundle.observations,
                sequences=wall_bundle.sequences,
                transitions=wall_bundle.transitions,
                segments=seg.segments,
                warmup_seconds=wall_params.warmup_seconds,
                timelines_with_walls=wall_bundle.timelines_with_walls or None,
            )
            integrity["phase4"] = phase4_integ
            if not phase4_integ.get("ok"):
                integrity["ok"] = False
                integrity.setdefault("errors", []).extend(phase4_integ.get("errors") or [])

            module_decisions = [
                d
                for d in (replay_decision, market_decision, wall_decision)
                if d is not None
            ]
            if params.run_segment_replay or params.run_market_context:
                decision = decide_full_analysis(
                    integrity_ok=bool(integrity.get("ok")),
                    gap_count=len(seg.gaps),
                    module_decisions=module_decisions,
                )
            else:
                decision = wall_decision

        pattern_bundle = None
        pattern_decision = None
        pattern_stats: dict[str, Any] = {
            "pattern_candidates_requested": bool(params.run_pattern_candidates),
            "pattern_candidates_ok": False,
        }
        if params.run_pattern_candidates:
            if wall_bundle is None or not wall_bundle.timelines_with_walls:
                pattern_stats = {
                    "pattern_candidates_requested": True,
                    "pattern_candidates_ok": False,
                    "error_message": "wall timelines_with_walls required for pattern candidates",
                    "pattern_candidate_count": 0,
                    "pattern_integrity_error_count": 1,
                    "pattern_segments_failed": 1,
                    "pattern_segments_ok": 0,
                    "top_pattern_types": [],
                }
                pattern_decision = "FULL_HISTORY_PATTERN_CANDIDATES_FAILED"
                integrity["ok"] = False
                integrity.setdefault("errors", []).append(
                    "phase5 requires analysis_timeline_*_with_walls from wall history"
                )
            else:
                logger.info("Phase 5: descriptive pattern candidates timeframe=%s", params.pattern_timeframe)
                try:
                    pattern_params = validate_pattern_params(
                        PatternParams(
                            timeframe=params.pattern_timeframe,
                            lookback_bars=int(params.pattern_lookback_bars),
                            min_wall_age_sec=float(params.pattern_min_wall_age_sec),
                            min_wall_samples=int(params.pattern_min_wall_samples),
                            near_distance_bps=float(params.pattern_near_distance_bps),
                            strong_wall_multiple=float(params.pattern_strong_wall_multiple),
                            dominant_depth_share=float(params.pattern_dominant_depth_share),
                            delta_ratio_threshold=float(params.pattern_delta_ratio_threshold),
                            oi_change_threshold_pct=float(params.pattern_oi_change_threshold_pct),
                            price_change_threshold_pct=float(params.pattern_price_change_threshold_pct),
                            wall_growth_threshold_pct=float(params.pattern_wall_growth_threshold_pct),
                            wall_imbalance_threshold=float(params.pattern_wall_imbalance_threshold),
                            cooldown_bars=int(params.pattern_cooldown_bars),
                            output_mode=str(params.pattern_output_mode),
                        )
                    )
                except PatternCandidateError as exc:
                    pattern_stats = {
                        "pattern_candidates_requested": True,
                        "pattern_candidates_ok": False,
                        "error_message": str(exc),
                        "pattern_candidate_count": 0,
                        "pattern_integrity_error_count": 1,
                        "pattern_segments_failed": 1,
                        "pattern_segments_ok": 0,
                        "top_pattern_types": [],
                    }
                    pattern_decision = "FULL_HISTORY_PATTERN_CANDIDATES_FAILED"
                    integrity["ok"] = False
                    integrity.setdefault("errors", []).append(f"phase5 params: {exc}")
                    pattern_bundle = None
                else:
                    pattern_bundle = run_pattern_candidates(
                        symbol=params.symbol,
                        segments=seg.segments,
                        gaps=seg.gaps,
                        timelines_with_walls=wall_bundle.timelines_with_walls,
                        transitions=wall_bundle.transitions,
                        params=pattern_params,
                    )
                    pattern_stats = dict(pattern_bundle.stats)
                    phase5_integ = check_pattern_integrity(
                        candidates=pattern_bundle.candidates,
                        features=pattern_bundle.features,
                        segments=seg.segments,
                        gaps=seg.gaps,
                    )
                    integrity["phase5"] = phase5_integ
                    if not phase5_integ.get("ok"):
                        integrity["ok"] = False
                        integrity.setdefault("errors", []).extend(phase5_integ.get("errors") or [])
                    has_fail = int(pattern_stats.get("pattern_segments_failed") or 0) > 0
                    has_ok = int(pattern_stats.get("pattern_segments_ok") or 0) > 0
                    pattern_decision = decide_phase5_patterns(
                        ok=bool(pattern_stats.get("pattern_candidates_ok"))
                        and bool(phase5_integ.get("ok")),
                        gap_count=len(seg.gaps),
                        has_failures=has_fail or not bool(phase5_integ.get("ok")),
                        has_success=has_ok,
                    )
                    module_decisions = [
                        d
                        for d in (
                            replay_decision,
                            market_decision,
                            wall_decision,
                            pattern_decision,
                        )
                        if d is not None
                    ]
                    decision = decide_full_analysis(
                        integrity_ok=bool(integrity.get("ok")),
                        gap_count=len(seg.gaps),
                        module_decisions=module_decisions,
                    )
                    integrity.setdefault("csv_counts", {})
                    integrity["csv_counts"]["pattern_candidates"] = len(
                        pattern_bundle.candidates
                    )
                    integrity["csv_counts"]["pattern_feature_matrix"] = len(
                        pattern_bundle.features
                    )

        if params.run_pattern_candidates and params.run_wall_history and params.run_market_context and params.run_segment_replay:
            phase_label = "2_3_4_5_full_stack"
        elif params.run_wall_history and params.run_market_context and params.run_segment_replay:
            phase_label = "2_3_4_full_stack"
        elif params.run_wall_history and params.run_segment_replay:
            phase_label = "2_4_replay_and_wall_history"
        elif params.run_wall_history:
            phase_label = "4_wall_history"
        elif params.run_market_context and params.run_segment_replay:
            phase_label = "2_3_replay_and_market_context"
        elif params.run_market_context:
            phase_label = "3_market_context"
        elif params.run_segment_replay:
            phase_label = "2_segment_replay"
        else:
            phase_label = "0_1_segment_discovery"

        summary = {
            "symbol": params.symbol,
            "analysis_start": None if analysis_start is None else analysis_start.isoformat(),
            "analysis_end": None if analysis_end is None else analysis_end.isoformat(),
            "orderbook_start": None if ob_start is None else ob_start.isoformat(),
            "orderbook_end": None if ob_end is None else ob_end.isoformat(),
            "table_inventory": {
                r["table_name"]: {
                    "first_ts": r["first_ts"],
                    "last_ts": r["last_ts"],
                    "row_count": r["row_count"],
                    "distinct_message_count": r.get("distinct_message_count"),
                    "snapshot_message_count": r.get("snapshot_message_count"),
                    "delta_message_count": r.get("delta_message_count"),
                }
                for r in inventory
            },
            "complete_snapshots": seg.complete_snapshot_count,
            "incomplete_snapshots": seg.incomplete_snapshot_count,
            "gap_count": len(seg.gaps),
            "segment_count": len(seg.segments),
            "replayable_segment_count": len(replayable),
            "replayable_duration_sec": replayable_dur,
            "orderbook_span_sec": span,
            "coverage_pct": coverage_pct,
            "decision": decision,
            "phase01_decision": phase01_decision,
            "limitations": list(limitations),
            "recorder_health": dict(health),
            "phase": phase_label,
            "replay_requested": bool(params.run_segment_replay),
            "market_context_requested": bool(params.run_market_context),
            "market_context_ok": market_stats.get("market_context_ok"),
            "bar_timeframes": market_stats.get("bar_timeframes"),
            "ticker_rows": market_stats.get("ticker_rows", 0),
            "trade_rows": market_stats.get("trade_rows", 0),
            "liquidation_rows": market_stats.get("liquidation_rows", 0),
            "price_bars_1m": market_stats.get("price_bars_1m", 0),
            "price_bars_5m": market_stats.get("price_bars_5m", 0),
            "tradeflow_bars_1m": market_stats.get("tradeflow_bars_1m", 0),
            "tradeflow_bars_5m": market_stats.get("tradeflow_bars_5m", 0),
            "oi_bars_1m": market_stats.get("oi_bars_1m", 0),
            "oi_bars_5m": market_stats.get("oi_bars_5m", 0),
            "liquidation_bars_1m": market_stats.get("liquidation_bars_1m", 0),
            "liquidation_bars_5m": market_stats.get("liquidation_bars_5m", 0),
            "timeline_rows_1m": market_stats.get("timeline_rows_1m", 0),
            "timeline_rows_5m": market_stats.get("timeline_rows_5m", 0),
            "price_start": market_stats.get("price_start"),
            "price_end": market_stats.get("price_end"),
            "price_change_pct": market_stats.get("price_change_pct"),
            "price_high": market_stats.get("price_high"),
            "price_low": market_stats.get("price_low"),
            "trade_total_notional": market_stats.get("trade_total_notional"),
            "trade_buy_notional": market_stats.get("trade_buy_notional"),
            "trade_sell_notional": market_stats.get("trade_sell_notional"),
            "trade_delta_notional": market_stats.get("trade_delta_notional"),
            "oi_start": market_stats.get("oi_start"),
            "oi_end": market_stats.get("oi_end"),
            "oi_change_pct": market_stats.get("oi_change_pct"),
            "liquidation_event_count": market_stats.get("liquidation_event_count", 0),
            "liquidation_total_notional": market_stats.get("liquidation_total_notional"),
            "market_decision": market_decision,
            "replay_decision": replay_decision,
            "wall_decision": wall_decision,
            "pattern_decision": pattern_decision,
            "pattern_candidates_requested": bool(params.run_pattern_candidates),
            "pattern_candidates_ok": pattern_stats.get("pattern_candidates_ok"),
            "pattern_timeframe": pattern_stats.get("pattern_timeframe"),
            "pattern_lookback_bars": pattern_stats.get("pattern_lookback_bars"),
            "pattern_candidate_count": pattern_stats.get("pattern_candidate_count", 0),
            "pattern_type_count": pattern_stats.get("pattern_type_count", 0),
            "pattern_family_count": pattern_stats.get("pattern_family_count", 0),
            "pattern_symbols_count": pattern_stats.get("pattern_symbols_count", 0),
            "pattern_segments_count": pattern_stats.get("pattern_segments_count", 0),
            "pattern_data_complete_count": pattern_stats.get("pattern_data_complete_count", 0),
            "pattern_data_incomplete_count": pattern_stats.get("pattern_data_incomplete_count", 0),
            "pattern_wall_lifecycle_count": pattern_stats.get("pattern_wall_lifecycle_count", 0),
            "pattern_price_delta_count": pattern_stats.get("pattern_price_delta_count", 0),
            "pattern_price_oi_count": pattern_stats.get("pattern_price_oi_count", 0),
            "pattern_wall_flow_count": pattern_stats.get("pattern_wall_flow_count", 0),
            "pattern_liquidation_count": pattern_stats.get("pattern_liquidation_count", 0),
            "pattern_absorption_candidate_count": pattern_stats.get("pattern_absorption_candidate_count", 0),
            "pattern_wall_failure_candidate_count": pattern_stats.get("pattern_wall_failure_candidate_count", 0),
            "pattern_integrity_error_count": pattern_stats.get("pattern_integrity_error_count", 0),
            "pattern_runtime_sec": pattern_stats.get("pattern_runtime_sec"),
            "wall_history_requested": bool(params.run_wall_history),
            "wall_history_ok": wall_stats.get("wall_history_ok"),
            "wall_sample_interval_sec": wall_stats.get("wall_sample_interval_sec"),
            "wall_warmup_seconds": wall_stats.get("wall_warmup_seconds"),
            "wall_segments_total": wall_stats.get("wall_segments_total", 0),
            "wall_segments_ok": wall_stats.get("wall_segments_ok", 0),
            "wall_segments_failed": wall_stats.get("wall_segments_failed", 0),
            "wall_samples_total": wall_stats.get("wall_samples_total", 0),
            "wall_observations_total": wall_stats.get("wall_observations_total", 0),
            "wall_clusters_total": wall_stats.get("wall_clusters_total", 0),
            "wall_sequences_total": wall_stats.get("wall_sequences_total", 0),
            "wall_transitions_total": wall_stats.get("wall_transitions_total", 0),
            "bid_wall_sequences": wall_stats.get("bid_wall_sequences", 0),
            "ask_wall_sequences": wall_stats.get("ask_wall_sequences", 0),
            "tested_wall_sequences": wall_stats.get("tested_wall_sequences", 0),
            "broken_wall_sequences": wall_stats.get("broken_wall_sequences", 0),
            "disappeared_before_test_sequences": wall_stats.get("disappeared_before_test_sequences", 0),
            "timeline_with_walls_rows_1m": wall_stats.get("timeline_with_walls_rows_1m", 0),
            "timeline_with_walls_rows_5m": wall_stats.get("timeline_with_walls_rows_5m", 0),
            "wall_history_runtime_sec_total": wall_stats.get("wall_history_runtime_sec_total", 0),
            "segments_total": replay_stats["segments_total"],
            "segments_replayable": replay_stats["segments_replayable"],
            "segments_replayed": replay_stats["segments_replayed"],
            "segments_replay_ok": replay_stats["segments_replay_ok"],
            "segments_replay_failed": replay_stats["segments_replay_failed"],
            "segments_no_post_warmup": replay_stats["segments_no_post_warmup"],
            "messages_loaded_total": replay_stats["messages_loaded_total"],
            "level_rows_loaded_total": replay_stats["level_rows_loaded_total"],
            "replay_runtime_sec_total": replay_stats["replay_runtime_sec_total"],
            "replay_invariants_ok": replay_stats["replay_invariants_ok"],
            "warmup_seconds": params.warmup_seconds,
            "replay_sample_interval": params.replay_sample_interval,
        }

        # CSV counts vs summary for integrity
        integrity["csv_counts"] = {
            "replay_segments": len(seg.segments),
            "replay_gaps": len(seg.gaps),
            "snapshot_inventory": len(seg.snapshots),
            "data_inventory": len(inventory),
            "data_quality": len(quality),
        }
        if params.run_segment_replay:
            integrity["csv_counts"]["segment_replay_results"] = len(replay_result_rows)
            integrity["csv_counts"]["segment_book_end_states"] = len(end_state_rows)
            integrity["csv_counts"]["segment_replay_errors"] = len(error_rows)
            integrity["csv_counts"]["segment_replay_samples"] = len(sample_rows)
        if params.run_market_context and market_bundle is not None:
            integrity["csv_counts"]["price_summary"] = 1 if market_bundle.price_summary else 0
            integrity["csv_counts"]["liquidations"] = len(market_bundle.liquidations)
            for tf in bar_timeframes:
                integrity["csv_counts"][f"price_bars_{tf}"] = len(
                    market_bundle.price_bars.get(tf) or []
                )
                integrity["csv_counts"][f"tradeflow_{tf}"] = len(
                    market_bundle.tradeflow_bars.get(tf) or []
                )
                integrity["csv_counts"][f"oi_{tf}"] = len(
                    market_bundle.oi_bars.get(tf) or []
                )
                integrity["csv_counts"][f"liquidation_bars_{tf}"] = len(
                    market_bundle.liquidation_bars.get(tf) or []
                )
                integrity["csv_counts"][f"analysis_timeline_{tf}"] = len(
                    market_bundle.timelines.get(tf) or []
                )
        if params.run_wall_history and wall_bundle is not None:
            integrity["csv_counts"]["wall_observations"] = len(wall_bundle.observations)
            integrity["csv_counts"]["wall_sequences"] = len(wall_bundle.sequences)
            integrity["csv_counts"]["wall_transitions"] = len(wall_bundle.transitions)
            integrity["csv_counts"]["wall_segment_summary"] = len(wall_bundle.segment_summaries)
            for tf, rows in (wall_bundle.timelines_with_walls or {}).items():
                integrity["csv_counts"][f"analysis_timeline_{tf}_with_walls"] = len(rows)
        integrity["summary_matches_csvs"] = (
            integrity["csv_counts"]["replay_segments"] == summary["segment_count"]
            and integrity["csv_counts"]["replay_gaps"] == summary["gap_count"]
            and integrity["csv_counts"]["snapshot_inventory"]
            == seg.complete_snapshot_count + seg.incomplete_snapshot_count
        )
        if not integrity["summary_matches_csvs"]:
            integrity["ok"] = False
            integrity.setdefault("errors", []).append("summary/csv count mismatch")

        config = {
            "params": {
                "symbol": params.symbol,
                "start": None if params.start is None else params.start.isoformat(),
                "end": None if params.end is None else params.end.isoformat(),
                "segment_minutes_min": params.segment_minutes_min,
                "min_snapshot_levels_per_side": params.min_snapshot_levels_per_side,
                "run_segment_replay": params.run_segment_replay,
                "run_market_context": params.run_market_context,
                "run_wall_history": params.run_wall_history,
                "run_pattern_candidates": params.run_pattern_candidates,
                "pattern_timeframe": params.pattern_timeframe,
                "pattern_lookback_bars": params.pattern_lookback_bars,
                "pattern_cooldown_bars": params.pattern_cooldown_bars,
                "pattern_output_mode": params.pattern_output_mode,
                "warmup_seconds": params.warmup_seconds,
                "replay_sample_interval": params.replay_sample_interval,
                "bar_timeframes": params.bar_timeframes,
                "tiny_liquidation_notional": params.tiny_liquidation_notional,
                "wall_sample_interval": params.wall_sample_interval,
                "wall_warmup_seconds": params.wall_warmup_seconds,
                "wall_resolutions": params.wall_resolutions,
                "wall_output_mode": params.wall_output_mode,
            },
            "continuity": {
                "primary": "update_id == previous + 1",
                "cross_sequence_backwards": "anomaly",
                "snapshot_resets_state": True,
                "message_key": "(exchange_ts, update_id, cross_sequence, message_type)",
            },
            "coverage_definition": (
                "sum(replayable segment duration) / full orderbook time span"
            ),
            "warmup_semantics": (
                "Replay from bootstrap; feature_emission_start_ts = segment_start + warmup; "
                "short segments → REPLAY_OK_NO_POST_WARMUP"
            ),
            "market_context_semantics": (
                "ClickHouse GROUP BY bars; ticker-based timeline; no walls/signals"
            ),
            "phase": summary["phase"],
            "readonly": True,
        }

        write_csv_headered(
            out_dir / "data_inventory.csv",
            inventory,
            headers=[
                "symbol",
                "table_name",
                "timestamp_column",
                "first_ts",
                "last_ts",
                "row_count",
                "distinct_message_count",
                "snapshot_message_count",
                "delta_message_count",
                "notes",
            ],
        )
        write_csv_headered(
            out_dir / "data_quality.csv",
            quality,
            headers=["metric", "value", "status", "details"],
        )
        write_csv_headered(
            out_dir / "snapshot_inventory.csv",
            [s.to_row() for s in seg.snapshots],
            headers=[
                "snapshot_ts",
                "update_id",
                "cross_sequence",
                "bid_level_count",
                "ask_level_count",
                "total_level_count",
                "is_complete",
            ],
        )
        write_csv_headered(
            out_dir / "replay_segments.csv",
            [s.to_row() for s in seg.segments],
            headers=[
                "segment_id",
                "symbol",
                "segment_start_ts",
                "segment_end_ts",
                "bootstrap_snapshot_ts",
                "bootstrap_update_id",
                "bootstrap_cross_sequence",
                "first_delta_update_id",
                "last_update_id",
                "last_cross_sequence",
                "message_count",
                "delta_message_count",
                "snapshot_message_count",
                "duration_sec",
                "bid_snapshot_levels",
                "ask_snapshot_levels",
                "is_replayable",
                "discard_reason",
                "end_reason",
            ],
        )
        write_csv_headered(
            out_dir / "replay_gaps.csv",
            [g.to_row() for g in seg.gaps],
            headers=[
                "gap_id",
                "symbol",
                "gap_start_ts",
                "gap_end_ts",
                "previous_update_id",
                "next_update_id",
                "missing_update_count",
                "previous_cross_sequence",
                "next_cross_sequence",
                "next_message_type",
                "next_snapshot_complete",
                "recovered_at_snapshot_ts",
                "discarded_duration_sec",
                "reason",
            ],
        )

        required_outputs = list(OUTPUT_FILES)
        if params.run_segment_replay:
            write_csv_headered(
                out_dir / "segment_replay_results.csv",
                replay_result_rows,
                headers=[
                    "segment_id",
                    "symbol",
                    "segment_start_ts",
                    "segment_end_ts",
                    "bootstrap_snapshot_ts",
                    "bootstrap_update_id",
                    "expected_last_update_id",
                    "actual_last_update_id",
                    "expected_last_cross_sequence",
                    "actual_last_cross_sequence",
                    "messages_loaded",
                    "snapshot_messages_loaded",
                    "delta_messages_loaded",
                    "events_or_level_rows_loaded",
                    "duration_sec",
                    "warmup_seconds",
                    "feature_emission_start_ts",
                    "post_warmup_duration_sec",
                    "replay_status",
                    "invariants_ok",
                    "error_type",
                    "error_message",
                    "runtime_sec",
                ],
            )
            write_csv_headered(
                out_dir / "segment_book_end_states.csv",
                end_state_rows,
                headers=[
                    "segment_id",
                    "symbol",
                    "end_ts",
                    "last_update_id",
                    "last_cross_sequence",
                    "best_bid",
                    "best_ask",
                    "mid_price",
                    "spread",
                    "spread_bps",
                    "active_bid_levels",
                    "active_ask_levels",
                    "active_levels",
                    "bid_depth_notional",
                    "ask_depth_notional",
                    "total_depth_notional",
                ],
            )
            write_csv_headered(
                out_dir / "segment_replay_errors.csv",
                error_rows,
                headers=[
                    "segment_id",
                    "symbol",
                    "error_ts",
                    "previous_update_id",
                    "current_update_id",
                    "previous_cross_sequence",
                    "current_cross_sequence",
                    "error_type",
                    "error_message",
                ],
            )
            write_csv_headered(
                out_dir / "segment_replay_samples.csv",
                sample_rows,
                headers=[
                    "segment_id",
                    "sample_ts",
                    "last_update_id",
                    "best_bid",
                    "best_ask",
                    "mid_price",
                    "spread_bps",
                    "active_bid_levels",
                    "active_ask_levels",
                    "bid_depth_notional",
                    "ask_depth_notional",
                ],
            )
            required_outputs.extend(PHASE2_OUTPUT_FILES)

        if params.run_market_context and market_bundle is not None:
            write_csv_headered(
                out_dir / "price_summary.csv",
                [market_bundle.price_summary],
                headers=PRICE_SUMMARY_HEADERS,
            )
            write_csv_headered(
                out_dir / "liquidations.csv",
                market_bundle.liquidations,
                headers=LIQUIDATION_EVENT_HEADERS,
            )
            for tf in bar_timeframes:
                write_csv_headered(
                    out_dir / f"price_bars_{tf}.csv",
                    market_bundle.price_bars.get(tf) or [],
                    headers=PRICE_BAR_HEADERS,
                )
                write_csv_headered(
                    out_dir / f"tradeflow_{tf}.csv",
                    market_bundle.tradeflow_bars.get(tf) or [],
                    headers=TRADEFLOW_HEADERS,
                )
                write_csv_headered(
                    out_dir / f"oi_{tf}.csv",
                    market_bundle.oi_bars.get(tf) or [],
                    headers=OI_HEADERS,
                )
                write_csv_headered(
                    out_dir / f"liquidation_bars_{tf}.csv",
                    market_bundle.liquidation_bars.get(tf) or [],
                    headers=LIQUIDATION_BAR_HEADERS,
                )
                write_csv_headered(
                    out_dir / f"analysis_timeline_{tf}.csv",
                    market_bundle.timelines.get(tf) or [],
                    headers=TIMELINE_HEADERS,
                )
            required_outputs.extend(phase3_output_files(bar_timeframes))

        if params.run_wall_history and wall_bundle is not None:
            write_csv_headered(out_dir / "wall_observations.csv", wall_bundle.observations, headers=WALL_OBSERVATION_HEADERS)
            write_csv_headered(out_dir / "wall_candidates_history.csv", wall_bundle.candidates, headers=WALL_CANDIDATE_HEADERS)
            write_csv_headered(out_dir / "wall_clusters_history.csv", wall_bundle.clusters, headers=WALL_CLUSTER_HEADERS)
            write_csv_headered(out_dir / "wall_sequences.csv", wall_bundle.sequences, headers=WALL_SEQUENCE_HEADERS)
            write_csv_headered(out_dir / "wall_transitions.csv", wall_bundle.transitions, headers=WALL_TRANSITION_HEADERS)
            write_csv_headered(out_dir / "wall_segment_summary.csv", wall_bundle.segment_summaries, headers=WALL_SEGMENT_SUMMARY_HEADERS)
            write_csv_headered(out_dir / "wall_history_errors.csv", wall_bundle.errors, headers=WALL_ERROR_HEADERS)
            tfs = list(bar_timeframes) if bar_timeframes else ["1m", "5m"]
            for tf in tfs:
                rows = wall_bundle.timelines_with_walls.get(tf) or []
                headers = list(TIMELINE_HEADERS) + list(TIMELINE_WALL_EXTRA_HEADERS)
                write_csv_headered(out_dir / f"analysis_timeline_{tf}_with_walls.csv", rows, headers=headers)
            if wall_bundle.timelines_with_walls:
                required_outputs.extend(phase4_output_files(tfs))
            else:
                required_outputs.extend([
                    "wall_observations.csv",
                    "wall_candidates_history.csv",
                    "wall_clusters_history.csv",
                    "wall_sequences.csv",
                    "wall_transitions.csv",
                    "wall_segment_summary.csv",
                    "wall_history_errors.csv",
                ])

        if params.run_pattern_candidates and pattern_bundle is not None:
            write_csv_headered(out_dir / "pattern_candidates.csv", pattern_bundle.candidates, headers=CANDIDATE_HEADERS)
            write_csv_headered(out_dir / "pattern_feature_matrix.csv", pattern_bundle.features, headers=FEATURE_HEADERS)
            write_csv_headered(out_dir / "pattern_transitions_context.csv", pattern_bundle.transition_contexts, headers=TRANSITION_CONTEXT_HEADERS)
            write_csv_headered(out_dir / "pattern_summary_by_symbol.csv", pattern_bundle.summary_by_symbol, headers=SUMMARY_SYMBOL_HEADERS)
            write_csv_headered(out_dir / "pattern_summary_by_segment.csv", pattern_bundle.summary_by_segment, headers=SUMMARY_SEGMENT_HEADERS)
            write_csv_headered(out_dir / "pattern_summary_by_type.csv", pattern_bundle.summary_by_type, headers=SUMMARY_TYPE_HEADERS)
            write_csv_headered(out_dir / "pattern_integrity_errors.csv", pattern_bundle.integrity_errors, headers=INTEGRITY_ERROR_HEADERS)
            ptf = params.pattern_timeframe
            prows = pattern_bundle.timelines.get(ptf) or []
            headers = list(TIMELINE_HEADERS) + list(TIMELINE_WALL_EXTRA_HEADERS) + list(PATTERN_TIMELINE_EXTRA_HEADERS)
            write_csv_headered(out_dir / f"pattern_timeline_{ptf}.csv", prows, headers=headers)
            required_outputs.extend(phase5_output_files([ptf]))
        elif params.run_pattern_candidates:
            # still emit empty headers
            write_csv_headered(out_dir / "pattern_candidates.csv", [], headers=CANDIDATE_HEADERS)
            write_csv_headered(out_dir / "pattern_feature_matrix.csv", [], headers=FEATURE_HEADERS)
            write_csv_headered(out_dir / "pattern_transitions_context.csv", [], headers=TRANSITION_CONTEXT_HEADERS)
            write_csv_headered(out_dir / "pattern_summary_by_symbol.csv", [], headers=SUMMARY_SYMBOL_HEADERS)
            write_csv_headered(out_dir / "pattern_summary_by_segment.csv", [], headers=SUMMARY_SEGMENT_HEADERS)
            write_csv_headered(out_dir / "pattern_summary_by_type.csv", [], headers=SUMMARY_TYPE_HEADERS)
            write_csv_headered(out_dir / "pattern_integrity_errors.csv", [], headers=INTEGRITY_ERROR_HEADERS)
            required_outputs.extend(phase5_output_files([params.pattern_timeframe]))

        (out_dir / "summary.json").write_bytes(
            orjson.dumps(summary, option=orjson.OPT_INDENT_2)
        )
        (out_dir / "integrity.json").write_bytes(
            orjson.dumps(integrity, option=orjson.OPT_INDENT_2)
        )
        (out_dir / "config.json").write_bytes(
            orjson.dumps(config, option=orjson.OPT_INDENT_2)
        )
        (out_dir / "REPORT.md").write_text(
            render_report(
                decision=decision,
                symbol=params.symbol,
                analysis_start=analysis_start,
                analysis_end=analysis_end,
                inventory=inventory,
                seg=seg,
                quality=quality,
                health=health,
                coverage_pct=float(coverage_pct),
                limitations=limitations,
                replay_stats=replay_stats if params.run_segment_replay else None,
                replay_results=replay_result_rows if params.run_segment_replay else None,
                end_states=end_state_rows if params.run_segment_replay else None,
                warmup_seconds=params.warmup_seconds if params.run_segment_replay else None,
                market_stats=market_stats if params.run_market_context else None,
                market_coverage=market_bundle.coverage if market_bundle else None,
                price_summary=market_bundle.price_summary if market_bundle else None,
                quadrant_summary=(
                    quadrant_counts(market_bundle.oi_bars.get("1m") or [])
                    if market_bundle
                    else None
                ),
                wall_stats=wall_stats if params.run_wall_history else None,
                wall_segment_summaries=(
                    wall_bundle.segment_summaries if wall_bundle is not None else None
                ),
                pattern_stats=pattern_stats if params.run_pattern_candidates else None,
            ),
            encoding="utf-8",
        )

        for name in required_outputs:
            if not (out_dir / name).exists():
                raise RuntimeError(f"missing required output {name}")

        return {
            "decision": decision,
            "output_dir": str(out_dir),
            "summary": summary,
            "integrity": integrity,
            "segmentation": seg,
            "replay": replay_bundle,
            "market_context": market_bundle,
            "wall_history": wall_bundle,
            "pattern_candidates": pattern_bundle,
        }
    finally:
        if own_db:
            client.close()


def _phase2_integrity_checks(
    *,
    segments: Sequence[Any],
    results: Sequence[Any],
    end_states: Sequence[Any],
    errors: Sequence[Mapping[str, Any]],
    samples: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
) -> dict[str, Any]:
    errs: list[str] = []
    warns: list[str] = []
    seg_by_id = {s.segment_id: s for s in segments}
    result_ids = [r.segment_id for r in results]

    if len(results) != len(segments):
        errs.append(
            f"result row count {len(results)} != segment count {len(segments)}"
        )
    if len(results) != int(stats.get("segments_total") or 0):
        errs.append("segments_total in stats does not match result rows")

    for r in results:
        seg = seg_by_id.get(r.segment_id)
        if seg is None:
            errs.append(f"result {r.segment_id} not in known segments")
            continue
        if r.replay_status.startswith("REPLAY_OK"):
            if not seg.is_replayable:
                errs.append(f"non-replayable segment {r.segment_id} was replayed ok")
            if r.actual_last_update_id != seg.last_update_id:
                errs.append(
                    f"{r.segment_id}: end update_id mismatch "
                    f"{r.actual_last_update_id} != {seg.last_update_id}"
                )
        if r.replay_status.startswith("REPLAY_FAILED"):
            if not any(e.get("segment_id") == r.segment_id for e in errors):
                errs.append(f"failed segment {r.segment_id} missing from errors file")
        if (
            r.replay_status.startswith("REPLAY")
            and not seg.is_replayable
            and r.replay_status != "SKIPPED_NOT_REPLAYABLE"
        ):
            errs.append(f"non-replayable {r.segment_id} status={r.replay_status}")

    ok_ids = {
        r.segment_id for r in results if r.replay_status.startswith("REPLAY_OK")
    }
    end_ids = {e.segment_id for e in end_states}
    if ok_ids != end_ids:
        errs.append(
            f"end states {sorted(end_ids)} != successful replays {sorted(ok_ids)}"
        )

    # samples chronological per segment
    last_by_seg: dict[str, str] = {}
    for row in samples:
        sid = str(row.get("segment_id"))
        sts = str(row.get("sample_ts") or "")
        if sid not in seg_by_id:
            errs.append(f"sample for unknown segment {sid}")
            continue
        prev = last_by_seg.get(sid)
        if prev is not None and sts < prev:
            errs.append(f"sample timestamps not chronological for {sid}")
        last_by_seg[sid] = sts

    ok = len(errs) == 0
    return {
        "ok": ok,
        "errors": errs,
        "warnings": warns,
        "result_segment_ids": result_ids,
        "successful_segment_ids": sorted(ok_ids),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Full-history orderbook analysis "
            "(Phase 0/1–5: segments, replay, market context, wall history, pattern candidates)"
        )
    )
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--output-dir", default=None)
    p.add_argument("--segment-minutes-min", type=float, default=5.0)
    p.add_argument("--min-snapshot-levels-per-side", type=int, default=150)
    p.add_argument(
        "--run-segment-replay",
        action="store_true",
        help="Run Phase 2 segment-wise orderbook replay smoke",
    )
    p.add_argument(
        "--run-market-context",
        action="store_true",
        help="Run Phase 3 market context aggregation (price/trades/OI/liquidations)",
    )
    p.add_argument("--warmup-seconds", type=int, default=300)
    p.add_argument("--replay-sample-interval", type=int, default=60)
    p.add_argument(
        "--bar-timeframes",
        default="1m,5m",
        help="Comma-separated bar timeframes for Phase 3 (supported: 1m,5m)",
    )
    p.add_argument(
        "--tiny-liquidation-notional",
        type=float,
        default=1.0,
        help="Tiny liquidation event threshold in USDT (data quality flag only)",
    )
    p.add_argument(
        "--max-bar-range-pct",
        type=float,
        default=20.0,
        help="Integrity guard: max allowed 1m/5m bar high/low span percent (cross-symbol mix detector)",
    )
    p.add_argument(
        "--max-oi-open-close-ratio",
        type=float,
        default=100.0,
        help="Integrity guard: max allowed OI open/close ratio within one bar",
    )
    p.add_argument(
        "--run-wall-history",
        action="store_true",
        help="Run Phase 4 wall observations/lifecycle (auto-enables segment replay)",
    )
    p.add_argument("--wall-sample-interval", type=int, default=60)
    p.add_argument("--wall-warmup-seconds", type=int, default=300)
    p.add_argument("--wall-resolutions", default="5,10,20,50", help="BPS resolutions, comma-separated")
    p.add_argument("--wall-distance-max-pct", type=float, default=5.0)
    p.add_argument("--wall-multiple-min", type=float, default=3.0)
    p.add_argument("--wall-percentile-min", type=float, default=90.0)
    p.add_argument("--wall-depth-share-min", type=float, default=0.01)
    p.add_argument("--wall-local-radius", type=int, default=5)
    p.add_argument("--wall-cluster-max-gap-buckets", type=int, default=1)
    p.add_argument("--wall-match-distance-bps", type=float, default=10.0)
    p.add_argument("--wall-test-distance-bps", type=float, default=5.0)
    p.add_argument("--wall-break-distance-bps", type=float, default=5.0)
    p.add_argument("--wall-min-age-seconds", type=float, default=60.0)
    p.add_argument("--wall-notional-change-threshold-pct", type=float, default=20.0)
    p.add_argument("--wall-output-mode", default="candidates", choices=["candidates", "all_buckets"])
    p.add_argument(
        "--run-pattern-candidates",
        action="store_true",
        help="Run Phase 5 descriptive pattern candidates (auto-enables wall history + market context)",
    )
    p.add_argument("--pattern-timeframe", default="1m", help="Primary pattern timeframe (1m or 5m)")
    p.add_argument("--pattern-lookback-bars", type=int, default=5)
    p.add_argument("--pattern-min-wall-age-sec", type=float, default=120.0)
    p.add_argument("--pattern-min-wall-samples", type=int, default=2)
    p.add_argument("--pattern-near-distance-bps", type=float, default=100.0)
    p.add_argument("--pattern-strong-wall-multiple", type=float, default=3.0)
    p.add_argument("--pattern-dominant-depth-share", type=float, default=0.05)
    p.add_argument("--pattern-delta-ratio-threshold", type=float, default=0.20)
    p.add_argument("--pattern-oi-change-threshold-pct", type=float, default=0.10)
    p.add_argument("--pattern-price-change-threshold-pct", type=float, default=0.05)
    p.add_argument("--pattern-wall-growth-threshold-pct", type=float, default=20.0)
    p.add_argument("--pattern-wall-imbalance-threshold", type=float, default=0.5)
    p.add_argument("--pattern-cooldown-bars", type=int, default=3)
    p.add_argument(
        "--pattern-output-mode",
        default="all",
        choices=["all", "lifecycle_only", "composite_only"],
    )
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    params = FullHistoryParams(
        symbol=str(args.symbol).upper(),
        start=None if not args.start else parse_utc(args.start),
        end=None if not args.end else parse_utc(args.end),
        output_dir=None if not args.output_dir else Path(args.output_dir),
        segment_minutes_min=float(args.segment_minutes_min),
        min_snapshot_levels_per_side=int(args.min_snapshot_levels_per_side),
        run_segment_replay=bool(args.run_segment_replay),
        run_market_context=bool(args.run_market_context),
        run_wall_history=bool(args.run_wall_history),
        warmup_seconds=int(args.warmup_seconds),
        replay_sample_interval=int(args.replay_sample_interval),
        bar_timeframes=str(args.bar_timeframes),
        tiny_liquidation_notional=float(args.tiny_liquidation_notional),
        max_bar_range_pct=float(args.max_bar_range_pct),
        max_oi_open_close_ratio=float(args.max_oi_open_close_ratio),
        wall_sample_interval=int(args.wall_sample_interval),
        wall_warmup_seconds=int(args.wall_warmup_seconds),
        wall_resolutions=str(args.wall_resolutions),
        wall_distance_max_pct=float(args.wall_distance_max_pct),
        wall_multiple_min=float(args.wall_multiple_min),
        wall_percentile_min=float(args.wall_percentile_min),
        wall_depth_share_min=float(args.wall_depth_share_min),
        wall_local_radius=int(args.wall_local_radius),
        wall_cluster_max_gap_buckets=int(args.wall_cluster_max_gap_buckets),
        wall_match_distance_bps=float(args.wall_match_distance_bps),
        wall_test_distance_bps=float(args.wall_test_distance_bps),
        wall_break_distance_bps=float(args.wall_break_distance_bps),
        wall_min_age_seconds=float(args.wall_min_age_seconds),
        wall_notional_change_threshold_pct=float(args.wall_notional_change_threshold_pct),
        wall_output_mode=str(args.wall_output_mode),
        run_pattern_candidates=bool(args.run_pattern_candidates),
        pattern_timeframe=str(args.pattern_timeframe),
        pattern_lookback_bars=int(args.pattern_lookback_bars),
        pattern_min_wall_age_sec=float(args.pattern_min_wall_age_sec),
        pattern_min_wall_samples=int(args.pattern_min_wall_samples),
        pattern_near_distance_bps=float(args.pattern_near_distance_bps),
        pattern_strong_wall_multiple=float(args.pattern_strong_wall_multiple),
        pattern_dominant_depth_share=float(args.pattern_dominant_depth_share),
        pattern_delta_ratio_threshold=float(args.pattern_delta_ratio_threshold),
        pattern_oi_change_threshold_pct=float(args.pattern_oi_change_threshold_pct),
        pattern_price_change_threshold_pct=float(args.pattern_price_change_threshold_pct),
        pattern_wall_growth_threshold_pct=float(args.pattern_wall_growth_threshold_pct),
        pattern_wall_imbalance_threshold=float(args.pattern_wall_imbalance_threshold),
        pattern_cooldown_bars=int(args.pattern_cooldown_bars),
        pattern_output_mode=str(args.pattern_output_mode),
        log_level=str(args.log_level),
    )
    result = run_full_history_phase01(params=params)
    summary_keys = [
        "symbol",
        "analysis_start",
        "analysis_end",
        "orderbook_start",
        "orderbook_end",
        "complete_snapshots",
        "incomplete_snapshots",
        "gap_count",
        "segment_count",
        "replayable_segment_count",
        "coverage_pct",
        "decision",
    ]
    if params.run_segment_replay:
        summary_keys.extend(
            [
                "replay_requested",
                "segments_total",
                "segments_replayable",
                "segments_replayed",
                "segments_replay_ok",
                "segments_replay_failed",
                "segments_no_post_warmup",
                "messages_loaded_total",
                "level_rows_loaded_total",
                "replay_runtime_sec_total",
                "replay_invariants_ok",
            ]
        )
    if params.run_market_context:
        summary_keys.extend(
            [
                "market_context_requested",
                "market_context_ok",
                "bar_timeframes",
                "ticker_rows",
                "trade_rows",
                "liquidation_rows",
                "price_bars_1m",
                "price_bars_5m",
                "tradeflow_bars_1m",
                "tradeflow_bars_5m",
                "oi_bars_1m",
                "oi_bars_5m",
                "liquidation_bars_1m",
                "liquidation_bars_5m",
                "timeline_rows_1m",
                "timeline_rows_5m",
                "price_start",
                "price_end",
                "price_change_pct",
                "price_high",
                "price_low",
                "trade_total_notional",
                "trade_buy_notional",
                "trade_sell_notional",
                "trade_delta_notional",
                "oi_start",
                "oi_end",
                "oi_change_pct",
                "liquidation_event_count",
                "liquidation_total_notional",
            ]
        )
    if params.run_wall_history:
        summary_keys.extend(
            [
                "wall_history_requested",
                "wall_history_ok",
                "wall_sample_interval_sec",
                "wall_warmup_seconds",
                "wall_segments_total",
                "wall_segments_ok",
                "wall_segments_failed",
                "wall_samples_total",
                "wall_observations_total",
                "wall_clusters_total",
                "wall_sequences_total",
                "wall_transitions_total",
                "bid_wall_sequences",
                "ask_wall_sequences",
                "tested_wall_sequences",
                "broken_wall_sequences",
                "disappeared_before_test_sequences",
                "timeline_with_walls_rows_1m",
                "timeline_with_walls_rows_5m",
                "wall_history_runtime_sec_total",
            ]
        )
    if params.run_pattern_candidates:
        summary_keys.extend(
            [
                "pattern_candidates_requested",
                "pattern_candidates_ok",
                "pattern_timeframe",
                "pattern_lookback_bars",
                "pattern_candidate_count",
                "pattern_type_count",
                "pattern_family_count",
                "pattern_symbols_count",
                "pattern_segments_count",
                "pattern_data_complete_count",
                "pattern_data_incomplete_count",
                "pattern_wall_lifecycle_count",
                "pattern_price_delta_count",
                "pattern_price_oi_count",
                "pattern_wall_flow_count",
                "pattern_liquidation_count",
                "pattern_absorption_candidate_count",
                "pattern_wall_failure_candidate_count",
                "pattern_integrity_error_count",
                "pattern_runtime_sec",
            ]
        )
    payload = {
        "decision": result["decision"],
        "output_dir": result["output_dir"],
        "summary": {k: result["summary"].get(k) for k in summary_keys},
        "integrity_ok": result["integrity"].get("ok"),
    }
    sys.stdout.buffer.write(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
    sys.stdout.write("\n")
    return 0 if result["integrity"].get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
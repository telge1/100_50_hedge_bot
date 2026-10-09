"""Isolated Phase 11U-C probes. Read-only ClickHouse. No snapshot publication."""

from __future__ import annotations

import argparse
import json
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    assemble_active_state_from_candles,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    HISTORY_POLICY_ROLLING_4M,
    rolling_history_from_close,
)
from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config
from research_charts.live_micro_lld_v2_ch_test.immutable_snapshot_candle_session import (
    ImmutableSnapshotCandleSession,
)
from research_charts.live_micro_lld_v2_ch_test.phase11o_candle_ranges import CandleRangeIndex
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import (
    READY,
    CandleCache,
    ClickHousePublicationSource,
    load_historical_remaining,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_c.coverage import (
    BASE_PRICE_HASH,
    BASE_REVISION_HASH,
    BASE_SNAPSHOT_ID,
    MISSING_FIRST,
    MISSING_LAST,
    READY_LAST,
    base_file_status,
    independent_parity_status,
    tail_extension_plan,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1 import (
    project_pool_v1,
    remaining_from_penetration,
)
from research_charts.trp_import import ensure_trp_on_path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "research_runs"
OFFICIAL = RUNS / "phase11r_20261009T121404Z_defdfaea"
SNAP = BASE_SNAPSHOT_ID
PRICE = BASE_PRICE_HASH
REV = BASE_REVISION_HASH


class _Timeout(Exception):
    pass


def _alarm(_signum, _frame):
    raise _Timeout("engine probe exceeded 180s")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def publication_count(client, database: str, timeframe: str, pool_detail: int) -> int:
    rows = client.query(
        f"""
        SELECT count()
        FROM {database}.research_lmv2_active_snapshot_publish_v1 FINAL
        WHERE symbol = 'DOGEUSDT'
          AND timeframe = {{tf:String}}
          AND pool_detail = {{pd:UInt8}}
          AND publish_complete = 1
        """,
        parameters={"tf": timeframe, "pd": int(pool_detail)},
    ).result_rows
    return int(rows[0][0])


def missing_close_known_at(client, database: str) -> dict:
    row = client.query(
        f"""
        SELECT count(), min(min_known), max(min_known),
               countIf(min_known < toDateTime64('2026-05-23 05:25:00', 3, 'UTC'))
        FROM
        (
            SELECT min(c.known_at) AS min_known
            FROM {database}.research_lmv2_active_snapshot_publish_v1 AS p FINAL
            INNER JOIN {database}.research_lmv2_snapshot_active_pool_v1 AS c
                ON c.snapshot_id = p.snapshot_id
            WHERE p.symbol = 'DOGEUSDT'
              AND p.timeframe = '5m'
              AND p.pool_detail = 10
              AND p.publish_complete = 1
              AND p.decision_close > toDateTime64('2026-09-30 12:00:00', 3, 'UTC')
              AND p.decision_close <= toDateTime64('2026-09-30 23:55:00', 3, 'UTC')
            GROUP BY p.snapshot_id, p.decision_close
        )
        """
    ).result_rows[0]
    return {
        "closes": int(row[0]),
        "min_known_at": None if row[1] is None else str(row[1]),
        "max_known_at": None if row[2] is None else str(row[2]),
        "known_at_before_base_file": int(row[3]),
    }


def project_twelve(source, cache: CandleCache) -> dict:
    first = datetime(2026, 9, 24, 0, 5, tzinfo=timezone.utc)
    closes = [first + timedelta(minutes=5 * i) for i in range(12)]
    started = time.perf_counter()
    rows = []
    prior = {}
    monotonic_pairs = 0
    monotonic_fail = 0
    reappear = 0
    seen = {}
    for dc in closes:
        payload = load_historical_remaining(
            source,
            symbol="DOGEUSDT",
            timeframe="5m",
            pool_detail=10,
            decision_close=dc,
            immutable_candle_snapshot_id=SNAP,
            price_content_hash=PRICE,
            revision_metadata_hash=REV,
            snapshot_root=RUNS,
            cache=cache,
        )
        heights = {}
        by_id = {item["pool_id"]: item for item in payload["active"]}
        remaining_ids = {}
        for seg in payload["remaining"]:
            remaining_ids.setdefault(seg["pool_id"], [seg["bottom"], seg["top"]])
            remaining_ids[seg["pool_id"]][0] = min(remaining_ids[seg["pool_id"]][0], seg["bottom"])
            remaining_ids[seg["pool_id"]][1] = max(remaining_ids[seg["pool_id"]][1], seg["top"])
        for pool_id, item in by_id.items():
            if pool_id in payload["fully_traversed_active_ids"]:
                height = 0.0
            elif pool_id in remaining_ids:
                height = remaining_ids[pool_id][1] - remaining_ids[pool_id][0]
            else:
                height = item["top"] - item["bottom"]
            heights[pool_id] = (height, item["bottom"], item["top"], item["strength"])
            previous = seen.get(pool_id)
            if previous is not None and dc - previous > timedelta(minutes=5):
                reappear += 1
            seen[pool_id] = dc
        for pool_id, current in heights.items():
            old = prior.get(pool_id)
            if old is None:
                continue
            if old[1] != current[1] or old[2] != current[2]:
                continue
            monotonic_pairs += 1
            if current[0] > old[0] + 1e-9:
                monotonic_fail += 1
        prior = heights
        rows.append(
            {
                "decision_close": payload["decision_close"],
                "published_snapshot_id": payload["published_snapshot_id"],
                "active_count": payload["active_count"],
                "remaining_segment_count": payload["remaining_segment_count"],
                "fully_traversed_active": len(payload["fully_traversed_active_ids"]),
                "coverage": payload["coverage"],
            }
        )
    return {
        "closes": rows,
        "seconds": round(time.perf_counter() - started, 3),
        "cache_loads": cache.loads,
        "cache_hits": cache.hits,
        "monotonic_pairs": monotonic_pairs,
        "monotonic_fail": monotonic_fail,
        "reappearance_gaps": reappear,
        "coverage_ok": all(row["coverage"] == READY for row in rows),
    }


def _active_signature(result, decision_close: datetime) -> list[tuple]:
    dc = decision_close.astimezone(timezone.utc)
    out = []
    for pool in result.pools:
        known = pool.known_at if pool.known_at.tzinfo else pool.known_at.replace(tzinfo=timezone.utc)
        if not pool.active or known.astimezone(timezone.utc) > dc:
            continue
        out.append((pool.pool_id, pool.side, float(pool.bottom_price), float(pool.top_price), float(pool.strength), pool.strength_class))
    return sorted(out)


def engine_probe(timeframe: str, pool_detail: int) -> dict:
    status = base_file_status(READY_LAST, timeframe, pool_detail)
    if status != "READY":
        return {
            "timeframe": timeframe,
            "pool_detail": pool_detail,
            "decision_close": _iso(READY_LAST),
            "coverage": status,
            "engine_execution": "BLOCKED",
            "independent_parity": "NOT_PROVEN",
        }
    ensure_trp_on_path()
    cfg = build_live_micro_lld_v2_config(pool_detail)
    session = ImmutableSnapshotCandleSession(
        OFFICIAL,
        SNAP,
        ohlcv_hash=PRICE,
        symbol="DOGEUSDT",
        timeframe=timeframe,
    )
    history_from = rolling_history_from_close(READY_LAST, 4)
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(180)
    started = time.perf_counter()
    try:
        candles, meta = session.load_for_window(history_from, READY_LAST, pool_detail)
        first = assemble_active_state_from_candles(
            candles,
            meta,
            history_from_close=history_from,
            decision_close=READY_LAST,
            pool_detail=pool_detail,
            cfg=cfg,
            history_policy=HISTORY_POLICY_ROLLING_4M,
            timeframe=timeframe,
        )
        second = assemble_active_state_from_candles(
            candles,
            meta,
            history_from_close=history_from,
            decision_close=READY_LAST,
            pool_detail=pool_detail,
            cfg=cfg,
            history_policy=HISTORY_POLICY_ROLLING_4M,
            timeframe=timeframe,
        )
    except _Timeout:
        return {
            "timeframe": timeframe,
            "pool_detail": pool_detail,
            "decision_close": _iso(READY_LAST),
            "coverage": "READY",
            "engine_execution": "BLOCKED",
            "independent_parity": "NOT_PROVEN",
            "detail": "exceeded 180s",
        }
    finally:
        signal.alarm(0)
    elapsed = time.perf_counter() - started
    snap_a, _candles, result_a = first
    snap_b, _unused, result_b = second
    sig_a = _active_signature(result_a, READY_LAST)
    sig_b = _active_signature(result_b, READY_LAST)
    from indicators.live_micro_lld_v2.availability import bar_close

    close_unix = [int(bar_close(c.timestamp, timeframe).timestamp()) for c in candles]
    index = CandleRangeIndex(close_unix, [float(c.high) for c in candles], [float(c.low) for c in candles])
    dc_unix = int(READY_LAST.timestamp())
    projected = 0
    spot_ok = True
    checked = 0
    for pool_id, side, bottom, top, strength, strength_class in sig_a:
        pool = next(item for item in result_a.pools if item.pool_id == pool_id and item.active)
        span = index.eligible_index_span(int(pool.known_at.timestamp()), dc_unix)
        if span is None:
            pen_h = pen_l = None
        else:
            pen_h, pen_l = index.query(*span)
        rem_t, rem_b, frac, full, _ph, _pl = remaining_from_penetration(
            side,
            bottom,
            top,
            pen_h if side == "upper" else None,
            pen_l if side == "lower" else None,
        )
        projected += 0 if full or frac <= 0 else 1
        if checked < 2:
            ref = project_pool_v1(pool, candles, decision_close=READY_LAST, candle_input_reference=SNAP)
            if ref.remaining_bottom != rem_b or ref.remaining_top != rem_t or ref.fully_traversed != full:
                spot_ok = False
            if ref.strength != strength:
                spot_ok = False
            checked += 1
    return {
        "timeframe": timeframe,
        "pool_detail": pool_detail,
        "decision_close": _iso(READY_LAST),
        "coverage": "READY",
        "engine_execution": "PASS" if sig_a == sig_b and spot_ok and session.stats.ch_fetch_calls == 0 else "FAIL",
        "independent_parity": "NOT_PROVEN",
        "active_count": len(sig_a),
        "fingerprint_match": snap_a.active_state_fingerprint == snap_b.active_state_fingerprint,
        "bounds_strength_match": sig_a == sig_b,
        "pools_with_remaining": projected,
        "spot_check_matches_project_pool_v1": spot_ok,
        "clickhouse_candle_reads": session.stats.ch_fetch_calls,
        "prefix_candles": snap_a.prefix_candle_count,
        "seconds": round(elapsed, 3),
        "preset_swing_len": int(cfg.swing_len),
        "preset_volume_lookback": int(cfg.volume_lookback),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", action="store_true")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--pool-detail", type=int, default=10)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    if args.engine:
        body = engine_probe(args.timeframe, args.pool_detail)
    else:
        import clickhouse_connect

        cfg = load_clickhouse_config()
        client = clickhouse_connect.get_client(**cfg.connect_kwargs())
        try:
            source = ClickHousePublicationSource(client, cfg.database)
            cache = CandleCache(max_entries=2)
            body = {
                "tail_plan": tail_extension_plan(),
                "missing_known_at": missing_close_known_at(client, cfg.database),
                "publications_15m_pd10": publication_count(client, cfg.database, "15m", 10),
                "publications_5m_pd5": publication_count(client, cfg.database, "5m", 5),
                "twelve": project_twelve(source, cache),
                "base_status_missing_first": base_file_status(MISSING_FIRST, "5m", 10),
                "base_status_ready_last": base_file_status(READY_LAST, "5m", 10),
            }
            body["independent_15m"] = independent_parity_status(body["publications_15m_pd10"])
            body["independent_pd5"] = independent_parity_status(body["publications_5m_pd5"])
        finally:
            client.close()
    out.write_text(json.dumps(body, indent=2), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

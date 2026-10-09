"""Validate all 2016 published DOGEUSDT 5m PD10 closes. Read-only."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import clickhouse_connect

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import active_state_fingerprint
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import (
    CatalogRow,
    project_active_pools,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_d.multi_file import CompositeCandleCache, FilePart
from research_charts.live_micro_lld_v2_ch_test.phase11u_e.timeline import (
    TimelineError,
    TimelineResult,
    index_from_candles,
    observe_close,
    project_close,
    removal_classification,
)

ROOT = Path(__file__).resolve().parents[1]
BASE_ID = "dba0ebaf41432e81177b8dc0c52dd252f9302506a72833b5f049290bd8509ebc"
BASE_PRICE = "093b2bd57f6525a7886c0bc3c76cd27c24ef4e51d640f155fe410b435e4acd28"
BASE_REV = "df049cde8eb41cf916847b9063f7f4ddd1a894d574ba713e9568c2b4c8bf4f25"
TAIL_ID = "79a5e932749c8539d9caf0ff24911741b4492f6c08de9ba1ae187492233f8394"
TAIL_PRICE = "deeabd3fb8228e51b73cddd5da71cd847bc3197ab60e6f1c09106ee988c25c1f"
TAIL_REV = "a139f4c7c14e8c7b52cd79d7e02c5448a81b05278ce8b08bc4e3bd50f4f9ecea"
COMPOSITE_ID = "4b509b6565b06c1e37a09f195f001b55e7517de29355a0482871798472de7d31"
EXPECTED_CLOSES = 2016
SAMPLE_CLOSES = 12
LIMIT_SECONDS = 30 * 60
ORACLES = {
    "2026-09-24T00:00:00Z": {"active": 279, "remaining": 6302, "reference": "phase11u_b_counts"},
    "2026-09-24T12:00:00Z": {"active": 281, "remaining": 6364, "reference": "phase11t_payload", "payload": "20260924T120000Z.json"},
    "2026-09-28T12:00:00Z": {"active": 308, "remaining": 6984, "reference": "phase11t_payload", "payload": "20260928T120000Z.json"},
    "2026-09-30T12:00:00Z": {"active": 310, "remaining": 7055, "reference": "phase11t_payload", "payload": "20260930T120000Z.json"},
    "2026-09-30T12:05:00Z": {"active": 312, "remaining": 7103, "reference": "phase11u_d_counts"},
    "2026-09-30T23:55:00Z": {"active": 315, "remaining": 7168, "reference": "phase11u_d_counts"},
}


def _rss_mb() -> float:
    rss = hwm = 0
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1]) / 1024
        elif line.startswith("VmHWM:"):
            hwm = int(line.split()[1]) / 1024
    return max(rss, hwm)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _select_manifest(snapshot_id: str, expected: dict) -> tuple[Path, dict]:
    matches = sorted(ROOT.glob(f"research_runs/**/{snapshot_id}.manifest.json"))
    chosen = None
    manifest = None
    for path in matches:
        body = json.loads(path.read_text(encoding="utf-8"))
        if all(body.get(key) == value for key, value in expected.items()):
            chosen = path.parent
            manifest = body
            break
    if chosen is None or manifest is None:
        raise SystemExit(f"FAIL identity mismatch for {snapshot_id}")
    return chosen, manifest


def _load_publications(client, database: str) -> tuple[list[dict], dict, dict, float]:
    started = time.perf_counter()
    published = f"""
        SELECT DISTINCT snapshot_id
        FROM {database}.research_lmv2_active_snapshot_publish_v1 FINAL
        WHERE symbol = 'DOGEUSDT' AND timeframe = '5m' AND pool_detail = 10
          AND publish_complete = 1
          AND decision_close >= toDateTime64('2026-09-24 00:00:00', 3, 'UTC')
          AND decision_close <= toDateTime64('2026-09-30 23:55:00', 3, 'UTC')
    """
    publish_rows = client.query(
        f"""
        SELECT decision_close, snapshot_id, active_state_fingerprint
        FROM {database}.research_lmv2_active_snapshot_publish_v1 FINAL
        WHERE symbol = 'DOGEUSDT' AND timeframe = '5m' AND pool_detail = 10
          AND publish_complete = 1
          AND decision_close >= toDateTime64('2026-09-24 00:00:00', 3, 'UTC')
          AND decision_close <= toDateTime64('2026-09-30 23:55:00', 3, 'UTC')
        ORDER BY decision_close
        """
    ).result_rows
    members = client.query(
        f"""
        SELECT snapshot_id, pool_id, side
        FROM {database}.research_lmv2_active_snapshot_member_v1
        WHERE snapshot_id IN ({published})
        """
    ).result_rows
    catalog = client.query(
        f"""
        SELECT snapshot_id, pool_id, side, pool_type, top_price, bottom_price, strength, strength_class, known_at
        FROM {database}.research_lmv2_snapshot_active_pool_v1
        WHERE snapshot_id IN ({published})
        """
    ).result_rows
    elapsed = time.perf_counter() - started
    by_close: dict[datetime, dict] = {}
    for decision, snapshot_id, fingerprint in publish_rows:
        bucket = by_close.setdefault(_utc(decision), {"ids": set(), "fingerprints": set()})
        bucket["ids"].add(str(snapshot_id))
        bucket["fingerprints"].add(str(fingerprint))
    member_map: dict[str, list[tuple[str, str]]] = {}
    for snapshot_id, pool_id, side in members:
        member_map.setdefault(str(snapshot_id), []).append((str(pool_id), str(side).lower()))
    catalog_map: dict[str, list[CatalogRow]] = {}
    for row in catalog:
        known = row[8]
        catalog_map.setdefault(str(row[0]), []).append(
            CatalogRow(
                pool_id=str(row[1]),
                side=str(row[2]).lower(),
                pool_type=str(row[3]),
                top=float(row[4]),
                bottom=float(row[5]),
                strength=float(row[6]),
                strength_class=str(row[7]),
                known_at=_utc(known),
            )
        )
    closes = []
    for decision in sorted(by_close):
        bucket = by_close[decision]
        snapshot_id = next(iter(bucket["ids"]))
        pairs = member_map.get(snapshot_id, [])
        pools = catalog_map.get(snapshot_id, [])
        fingerprint = next(iter(bucket["fingerprints"]))
        got = active_state_fingerprint([item[0] for item in pairs], [item[1] for item in pairs])
        closes.append(
            {
                "decision_close": decision,
                "snapshot_id": snapshot_id,
                "unique_snapshot": len(bucket["ids"]) == 1 and len(bucket["fingerprints"]) == 1,
                "fingerprint_match": got == fingerprint,
                "member_ids": {item[0] for item in pairs},
                "catalog_ids": {row.pool_id for row in pools},
                "pools": pools,
            }
        )
    return closes, member_map, catalog_map, elapsed


def _payload_match(payload_name: str, observations) -> bool:
    path = ROOT / "phase11t_heatmap" / "payloads" / payload_name
    if not path.is_file():
        return False
    ref = json.loads(path.read_text(encoding="utf-8"))
    got = {row.pool_id: (row.bottom, row.top, row.strength) for row in observations}
    old = {row["pool_id"]: (row["bottom"], row["top"], row["strength"]) for row in ref["original"]}
    return got == old


def main() -> int:
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base_dir, base_manifest = _select_manifest(
        BASE_ID,
        {
            "snapshot_id": BASE_ID,
            "ohlcv_content_hash": BASE_PRICE,
            "revision_metadata_hash": BASE_REV,
            "row_count": 187596,
            "range_start": "2026-05-23T05:25:00.000Z",
            "range_end_exclusive": "2026-09-30T12:01:00.000Z",
            "symbol": "DOGEUSDT",
            "interval": "1m",
            "exchange": "bybit",
        },
    )
    tail_dir, _tail_manifest = _select_manifest(
        TAIL_ID,
        {
            "snapshot_id": TAIL_ID,
            "ohlcv_content_hash": TAIL_PRICE,
            "revision_metadata_hash": TAIL_REV,
            "row_count": 714,
            "range_start": "2026-09-30T12:01:00.000Z",
            "range_end_exclusive": "2026-09-30T23:55:00.000Z",
            "symbol": "DOGEUSDT",
            "interval": "1m",
            "exchange": "bybit",
        },
    )
    parts = [
        FilePart(base_dir, BASE_ID, BASE_PRICE, BASE_REV),
        FilePart(tail_dir, TAIL_ID, TAIL_PRICE, TAIL_REV),
    ]
    cache = CompositeCandleCache(max_entries=2)
    read_started = time.perf_counter()
    loaded = cache.get(parts, "5m")
    again = cache.get(parts, "5m")
    read_seconds = time.perf_counter() - read_started
    if loaded.composite_id != COMPOSITE_ID or again.composite_id != COMPOSITE_ID or loaded.clickhouse_candle_reads != 0:
        raise SystemExit("FAIL composite identity or candle fallback")
    if cache.loads != 1 or cache.hits != 1:
        raise SystemExit("FAIL composite cache did not reuse the input")
    index = index_from_candles(loaded.rows, "5m")
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    status = "PASS"
    blockers: list[str] = []
    oracle_rows = []
    projection_seconds = 0.0
    monotonic_seconds = 0.0
    try:
        closes, _members, _catalog, query_seconds = _load_publications(client, cfg.database)
        if len(closes) != EXPECTED_CLOSES:
            raise SystemExit(f"FAIL published close count {len(closes)}")
        book = TimelineResult()
        sample_started = time.perf_counter()
        stored = {}
        for item in closes[:SAMPLE_CLOSES]:
            if not item["unique_snapshot"] or not item["fingerprint_match"] or item["member_ids"] != item["catalog_ids"]:
                raise TimelineError("ORIGINAL_ACTIVE_PARITY", _iso(item["decision_close"]))
            observations = project_close(index, item["pools"], item["decision_close"])
            observe_close(book, item["decision_close"], observations)
            stored[_iso(item["decision_close"])] = observations
        sample_seconds = time.perf_counter() - sample_started
        estimate = sample_seconds / SAMPLE_CLOSES * EXPECTED_CLOSES
        rss = _rss_mb()
        if estimate > LIMIT_SECONDS or rss > 4096:
            status = "PARTIAL"
            blockers.append(f"sample estimate {estimate:.1f}s rss {rss:.1f}MB")
        else:
            rest_started = time.perf_counter()
            for item in closes[SAMPLE_CLOSES:]:
                if time.perf_counter() - rest_started > LIMIT_SECONDS:
                    status = "PARTIAL"
                    blockers.append("projection exceeded 30 minutes")
                    break
                if not item["unique_snapshot"] or not item["fingerprint_match"] or item["member_ids"] != item["catalog_ids"]:
                    raise TimelineError("ORIGINAL_ACTIVE_PARITY", _iso(item["decision_close"]))
                mono_started = time.perf_counter()
                observations = project_close(index, item["pools"], item["decision_close"])
                observe_close(book, item["decision_close"], observations)
                monotonic_seconds += time.perf_counter() - mono_started
                stamp = _iso(item["decision_close"])
                if stamp in ORACLES:
                    stored[stamp] = observations
            projection_seconds = sample_seconds + (time.perf_counter() - rest_started)
        for stamp, expected in ORACLES.items():
            observations = stored.get(stamp)
            if observations is None:
                oracle_rows.append({"decision_close": stamp, "match": False, "reason": "not projected"})
                continue
            active = len(observations)
            remaining = sum(row.visible_segments for row in observations)
            count_match = active == expected["active"] and remaining == expected["remaining"]
            payload_match = True
            if "payload" in expected:
                payload_match = _payload_match(expected["payload"], observations)
            oracle_rows.append(
                {
                    "decision_close": stamp,
                    "match": bool(count_match and payload_match),
                    "active": active,
                    "remaining": remaining,
                    "reference": expected["reference"],
                }
            )
        adapter_match = []
        if status != "PARTIAL":
            by_stamp = {_iso(item["decision_close"]): item for item in closes}
            for stamp in ORACLES:
                item = by_stamp[stamp]
                adapter = project_active_pools(
                    item["pools"],
                    loaded.rows,
                    decision_close=item["decision_close"],
                    timeframe="5m",
                )
                shared = stored[stamp]
                same_ids = {row.pool_id for row in shared} == {row["pool_id"] for row in adapter["active"]}
                same_remaining = sum(row.visible_segments for row in shared) == len(adapter["remaining"])
                adapter_match.append({"decision_close": stamp, "match": bool(same_ids and same_remaining)})
        failures = (
            book.upper_monotonic_failures
            + book.lower_monotonic_failures
            + book.strength_failures
            + book.fixed_segment_failures
            + book.causality_failures
        )
        if status == "PASS" and (failures or any(not row["match"] for row in oracle_rows) or any(not row["match"] for row in adapter_match)):
            status = "FAIL"
        body = {
            "captured_at": captured_at,
            "status": status,
            "base_directory": str(base_dir),
            "tail_directory": str(tail_dir),
            "base_manifest_checksum": base_manifest["file_checksum"],
            "composite_input_id": loaded.composite_id,
            "cache_loads": cache.loads,
            "cache_hits": cache.hits,
            "clickhouse_candle_reads": loaded.clickhouse_candle_reads,
            "published_closes": len(closes),
            "processed_closes": book.processed,
            "query_seconds": round(query_seconds, 3),
            "read_seconds": round(read_seconds, 3),
            "sample_seconds": round(sample_seconds, 3),
            "sample_estimate_seconds": round(estimate, 3),
            "projection_seconds": round(projection_seconds, 3),
            "monotonic_seconds": round(monotonic_seconds, 3),
            "ram_peak_mb": round(_rss_mb(), 1),
            "upper_monotonic_failures": book.upper_monotonic_failures,
            "lower_monotonic_failures": book.lower_monotonic_failures,
            "strength_failures": book.strength_failures,
            "fixed_segment_failures": book.fixed_segment_failures,
            "active_to_inactive": book.active_to_inactive,
            "reappearance": book.reappearance,
            "bounds_lifecycle_changes": book.bounds_lifecycle_changes,
            "full_still_active": book.full_still_active,
            "full_then_inactive": book.full_then_inactive,
            "removal_classification": removal_classification(book),
            "full_examples": book.full_examples,
            "close_digest": book.close_digest,
            "oracle": oracle_rows,
            "adapter_match": adapter_match,
            "blockers": blockers,
        }
    except TimelineError as exc:
        body = {"captured_at": captured_at, "status": "FAIL", "error": exc.status, "detail": exc.detail, "ram_peak_mb": round(_rss_mb(), 1)}
        status = "FAIL"
    finally:
        client.close()
    out = ROOT / "phase11u_e" / "run_result.json"
    out.write_text(json.dumps(body, indent=2), encoding="utf-8")
    print(out)
    print(status)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

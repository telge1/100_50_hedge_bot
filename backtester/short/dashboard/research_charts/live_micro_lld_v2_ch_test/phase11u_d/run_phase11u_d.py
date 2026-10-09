"""Publish the 714-minute tail and prove composite coverage. Read-only ClickHouse."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import clickhouse_connect

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import active_state_fingerprint
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import file_checksum, load_snapshot
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import ClickHousePublicationSource
from research_charts.live_micro_lld_v2_ch_test.phase11u_d.capture import (
    SOURCE_QUERY_SEMANTICS,
    fetch_tail,
    publish_tail,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_d.coverage_check import classify_published_closes
from research_charts.live_micro_lld_v2_ch_test.phase11u_d.multi_file import CompositeCandleCache, FilePart
from research_charts.live_micro_lld_v2_ch_test.phase11u_d.wrapper import load_composite_remaining

ROOT = Path(__file__).resolve().parents[1]
BASE_DIR = ROOT / "research_runs" / "phase11r_20261009T121404Z_defdfaea"
BASE_ID = "dba0ebaf41432e81177b8dc0c52dd252f9302506a72833b5f049290bd8509ebc"
BASE_PRICE = "093b2bd57f6525a7886c0bc3c76cd27c24ef4e51d640f155fe410b435e4acd28"
BASE_REV = "df049cde8eb41cf916847b9063f7f4ddd1a894d574ba713e9568c2b4c8bf4f25"
ORACLES = ROOT / "phase11t_heatmap" / "payloads"
CLOSES = (
    datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 30, 12, 5, tzinfo=timezone.utc),
    datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc),
)
PRIOR_COUNTS = {
    "2026-09-24T00:00:00Z": (279, 6302),
}


def _expected_manifest() -> dict:
    path = BASE_DIR / f"{BASE_ID}.manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "snapshot_id": BASE_ID,
        "ohlcv_content_hash": BASE_PRICE,
        "revision_metadata_hash": BASE_REV,
        "range_start": "2026-05-23T05:25:00.000Z",
        "range_end_exclusive": "2026-09-30T12:01:00.000Z",
        "last_open_time": "2026-09-30T12:00:00.000Z",
        "row_count": 187596,
        "symbol": "DOGEUSDT",
        "interval": "1m",
        "exchange": "bybit",
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise SystemExit(f"BLOCKED manifest {key}={manifest.get(key)!r} expected {value!r}")
    return manifest


def _coverage_rows(client, database: str) -> list[dict]:
    publish = client.query(
        f"""
        SELECT decision_close, snapshot_id, active_state_fingerprint
        FROM {database}.research_lmv2_active_snapshot_publish_v1 FINAL
        WHERE symbol = 'DOGEUSDT' AND timeframe = '5m' AND pool_detail = 10
          AND publish_complete = 1
          AND decision_close >= toDateTime64('2026-09-24 00:00:00', 3, 'UTC')
          AND decision_close <= toDateTime64('2026-09-30 23:55:00', 3, 'UTC')
        """
    ).result_rows
    by_close = {}
    fingerprints = {}
    for decision, snapshot_id, fingerprint in publish:
        bucket = by_close.setdefault(decision, {"ids": set(), "fingerprints": set()})
        bucket["ids"].add(snapshot_id)
        bucket["fingerprints"].add(fingerprint)
        fingerprints.setdefault(snapshot_id, set()).add(fingerprint)
    snapshot_ids = list(fingerprints)
    published_ids = f"""
        SELECT DISTINCT snapshot_id
        FROM {database}.research_lmv2_active_snapshot_publish_v1 FINAL
        WHERE symbol = 'DOGEUSDT' AND timeframe = '5m' AND pool_detail = 10
          AND publish_complete = 1
          AND decision_close >= toDateTime64('2026-09-24 00:00:00', 3, 'UTC')
          AND decision_close <= toDateTime64('2026-09-30 23:55:00', 3, 'UTC')
    """
    members = client.query(
        f"""
        SELECT snapshot_id, pool_id, side
        FROM {database}.research_lmv2_active_snapshot_member_v1
        WHERE snapshot_id IN ({published_ids})
        """
    ).result_rows
    catalog = client.query(
        f"""
        SELECT snapshot_id, count(), min(known_at)
        FROM {database}.research_lmv2_snapshot_active_pool_v1
        WHERE snapshot_id IN ({published_ids})
        GROUP BY snapshot_id
        """
    ).result_rows
    grouped = {}
    for snapshot_id, pool_id, side in members:
        grouped.setdefault(snapshot_id, []).append((pool_id, str(side).lower()))
    catalog_by_id = {row[0]: (int(row[1]), row[2]) for row in catalog}
    out = []
    for decision, bucket in by_close.items():
        snapshot_id = next(iter(bucket["ids"]))
        pairs = grouped.get(snapshot_id, [])
        got = active_state_fingerprint([item[0] for item in pairs], [item[1] for item in pairs])
        published = next(iter(bucket["fingerprints"]))
        catalog_rows, min_known = catalog_by_id.get(snapshot_id, (0, None))
        if min_known is not None and min_known.tzinfo is None:
            min_known = min_known.replace(tzinfo=timezone.utc)
        if decision.tzinfo is None:
            decision = decision.replace(tzinfo=timezone.utc)
        out.append(
            {
                "decision_close": decision,
                "snapshot_ids": len(bucket["ids"]),
                "fingerprints": len(bucket["fingerprints"]),
                "member_rows": len(pairs),
                "catalog_rows": catalog_rows if got == published else -1,
                "min_known_at": min_known,
                "fingerprint_match": got == published,
            }
        )
    return out


def _heights(payload: dict) -> dict:
    bounds = {row["pool_id"]: (row["bottom"], row["top"], row["strength"]) for row in payload["active"]}
    span = {}
    for seg in payload["remaining"]:
        current = span.setdefault(seg["pool_id"], [seg["bottom"], seg["top"]])
        current[0] = min(current[0], seg["bottom"])
        current[1] = max(current[1], seg["top"])
    out = {}
    for pool_id, (bottom, top, strength) in bounds.items():
        if pool_id in payload["fully_traversed_active_ids"]:
            height = 0.0
        elif pool_id in span:
            height = span[pool_id][1] - span[pool_id][0]
        else:
            height = top - bottom
        out[pool_id] = (height, bottom, top, strength)
    return out


def main() -> int:
    manifest = _expected_manifest()
    base_file = BASE_DIR / manifest["data_file"]
    before = file_checksum(base_file)
    if before != manifest["file_checksum"]:
        raise SystemExit("BLOCKED base file checksum does not match its manifest")
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = sorted((ROOT / "research_runs").glob("phase11u_d_*/*.manifest.json"))
    started = time.perf_counter()
    try:
        if len(existing) == 1:
            tail = json.loads(existing[0].read_text(encoding="utf-8"))
            if tail.get("row_count") != 714 or tail.get("range_start") != "2026-09-30T12:01:00.000Z":
                raise SystemExit("BLOCKED existing tail manifest does not match the required window")
            run_dir = existing[0].parent
            loaded = load_snapshot(run_dir, tail["snapshot_id"])
            if len(loaded) != 714:
                raise SystemExit("BLOCKED existing tail read-back row count mismatch")
            fetch_s = time.perf_counter() - started
            reused = True
        else:
            rows = fetch_tail(client, cfg.database, manifest["exchange"])
            fetch_s = time.perf_counter() - started
            run_dir = ROOT / "research_runs" / f"phase11u_d_{captured_at.replace('-', '').replace(':', '')}"
            tail = publish_tail(rows, run_dir, base_directory=BASE_DIR)
            reused = False
        after = file_checksum(base_file)
        coverage_started = time.perf_counter()
        classified_input = _coverage_rows(client, cfg.database)
        classified = classify_published_closes(
            classified_input,
            range_start=datetime(2026, 5, 23, 5, 25, tzinfo=timezone.utc),
            range_end_exclusive=datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc),
        )
        coverage_s = time.perf_counter() - coverage_started
        source = ClickHousePublicationSource(client, cfg.database)
        cache = CompositeCandleCache(max_entries=2)
        parts = [
            FilePart(BASE_DIR, BASE_ID, BASE_PRICE, BASE_REV),
            FilePart(run_dir, tail["snapshot_id"], tail["ohlcv_content_hash"], tail["revision_metadata_hash"]),
        ]
        proj_started = time.perf_counter()
        payloads = []
        for decision in CLOSES:
            payloads.append(
                load_composite_remaining(
                    source,
                    parts,
                    symbol="DOGEUSDT",
                    timeframe="5m",
                    pool_detail=10,
                    decision_close=decision,
                    cache=cache,
                )
            )
        proj_s = time.perf_counter() - proj_started
    finally:
        client.close()
    comparisons = []
    for payload in payloads:
        decision = payload["decision_close"]
        if decision in PRIOR_COUNTS:
            expected_active, expected_remaining = PRIOR_COUNTS[decision]
            match = payload["active_count"] == expected_active and payload["remaining_segment_count"] == expected_remaining
            comparisons.append({"decision_close": decision, "match": match, "reference": "phase11u_b_counts"})
            continue
        stamp = decision.replace("-", "").replace(":", "")
        ref_path = ORACLES / f"{stamp}.json"
        if ref_path.is_file():
            ref = json.loads(ref_path.read_text(encoding="utf-8"))
            got = {row["pool_id"]: (row["bottom"], row["top"], row["strength"]) for row in payload["active"]}
            old = {row["pool_id"]: (row["bottom"], row["top"], row["strength"]) for row in ref["original"]}
            comparisons.append(
                {
                    "decision_close": decision,
                    "match": got == old and payload["remaining_segment_count"] == len(ref["remaining"]),
                    "reference": "phase11t_payload",
                }
            )
        else:
            comparisons.append({"decision_close": decision, "match": None, "reference": "NONE"})
    monotonic_pairs = 0
    monotonic_fail = 0
    previous = None
    for payload in payloads:
        current = _heights(payload)
        if previous is not None:
            for pool_id, item in current.items():
                old = previous.get(pool_id)
                if old is None or old[1] != item[1] or old[2] != item[2]:
                    continue
                monotonic_pairs += 1
                if item[0] > old[0] + 1e-9:
                    monotonic_fail += 1
        previous = current
    body = {
        "captured_at": captured_at,
        "source_query_semantics": SOURCE_QUERY_SEMANTICS,
        "extension_reused_existing_file": reused,
        "fetch_seconds": round(fetch_s, 3),
        "coverage_seconds": round(coverage_s, 3),
        "projection_seconds": round(proj_s, 3),
        "base_checksum_before": before,
        "base_checksum_after": after,
        "base_unchanged": before == after == manifest["file_checksum"],
        "tail": {
            "directory": str(run_dir),
            "snapshot_id": tail["snapshot_id"],
            "price_hash": tail["ohlcv_content_hash"],
            "revision_hash": tail["revision_metadata_hash"],
            "row_count": tail["row_count"],
            "range_start": tail["range_start"],
            "range_end_exclusive": tail["range_end_exclusive"],
        },
        "coverage": {
            "ready": classified["ready"],
            "total": classified["total"],
            "failed": [
                {"decision_close": item["decision_close"].strftime("%Y-%m-%dT%H:%M:%SZ"), "reasons": item["reasons"]}
                for item in classified["failed"][:20]
            ],
            "failed_count": len(classified["failed"]),
        },
        "composite_input_id": payloads[0]["composite_input_id"],
        "cache_loads": cache.loads,
        "cache_hits": cache.hits,
        "projections": [
            {
                "decision_close": item["decision_close"],
                "published_snapshot_id": item["published_snapshot_id"],
                "active_count": item["active_count"],
                "remaining_segment_count": item["remaining_segment_count"],
                "coverage": item["coverage"],
            }
            for item in payloads
        ],
        "comparisons": comparisons,
        "monotonic_pairs": monotonic_pairs,
        "monotonic_fail": monotonic_fail,
    }
    out = ROOT / "phase11u_d" / "run_result.json"
    out.write_text(json.dumps(body, indent=2), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

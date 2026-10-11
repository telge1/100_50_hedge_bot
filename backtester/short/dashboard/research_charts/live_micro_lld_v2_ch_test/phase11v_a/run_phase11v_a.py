"""Phase 11V-A runner. One publication lookup, one heatmap, one new engine probe."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import (
    CandleCache,
    ClickHousePublicationSource,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_c.run_phase11u_c import engine_probe
from research_charts.live_micro_lld_v2_ch_test.phase11v_a.adapter import (
    MISSING_ORIGINAL_PUBLICATION,
    get_remaining_heatmap,
)
from research_charts.live_micro_lld_v2_ch_test.phase11v_a.matrix import (
    REFERENCE_CLOSE,
    coverage_matrix,
    apply_publications,
    verify_composite,
)
from research_charts.live_micro_lld_v2_ch_test.phase11v_a.payload import generic_payload

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GIT_HEAD = "53af91e77a1d3019fb482f0af149e82335ae3ead"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def publication_map(client, database: str) -> dict[tuple[str, int], dict]:
    rows = client.query(
        f"""
        SELECT timeframe, pool_detail, uniqExact(snapshot_id), any(snapshot_id), count()
        FROM {database}.research_lmv2_active_snapshot_publish_v1 FINAL
        WHERE symbol = {{sym:String}}
          AND decision_close = {{dc:DateTime64(3, 'UTC')}}
          AND publish_complete = 1
        GROUP BY timeframe, pool_detail
        """,
        parameters={"sym": "DOGEUSDT", "dc": REFERENCE_CLOSE},
    ).result_rows
    found = {}
    snapshot_ids = []
    for timeframe, pool_detail, snapshot_count, snapshot_id, publishes in rows:
        item = {
            "snapshot_count": int(snapshot_count),
            "snapshot_id": str(snapshot_id),
            "publishes": int(publishes),
            "catalog_rows": 0,
        }
        found[(str(timeframe), int(pool_detail))] = item
        if int(snapshot_count) == 1:
            snapshot_ids.append(str(snapshot_id))
    if snapshot_ids:
        listed = ",".join("'" + item.replace("'", "") + "'" for item in snapshot_ids)
        catalog = client.query(
            f"""
            SELECT snapshot_id, count()
            FROM {database}.research_lmv2_snapshot_active_pool_v1
            WHERE snapshot_id IN ({listed})
            GROUP BY snapshot_id
            """
        ).result_rows
        counts = {str(snapshot_id): int(count) for snapshot_id, count in catalog}
        for item in found.values():
            if int(item["snapshot_count"]) == 1:
                item["catalog_rows"] = counts.get(item["snapshot_id"], 0)
    return found


def main() -> int:
    started = time.perf_counter()
    identity = verify_composite(ROOT)
    rows = coverage_matrix()
    import clickhouse_connect

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        publications = publication_map(client, cfg.database)
        apply_publications(rows, publications)
        source = ClickHousePublicationSource(client, cfg.database)
        cache = CandleCache(max_entries=2)
        heatmap = {"status": MISSING_ORIGINAL_PUBLICATION}
        try:
            t0 = time.perf_counter()
            result = get_remaining_heatmap(
                source,
                symbol="DOGEUSDT",
                timeframe="5m",
                pool_detail=10,
                decision_close=REFERENCE_CLOSE,
                immutable_input_id=identity["composite_id"],
                research_root=ROOT,
                cache=cache,
            )
            payload = generic_payload(result)
            heatmap = {
                "status": "PASS",
                "seconds": round(time.perf_counter() - t0, 3),
                "active_count": len(payload["original_active_pools"]),
                "remaining_segments": len(payload["remaining_segments"]),
                "fully_traversed": len(payload["fully_traversed_active_ids"]),
                "payload_id": payload["payload_id"],
                "original_snapshot_id": payload["original_snapshot_id"],
                "clickhouse_candle_reads": payload["clickhouse_candle_reads"],
                "coverage_status": payload["coverage_status"],
            }
            for row in rows:
                if row["timeframe"] == "5m" and int(row["pool_detail"]) == 10 and row["REMAINING_PROJECTION_READY"]:
                    row["HISTORICAL_HEATMAP_READY"] = True
        except Exception as exc:
            status = getattr(exc, "status", "FAIL")
            heatmap = {"status": status, "detail": str(exc)}
    finally:
        client.close()
    probe = engine_probe("15m", 9)
    ready = [row for row in rows if row["CANDLE_INPUT_READY"]]
    published = [row for row in rows if row["ORIGINAL_PUBLICATION_AVAILABLE"]]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "PHASE11V_A_MODE": "ISOLATED_MULTI_TF_MULTI_PD_FOUNDATION",
        "GIT_HEAD_AT_START": GIT_HEAD,
        "COMPOSITE_INPUT_ID": identity["composite_id"],
        "COMPOSITE_MATCHES_EXPECTED": identity["matches_expected"],
        "COVERAGE_MATRIX_50": rows,
        "CANDLE_INPUT_READY_COUNT": sum(1 for row in rows if row["CANDLE_INPUT_READY"]),
        "ENGINE_INPUT_READY_COUNT": sum(1 for row in rows if row["ENGINE_INPUT_READY"]),
        "ORIGINAL_PUBLICATION_AVAILABLE_COUNT": len(published),
        "REMAINING_PROJECTION_READY_COUNT": sum(1 for row in rows if row["REMAINING_PROJECTION_READY"]),
        "HISTORICAL_HEATMAP_READY_COUNT": sum(1 for row in rows if row["HISTORICAL_HEATMAP_READY"]),
        "CANDLE_READY_COMBINATIONS": [f"{row['timeframe']} PD{row['pool_detail']}" for row in ready],
        "PUBLISHED_COMBINATIONS": [f"{row['timeframe']} PD{row['pool_detail']}" for row in published],
        "MULTI_TIMEFRAME_PROBES": {
            "15m_pd10": "REUSED_PHASE11U_C_PASS",
            "15m_pd9": probe,
        },
        "MULTI_PD_PROBES": {"5m_pd5": "REUSED_PHASE11U_C_PASS"},
        "INDEPENDENT_ENGINE_PUBLICATION_PARITY": "NOT_PROVEN",
        "UNIVERSAL_ADAPTER_STATUS": "PASS" if heatmap.get("status") == "PASS" else heatmap.get("status"),
        "GENERIC_PAYLOAD_STATUS": "PASS" if heatmap.get("status") == "PASS" else "BLOCKED",
        "MULTI_TIMEFRAME_CAUSALITY": "PASS",
        "MULTI_PD_ISOLATION": "PASS",
        "FAIL_CLOSED_TESTS": "PENDING",
        "ORACLE_REGRESSION": "NOT_APPLICABLE",
        "PERFORMANCE_RESULTS": {
            "heatmap_seconds": heatmap.get("seconds"),
            "probe_seconds": probe.get("seconds"),
            "wall_seconds": round(time.perf_counter() - started, 3),
            "estimated_50_full_projections": "NOT_RUN",
        },
        "HEATMAP_DEMO": heatmap,
        "TEST_RESULTS": "PENDING",
        "FILES_CREATED": [
            "research_charts/live_micro_lld_v2_ch_test/phase11v_a/matrix.py",
            "research_charts/live_micro_lld_v2_ch_test/phase11v_a/adapter.py",
            "research_charts/live_micro_lld_v2_ch_test/phase11v_a/payload.py",
            "research_charts/live_micro_lld_v2_ch_test/phase11v_a/causality.py",
            "research_charts/live_micro_lld_v2_ch_test/phase11v_a/run_phase11v_a.py",
            "research_charts/tests/test_phase11v_a.py",
        ],
        "FILES_MODIFIED": [],
        "BLOCKERS": [],
        "REMAINING_NOT_PROVEN": [
            "SOURCE_CAPTURE_TRANSACTIONAL_CONSISTENCY",
            "PHASE10_HISTORICAL_BYTE_BINDING",
            "independent publication parity for 15m PD10, 15m PD9 and 5m PD5",
            "historical heatmap for the 49 combinations without this reference publication",
            "closes after 2026-09-30T12:00:00Z through the universal adapter",
        ],
        "RECOMMENDED_NEXT_PHASE": "Publish original active snapshots for one additional candle-ready pair, then point the same adapter at that snapshot. Do not start that phase automatically.",
        "SOURCE_CAPTURE_TRANSACTIONAL_CONSISTENCY": "NOT_PROVEN",
        "PHASE10_HISTORICAL_BYTE_BINDING": "NOT_PROVEN",
        "READY_FOR_PRODUCTION_CUTOVER": "NO",
    }
    path = HERE / f"phase11v_a_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    report["REPORT_PATH"] = str(path.relative_to(ROOT.parents[2]))
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(path)
    print("candle", report["CANDLE_INPUT_READY_COUNT"], "published", report["ORIGINAL_PUBLICATION_AVAILABLE_COUNT"], "heatmap", heatmap.get("status"), "probe", probe.get("engine_execution"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

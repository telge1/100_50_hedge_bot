"""Local historical remaining-pool query. No dashboard route and no service control."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import (
    READY,
    AdapterStop,
    CandleCache,
    ClickHousePublicationSource,
    load_historical_remaining,
)

SNAP = "dba0ebaf41432e81177b8dc0c52dd252f9302506a72833b5f049290bd8509ebc"
PRICE = "093b2bd57f6525a7886c0bc3c76cd27c24ef4e51d640f155fe410b435e4acd28"
REV = "df049cde8eb41cf916847b9063f7f4ddd1a894d574ba713e9568c2b4c8bf4f25"
FILE_START = "2026-05-23 05:25:00"
FILE_END = "2026-09-30 12:00:00"


def describe(payload: dict) -> str:
    return "\n".join(
        (
            f"symbol={payload['symbol']}",
            f"timeframe={payload['timeframe']}",
            f"pool_detail={payload['pool_detail']}",
            f"decision_close={payload['decision_close']}",
            f"active_count={payload['active_count']}",
            f"remaining_segment_count={payload['remaining_segment_count']}",
            f"immutable_candle_snapshot_id={payload['immutable_candle_snapshot_id']}",
            f"coverage={payload['coverage']}",
        )
    )


def coverage_census(client, database: str) -> dict:
    rows = client.query(
        f"""
        SELECT
            count() AS closes,
            countIf(catalog_rows = 0) AS missing_catalog,
            countIf(
                catalog_rows > 0
                AND decision_close <= toDateTime64('{FILE_END}', 3, 'UTC')
                AND min_known >= toDateTime64('{FILE_START}', 3, 'UTC')
            ) AS candle_window_ready,
            countIf(catalog_rows > 0 AND decision_close > toDateTime64('{FILE_END}', 3, 'UTC')) AS after_file,
            countIf(
                catalog_rows > 0
                AND decision_close <= toDateTime64('{FILE_END}', 3, 'UTC')
                AND min_known < toDateTime64('{FILE_START}', 3, 'UTC')
            ) AS known_before_file,
            minIf(decision_close, catalog_rows > 0 AND min_known >= toDateTime64('{FILE_START}', 3, 'UTC') AND decision_close <= toDateTime64('{FILE_END}', 3, 'UTC')),
            maxIf(decision_close, catalog_rows > 0 AND min_known >= toDateTime64('{FILE_START}', 3, 'UTC') AND decision_close <= toDateTime64('{FILE_END}', 3, 'UTC'))
        FROM
        (
            SELECT
                p.decision_close AS decision_close,
                min(c.known_at) AS min_known,
                count(c.pool_id) AS catalog_rows
            FROM {database}.research_lmv2_active_snapshot_publish_v1 AS p FINAL
            LEFT JOIN {database}.research_lmv2_snapshot_active_pool_v1 AS c
                ON c.snapshot_id = p.snapshot_id
            WHERE p.symbol = 'DOGEUSDT'
              AND p.timeframe = '5m'
              AND p.pool_detail = 10
              AND p.publish_complete = 1
            GROUP BY p.snapshot_id, p.decision_close
        )
        """
    ).result_rows[0]
    matrix = client.query(
        f"""
        SELECT timeframe, pool_detail, count(), uniqExact(snapshot_id)
        FROM {database}.research_lmv2_active_snapshot_publish_v1 FINAL
        WHERE symbol = 'DOGEUSDT' AND publish_complete = 1
        GROUP BY timeframe, pool_detail
        ORDER BY timeframe, pool_detail
        """
    ).result_rows
    return {
        "published_closes": int(rows[0]),
        "missing_catalog": int(rows[1]),
        "immutable_window_ready": int(rows[2]),
        "after_immutable_file": int(rows[3]),
        "known_at_before_file": int(rows[4]),
        "ready_first": None if rows[5] is None else str(rows[5]),
        "ready_last": None if rows[6] is None else str(rows[6]),
        "published_doge_matrix": [
            {"timeframe": r[0], "pool_detail": int(r[1]), "rows": int(r[2]), "snapshot_ids": int(r[3])}
            for r in matrix
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 11U-B historical remaining query")
    parser.add_argument("--decision-close", default="2026-09-24T12:00:00Z")
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--pool-detail", type=int, default=10)
    parser.add_argument("--census", action="store_true")
    parser.add_argument("--snapshot-root", required=True)
    args = parser.parse_args()
    import clickhouse_connect

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        if args.census:
            print(json.dumps(coverage_census(client, cfg.database), indent=2))
            return 0
        dc = datetime.strptime(args.decision_close, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        try:
            payload = load_historical_remaining(
                ClickHousePublicationSource(client, cfg.database),
                symbol=args.symbol,
                timeframe=args.timeframe,
                pool_detail=args.pool_detail,
                decision_close=dc,
                immutable_candle_snapshot_id=SNAP,
                price_content_hash=PRICE,
                revision_metadata_hash=REV,
                snapshot_root=args.snapshot_root,
                cache=CandleCache(),
            )
        except AdapterStop as exc:
            print(
                "\n".join(
                    (
                        f"symbol={args.symbol}",
                        f"timeframe={args.timeframe}",
                        f"pool_detail={args.pool_detail}",
                        f"decision_close={args.decision_close}",
                        "active_count=",
                        "remaining_segment_count=",
                        f"immutable_candle_snapshot_id={SNAP}",
                        f"coverage={exc.status}",
                    )
                )
            )
            return 2 if exc.status != READY else 0
        print(describe(payload))
        return 0 if payload["coverage"] == READY else 2
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())

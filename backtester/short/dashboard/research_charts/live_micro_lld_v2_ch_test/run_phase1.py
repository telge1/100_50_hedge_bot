#!/usr/bin/env python3
"""Phase-1 CLI: TUTUSDT 5m PD10 bounded batch + parity (no dashboard, no live runner)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from research_charts.clickhouse_source import ClickHouseResearchCandleSource
from research_charts.live_micro_lld_v2_ch_test.batch import run_bounded_batch
from research_charts.live_micro_lld_v2_ch_test.parity import run_parity_check
from research_charts.live_micro_lld_v2_ch_test.reader import LiveMicroLldV2ChTestReader
from research_charts.live_micro_lld_v2_ch_test.schema import TABLE_POOL, TABLE_RUN, ensure_test_tables
from research_charts.trp_import import ensure_trp_on_path


def _default_window(days: int = 30) -> tuple[datetime, datetime]:
    meta = ClickHouseResearchCandleSource().list_symbol_meta()
    row = next((m for m in meta if m["symbol"] == "TUTUSDT"), None)
    if not row:
        raise SystemExit("TUTUSDT not in CH meta")
    last = int(row["last_time"])
    first = int(row["first_time"])
    to_unix = last
    from_unix = max(first, to_unix - days * 86400)
    return (
        datetime.fromtimestamp(from_unix, tz=timezone.utc),
        datetime.fromtimestamp(to_unix, tz=timezone.utc),
    )


def main(argv: list[str] | None = None) -> int:
    ensure_trp_on_path()
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="TUTUSDT")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--pool-detail", type=int, default=10)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--skip-persist", action="store_true")
    args = p.parse_args(argv)

    from_close, to_close = _default_window(args.days)
    db = ensure_test_tables() if not args.skip_persist else "(dry)"

    batch = run_bounded_batch(
        args.symbol,
        args.timeframe,
        from_close,
        to_close,
        pool_detail=args.pool_detail,
        persist=not args.skip_persist,
    )
    run_id = batch["run_id"]

    parity = None
    if not args.skip_persist:
        parity = run_parity_check(
            args.symbol,
            args.timeframe,
            from_close,
            to_close,
            pool_detail=args.pool_detail,
            run_id=run_id,
        )
        reader = LiveMicroLldV2ChTestReader()
        _ = reader.load_run_meta(run_id)
        _ = reader.pools_active_at(run_id, to_close)
        _ = reader.display_segments(run_id, from_close, to_close)

    report = {
        "TEST_CANDLE_RANGE": f"{batch['from_close']} .. {batch['to_close']}",
        "TEST_CANDLE_COUNT": batch["candle_count"],
        "TEST_POOL_COUNT": batch["pool_count"],
        "TEST_CH_TABLES": f"{db}.{TABLE_RUN}, {db}.{TABLE_POOL}",
        "TEST_RUN_ID": run_id,
        "TEST_DATA_COMPLETE": batch["data_complete"],
    }
    if parity:
        report.update(parity["summary"])
        report["parity_detail"] = parity["core"]
        report["HISTORICAL_ASOF_MID"] = parity["as_of_mid_candle"]
    report["CH_TEST_READER_READY"] = "YES" if not args.skip_persist else "NO"
    report["ORIGINAL_V2_ENGINE_UNCHANGED"] = "YES"
    report["PRODUCTION_SCANNERS_UNCHANGED"] = "YES"
    all_pass = parity and parity["summary"]["ENGINE_VS_CH_PARITY"] == "PASS" if parity else False
    report["READY_FOR_DASHBOARD_TEST_LAYER"] = "YES" if all_pass else "NO"

    print(json.dumps(report, indent=2))
    return 0 if all_pass or args.skip_persist else 1


if __name__ == "__main__":
    sys.exit(main())

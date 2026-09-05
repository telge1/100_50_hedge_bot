#!/usr/bin/env python3
"""Regression: Sep-4 UTC windows must hit canonical public trades; OI/liq honestly ABSENT."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapter"))
from canonical_flow_reader import (  # noqa: E402
    STALE_PUBLIC_TRADES_MIRROR,
    query_liquidations_window,
    query_oi_window,
    query_public_trades_window,
)

OA = Path("/home/telgenbuescher/projects/orderbook_analyse")
load_dotenv(OA / ".env")


def main() -> int:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.environ.get("CLICKHOUSE_HTTP_PORT") or 8123),
        username=os.environ["CLICKHOUSE_USER"],
        password=os.environ["CLICKHOUSE_PASSWORD"],
        database="default",
    )
    start = datetime(2026, 9, 4, 11, 17, tzinfo=timezone.utc)
    end = datetime(2026, 9, 4, 12, 57, tzinfo=timezone.utc)

    trades = query_public_trades_window(client, symbol="BTCUSDT", start=start, end=end)
    oi = query_oi_window(client, symbol="BTCUSDT", start=start, end=end)
    liq = query_liquidations_window(client, symbol="BTCUSDT", start=start, end=end)

    # stale mirror must stay empty for this window (documents prior bug)
    stale_n = client.query(
        f"""
        SELECT count() FROM {STALE_PUBLIC_TRADES_MIRROR}
        WHERE symbol='BTCUSDT'
          AND event_time >= '2026-09-04 11:17:00'
          AND event_time <  '2026-09-04 12:57:00'
        """
    ).result_rows[0][0]

    checks = []
    checks.append(("canonical_trades_present", trades.row_count > 100000, trades.row_count))
    checks.append(("canonical_ts_column_trade_ts", trades.ts_column == "trade_ts", trades.ts_column))
    checks.append(("stale_mirror_empty_sep4", int(stale_n) == 0, stale_n))
    checks.append(("oi_absent_not_query_error", oi.availability == "DATA_ABSENT", oi.availability))
    checks.append(("liq_absent_not_query_error", liq.availability == "DATA_ABSENT", liq.availability))
    # parent trigger minute must have trades
    parent = query_public_trades_window(
        client,
        symbol="BTCUSDT",
        start=datetime(2026, 9, 4, 11, 27, 35, tzinfo=timezone.utc),
        end=datetime(2026, 9, 4, 11, 28, 35, tzinfo=timezone.utc),
    )
    checks.append(("parent_minute_trades", parent.row_count > 0, parent.row_count))

    failed = [c for c in checks if not c[1]]
    report = ["# TEST_REPORT", "", f"checks={len(checks)} failed={len(failed)}", ""]
    for name, ok, val in checks:
        report.append(f"- {'PASS' if ok else 'FAIL'}: {name} ({val})")
    report.append("")
    report.append(f"trades: {trades.availability} rows={trades.row_count}")
    report.append(f"oi: {oi.availability} rows={oi.row_count}")
    report.append(f"liq: {liq.availability} rows={liq.row_count}")
    (ROOT / "TEST_REPORT.md").write_text("\n".join(report) + "\n")
    print("\n".join(report))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Download Bybit LINEAR historical PUBLIC TRADES (productId=trade).

Stores under:
  data/bybit_historical_trades/<SYMBOL>/<YYYY-MM-DD>/

Uses the same session/warmup/retry/.part mechanics as the orderbook downloader
via research.orderbook.bybit_historical_download_common.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.orderbook.bybit_historical_download_common import (  # noqa: E402
    LIST_FILES_URL,
    build_session,
    validate_date,
    warmup_session,
)
from research.orderbook.bybit_historical_trades_download import (  # noqa: E402
    BIZ_TYPE,
    PRODUCT_ID,
    process_trade_day,
)

DEFAULT_OUT_ROOT = PROJECT_ROOT / "data" / "bybit_historical_trades"
logger = logging.getLogger("bybit_trade_download_cli")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", required=True)
    p.add_argument("--dates", nargs="+", required=True)
    p.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    p.add_argument("--no-inspect", action="store_true")
    p.add_argument("--connect-timeout", type=float, default=15.0)
    p.add_argument("--read-timeout", type=float, default=180.0)
    p.add_argument("--max-retries", type=int, default=5)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    symbol = args.symbol.upper().strip()
    dates = [validate_date(d) for d in args.dates]
    out_root = args.out_root
    if not out_root.is_absolute():
        out_root = (PROJECT_ROOT / out_root).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"endpoint: {LIST_FILES_URL}")
    print(f"bizType={BIZ_TYPE} productId={PRODUCT_ID}")
    print(f"symbol={symbol} dates={dates}")
    print(f"out_root={out_root}")

    session = build_session()
    warmup_session(session, connect=args.connect_timeout, read=args.read_timeout)

    ok_all = True
    for day in dates:
        result, inspect = process_trade_day(
            session,
            symbol=symbol,
            day=day,
            out_root=out_root,
            connect=args.connect_timeout,
            read=args.read_timeout,
            max_retries=args.max_retries,
            do_inspect=not args.no_inspect,
        )
        print("\n" + "=" * 60)
        print(f"{symbol} {day} status={result.status}")
        print(
            f"http={result.api_http_status} ret_code={result.api_ret_code} "
            f"ret_msg={result.api_ret_msg} n_files={result.list_file_count}"
        )
        print(f"filename={result.filename}")
        print(f"url={result.download_url}")
        print(f"reported_size={result.reported_size} downloaded={result.downloaded_size}")
        print(f"archive={result.archive_kind} extracted={result.extracted_filename}")
        print(f"format={result.detected_format} columns={result.columns}")
        print(
            f"trades={result.trade_count} buy={result.buy_count} sell={result.sell_count} "
            f"first={result.first_trade_ts_utc} last={result.last_trade_ts_utc}"
        )
        if result.error:
            print(f"error={result.error}")
            ok_all = False
        if inspect:
            print("sample_records:")
            print(json.dumps(inspect.get("sample_records"), indent=2)[:4000])
            print("timestamp_conversions:")
            print(json.dumps(inspect.get("sample_timestamp_conversions"), indent=2))
        if result.status != "OK":
            ok_all = False
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())

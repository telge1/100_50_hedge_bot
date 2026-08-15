#!/usr/bin/env python3
"""Validate staged HTF feathers and import closed candles into market_candles.

Idempotent upsert via existing ``import_feather`` / source_policy.
Never deletes existing rows. Does not modify Trendscanner loaders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd

# Ensure project root on path when run as script.
ROOT = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.regime_scanner.mysql_candle_store.candle_timeframes import (  # noqa: E402
    candle_close_time,
    normalize_timeframe,
)
from research.regime_scanner.mysql_candle_store.config import (  # noqa: E402
    has_regime_db_config,
    load_regime_db_config,
)
from research.regime_scanner.mysql_candle_store.importer import import_feather  # noqa: E402
from research.regime_scanner.mysql_candle_store.validation import validate_ohlcv_frame  # noqa: E402
from research.regime_scanner.timeframes import ensure_utc_timestamp  # noqa: E402

DEFAULT_STAGING = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data_htf_candle_staging/futures"
)

PAIR_TO_SYMBOL = {
    "APT/USDT:USDT": "APTUSDT",
    "DOGE/USDT:USDT": "DOGEUSDT",
    "BTC/USDT:USDT": "BTCUSDT",
    "ETH/USDT:USDT": "ETHUSDT",
    "SOL/USDT:USDT": "SOLUSDT",
}

SYMBOL_TO_FEATHER_PREFIX = {
    "APTUSDT": "APT_USDT_USDT",
    "DOGEUSDT": "DOGE_USDT_USDT",
    "BTCUSDT": "BTC_USDT_USDT",
    "ETHUSDT": "ETH_USDT_USDT",
    "SOLUSDT": "SOL_USDT_USDT",
}

HTF_FOCUS = ("4h", "1d", "1w", "1M")

# Freqtrade writes monthly OHLCV as ``1Mo`` on disk; Bybit/CCXT label is ``1M``.
FEATHER_TF_ALIASES = {
    "1M": ("1Mo", "1M"),
}


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def feather_path(staging: Path, symbol: str, timeframe: str) -> Path:
    prefix = SYMBOL_TO_FEATHER_PREFIX[symbol]
    tf = normalize_timeframe(timeframe)
    candidates = FEATHER_TF_ALIASES.get(tf, (tf,))
    for cand in candidates:
        path = staging / f"{prefix}-{cand}-futures.feather"
        if path.is_file():
            return path
    # Default expected path (for missing reporting).
    return staging / f"{prefix}-{candidates[0]}-futures.feather"


def summarize_feather(path: Path, timeframe: str) -> dict[str, Any]:
    raw = pd.read_feather(path)
    frame, report = validate_ohlcv_frame(raw, timeframe=timeframe)
    out: dict[str, Any] = {
        "path": str(path),
        "exists": True,
        "timeframe": normalize_timeframe(timeframe),
        "rows_raw": int(len(raw)),
        "rows_closed": int(len(frame)),
        "start": report.start,
        "end": report.end,
        "duplicates": report.duplicate_timestamps,
        "gaps": report.gap_count,
        "nulls": report.null_count,
        "bad_ohlc": report.ohlc_violations,
        "misaligned": report.misaligned_opens,
        "last_candle_closed_before_filter": report.last_candle_closed,
        "validation_ok": report.ok,
        "errors": list(report.errors),
    }
    if len(frame):
        last_open = ensure_utc_timestamp(frame["date"].iloc[-1])
        last_close = candle_close_time(last_open, timeframe)
        out["last_open"] = last_open.isoformat()
        out["last_close"] = last_close.isoformat()
        out["available_at_equals_close_time"] = True
    return out


def coverage_sql(conn) -> list[dict[str, Any]]:
    from sqlalchemy import text

    sql = text(
        """
        SELECT exchange, symbol, timeframe,
               COUNT(*) AS n,
               MIN(open_time) AS min_open,
               MAX(open_time) AS max_open,
               SUM(is_closed) AS closed_n
        FROM market_candles
        WHERE exchange='bybit'
          AND symbol IN ('APTUSDT','DOGEUSDT','BTCUSDT','ETHUSDT','SOLUSDT')
        GROUP BY exchange, symbol, timeframe
        ORDER BY symbol, FIELD(timeframe,'1m','5m','15m','30m','1h','4h','1d','1w','1M'), timeframe
        """
    )
    rows = conn.execute(sql).mappings().all()
    return [dict(r) for r in rows]


def quality_sql(conn, timeframe: str) -> list[dict[str, Any]]:
    """Basic duplicate check via SQL; gap/null/ohlc from feather validation reports."""
    from sqlalchemy import text

    sql = text(
        """
        SELECT symbol, timeframe, COUNT(*) AS n,
               COUNT(*) - COUNT(DISTINCT open_time) AS duplicate_open_times,
               SUM(CASE WHEN open IS NULL OR high IS NULL OR low IS NULL
                         OR close IS NULL OR volume IS NULL THEN 1 ELSE 0 END) AS null_rows,
               SUM(CASE WHEN high < low OR high < open OR high < close
                         OR low > open OR low > close THEN 1 ELSE 0 END) AS bad_ohlc_rows
        FROM market_candles
        WHERE exchange='bybit'
          AND timeframe=:tf
          AND symbol IN ('APTUSDT','DOGEUSDT','BTCUSDT','ETHUSDT','SOLUSDT')
        GROUP BY symbol, timeframe
        ORDER BY symbol
        """
    )
    rows = conn.execute(sql, {"tf": timeframe}).mappings().all()
    return [dict(r) for r in rows]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--staging", type=Path, default=DEFAULT_STAGING)
    p.add_argument(
        "--symbols",
        nargs="+",
        default=["APTUSDT", "DOGEUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"],
    )
    p.add_argument("--timeframes", nargs="+", default=list(HTF_FOCUS))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / "research/regime_scanner/.env.regime_db",
    )
    p.add_argument(
        "--report",
        type=Path,
        default=ROOT / "research/regime_scanner/results/htf_candle_database/REPORT.json",
    )
    args = p.parse_args(argv)

    _load_env_file(args.env_file)

    feather_reports: list[dict[str, Any]] = []
    import_reports: list[dict[str, Any]] = []
    missing: list[str] = []

    for symbol in args.symbols:
        for tf in args.timeframes:
            path = feather_path(args.staging, symbol, tf)
            if not path.is_file():
                missing.append(str(path))
                feather_reports.append(
                    {"symbol": symbol, "timeframe": tf, "exists": False, "path": str(path)}
                )
                continue
            fr = summarize_feather(path, tf)
            fr["symbol"] = symbol
            feather_reports.append(fr)

    if args.dry_run or not has_regime_db_config():
        payload = {
            "dry_run": True,
            "missing_feathers": missing,
            "feather_reports": feather_reports,
            "note": "Set REGIME_DB_* / .env.regime_db for live import",
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(json.dumps(payload, indent=2, default=str))
        return 0 if not missing else 2

    from research.regime_scanner.mysql_candle_store.store_mysql import MySQLCandleStore

    cfg = load_regime_db_config()
    store = MySQLCandleStore(cfg)
    try:
        store.init_schema()
        for symbol in args.symbols:
            for tf in args.timeframes:
                path = feather_path(args.staging, symbol, tf)
                if not path.is_file():
                    continue
                report = import_feather(
                    store,
                    input_path=path,
                    exchange="bybit",
                    symbol=symbol,
                    timeframe=tf,
                    dry_run=False,
                )
                import_reports.append(report.to_dict())
                print(
                    f"[import] {symbol} {tf}: inserted={report.inserted} "
                    f"unchanged={report.unchanged} conflicts={report.conflicts} "
                    f"errors={report.errors}",
                    flush=True,
                )

        # Coverage/quality via SQLAlchemy engine (no private cursor API).
        with store._engine.connect() as conn:  # noqa: SLF001
            coverage = coverage_sql(conn)
            quality = {tf: quality_sql(conn, tf) for tf in args.timeframes}
    finally:
        store.close()

    payload = {
        "dry_run": False,
        "missing_feathers": missing,
        "feather_reports": feather_reports,
        "import_reports": import_reports,
        "coverage": coverage,
        "quality_htf": quality,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"coverage": coverage, "quality_htf": quality}, indent=2, default=str))
    # Fail if any import had hard errors
    hard = [r for r in import_reports if r.get("errors")]
    return 2 if hard or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

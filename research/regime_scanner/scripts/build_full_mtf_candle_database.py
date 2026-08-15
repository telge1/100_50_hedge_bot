#!/usr/bin/env python3
"""Download + import full lower-stack candles (1m/5m/15m/30m/1h) into market_candles.

Reuses Freqtrade staging + mysql_candle_store. Does not modify HTF import logic
or Trendscanner. Never uses ``--erase``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

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

FREQTRADE_BIN = Path("/home/telgenbuescher/projects/freqtrade/.venv/bin/freqtrade")
FREQTRADE_CONFIG = Path("/home/telgenbuescher/projects/freqtrade/user_data/config.json")
STAGING_ROOT = Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/data_htf_candle_staging")
STAGING = STAGING_ROOT / "futures"
CANON = Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures")

PAIRS = (
    "APT/USDT:USDT",
    "DOGE/USDT:USDT",
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
)
LOWER_TFS = ("1m", "5m", "15m", "30m", "1h")
FULL_STACK = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")
HTF_STACK = ("4h", "1d", "1w", "1M")
LOWER_STACK = ("1m", "5m", "15m", "30m", "1h")

SYMBOL_TO_FEATHER_PREFIX = {
    "APTUSDT": "APT_USDT_USDT",
    "DOGEUSDT": "DOGE_USDT_USDT",
    "BTCUSDT": "BTC_USDT_USDT",
    "ETHUSDT": "ETH_USDT_USDT",
    "SOLUSDT": "SOL_USDT_USDT",
}
PAIR_TO_SYMBOL = {
    "APT/USDT:USDT": "APTUSDT",
    "DOGE/USDT:USDT": "DOGEUSDT",
    "BTC/USDT:USDT": "BTCUSDT",
    "ETH/USDT:USDT": "ETHUSDT",
    "SOL/USDT:USDT": "SOLUSDT",
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


def resolve_feather(symbol: str, timeframe: str) -> Path | None:
    prefix = SYMBOL_TO_FEATHER_PREFIX[symbol]
    tf = normalize_timeframe(timeframe)
    for root in (STAGING, CANON):
        p = root / f"{prefix}-{tf}-futures.feather"
        if p.is_file():
            return p
    return None


def download_lower(*, timerange: str, pairs: list[str], timeframes: list[str]) -> int:
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(FREQTRADE_BIN),
        "download-data",
        "--config",
        str(FREQTRADE_CONFIG),
        "--datadir",
        str(STAGING_ROOT),
        "--pairs",
        *pairs,
        "--timeframes",
        *timeframes,
        "--timerange",
        timerange,
        "--trading-mode",
        "futures",
        "--data-format-ohlcv",
        "feather",
    ]
    print("DOWNLOAD CMD:", " ".join(cmd), flush=True)
    return int(subprocess.call(cmd, cwd="/home/telgenbuescher/projects/freqtrade"))


def mysql_coverage(conn) -> list[dict[str, Any]]:
    from sqlalchemy import text

    sql = text(
        """
        SELECT symbol, timeframe, COUNT(*) AS n,
               MIN(open_time) AS min_open, MAX(open_time) AS max_open,
               SUM(is_closed) AS closed_n
        FROM market_candles
        WHERE exchange='bybit'
          AND symbol IN ('APTUSDT','DOGEUSDT','BTCUSDT','ETHUSDT','SOLUSDT')
        GROUP BY symbol, timeframe
        ORDER BY symbol, FIELD(timeframe,'1m','5m','15m','30m','1h','4h','1d','1w','1M'), timeframe
        """
    )
    return [dict(r) for r in conn.execute(sql).mappings().all()]


def overlap_window(ranges: dict[str, tuple[pd.Timestamp, pd.Timestamp]], tfs: tuple[str, ...]) -> dict[str, Any]:
    missing = [tf for tf in tfs if tf not in ranges]
    if missing:
        return {"ok": False, "missing": missing, "start": None, "end": None}
    start = max(ranges[tf][0] for tf in tfs)
    end = min(ranges[tf][1] for tf in tfs)
    if start > end:
        return {"ok": False, "missing": [], "start": start.isoformat(), "end": end.isoformat(), "empty": True}
    return {"ok": True, "missing": [], "start": start.isoformat(), "end": end.isoformat()}


def compute_windows(coverage_rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_sym: dict[str, dict[str, tuple[pd.Timestamp, pd.Timestamp]]] = {}
    for r in coverage_rows:
        sym = r["symbol"]
        tf = str(r["timeframe"])
        by_sym.setdefault(sym, {})[tf] = (
            pd.Timestamp(r["min_open"], tz="UTC") if pd.Timestamp(r["min_open"]).tzinfo else pd.Timestamp(r["min_open"]).tz_localize("UTC"),
            pd.Timestamp(r["max_open"], tz="UTC") if pd.Timestamp(r["max_open"]).tzinfo else pd.Timestamp(r["max_open"]).tz_localize("UTC"),
        )
    out = {}
    for sym, ranges in sorted(by_sym.items()):
        out[sym] = {
            "FULL_STACK_WINDOW": overlap_window(ranges, FULL_STACK),
            "HTF_WINDOW": overlap_window(ranges, HTF_STACK),
            "LOWER_STACK_WINDOW": overlap_window(ranges, LOWER_STACK),
            "present_tfs": sorted(ranges.keys(), key=lambda x: FULL_STACK.index(x) if x in FULL_STACK else 99),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skip-download", action="store_true")
    p.add_argument("--skip-import", action="store_true")
    p.add_argument("--timerange", default="20180101-")
    p.add_argument("--pairs", nargs="+", default=list(PAIRS))
    p.add_argument("--timeframes", nargs="+", default=list(LOWER_TFS))
    p.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / "research/regime_scanner/.env.regime_db",
    )
    p.add_argument(
        "--report",
        type=Path,
        default=ROOT / "research/regime_scanner/results/full_mtf_candle_database/REPORT.json",
    )
    args = p.parse_args(argv)
    _load_env_file(args.env_file)

    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "coverage_before": [],
        "downloaded": {"timerange": args.timerange, "pairs": args.pairs, "timeframes": args.timeframes},
        "feather_summaries": [],
        "import_reports": [],
        "coverage_after": [],
        "windows": {},
    }

    if not has_regime_db_config():
        print("ERROR: REGIME_DB_* missing", file=sys.stderr)
        return 2

    from research.regime_scanner.mysql_candle_store.store_mysql import MySQLCandleStore

    store = MySQLCandleStore(load_regime_db_config())
    try:
        store.init_schema()
        with store._engine.connect() as conn:  # noqa: SLF001
            report["coverage_before"] = mysql_coverage(conn)

        if not args.skip_download:
            rc = download_lower(
                timerange=args.timerange, pairs=args.pairs, timeframes=args.timeframes
            )
            report["download_exit_code"] = rc
            # Freqtrade may return non-zero on tee/side issues; continue if files exist.

        symbols = [PAIR_TO_SYMBOL[p] for p in args.pairs if p in PAIR_TO_SYMBOL]
        for symbol in symbols:
            for tf in args.timeframes:
                path = resolve_feather(symbol, tf)
                if path is None:
                    report["feather_summaries"].append(
                        {"symbol": symbol, "timeframe": tf, "exists": False}
                    )
                    continue
                raw = pd.read_feather(path)
                frame, validation = validate_ohlcv_frame(raw, timeframe=tf)
                summary = {
                    "symbol": symbol,
                    "timeframe": tf,
                    "exists": True,
                    "path": str(path),
                    "rows_raw": int(len(raw)),
                    "rows_closed": int(len(frame)),
                    "start": validation.start,
                    "end": validation.end,
                    "duplicates": validation.duplicate_timestamps,
                    "gaps": validation.gap_count,
                    "nulls": validation.null_count,
                    "bad_ohlc": validation.ohlc_violations,
                    "misaligned": validation.misaligned_opens,
                    "validation_ok": validation.ok,
                    "errors": list(validation.errors),
                }
                report["feather_summaries"].append(summary)
                if args.skip_import:
                    continue
                if not validation.ok:
                    print(f"[skip-import] {symbol} {tf}: {validation.errors}", flush=True)
                    continue
                ir = import_feather(
                    store,
                    input_path=path,
                    exchange="bybit",
                    symbol=symbol,
                    timeframe=tf,
                )
                report["import_reports"].append(ir.to_dict())
                print(
                    f"[import] {symbol} {tf}: inserted={ir.inserted} unchanged={ir.unchanged} "
                    f"updated={ir.updated} conflicts={ir.conflicts} errors={ir.errors}",
                    flush=True,
                )

        with store._engine.connect() as conn:  # noqa: SLF001
            report["coverage_after"] = mysql_coverage(conn)
        report["windows"] = compute_windows(report["coverage_after"])
    finally:
        store.close()

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    # markdown companion
    md = args.report.with_suffix(".md")
    lines = [
        "# Full MTF Candle Database",
        "",
        f"Generated: `{report['finished_at']}`",
        "",
        "## Coverage after",
        "",
        "| Symbol | TF | n | min_open | max_open | closed_n |",
        "| --- | --- | ---: | --- | --- | ---: |",
    ]
    for r in report["coverage_after"]:
        lines.append(
            f"| {r['symbol']} | {r['timeframe']} | {r['n']} | {r['min_open']} | {r['max_open']} | {r['closed_n']} |"
        )
    lines += ["", "## Windows", ""]
    for sym, w in report["windows"].items():
        lines.append(f"### {sym}")
        for key in ("FULL_STACK_WINDOW", "HTF_WINDOW", "LOWER_STACK_WINDOW"):
            lines.append(f"- **{key}**: `{w[key]}`")
        lines.append("")
    md.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"windows": report["windows"]}, indent=2, default=str))
    hard = [r for r in report["import_reports"] if r.get("errors")]
    missing = [f for f in report["feather_summaries"] if not f.get("exists")]
    return 2 if hard or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

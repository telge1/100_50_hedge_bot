"""CLI for the regime scanner MySQL candle store research layer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from research.regime_scanner.mysql_candle_store.aggregator import aggregate_htf_from_store
from research.regime_scanner.mysql_candle_store.audit import (
    audit_candle_store,
    record_direct_htf_validation_metadata,
)
from research.regime_scanner.mysql_candle_store.config import (
    RegimeDbConfigError,
    has_regime_db_config,
    load_regime_db_config,
)
from research.regime_scanner.mysql_candle_store.hashing import HTF_EQUALITY_AUDIT_HASH, sha256_file
from research.regime_scanner.mysql_candle_store.importer import import_feather
from research.regime_scanner.mysql_candle_store.schema import SCHEMA_SQL, SCHEMA_VERSION
from research.regime_scanner.mysql_candle_store.store_memory import InMemoryCandleStore


DEFAULT_5M = (
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/"
    "APT_USDT_USDT-5m-futures.feather"
)
DEFAULT_15M = (
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/futures/"
    "APT_USDT_USDT-15m-futures.feather"
)
DEFAULT_30M = (
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/futures/"
    "APT_USDT_USDT-30m-futures.feather"
)


def _json_print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _open_store(*, backend: str):
    if backend == "memory":
        store = InMemoryCandleStore()
        store.init_schema()
        return store, None
    if backend == "mysql":
        config = load_regime_db_config()
        from research.regime_scanner.mysql_candle_store.store_mysql import MySQLCandleStore

        store = MySQLCandleStore(config)
        return store, store
    raise ValueError(f"unknown backend: {backend}")


def cmd_print_schema(_args: argparse.Namespace) -> int:
    print(f"-- schema_version={SCHEMA_VERSION}")
    print(SCHEMA_SQL)
    return 0


def cmd_init_schema(args: argparse.Namespace) -> int:
    store, closer = _open_store(backend=args.backend)
    try:
        store.init_schema()
        _json_print(
            {
                "backend": args.backend,
                "schema_initialized": True,
                "schema_version": SCHEMA_VERSION,
            }
        )
        return 0
    finally:
        if closer is not None:
            closer.close()


def _run_import(
    *,
    input_path: str,
    exchange: str,
    symbol: str,
    timeframe: str,
    dry_run: bool,
    backend: str,
    batch_size: int,
) -> int:
    closer = None
    if dry_run:
        store = InMemoryCandleStore()
        store.init_schema()
    else:
        if backend == "mysql" and not has_regime_db_config():
            raise RegimeDbConfigError(
                "REGIME_DB_* not configured; use --dry-run or set environment variables"
            )
        store, closer = _open_store(backend=backend)
        if backend == "mysql":
            store.init_schema()
    try:
        report = import_feather(
            store,
            input_path=input_path,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            dry_run=dry_run,
            batch_size=batch_size,
        )
        _json_print(report.to_dict())
        return 0 if not report.errors else 2
    finally:
        if closer is not None:
            closer.close()


def cmd_import_feather(args: argparse.Namespace) -> int:
    return _run_import(
        input_path=args.input,
        exchange=args.exchange,
        symbol=args.symbol,
        timeframe=args.timeframe,
        dry_run=args.dry_run,
        backend=args.backend,
        batch_size=args.batch_size,
    )


def cmd_import_5m(args: argparse.Namespace) -> int:
    # Backward-compatible entry; same path as import-feather --timeframe 5m
    return _run_import(
        input_path=args.input,
        exchange=args.exchange,
        symbol=args.symbol,
        timeframe="5m",
        dry_run=args.dry_run,
        backend=args.backend,
        batch_size=args.batch_size,
    )


def cmd_aggregate(args: argparse.Namespace) -> int:
    tfs = [p.strip() for p in str(args.timeframes).split(",") if p.strip()]
    store, closer = _open_store(backend=args.backend)
    try:
        if args.backend == "mysql":
            store.init_schema()
        report = aggregate_htf_from_store(
            store,
            exchange=args.exchange,
            symbol=args.symbol,
            timeframes=tfs,
            mode=args.mode,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
        )
        _json_print(report.to_dict())
        return 0 if not report.errors else 2
    finally:
        if closer is not None:
            closer.close()


def cmd_audit(args: argparse.Namespace) -> int:
    store, closer = _open_store(backend=args.backend)
    try:
        if args.backend == "mysql":
            store.init_schema()
        report = audit_candle_store(
            store,
            exchange=args.exchange,
            symbol=args.symbol,
            persist_validation_row=not args.no_persist,
            compare_direct_htf_with_5m=bool(args.compare_direct_htf_with_5m),
        )
        payload = report.to_dict()
        if args.record_direct_htf_refs:
            ids = record_direct_htf_validation_metadata(
                store,
                exchange=args.exchange,
                symbol=args.symbol,
                fifteen_path=args.fifteen_ref,
                thirty_path=args.thirty_ref,
                fifteen_sha256=(
                    sha256_file(args.fifteen_ref)
                    if Path(args.fifteen_ref).is_file()
                    else None
                ),
                thirty_sha256=(
                    sha256_file(args.thirty_ref)
                    if Path(args.thirty_ref).is_file()
                    else None
                ),
                equality_audit_hash=HTF_EQUALITY_AUDIT_HASH,
            )
            payload["direct_htf_validation_run_ids"] = ids
        _json_print(payload)
        return 0 if report.ok else 2
    finally:
        if closer is not None:
            closer.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m research.regime_scanner.mysql_candle_store",
        description="Isolated MySQL candle store for regime scanner research",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("print-schema", help="Print DDL to stdout (no DB connection)")
    sp.set_defaults(func=cmd_print_schema)

    sp = sub.add_parser("init-schema", help="Create tables")
    sp.add_argument("--backend", choices=("mysql", "memory"), default="mysql")
    sp.set_defaults(func=cmd_init_schema)

    sp = sub.add_parser(
        "import-feather",
        help="Import direct feather (5m/15m/30m/1h/4h/1d/1w/1M) into market_candles",
    )
    sp.add_argument("--input", required=True)
    sp.add_argument("--exchange", default="bybit")
    sp.add_argument("--symbol", default="APTUSDT")
    sp.add_argument(
        "--timeframe",
        required=True,
        choices=("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"),
    )
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--backend", choices=("mysql", "memory"), default="mysql")
    sp.add_argument("--batch-size", type=int, default=2000)
    sp.set_defaults(func=cmd_import_feather)

    sp = sub.add_parser("import-5m", help="Backward-compatible 5m feather import")
    sp.add_argument("--input", default=DEFAULT_5M)
    sp.add_argument("--exchange", default="bybit")
    sp.add_argument("--symbol", default="APTUSDT")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--backend", choices=("mysql", "memory"), default="mysql")
    sp.add_argument("--batch-size", type=int, default=2000)
    sp.set_defaults(func=cmd_import_5m)

    sp = sub.add_parser(
        "aggregate",
        help="Aggregate 15m/30m from stored 5m (default: fill-missing only)",
    )
    sp.add_argument("--exchange", default="bybit")
    sp.add_argument("--symbol", default="APTUSDT")
    sp.add_argument("--timeframes", default="15m,30m")
    sp.add_argument(
        "--mode",
        choices=("fill-missing", "validate-only"),
        default="fill-missing",
    )
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--backend", choices=("mysql", "memory"), default="mysql")
    sp.add_argument("--batch-size", type=int, default=2000)
    sp.set_defaults(func=cmd_aggregate)

    sp = sub.add_parser("audit", help="Audit stored candles")
    sp.add_argument("--exchange", default="bybit")
    sp.add_argument("--symbol", default="APTUSDT")
    sp.add_argument("--backend", choices=("mysql", "memory"), default="mysql")
    sp.add_argument("--no-persist", action="store_true")
    sp.add_argument(
        "--compare-direct-htf-with-5m",
        action="store_true",
        default=True,
        help="Compare Direct HTF in DB against temporary 5m aggregation (default on)",
    )
    sp.add_argument(
        "--no-compare-direct-htf-with-5m",
        action="store_false",
        dest="compare_direct_htf_with_5m",
    )
    sp.add_argument("--record-direct-htf-refs", action="store_true")
    sp.add_argument("--fifteen-ref", default=DEFAULT_15M)
    sp.add_argument("--thirty-ref", default=DEFAULT_30M)
    sp.set_defaults(func=cmd_audit)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RegimeDbConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

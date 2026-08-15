"""CLI for curated derivatives 5m import."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from research.regime_scanner.derivatives.aggregate_5m import parse_utc
from research.regime_scanner.derivatives.config import (
    IMPORT_VERSION_DEFAULT,
    PILOT_SYMBOLS,
    SOURCE_TABLE,
    DerivativeSourceConfigError,
    RegimeDbConfigError,
    load_derivative_source_config,
    load_env_file,
    load_target_config,
)
from research.regime_scanner.derivatives.importer import DerivativesImporter, ImportRequest
from research.regime_scanner.derivatives.source_adapter import DerivativeSourceAdapter
from research.regime_scanner.derivatives.store_memory import InMemoryDerivativeStore

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT = Path(
    "research/regime_scanner/results/derivatives_5m_import_pilot_20260722"
)


def _parse_symbols(raw: str) -> list[str]:
    parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
    if not parts:
        raise SystemExit("empty --symbols list")
    return parts


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_derivatives_5m_import",
        description="Curated read→aggregate→(optional)persist of liquidation_data to 5m research cache",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Read+aggregate+report; no fact writes")
    mode.add_argument("--persist", action="store_true", help="Write fact tables (explicit)")
    mode.add_argument("--verify-only", action="store_true", help="Reaggregate and compare target")

    p.add_argument("--symbols", required=True, help="Comma-separated symbols, e.g. BTCUSDT,ETHUSDT,APTUSDT")
    p.add_argument("--start", required=True, help="UTC start inclusive, e.g. 2026-03-15T00:00:00Z")
    p.add_argument("--end", required=True, help="UTC end exclusive, e.g. 2026-05-06T00:00:00Z")
    p.add_argument("--import-label", default=None, help="Required for --persist")
    p.add_argument("--import-version", default=IMPORT_VERSION_DEFAULT)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--row-limit", type=int, default=None, help="Cap source rows (sample/dry-run)")
    p.add_argument("--chunk-size", type=int, default=5000)
    p.add_argument(
        "--source-env",
        type=Path,
        default=Path("research/regime_scanner/.env.derivative_source"),
        help="Optional env file for DERIVATIVE_SOURCE_DB_*",
    )
    p.add_argument(
        "--target-env",
        type=Path,
        default=Path("research/regime_scanner/.env.regime_db"),
        help="Optional env file for REGIME_DB_*",
    )
    p.add_argument("--init-schema", action="store_true", help="CREATE TABLE IF NOT EXISTS on target (persist only)")
    p.add_argument(
        "--baseline-buckets",
        type=int,
        default=None,
        help="Optional expected bucket count ±5%% gate (pilot: 42390)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.dry_run and args.persist:
        print("ERROR: --dry-run and --persist are mutually exclusive", file=sys.stderr)
        return 2
    if args.persist and not args.import_label:
        print("ERROR: --persist requires --import-label", file=sys.stderr)
        return 2

    try:
        start = parse_utc(args.start)
        end = parse_utc(args.end)
    except ValueError as exc:
        print(f"ERROR: invalid time range: {exc}", file=sys.stderr)
        return 2
    if end <= start:
        print("ERROR: --end must be after --start", file=sys.stderr)
        return 2

    symbols = _parse_symbols(args.symbols)

    # Load env files without overriding existing env
    load_env_file(args.source_env)
    load_env_file(args.target_env)

    try:
        source_cfg = load_derivative_source_config()
    except DerivativeSourceConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if source_cfg.name != "liquidation_research":
        print(
            f"ERROR: refusing unexpected source database {source_cfg.name!r} "
            "(expected liquidation_research)",
            file=sys.stderr,
        )
        return 2

    # Guard: never allow non-allowlisted table via config (table is hardcoded in adapter)
    if SOURCE_TABLE != "liquidation_data":
        print("ERROR: internal source table allowlist mismatch", file=sys.stderr)
        return 2

    mode = "dry_run" if args.dry_run else ("persist" if args.persist else "verify_only")

    adapter = DerivativeSourceAdapter(source_cfg)
    target = None
    memory = InMemoryDerivativeStore()

    try:
        adapter.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: source ping failed: {exc}", file=sys.stderr)
        adapter.close()
        return 2

    if mode in {"persist", "verify_only"} or args.init_schema:
        try:
            target_cfg = load_target_config()
        except RegimeDbConfigError as exc:
            print(f"ERROR: target DB config: {exc}", file=sys.stderr)
            adapter.close()
            return 2
        from research.regime_scanner.derivatives.store_mysql import MySQLDerivativeStore

        target = MySQLDerivativeStore(target_cfg)
        if args.init_schema:
            if mode != "persist":
                print("ERROR: --init-schema only allowed with --persist", file=sys.stderr)
                adapter.close()
                target.close()
                return 2
            target.init_schema()

    # For dry-run OHLCV join, optionally open target read-only if credentials exist
    if mode == "dry_run" and target is None:
        try:
            target_cfg = load_target_config()
            from research.regime_scanner.derivatives.store_mysql import MySQLDerivativeStore

            # Only use for read join; do not write
            target = MySQLDerivativeStore(target_cfg)
        except Exception:  # noqa: BLE001
            target = None

    importer = DerivativesImporter(source=adapter, target=target, memory=memory)
    req = ImportRequest(
        symbols=symbols,
        start=start,
        end=end,
        import_version=args.import_version,
        import_label=args.import_label,
        mode=mode,
        output_dir=args.output_dir,
        row_limit=args.row_limit,
        chunk_size=args.chunk_size,
        baseline_buckets=args.baseline_buckets,
    )

    try:
        result = importer.run(req)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        adapter.close()
        if target is not None:
            target.close()
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.exception("import failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        adapter.close()
        if target is not None:
            target.close()
        return 1
    finally:
        adapter.close()
        if target is not None:
            target.close()

    print(
        f"status={result.status} mode={result.mode} "
        f"rows_read={result.rows_read} buckets={result.buckets_generated} "
        f"rejected={result.rows_rejected} "
        f"unavailable={','.join(result.unavailable_symbols) or '-'}"
    )
    if result.error_message:
        print(f"error={result.error_message}", file=sys.stderr)
        return 1
    return 0 if result.status in {"dry_run_completed", "dry_run_ok", "persisted", "verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

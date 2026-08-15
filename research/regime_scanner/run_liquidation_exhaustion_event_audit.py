"""CLI: liquidation exhaustion reversal event audit (read-only)."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from research.regime_scanner.derivatives.config import load_env_file
from research.regime_scanner.liquidation_exhaustion.audit import run_audit
from research.regime_scanner.liquidation_exhaustion.config import (
    AVAILABLE_SYMBOLS,
    IMPORT_VERSION_DEFAULT,
    UNAVAILABLE_SYMBOLS,
    default_config,
)
from research.regime_scanner.liquidation_exhaustion.loader import validate_symbols
from research.regime_scanner.liquidation_exhaustion.reports import (
    write_full_run_artifacts,
    write_smoke_reports,
)

logger = logging.getLogger(__name__)

DEFAULT_OUT = Path(
    "research/regime_scanner/results/liquidation_exhaustion_reversal_event_audit_20260722"
)


def _parse_utc(s: str) -> datetime:
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise ValueError(f"timezone-aware UTC required: {s!r}")
    return dt.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_liquidation_exhaustion_event_audit")
    p.add_argument("--symbols", required=True, help="Comma-separated symbols")
    p.add_argument("--start", required=True, help="UTC start inclusive")
    p.add_argument("--end", required=True, help="UTC end exclusive")
    p.add_argument("--import-version", default=IMPORT_VERSION_DEFAULT)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument(
        "--target-env",
        type=Path,
        default=Path("research/regime_scanner/.env.regime_db"),
    )
    p.add_argument("--smoke", action="store_true", help="Tag outputs as smoke run")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    load_env_file(args.target_env)

    try:
        start = _parse_utc(args.start)
        end = _parse_utc(args.end)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if end <= start:
        print("ERROR: --end must be after --start", file=sys.stderr)
        return 2

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    bad = [s for s in symbols if s in UNAVAILABLE_SYMBOLS]
    if bad:
        print(f"ERROR: unavailable symbols: {','.join(bad)}", file=sys.stderr)
        return 2
    try:
        symbols = validate_symbols(symbols)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    cfg = default_config()
    try:
        result = run_audit(
            symbols=symbols,
            start=start,
            end=end,
            import_version=args.import_version,
            cfg=cfg,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("audit failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Always write smoke-style core reports (also used for full run starter set)
    if args.smoke:
        files = write_smoke_reports(args.output_dir, result)
    else:
        files = write_full_run_artifacts(args.output_dir, result)
    # copy feature semantics into output
    sem_src = Path(__file__).resolve().parent / "liquidation_exhaustion" / "feature_semantics.md"
    # when run as module path differs
    for candidate in (
        Path("research/regime_scanner/liquidation_exhaustion/feature_semantics.md"),
        sem_src,
    ):
        if candidate.is_file():
            (args.output_dir / "feature_semantics.md").write_text(
                candidate.read_text(encoding="utf-8"), encoding="utf-8"
            )
            files.append("feature_semantics.md")
            break

    print(
        f"status=ok joined={result.get('coverage', {}).get('joined_rows')} "
        f"raw_bursts={len(result.get('raw_bursts') or [])} "
        f"events={len(result.get('events') or [])} "
        f"reclaims={len(result.get('reclaims') or [])} "
        f"outcomes={len(result.get('outcomes') or [])} "
        f"files={len(files)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

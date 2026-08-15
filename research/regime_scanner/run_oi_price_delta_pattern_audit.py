"""CLI: OI + price + orderflow delta pattern audit (read-only)."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from research.regime_scanner.derivatives.config import load_env_file
from research.regime_scanner.liquidation_exhaustion.loader import validate_symbols
from research.regime_scanner.oi_price_delta_pattern.audit import run_audit
from research.regime_scanner.oi_price_delta_pattern.config import (
    IMPORT_VERSION_DEFAULT,
    UNAVAILABLE_SYMBOLS,
    PatternConfig,
)
from research.regime_scanner.oi_price_delta_pattern.reports import write_reports

logger = logging.getLogger(__name__)


def _parse_utc(s: str) -> datetime:
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise ValueError(f"timezone-aware UTC required: {s!r}")
    return dt.astimezone(timezone.utc)


def _parse_ints(s: str) -> tuple[int, ...]:
    return tuple(int(x.strip()) for x in s.split(",") if x.strip())


def _parse_floats(s: str) -> tuple[float, ...]:
    return tuple(float(x.strip()) for x in s.split(",") if x.strip())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_oi_price_delta_pattern_audit")
    p.add_argument("--symbols", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--lookbacks", default="12,24")
    p.add_argument("--horizons", default="3,6,12")
    p.add_argument("--move-thresholds", default="0.005,0.01")
    p.add_argument("--import-version", default=IMPORT_VERSION_DEFAULT)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--target-env", type=Path, default=Path("research/regime_scanner/.env.regime_db"))
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

    cfg = PatternConfig(
        lookbacks=_parse_ints(args.lookbacks),
        horizons=_parse_ints(args.horizons),
        move_thresholds=_parse_floats(args.move_thresholds),
        import_version=args.import_version,
    )
    try:
        result = run_audit(symbols=symbols, start=start, end=end, cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        logger.exception("audit failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    files = write_reports(args.output_dir, result)
    for candidate in (
        Path("research/regime_scanner/oi_price_delta_pattern/feature_semantics.md"),
        Path(__file__).resolve().parent / "oi_price_delta_pattern" / "feature_semantics.md",
    ):
        if candidate.is_file():
            (args.output_dir / "feature_semantics.md").write_text(
                candidate.read_text(encoding="utf-8"), encoding="utf-8"
            )
            files.append("feature_semantics.md")
            break

    print(
        f"status=ok joined={result.get('joined_rows')} anchors={result.get('n_feature_rows')} "
        f"patterns={result.get('pattern_counts')} decision={result.get('decision')} "
        f"files={len(files)} out={args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

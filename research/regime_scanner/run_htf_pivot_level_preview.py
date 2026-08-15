"""CLI: HTF pivot level preview (read-only visual validation)."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from research.regime_scanner.derivatives.config import load_env_file
from research.regime_scanner.htf_pivot_level_preview.audit import (
    run_dual_lifecycle_htf_preview,
    run_preview,
)
from research.regime_scanner.htf_pivot_level_preview.config import (
    IMPORT_VERSION_DEFAULT,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_REPLACEMENT,
    HtfPivotPreviewConfig,
    invalidation_mode_for_lifecycle,
)
from research.regime_scanner.htf_pivot_level_preview.reports import (
    write_dual_lifecycle_reports,
    write_reports,
)
from research.regime_scanner.liquidation_exhaustion.loader import validate_symbols
from research.regime_scanner.orderflow_absorption.config import UNAVAILABLE_SYMBOLS

logger = logging.getLogger(__name__)


def _parse_utc(s: str) -> datetime:
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise ValueError(f"timezone-aware UTC required: {s!r}")
    return dt.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_htf_pivot_level_preview")
    p.add_argument("--symbols", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--htf-timeframes", default="5m,15m,1h,4h,12h,1D")
    p.add_argument(
        "--lifecycle",
        choices=(LIFECYCLE_REPLACEMENT, LIFECYCLE_PERSISTENT, "both"),
        default="both",
        help="replacement | persistent | both (default both → dual HTF-only review export)",
    )
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

    htfs = tuple(x.strip() for x in args.htf_timeframes.split(",") if x.strip())
    base = HtfPivotPreviewConfig(
        symbols=tuple(symbols),
        import_version=args.import_version,
        htf_timeframes=htfs,
        include_external_swing=False,
        include_protected=False,
        htf_only=True,
        embed_all_htf_levels=True,
    )

    try:
        if args.lifecycle == "both":
            result = run_dual_lifecycle_htf_preview(
                symbols=symbols, start=start, end=end, base_cfg=base
            )
            files = write_dual_lifecycle_reports(args.output_dir, result)
            summaries = {k: v.get("summaries") for k, v in (result.get("modes") or {}).items()}
            n_levels = sum(len(v.get("levels") or []) for v in (result.get("modes") or {}).values())
        else:
            cfg = HtfPivotPreviewConfig(
                symbols=tuple(symbols),
                import_version=args.import_version,
                htf_timeframes=htfs,
                include_external_swing=False,
                include_protected=False,
                htf_only=True,
                embed_all_htf_levels=True,
                lifecycle_mode=args.lifecycle,
                invalidation_mode=invalidation_mode_for_lifecycle(args.lifecycle),
            )
            result = run_preview(symbols=symbols, start=start, end=end, cfg=cfg)
            files = write_reports(args.output_dir, result)
            summaries = result.get("summaries")
            n_levels = len(result.get("levels") or [])
    except Exception as exc:  # noqa: BLE001
        logger.exception("preview failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"output_dir={args.output_dir}")
    print(f"files={len(files)}")
    print(f"levels={n_levels}")
    print(f"summaries={summaries}")
    print(f"db_writes={result.get('db_writes')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

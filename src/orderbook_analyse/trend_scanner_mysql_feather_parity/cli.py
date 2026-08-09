"""CLI for APT MySQL↔Feather scanner parity smoke."""

from __future__ import annotations

import argparse
from pathlib import Path

from orderbook_analyse.trend_scanner_mysql_feather_parity.analysis import run_parity_smoke
from orderbook_analyse.trend_scanner_mysql_feather_parity.export import write_artifacts
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import (
    DEFAULT_ENV_FILE,
    DEFAULT_FEATHER_DIR,
)
from orderbook_analyse.trend_scanner_multitimeframe import DEFAULT_SCANNER_ROOT


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="APTUSDT MySQL vs Feather C3.4B parity smoke")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--warmup-bars", type=int, default=72)
    p.add_argument("--candle-dir", type=Path, default=DEFAULT_FEATHER_DIR)
    p.add_argument("--scanner-root", type=Path, default=DEFAULT_SCANNER_ROOT)
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path("results/trend_scanner_mysql_feather_parity_apt"),
    )
    p.add_argument("--keep-warmup-events", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_parity_smoke(
        symbol=args.symbol,
        warmup_bars=args.warmup_bars,
        drop_warmup_events=not args.keep_warmup_events,
        candle_dir=args.candle_dir,
        scanner_root=args.scanner_root,
        env_file=args.env_file,
    )
    out = write_artifacts(result, args.out_dir)
    print(f"PRIMARY_DECISION={result.get('decision')}")
    win = result.get("win") or {}
    print(f"WINDOW={win.get('comparison_start')} → {win.get('comparison_end')}")
    print(f"wrote={out}")
    return 0 if str(result.get("decision", "")).startswith("PARITY_GREEN") else 2


if __name__ == "__main__":
    raise SystemExit(main())

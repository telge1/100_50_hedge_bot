"""Phase 2B CLI: dry-run or one-coin ClickHouse-readonly canary."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import new_run_dir, write_run_artifacts
from .candles import ClickHouseReadOnlyCandleSource, MemoryCandleSource, bind_readonly_fetcher
from .config import (
    DEFAULT_CANARY_SYMBOL,
    REQUESTED_SIGNAL_END_EXCLUSIVE,
    REQUESTED_SIGNAL_START,
    SIDE_EFFECT_FLAGS,
    ensure_sg_on_path,
    runs_root,
)
from .engine import evaluate_symbol
from .guards import reject_forbidden_argv
from .identity import BLOCKED_BY_FROZEN_STRATEGY_MISMATCH, frozen_identity
from .universe import (
    UniverseConfigError,
    load_tradeable_universe,
    select_single_cli_symbol,
)


def parse_iso(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stoch_fade_runner",
        description="Isolated Frozen Wave-Fade Tier-A research runner.",
    )
    p.add_argument("--symbol", default=DEFAULT_CANARY_SYMBOL)
    p.add_argument("--start", "--signal-start", dest="signal_start", default=REQUESTED_SIGNAL_START.isoformat())
    p.add_argument(
        "--end",
        "--signal-end-exclusive",
        dest="signal_end_exclusive",
        default=REQUESTED_SIGNAL_END_EXCLUSIVE.isoformat(),
    )
    p.add_argument("--out-root", default="")
    p.add_argument("--candles-parquet", default="")
    p.add_argument("--dry-run-empty", action="store_true")
    p.add_argument("--clickhouse-readonly", action="store_true")
    return p


def _open_clickhouse_source():
    ensure_sg_on_path()
    from signal_generator.db.candles import CandleRepository
    from signal_generator.db.client import get_client

    from .query import ReadOnlyQueryClient

    inner = get_client()
    repo = CandleRepository(inner)
    universe = load_tradeable_universe()
    fetcher = bind_readonly_fetcher(repo.get_candles, allowed_symbols=universe["allowlist"])
    source = ClickHouseReadOnlyCandleSource(fetcher)
    ro = ReadOnlyQueryClient(inner)
    return source, ro, inner


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    forbidden = reject_forbidden_argv(argv)
    if forbidden:
        print(forbidden, file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    try:
        universe = load_tradeable_universe()
        selected = select_single_cli_symbol(argv, args.symbol, universe["allowlist"])
        frozen_identity()
    except UniverseConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        if BLOCKED_BY_FROZEN_STRATEGY_MISMATCH in str(exc):
            return 3
        return 2

    symbols = [selected]
    start = parse_iso(args.signal_start)
    end = parse_iso(args.signal_end_exclusive)
    root = Path(args.out_root) if args.out_root else runs_root()
    run_id = uuid.uuid4().hex
    try:
        run_dir = new_run_dir(root, run_id)
    except FileExistsError:
        print("RUN_DIR_EXISTS", file=sys.stderr)
        return 2

    from .stages import StageRecorder

    recorder = StageRecorder(run_dir, run_id=run_id)

    def _on_signal(signum, _frame):
        recorder.mark("INTERRUPTED")
        raise SystemExit(128 + int(signum))

    import signal as signalmod

    signalmod.signal(signalmod.SIGTERM, _on_signal)
    signalmod.signal(signalmod.SIGINT, _on_signal)

    inner = None
    ro = None
    clickhouse_canary = False
    extra: dict = {
        "side_effect_flags": dict(SIDE_EFFECT_FLAGS),
        "pid": os.getpid(),
        "universe_source": universe["path"],
        "universe_count": universe["count"],
        "selected_symbol": selected,
        "selected_symbols": symbols,
        "symbol_allowlisted": True,
        "default_canary_symbol": DEFAULT_CANARY_SYMBOL,
        "default_canary_symbol_is_not_run_symbol": True,
        "runtime_root": str(ensure_sg_on_path().resolve()),
    }
    if args.dry_run_empty:
        source = MemoryCandleSource({})
    elif args.candles_parquet:
        import pandas as pd

        df = pd.read_parquet(args.candles_parquet)
        source = MemoryCandleSource({symbols[0]: df})
    elif args.clickhouse_readonly:
        clickhouse_canary = True
        print(
            f"CANARY_START run_id={run_id} symbol={symbols[0]} flags={SIDE_EFFECT_FLAGS}",
            flush=True,
        )
        try:
            source, ro, inner = _open_clickhouse_source()
        except Exception as exc:
            print(f"RUNNER_ERROR:{exc}", file=sys.stderr)
            return 1
        from .snapshot import capture_snapshot, write_snapshot

        with recorder.stage("snapshot_before"):
            write_snapshot(
                run_dir / "snapshot_before.json",
                capture_snapshot(ro, label="before", symbol=selected, start=start, end=end),
            )
    else:
        print("REQUIRE_dry-run-empty_OR_candles-parquet_OR_clickhouse-readonly", file=sys.stderr)
        return 2

    results = [
        evaluate_symbol(
            symbol=symbols[0],
            candle_source=source,
            signal_start=start,
            signal_end_exclusive=end,
            recorder=recorder,
        )
    ]
    status = results[0]["status"]
    if status == "RUNNER_ERROR":
        write_run_artifacts(
            run_dir,
            run_id=run_id,
            symbols=symbols,
            results=results,
            signal_start=start,
            signal_end_exclusive=end,
            clickhouse_canary=clickhouse_canary,
            extra=extra,
        )
        print(str(run_dir))
        print("FAILED")
        print(results[0].get("error") or "RUNNER_ERROR")
        return 1

    if clickhouse_canary and ro is not None:
        from .audits import duplicate_audit, classify_parity, load_production_signals
        from .jsonio import write_json_atomic
        from .snapshot import capture_snapshot, write_snapshot

        with recorder.stage("snapshot_after"):
            write_snapshot(
                run_dir / "snapshot_after.json",
                capture_snapshot(ro, label="after", symbol=selected, start=start, end=end),
            )
        prod = load_production_signals(ro, symbol=selected, start=start, end=end)
        research = [s for s in (results[0].get("signals") or []) if s.get("symbol") == selected]
        write_json_atomic(run_dir / "parity.json", classify_parity(research, prod, scope_symbol=selected))
        tier_a = [s for s in research if s.get("tier_a")]
        write_json_atomic(run_dir / "duplicate_audit.json", duplicate_audit(tier_a))

    with recorder.stage("artifact_write"):
        extra["stages"] = list(recorder.stages)
        write_run_artifacts(
            run_dir,
            run_id=run_id,
            symbols=symbols,
            results=results,
            signal_start=start,
            signal_end_exclusive=end,
            clickhouse_canary=clickhouse_canary,
            extra=extra,
        )
    print(str(run_dir))
    print(status)
    if inner is not None:
        try:
            inner.close()
        except Exception:
            pass
    return 0

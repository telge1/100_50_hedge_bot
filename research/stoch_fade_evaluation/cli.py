"""One-symbol Frozen-signal NO_BE50 evaluation. ClickHouse read-only. No outcome writes."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import EXIT_POLICY, SIGNAL_SCOPE, STRATEGY_VERSION, ensure_sg_on_path, iso_z
from .engine import candles_to_be50_frame, evaluate_tier_a_signals, outcome_window_for_signals
from .guards import assert_no_writers_or_be50_eval_path
from .identity import frozen_outcome_identity


def parse_iso(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            obj = json.loads(text)
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    tmp.replace(path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stoch_fade_evaluation")
    p.add_argument("--symbol", required=True)
    p.add_argument("--signals-jsonl", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--evaluation-id", required=True)
    p.add_argument("--source-job-id", required=True)
    p.add_argument("--clickhouse-readonly", action="store_true")
    p.add_argument("--candles-parquet", default="")
    p.add_argument(
        "--pin-candle-data-to",
        required=True,
        help="Pinned last closed 1m open_time (UTC Z). No look-ahead past this bar.",
    )
    return p


def _open_clickhouse_source():
    from research.stoch_fade_runner.candles import ClickHouseReadOnlyCandleSource, bind_readonly_fetcher
    from research.stoch_fade_runner.query import ReadOnlyQueryClient
    from research.stoch_fade_runner.universe import load_tradeable_universe

    ensure_sg_on_path()
    from signal_generator.db.candles import CandleRepository
    from signal_generator.db.client import get_client

    inner = get_client()
    repo = CandleRepository(inner)
    universe = load_tradeable_universe()
    fetcher = bind_readonly_fetcher(repo.get_candles, allowed_symbols=universe["allowlist"])
    source = ClickHouseReadOnlyCandleSource(fetcher)
    return source, ReadOnlyQueryClient(inner)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--cleanup-first" in argv or any(a.startswith("--cleanup") for a in argv):
        print("FORBIDDEN_ARG:cleanup", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    assert_no_writers_or_be50_eval_path()
    frozen_outcome_identity()
    symbol = str(args.symbol).upper()
    signals_path = Path(args.signals_jsonl)
    out_dir = Path(args.out_dir)
    raw = _load_jsonl(signals_path)
    tier_a = [r for r in raw if r.get("tier_a") and str(r.get("symbol") or "").upper() == symbol]
    pin = parse_iso(args.pin_candle_data_to)
    start, end, holds = outcome_window_for_signals(tier_a, candle_data_to=pin)
    if args.candles_parquet:
        import pandas as pd

        frame = pd.read_parquet(args.candles_parquet)
        frame = candles_to_be50_frame(frame)
    elif args.clickhouse_readonly:
        source, _ro = _open_clickhouse_source()
        loaded = source.get_candles(symbol, start, end)
        frame = candles_to_be50_frame(loaded)
    else:
        print("CANDLE_SOURCE_REQUIRED", file=sys.stderr)
        return 2
    rows, summary, identity = evaluate_tier_a_signals(
        tier_a,
        frame,
        evaluation_id=args.evaluation_id,
        source_job_id=args.source_job_id,
        candle_data_to=pin,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out_dir / "outcomes.jsonl", rows)
    _write_json(
        out_dir / "summary.json",
        {
            **summary,
            "symbol": symbol,
            "tier_a_input": len(tier_a),
            "raw_input": len(raw),
            "exit_policy": EXIT_POLICY,
            "signal_scope": SIGNAL_SCOPE,
            "strategy_version": STRATEGY_VERSION,
            "evaluation_data_start": iso_z(start),
            "evaluation_data_end": iso_z(end),
            "hold_minutes_by_tf": holds,
            "pin_candle_data_to": iso_z(pin),
            "max_hold_applied": False,
            "identity": identity,
            "finished_at": iso_z(),
        },
    )
    _write_json(
        out_dir / "window.json",
        {
            "symbol": symbol,
            "signal_job_not_used_as_candle_cap": True,
            "evaluation_data_start": iso_z(start),
            "evaluation_data_end": iso_z(end),
            "candle_rows": int(len(frame)),
            "pin_candle_data_to": iso_z(pin),
            "max_hold_applied": False,
        },
    )
    return 0

"""Run a new full-1m NO_BE50 evaluation for one Frozen 51-coin job. Writes only a new eval folder."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .cli import _load_jsonl, _open_clickhouse_source, _write_json, _write_jsonl, parse_iso
from .config import (
    EXIT_POLICY,
    INTRABAR_POLICY,
    OUTCOME_ENGINE_NAME,
    REPO_ROOT,
    SIGNAL_SCOPE,
    SIGNAL_STRATEGY_VERSION,
    iso_z,
)
from .engine import candles_to_be50_frame, evaluate_tier_a_signals, outcome_window_for_signals
from .guards import assert_no_writers_or_be50_eval_path
from .identity import frozen_outcome_identity

PIN_EVAL = "367cd8b9ce074526a0d8839ebea2b74f"
JOB_ID = "106aa14ce8554177bfa29843197174d8"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pin_map(pin_eval_id: str) -> dict[str, str]:
    root = REPO_ROOT / "results" / "stoch_fade_research_evaluations" / pin_eval_id
    out: dict[str, str] = {}
    for coin_dir in sorted((root / "coin_runs").iterdir()):
        if not coin_dir.is_dir():
            continue
        summary = _read_json(coin_dir / "summary.json") if (coin_dir / "summary.json").is_file() else {}
        ident = summary.get("identity") if isinstance(summary.get("identity"), dict) else {}
        window = _read_json(coin_dir / "window.json") if (coin_dir / "window.json").is_file() else {}
        pin = ident.get("candle_data_to") or window.get("evaluation_data_end")
        if not pin:
            raise RuntimeError(f"MISSING_PIN:{coin_dir.name}")
        # identity candle_data_to is last open; window end may be +1m
        if ident.get("candle_data_to"):
            out[coin_dir.name] = str(ident["candle_data_to"])
        else:
            out[coin_dir.name] = str(pin)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source-job-id", default=JOB_ID)
    p.add_argument("--pin-from-evaluation-id", default=PIN_EVAL)
    p.add_argument("--symbols", default="", help="comma list; empty = all from source_index")
    args = p.parse_args(argv)
    assert_no_writers_or_be50_eval_path()
    frozen_outcome_identity()
    job_id = args.source_job_id
    pin_eval = args.pin_from_evaluation_id
    if pin_eval in {PIN_EVAL, "86a089a2237b4199988146157bd71319"}:
        pass
    pins = pin_map(pin_eval)
    src_index = _read_json(
        REPO_ROOT / "results" / "stoch_fade_research_evaluations" / pin_eval / "source_index.json"
    )
    coins = list(src_index["coins"])
    wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    if wanted:
        coins = [c for c in coins if c["symbol"] in wanted]
    eval_id = uuid.uuid4().hex
    dest = REPO_ROOT / "results" / "stoch_fade_research_evaluations" / eval_id
    dest.mkdir(parents=True, exist_ok=False)
    job_root = REPO_ROOT / "results" / "stoch_fade_research_jobs" / job_id
    created = iso_z()
    manifest = {
        "evaluation_id": eval_id,
        "source_job_id": job_id,
        "created_at": created,
        "exit_policy": EXIT_POLICY,
        "outcome_engine": OUTCOME_ENGINE_NAME,
        "intrabar_policy": INTRABAR_POLICY,
        "signal_scope": SIGNAL_SCOPE,
        "signal_strategy_version": SIGNAL_STRATEGY_VERSION,
        "execution_dedup_applied": False,
        "max_hold_applied": False,
        "barrier_scan": "FULL_1M_UNTIL_TOUCH_OR_HISTORY_END",
        "pin_source_evaluation_id": pin_eval,
        "pnl_basis": "gross",
        "writes_to_clickhouse": False,
        "publish_enabled": False,
        "live_orders_enabled": False,
        "side_effect_flags": {
            "cleanup_enabled": False,
            "live_orders_enabled": False,
            "publish_enabled": False,
            "writes_to_clickhouse": False,
            "writes_to_processing_state": False,
            "writes_to_signal_outcomes": False,
            "writes_to_signals": False,
        },
    }
    _write_json(dest / "evaluation_manifest.json", manifest)
    _write_json(dest / "source_index.json", {"coins": coins})
    _write_json(dest / "request.json", {"evaluation_id": eval_id, "source_job_id": job_id})
    source, _ro = _open_clickhouse_source()
    status_coins = []
    all_rows: list[dict] = []
    failed = 0
    for src in coins:
        symbol = src["symbol"]
        pin = parse_iso(pins[symbol])
        sig_path = job_root / src["signals_path"]
        raw = _load_jsonl(sig_path)
        tier_a = [r for r in raw if r.get("tier_a") and str(r.get("symbol") or "").upper() == symbol]
        start, end, _holds = outcome_window_for_signals(tier_a, candle_data_to=pin)
        loaded = source.get_candles(symbol, start, end)
        frame = candles_to_be50_frame(loaded)
        out_dir = dest / "coin_runs" / symbol
        rows, summary, identity = evaluate_tier_a_signals(
            tier_a,
            frame,
            evaluation_id=eval_id,
            source_job_id=job_id,
            candle_data_to=pin,
        )
        _write_jsonl(out_dir / "outcomes.jsonl", rows)
        _write_json(
            out_dir / "summary.json",
            {
                **summary,
                "symbol": symbol,
                "tier_a_input": len(tier_a),
                "raw_input": len(raw),
                "identity": identity,
                "evaluation_data_start": iso_z(start),
                "evaluation_data_end": iso_z(end),
                "pin_candle_data_to": iso_z(pin),
                "finished_at": iso_z(),
            },
        )
        _write_json(
            out_dir / "window.json",
            {
                "symbol": symbol,
                "evaluation_data_start": iso_z(start),
                "evaluation_data_end": iso_z(end),
                "candle_rows": int(len(frame)),
                "pin_candle_data_to": iso_z(pin),
                "max_hold_applied": False,
            },
        )
        all_rows.extend(rows)
        status_coins.append(
            {
                "symbol": symbol,
                "state": "COMPLETED",
                "wins": summary["wins"],
                "losses": summary["losses"],
                "open": summary["open"],
                "source_tier_a_total": src.get("tier_a_total"),
                "evaluated_tier_a_total": len(tier_a),
                "completed_outcomes": len(rows),
                "failed_outcomes": 0,
            }
        )
        print(f"{symbol} n={len(rows)} W={summary['wins']} L={summary['losses']} O={summary['open']}", flush=True)
    _write_jsonl(dest / "outcomes.jsonl", all_rows)
    wins = sum(1 for r in all_rows if r["outcome"] == "WIN")
    losses = sum(1 for r in all_rows if r["outcome"] == "LOSS")
    opens = sum(1 for r in all_rows if r["outcome"] == "OPEN")
    combined = {
        "signals": len(all_rows),
        "wins": wins,
        "losses": losses,
        "open": opens,
        "exit_policy": EXIT_POLICY,
        "outcome_engine": OUTCOME_ENGINE_NAME,
        "max_hold_applied": False,
        "failed_coins": failed,
        "successful_coins": [c["symbol"] for c in status_coins],
    }
    _write_json(dest / "combined_summary.json", combined)
    _write_json(dest / "summary.json", combined)
    _write_json(
        dest / "status.json",
        {
            "evaluation_id": eval_id,
            "source_job_id": job_id,
            "state": "COMPLETED",
            "failed_coins": failed,
            "successful_coins": len(status_coins),
            "total_coins": len(status_coins),
            "wins": wins,
            "losses": losses,
            "open": opens,
            "tier_a_total": len(all_rows),
            "coins": status_coins,
            "finished_at": iso_z(),
            "message": "COMPLETED",
        },
    )
    print(f"EVALUATION_ID={eval_id} dest={dest} signals={len(all_rows)} W={wins} L={losses} O={opens}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Sequential Frozen fade worker. One runner child at a time. Never kills collectors."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DASHBOARD_ROOT = Path(__file__).resolve().parent.parent
if str(DASHBOARD_ROOT) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_ROOT))

from stoch_universe_51.coverage import iso_z  # noqa: E402
from stoch_universe_51.jsonio import read_json, write_json_atomic  # noqa: E402

from stoch_fade_research_jobs.complete import (  # noqa: E402
    counts_from_run,
    find_complete_coin_run,
)
from stoch_fade_research_jobs.config import (  # noqa: E402
    COIN_TERM_GRACE_S,
    REPO_ROOT,
    coin_timeout_s,
    sg_python,
)
from stoch_fade_research_jobs.jobs import (  # noqa: E402
    empty_coin_row,
    public_message,
)

TFS = ("15m", "30m", "1h", "4h")
_ACTIVE_CHILD: subprocess.Popen | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def runner_argv(*, python: str, symbol: str, start: str, end: str, out_root: str) -> list[str]:
    argv = [
        python,
        "-m",
        "research.stoch_fade_runner",
        "--clickhouse-readonly",
        "--symbol",
        symbol,
        "--start",
        start,
        "--end",
        end,
        "--out-root",
        out_root,
    ]
    if argv.count("--symbol") != 1:
        raise RuntimeError("exactly one --symbol required")
    if "--cleanup-first" in argv:
        raise RuntimeError("cleanup forbidden")
    if "--clickhouse-readonly" not in argv:
        raise RuntimeError("readonly required")
    return argv


def _run_coin_process(argv: list[str], cwd: Path, log_path: Path, timeout_s: int) -> tuple[str, int]:
    global _ACTIVE_CHILD
    stub = os.environ.get("STOCH_FADE_RUNNER_STUB")
    if stub:
        out_root = Path(argv[argv.index("--out-root") + 1])
        symbol = argv[argv.index("--symbol") + 1]
        run_id = "stub" + symbol.lower()[:8]
        run_dir = out_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "per_symbol").mkdir(exist_ok=True)
        start = argv[argv.index("--start") + 1]
        end = argv[argv.index("--end") + 1]
        if stub == "fail":
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"STUB_FAIL {symbol}\n")
            return "FAILED", 1
        if stub == "timeout":
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"STUB_TIMEOUT {symbol}\n")
            return "TIMEOUT", -9
        raw = 10 if stub != "empty" else 0
        tier = 2 if stub != "empty" else 0
        write_json_atomic(
            run_dir / "run_manifest.json",
            {
                "run_id": run_id,
                "selected_symbol": symbol,
                "selected_symbols": [symbol],
                "strategy_id": "wave_fade_frozen_f16ae32",
                "source_commit_pin": "f16ae32",
                "signal_start": start,
                "signal_end_exclusive": end,
            },
        )
        write_json_atomic(run_dir / "summary.json", {"run_id": run_id, "raw_total": raw, "tier_a_total": tier})
        write_json_atomic(
            run_dir / "per_symbol" / f"{symbol}.json",
            {
                "symbol": symbol,
                "warmup_complete": True,
                "first_valid_by_timeframe": {tf: {"warmup_complete": True} for tf in TFS},
                "counts_by_timeframe": {
                    tf: {"raw_candidates": raw // 4, "tier_a": tier // 4} for tf in TFS
                },
            },
        )
        write_json_atomic(run_dir / "duplicate_audit.json", {"same_entry_multi_tf_count": 0})
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"STUB_OK {symbol} argv={argv}\n")
        return "OK", 0

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(  # noqa: S603
            argv,
            cwd=str(cwd),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            shell=False,
            env=env,
            start_new_session=False,
        )
        _ACTIVE_CHILD = proc
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=COIN_TERM_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            _ACTIVE_CHILD = None
            return "TIMEOUT", -9
        _ACTIVE_CHILD = None
        return "OK", int(rc)


def _latest_run_dir(out_root: Path) -> Path | None:
    if not out_root.is_dir():
        return None
    dirs = [p for p in out_root.iterdir() if p.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _aggregate(coins: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [c for c in coins if c.get("state") in ("COMPLETED", "SKIPPED_RESUME_COMPLETE")]
    failed = [c for c in coins if c.get("state") in ("FAILED", "TIMEOUT", "INTERRUPTED")]
    tf_tot = {tf: {"raw": 0, "tier_a": 0} for tf in TFS}
    for c in successful:
        for tf in TFS:
            row = (c.get("per_timeframe") or {}).get(tf) or {}
            tf_tot[tf]["raw"] += int(row.get("raw") or 0)
            tf_tot[tf]["tier_a"] += int(row.get("tier_a") or 0)
    return {
        "coins_evaluated": len(coins),
        "successful_coins": len(successful),
        "failed_coins": len(failed),
        "raw_candidates": sum(int(c.get("raw_total") or 0) for c in successful),
        "tier_a": sum(int(c.get("tier_a_total") or 0) for c in successful),
        "tier_a_by_timeframe": {tf: tf_tot[tf]["tier_a"] for tf in TFS},
        "raw_by_timeframe": {tf: tf_tot[tf]["raw"] for tf in TFS},
        "coins_without_tier_a": sum(1 for c in successful if int(c.get("tier_a_total") or 0) == 0),
        "coins_with_signals": sum(1 for c in successful if int(c.get("raw_total") or 0) > 0),
        "warmup_warnings": sum(1 for c in successful if c.get("warmup_complete") is False),
        "multi_tf_collisions": sum(int(c.get("multi_tf_collision_count") or 0) for c in successful),
        "execution_dedup_applied": False,
        "outcome_evaluation_enabled": False,
        "writes_to_clickhouse": False,
        "fail_policy": "continue_next_coin_then_COMPLETED_WITH_ERRORS",
    }


def run_job(job_dir: Path) -> int:
    request = read_json(job_dir / "request.json")
    if request.get("jobs_root"):
        os.environ["STOCH_FADE_RESEARCH_JOBS_ROOT"] = str(request["jobs_root"])
    status = read_json(job_dir / "status.json")
    coins = {c["symbol"]: c for c in status.get("coins") or []}
    symbols = list(request["selected_symbols"])
    start = str(request["signal_start"])
    end = str(request["signal_end_exclusive"])
    python = str(sg_python())
    timeout_s = coin_timeout_s()
    log_path = job_dir / "worker.log"
    interrupted = {"flag": False}

    def _on_term(_signum, _frame):
        interrupted["flag"] = True
        child = _ACTIVE_CHILD
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    def flush(current: str | None, index: int, message: str) -> None:
        done = sum(1 for c in coins.values() if c.get("state") not in ("PENDING", "RUNNING"))
        total = len(symbols) or 1
        status["current_symbol"] = current
        status["current_index"] = index
        status["message"] = public_message(message)
        status["completed_coins"] = done
        status["successful_coins"] = sum(
            1 for c in coins.values() if c.get("state") in ("COMPLETED", "SKIPPED_RESUME_COMPLETE")
        )
        status["failed_coins"] = sum(
            1 for c in coins.values() if c.get("state") in ("FAILED", "TIMEOUT", "INTERRUPTED")
        )
        status["raw_total"] = sum(int(c.get("raw_total") or 0) for c in coins.values())
        status["tier_a_total"] = sum(int(c.get("tier_a_total") or 0) for c in coins.values())
        status["progress_percent"] = int(100 * done / total)
        status["coins"] = [coins[s] for s in symbols]
        write_json_atomic(job_dir / "status.json", status)
        write_json_atomic(job_dir / "progress.json", {"coins": status["coins"]})

    status["state"] = "RUNNING"
    status["started_at"] = status.get("started_at") or iso_z(_utcnow())
    flush(None, 0, "Frozen-Signale werden berechnet")

    for index, symbol in enumerate(symbols, start=1):
        if interrupted["flag"]:
            row = coins.get(symbol) or empty_coin_row(symbol)
            if row.get("state") in ("PENDING", "RUNNING"):
                row["state"] = "INTERRUPTED"
                row["error_code"] = "INTERRUPTED"
                coins[symbol] = row
            flush(symbol, index, "unterbrochen")
            break
        complete = find_complete_coin_run(
            job_dir / "coin_runs" / symbol,
            symbol=symbol,
            signal_start=start,
            signal_end_exclusive=end,
        )
        if complete is not None:
            counts = counts_from_run(complete, symbol)
            row = empty_coin_row(symbol)
            row.update(counts)
            row["state"] = "SKIPPED_RESUME_COMPLETE"
            row["finished_at"] = iso_z(_utcnow())
            coins[symbol] = row
            _append_jsonl(job_dir / "per_coin.jsonl", {"symbol": symbol, **row})
            flush(symbol, index, f"{symbol} bereits vollständig")
            continue

        row = empty_coin_row(symbol)
        row["state"] = "RUNNING"
        row["started_at"] = iso_z(_utcnow())
        coins[symbol] = row
        flush(symbol, index, f"{symbol} läuft")
        t0 = time.monotonic()
        out_root = job_dir / "coin_runs" / symbol
        out_root.mkdir(parents=True, exist_ok=True)
        argv = runner_argv(python=python, symbol=symbol, start=start, end=end, out_root=str(out_root))
        outcome, rc = _run_coin_process(argv, REPO_ROOT, log_path, timeout_s)
        duration = round(time.monotonic() - t0, 3)
        row["duration_seconds"] = duration
        row["returncode"] = rc
        row["finished_at"] = iso_z(_utcnow())
        run_dir = _latest_run_dir(out_root)
        if outcome == "TIMEOUT":
            row["state"] = "TIMEOUT"
            row["error_code"] = "COIN_TIMEOUT"
        elif interrupted["flag"]:
            row["state"] = "INTERRUPTED"
            row["error_code"] = "INTERRUPTED"
        elif rc != 0:
            row["state"] = "FAILED"
            row["error_code"] = "RUNNER_NONZERO"
        elif run_dir is None or not find_complete_coin_run(
            out_root, symbol=symbol, signal_start=start, signal_end_exclusive=end
        ):
            row["state"] = "FAILED"
            row["error_code"] = "INCOMPLETE_ARTIFACTS"
        else:
            complete_dir = find_complete_coin_run(
                out_root, symbol=symbol, signal_start=start, signal_end_exclusive=end
            )
            counts = counts_from_run(complete_dir, symbol)  # type: ignore[arg-type]
            row.update(counts)
            row["state"] = "COMPLETED"
        coins[symbol] = row
        _append_jsonl(job_dir / "per_coin.jsonl", {"symbol": symbol, "state": row["state"], "raw_total": row.get("raw_total")})
        flush(symbol, index, f"{symbol} {row['state']}")
        if interrupted["flag"]:
            break

    summary = _aggregate([coins[s] for s in symbols])
    write_json_atomic(job_dir / "combined_summary.json", summary)
    status["combined_summary"] = summary
    failed_n = summary["failed_coins"]
    if interrupted["flag"]:
        status["state"] = "INTERRUPTED"
    elif failed_n and summary["successful_coins"]:
        status["state"] = "COMPLETED_WITH_ERRORS"
    elif failed_n:
        status["state"] = "COMPLETED_WITH_ERRORS"
    else:
        status["state"] = "COMPLETED"
    status["finished_at"] = iso_z(_utcnow())
    status["progress_percent"] = 100
    status["last_worker_pid"] = status.get("worker_pid") or status.get("last_worker_pid")
    status["worker_pid"] = None
    flush(None, len(symbols), status["state"])
    from stoch_fade_research_jobs.jobs import clear_lock

    clear_lock()
    try:
        import stoch_heavy_job_gate as heavy_gate

        heavy_gate.release(str(request.get("job_id") or ""))
    except Exception:  # noqa: BLE001
        pass
    return 0 if status["state"] == "COMPLETED" else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print("usage: worker.py <job_id> <job_dir>", file=sys.stderr)
        return 2
    job_dir = Path(args[1])
    return run_job(job_dir)


if __name__ == "__main__":
    raise SystemExit(main())

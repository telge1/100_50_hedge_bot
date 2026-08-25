#!/usr/bin/env python3
"""Sequential Frozen-signal NO_BE50 evaluation worker. One SG child per coin. Never kills collectors."""

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

from stoch_universe_51.jsonio import read_json, write_json_atomic  # noqa: E402

from stoch_fade_research_evaluations.artifacts import (  # noqa: E402
    apply_source_counts,
    finalize_root_artifacts,
)
from stoch_fade_research_evaluations.config import (  # noqa: E402
    COIN_TERM_GRACE_S,
    REPO_ROOT,
    coin_timeout_s,
    sg_python,
)
from stoch_fade_research_evaluations.jobs import public_message  # noqa: E402
from stoch_fade_research_jobs.config import jobs_root  # noqa: E402
from stoch_fade_research_jobs.feed import parse_job_id  # noqa: E402
from worker_env import (  # noqa: E402
    PINNED_GOLD_ROOT,
    clickhouse_preflight,
    inject_worker_env,
    sg_python_preflight,
)

import stoch_heavy_job_gate as heavy_gate  # noqa: E402

_ACTIVE_CHILD: subprocess.Popen | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(ts: datetime | None = None) -> str:
    now = ts or _utcnow()
    return now.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def evaluator_argv(
    *,
    python: str,
    symbol: str,
    signals_jsonl: str,
    out_dir: str,
    evaluation_id: str,
    source_job_id: str,
    pin_candle_data_to: str,
) -> list[str]:
    argv = [
        python,
        "-m",
        "research.stoch_fade_evaluation",
        "--clickhouse-readonly",
        "--symbol",
        symbol,
        "--signals-jsonl",
        signals_jsonl,
        "--out-dir",
        out_dir,
        "--evaluation-id",
        evaluation_id,
        "--source-job-id",
        source_job_id,
        "--pin-candle-data-to",
        pin_candle_data_to,
    ]
    if argv.count("--symbol") != 1:
        raise RuntimeError("exactly one --symbol required")
    if "--cleanup-first" in argv:
        raise RuntimeError("cleanup forbidden")
    return argv


def _run_coin_process(argv: list[str], cwd: Path, log_path: Path, timeout_s: int) -> tuple[str, int]:
    global _ACTIVE_CHILD
    stub = os.environ.get("STOCH_FADE_EVAL_STUB")
    if stub:
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        symbol = argv[argv.index("--symbol") + 1]
        out_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(
            out_dir / "summary.json",
            {
                "symbol": symbol,
                "signals": 1,
                "wins": 1,
                "losses": 0,
                "open": 0,
                "tier_a_input": 1,
                "exit_policy": "NO_BE50",
                "evaluation_data_start": "2026-08-01T07:01:00Z",
                "evaluation_data_end": "2026-08-11T08:02:00Z",
                "identity": {"candle_data_to": "2026-08-11T08:01:00Z"},
            },
        )
        write_json_atomic(
            out_dir / "window.json",
            {
                "symbol": symbol,
                "evaluation_data_start": "2026-08-01T07:01:00Z",
                "evaluation_data_end": "2026-08-11T08:02:00Z",
                "candle_rows": 10,
                "signal_job_not_used_as_candle_cap": True,
            },
        )
        (out_dir / "outcomes.jsonl").write_text(
            json.dumps(
                {
                    "signal_id": "stub-" + symbol,
                    "symbol": symbol,
                    "timeframe": "15m",
                    "direction": "LONG",
                    "outcome": "WIN",
                    "display_result": "WIN",
                    "exit_reason": "TP",
                    "exit_policy": "NO_BE50",
                    "intrabar_policy": "SL_FIRST",
                    "pnl_pct_gross": 1.0,
                    "is_open": False,
                    "be_activated": False,
                    "source": "FROZEN_RESEARCH_EVALUATION",
                    "outcomes_computed": True,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return "ok", 0
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    gold_src = str(PINNED_GOLD_ROOT / "src")
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + gold_src + os.pathsep + env.get("PYTHONPATH", "")
    env["STOCH_FADE_SG_PYTHON"] = str(argv[0])
    env["STOCH_FADE_SIGNAL_GENERATOR_ROOT"] = str(PINNED_GOLD_ROOT)
    with log_path.open("ab") as log:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
        except FileNotFoundError:
            log.write(b"MISSING_SG_PYTHON\n")
            return "MISSING_SG_PYTHON", 127
        _ACTIVE_CHILD = proc
        try:
            returncode = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=COIN_TERM_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.send_signal(signal.SIGKILL)
                proc.wait(timeout=5)
            return "COIN_TIMEOUT", 124
        finally:
            _ACTIVE_CHILD = None
    return "ok", int(returncode)


def _merge_summaries(directory: Path, coins: list[dict[str, Any]]) -> dict[str, Any]:
    wins = losses = opens = signals = be_act = be_ex = 0
    gross_p = gross_l = 0.0
    by_symbol: dict[str, Any] = {}
    by_tf: dict[str, Any] = {}
    by_dir: dict[str, Any] = {}
    reasons: dict[str, int] = {}
    for coin in coins:
        if coin.get("state") != "COMPLETED":
            continue
        path = directory / "coin_runs" / str(coin["symbol"]) / "summary.json"
        if not path.is_file():
            continue
        s = read_json(path)
        wins += int(s.get("wins") or 0)
        losses += int(s.get("losses") or 0)
        opens += int(s.get("open") or 0)
        signals += int(s.get("signals") or 0)
        be_act += int(s.get("be50_activated_count") or 0)
        be_ex += int(s.get("be50_exit_count") or 0)
        gp = float(s.get("gross_profit_pct") or 0)
        gl = float(s.get("gross_loss_pct") or 0)
        gross_p += gp
        gross_l += gl
        by_symbol[str(coin["symbol"])] = {
            "signals": s.get("signals"),
            "wins": s.get("wins"),
            "losses": s.get("losses"),
            "open": s.get("open"),
        }
        outcomes = directory / "coin_runs" / str(coin["symbol"]) / "outcomes.jsonl"
        if outcomes.is_file():
            for line in outcomes.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                tf = str(row.get("timeframe") or "")
                direction = str(row.get("direction") or "")
                by_tf.setdefault(tf, {"signals": 0, "wins": 0, "losses": 0, "open": 0})
                by_dir.setdefault(direction, {"signals": 0, "wins": 0, "losses": 0, "open": 0})
                by_tf[tf]["signals"] += 1
                by_dir[direction]["signals"] += 1
                oc = str(row.get("outcome") or "")
                if oc == "WIN":
                    by_tf[tf]["wins"] += 1
                    by_dir[direction]["wins"] += 1
                elif oc == "LOSS":
                    by_tf[tf]["losses"] += 1
                    by_dir[direction]["losses"] += 1
                elif oc == "OPEN":
                    by_tf[tf]["open"] += 1
                    by_dir[direction]["open"] += 1
                reason = str(row.get("exit_reason") or oc or "")
                reasons[reason] = reasons.get(reason, 0) + 1
    closed = wins + losses
    return {
        "signals": signals,
        "wins": wins,
        "losses": losses,
        "open": opens,
        "win_rate_pct": (wins / closed * 100.0) if closed else None,
        "gross_profit_pct": gross_p,
        "gross_loss_pct": gross_l,
        "total_pnl_pct": gross_p + gross_l,
        "pnl_basis": "gross",
        "be50_activated_count": be_act,
        "be50_exit_count": be_ex,
        "by_symbol": by_symbol,
        "by_timeframe": by_tf,
        "by_direction": by_dir,
        "exit_reason_counts": reasons,
        "win_rate_denominator": "wins+losses (OPEN excluded)",
        "execution_dedup_applied": False,
        "signal_scope": "TIER_A_ONLY",
    }


def run_evaluation(directory: Path) -> None:
    inject_worker_env(os.environ)
    request = read_json(directory / "request.json")
    status = read_json(directory / "status.json")
    index = read_json(directory / "source_index.json")
    evaluation_id = str(request["evaluation_id"])
    source_job_id = str(request["source_job_id"])
    pin_candle_data_to = str(request.get("outcome_data_end") or "")
    if not pin_candle_data_to:
        raise RuntimeError("MISSING_OUTCOME_DATA_END")
    job_dir = jobs_root() / source_job_id
    coins = list(status.get("coins") or [])
    by_symbol = {c["symbol"]: c for c in coins}
    started = _utcnow()
    status["state"] = "RUNNING"
    status["started_at"] = status.get("started_at") or iso_z(started)
    write_json_atomic(directory / "status.json", status)
    failed = 0
    timeout_s = coin_timeout_s()
    python = sg_python()
    stubbed = bool(str(os.environ.get("STOCH_FADE_EVAL_STUB") or "").strip())
    interp = {"ok": True, "error_code": None} if stubbed else sg_python_preflight(os.environ)
    preflight = clickhouse_preflight(os.environ)
    if not interp.get("ok") or not preflight.get("ok"):
        status["state"] = "FAILED"
        status["finished_at"] = iso_z()
        status["worker_pid"] = None
        status["message"] = public_message(
            str(interp.get("error_code") or preflight.get("error_code") or "CLICKHOUSE_PREFLIGHT_FAILED")
        )
        write_json_atomic(directory / "status.json", status)
        heavy_gate.release(evaluation_id)
        return
    src_by_symbol = {c["symbol"]: c for c in index.get("coins") or []}
    for i, src in enumerate(index.get("coins") or []):
        symbol = src["symbol"]
        row = by_symbol.get(symbol) or {"symbol": symbol, "state": "PENDING"}
        if row.get("state") in ("COMPLETED", "SKIPPED_RESUME_COMPLETE"):
            continue
        out_dir = directory / "coin_runs" / symbol
        if (out_dir / "outcomes.jsonl").is_file() and (out_dir / "summary.json").is_file():
            row["state"] = "SKIPPED_RESUME_COMPLETE"
            apply_source_counts(row, src)
            oc_n = sum(1 for line in (out_dir / "outcomes.jsonl").read_text(encoding="utf-8").splitlines() if line.strip())
            row["evaluated_tier_a_total"] = int((read_json(out_dir / "summary.json")).get("tier_a_input") or oc_n)
            row["completed_outcomes"] = oc_n
            row["failed_outcomes"] = 0
            by_symbol[symbol] = row
            continue
        status["current_symbol"] = symbol
        status["current_index"] = i + 1
        status["coins"] = list(by_symbol.values())
        write_json_atomic(directory / "status.json", status)
        write_json_atomic(directory / "progress.json", {"coins": list(by_symbol.values())})
        signals_jsonl = str((job_dir / src["signals_path"]).resolve())
        argv = evaluator_argv(
            python=python,
            symbol=symbol,
            signals_jsonl=signals_jsonl,
            out_dir=str(out_dir),
            evaluation_id=evaluation_id,
            source_job_id=source_job_id,
            pin_candle_data_to=pin_candle_data_to,
        )
        t0 = time.time()
        msg, code = _run_coin_process(argv, REPO_ROOT, directory / "worker.log", timeout_s)
        row["duration_seconds"] = round(time.time() - t0, 3)
        if code == 0 and (out_dir / "summary.json").is_file():
            summary = read_json(out_dir / "summary.json")
            oc_n = 0
            oc_path = out_dir / "outcomes.jsonl"
            if oc_path.is_file():
                oc_n = sum(1 for line in oc_path.read_text(encoding="utf-8").splitlines() if line.strip())
            row["state"] = "COMPLETED"
            row["wins"] = summary.get("wins")
            row["losses"] = summary.get("losses")
            row["open"] = summary.get("open")
            apply_source_counts(row, src)
            row["evaluated_tier_a_total"] = int(summary.get("tier_a_input") or summary.get("signals") or oc_n)
            row["completed_outcomes"] = oc_n
            row["failed_outcomes"] = 0
            row["tier_a_total"] = row["evaluated_tier_a_total"]
            row["artifact_reference"] = f"coin_runs/{symbol}"
            row["message"] = ""
        else:
            failed += 1
            apply_source_counts(row, src)
            row["state"] = "FAILED"
            row["error_code"] = msg if msg != "ok" else f"exit:{code}"
            row["message"] = public_message(row["error_code"])
            row["evaluated_tier_a_total"] = 0
            row["completed_outcomes"] = 0
            row["failed_outcomes"] = int(row.get("source_tier_a_total") or 0)
        by_symbol[symbol] = row
        coins_out = [by_symbol[c["symbol"]] for c in index.get("coins") or []]
        done = sum(1 for c in coins_out if c.get("state") in ("COMPLETED", "FAILED", "SKIPPED_RESUME_COMPLETE"))
        status["coins"] = coins_out
        status["completed_coins"] = done
        status["progress_percent"] = int(100 * done / max(1, len(coins_out)))
        combined = _merge_summaries(directory, coins_out)
        status["wins"] = combined.get("wins")
        status["losses"] = combined.get("losses")
        status["open"] = combined.get("open")
        status["combined_summary"] = combined
        write_json_atomic(directory / "status.json", status)
        write_json_atomic(directory / "progress.json", {"coins": coins_out})

    coins_out = [by_symbol[c["symbol"]] for c in index.get("coins") or []]
    combined = finalize_root_artifacts(directory, coins_out)
    write_json_atomic(directory / "per_symbol_summary.json", combined.get("by_symbol") or {})
    write_json_atomic(
        directory / "duplicate_audit.json",
        {"execution_dedup_applied": False, "signals_evaluated_independently": True},
    )
    write_json_atomic(directory / "snapshot_after.json", {"at": iso_z(), "combined_summary": combined})
    success = sum(1 for c in coins_out if c.get("state") in ("COMPLETED", "SKIPPED_RESUME_COMPLETE"))
    failed_n = sum(1 for c in coins_out if c.get("state") == "FAILED")
    if failed_n and success:
        final = "COMPLETED_WITH_ERRORS"
    elif failed_n:
        final = "FAILED"
    else:
        final = "COMPLETED"
    status["state"] = final
    status["finished_at"] = iso_z()
    status["current_symbol"] = None
    status["successful_coins"] = success
    status["failed_coins"] = failed_n
    status["progress_percent"] = 100
    status["worker_pid"] = None
    status["message"] = final
    status["coins"] = coins_out
    status["combined_summary"] = combined
    write_json_atomic(directory / "status.json", status)
    heavy_gate.release(evaluation_id)
    lock = Path(directory).parent / "ACTIVE.lock"
    try:
        data = read_json(lock)
        if str(data.get("evaluation_id") or data.get("job_id")) == evaluation_id:
            lock.unlink()
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print("usage: worker.py <evaluation_id> <eval_dir>", file=sys.stderr)
        return 2
    evaluation_id = argv[0]
    if parse_job_id(evaluation_id) is None:
        return 2
    directory = Path(argv[1])
    try:
        run_evaluation(directory)
        return 0
    except Exception as exc:  # noqa: BLE001
        try:
            status = read_json(directory / "status.json")
            status["state"] = "FAILED"
            status["finished_at"] = iso_z()
            status["worker_pid"] = None
            status["message"] = public_message(str(exc))
            write_json_atomic(directory / "status.json", status)
        except Exception:  # noqa: BLE001
            pass
        heavy_gate.release(evaluation_id)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import (
    AMBIGUOUS_EVAL,
    DEFAULT_OUT_ROOT,
    EVALS_ROOT,
    EXIT_POLICY,
    INITIAL_BALANCE,
    INTRABAR_POLICY,
    JOBS_ROOT,
    MAX_SLOTS,
    NOTIONAL,
    OUTCOME_ENGINE,
    REPO_ROOT,
    SIGNAL_SCOPE,
    SIGNAL_STRATEGY_VERSION,
)
from .artifacts import base_manifest, new_run_id, write_run
from .dedup import dedup_pairs
from .guards import require_id
from .io_util import file_fingerprint, read_json
from .join import join_signals_outcomes
from .load import (
    iter_job_signal_files,
    load_evaluation,
    load_job,
    load_outcomes,
    load_tier_a_signals,
    load_universe,
    validate_evaluation_manifest,
)
from .metrics import breakdowns, independent_baseline, portfolio_summary
from .simulate import simulate_portfolio


class Blocked(RuntimeError):
    pass


def _status_state(path: Path) -> str:
    if not path.is_file():
        return ""
    return str(read_json(path).get("state") or "").upper()


def discover_matching_evaluations(job_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if not EVALS_ROOT.is_dir():
        return found
    for d in sorted(EVALS_ROOT.iterdir()):
        man_path = d / "evaluation_manifest.json"
        if not man_path.is_file():
            continue
        man = read_json(man_path)
        if str(man.get("source_job_id")) != job_id:
            continue
        if str(man.get("exit_policy")) != EXIT_POLICY:
            continue
        if str(man.get("outcome_engine")) != OUTCOME_ENGINE:
            continue
        if str(man.get("intrabar_policy")) != INTRABAR_POLICY:
            continue
        if str(man.get("signal_scope")) != SIGNAL_SCOPE:
            continue
        if str(man.get("signal_strategy_version")) != SIGNAL_STRATEGY_VERSION:
            continue
        if man.get("execution_dedup_applied") is True:
            continue
        st = _status_state(d / "status.json")
        if st and st != "COMPLETED":
            continue
        oc = d / "outcomes.jsonl"
        if not oc.is_file():
            continue
        fp = file_fingerprint(oc)
        found.append(
            {
                "evaluation_id": d.name,
                "source_job_id": job_id,
                "mtime_iso": fp["mtime_iso"],
                "size_bytes": fp["size_bytes"],
                "sha256": fp["sha256"],
                "window": {
                    "start": man.get("evaluation_data_start"),
                    "end": man.get("evaluation_data_end"),
                },
            }
        )
    return found


def run_backtest(
    evaluation_id: str,
    *,
    initial_balance: float = INITIAL_BALANCE,
    max_slots: int = MAX_SLOTS,
    notional: float = NOTIONAL,
    out_root: Path = DEFAULT_OUT_ROOT,
) -> Path:
    eid = require_id(evaluation_id, "EVALUATION_ID")
    eval_pack = load_evaluation(eid)
    man = eval_pack["manifest"]
    job_id = str(man.get("source_job_id") or "")
    errors = validate_evaluation_manifest(man, job_id)
    if errors:
        raise Blocked("BLOCKED_BY_PORTFOLIO_BACKTEST_INPUT_OR_SEMANTICS:" + ",".join(errors))
    if _status_state(eval_pack["root"] / "status.json") != "COMPLETED":
        raise Blocked("BLOCKED_BY_PORTFOLIO_BACKTEST_INPUT_OR_SEMANTICS:EVAL_NOT_COMPLETED")

    job = load_job(job_id)
    job_status = read_json(job["root"] / "status.json")
    if str(job_status.get("state") or "").upper() != "COMPLETED":
        raise Blocked("BLOCKED_BY_PORTFOLIO_BACKTEST_INPUT_OR_SEMANTICS:JOB_NOT_COMPLETED")
    if int(job_status.get("failed_coins") or 0) != 0:
        raise Blocked("BLOCKED_BY_PORTFOLIO_BACKTEST_INPUT_OR_SEMANTICS:FAILED_COINS")
    symbols = list(job["manifest"].get("selected_symbols") or [])
    universe = load_universe()
    if len(set(symbols)) != 51 or set(symbols) != set(universe):
        raise Blocked("BLOCKED_BY_PORTFOLIO_BACKTEST_INPUT_OR_SEMANTICS:UNIVERSE_MISMATCH")
    if str(job["manifest"].get("fixed_strategy_version")) != SIGNAL_STRATEGY_VERSION:
        raise Blocked("BLOCKED_BY_PORTFOLIO_BACKTEST_INPUT_OR_SEMANTICS:JOB_STRATEGY")

    before = {
        "outcomes": file_fingerprint(eval_pack["outcomes_path"]),
        "job_manifest": file_fingerprint(job["root"] / "job_manifest.json"),
        "eval_manifest": file_fingerprint(eval_pack["root"] / "evaluation_manifest.json"),
        "signal_files": {
            f"{sym}:{path}": file_fingerprint(path)
            for sym, path in iter_job_signal_files(job["root"])
        },
    }

    signals = load_tier_a_signals(job["root"])
    outcomes = load_outcomes(eval_pack["outcomes_path"])
    be_like = [o for o in outcomes if str(o.get("outcome") or "").upper() not in {"WIN", "LOSS", "OPEN"}]
    if be_like:
        raise Blocked("BLOCKED_BY_PORTFOLIO_BACKTEST_INPUT_OR_SEMANTICS:NON_WIN_LOSS_OPEN")
    joined = join_signals_outcomes(signals, outcomes)
    if not joined["audit"]["complete"]:
        raise Blocked(joined["audit"]["blocker"])

    deduped = dedup_pairs(joined["pairs"], notional)
    kept = deduped["kept"]
    independent = independent_baseline(kept, notional)
    sim = simulate_portfolio(
        kept,
        initial_balance=initial_balance,
        max_slots=max_slots,
        notional=notional,
        enforce_slots=True,
        enforce_symbol=True,
        enforce_cash=True,
    )
    summary = portfolio_summary(sim, initial_balance=initial_balance, max_slots=max_slots, independent=independent)
    br = breakdowns(sim.accepted)
    summary["per_direction"] = br["per_direction"]
    summary["per_month"] = br["per_month"]
    summary["input"] = {
        "tier_a_signals": len(signals),
        "unique_signal_ids": joined["audit"]["unique_signal_ids"],
        "outcomes": len(outcomes),
        "wins_before_portfolio": sum(1 for o in outcomes if o.get("outcome") == "WIN"),
        "losses_before_portfolio": sum(1 for o in outcomes if o.get("outcome") == "LOSS"),
        "open_before_portfolio": sum(1 for o in outcomes if o.get("outcome") == "OPEN"),
        "coins": 51,
        "timeframes": ["15m", "30m", "1h", "4h"],
        "window": {
            "evaluation_data_start": man.get("evaluation_data_start"),
            "evaluation_data_end": man.get("evaluation_data_end"),
        },
    }
    summary["dedup"] = deduped["stats"]
    summary["fees_applied"] = False
    summary["net_result_status"] = "NOT_EVALUATED_NO_AUTHORITATIVE_FEE_RATE"
    summary["fee_note"] = (
        "SG be50.py FEE_PCT=0.11 is diagnostic only; evaluation cards are gross. "
        "No unique authoritative roundtrip fee for this Frozen portfolio run."
    )

    after = {
        "outcomes": file_fingerprint(eval_pack["outcomes_path"]),
        "job_manifest": file_fingerprint(job["root"] / "job_manifest.json"),
        "eval_manifest": file_fingerprint(eval_pack["root"] / "evaluation_manifest.json"),
        "signal_files": {
            f"{sym}:{path}": file_fingerprint(path)
            for sym, path in iter_job_signal_files(job["root"])
        },
    }
    if after != before:
        raise Blocked("BLOCKED_BY_PORTFOLIO_BACKTEST_INPUT_OR_SEMANTICS:SOURCE_MUTATED")

    twins = discover_matching_evaluations(job_id)
    run_id = new_run_id()
    manifest = base_manifest(
        source_evaluation_id=eid,
        source_job_id=job_id,
        initial_balance=initial_balance,
        max_slots=max_slots,
        notional=notional,
        extra={
            "run_id": run_id,
            "considered_matching_evaluations": twins,
            "event_order_rule": sim.event_order_rule,
            "pnl_basis": "gross",
            "leverage": 1,
        },
    )
    log = "\n".join(
        [
            f"run_id={run_id}",
            f"evaluation_id={eid}",
            f"job_id={job_id}",
            f"tier_a={len(signals)} outcomes={len(outcomes)} after_dedup={len(kept)}",
            f"accepted={len(sim.accepted)} skipped={len(sim.skipped)}",
            f"realized_equity={summary['realized_equity_usdt']}",
            "sources unchanged",
        ]
    )
    dest = write_run(
        out_root=out_root,
        run_id=run_id,
        manifest=manifest,
        input_audit={
            "join": joined["audit"],
            "fingerprints_before": before,
            "fingerprints_after": after,
            "source_unchanged": True,
            "matching_evaluations": twins,
        },
        summary=summary,
        equity_curve=sim.equity_curve,
        accepted=sim.accepted,
        skipped=sim.skipped,
        duplicate_audit=deduped["dropped"],
        slot_history=sim.slot_history,
        open_at_end=sim.open_at_end,
        breakdowns=br,
        log_text=log + "\n",
    )
    return dest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="research.stoch_fade_portfolio_backtest")
    p.add_argument("--evaluation-id", required=True)
    p.add_argument("--initial-balance", type=float, default=INITIAL_BALANCE)
    p.add_argument("--max-slots", type=int, default=MAX_SLOTS)
    p.add_argument("--notional-per-trade", type=float, default=NOTIONAL)
    p.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    args = p.parse_args(argv)
    out_root = Path(args.out_root).resolve()
    allowed = DEFAULT_OUT_ROOT.resolve()
    if out_root != allowed and allowed not in out_root.parents and out_root != allowed:
        # only allow repo results path
        if REPO_ROOT.resolve() not in out_root.parents and out_root.parent != REPO_ROOT.resolve():
            raise SystemExit("INVALID_OUT_ROOT")
    if ".." in Path(args.out_root).parts:
        raise SystemExit("INVALID_OUT_ROOT")
    try:
        dest = run_backtest(
            args.evaluation_id,
            initial_balance=args.initial_balance,
            max_slots=args.max_slots,
            notional=args.notional_per_trade,
            out_root=Path(args.out_root),
        )
    except Blocked as exc:
        print(str(exc))
        return 2
    print(str(dest))
    return 0

from __future__ import annotations

import json
from pathlib import Path

from stoch_fade_research_evaluations.feed import catalog_response, load_outcomes
from stoch_fade_research_jobs.config import STRATEGY_VERSION
from stoch_fade_research_jobs.feed import load_job_signals
from stoch_universe_51.jsonio import write_json_atomic as atomic

JOB = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
EVAL = "cccccccccccccccccccccccccccccccc"
RUN = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
START = "2026-08-01T00:00:00Z"
END = "2026-08-02T00:00:00Z"


def _write_job(root: Path) -> None:
    job_dir = root / JOB
    run_dir = job_dir / "coin_runs" / "AAVEUSDT" / RUN
    run_dir.mkdir(parents=True)
    atomic(
        job_dir / "request.json",
        {
            "job_id": JOB,
            "fixed_strategy_version": STRATEGY_VERSION,
            "selected_symbols": ["AAVEUSDT"],
            "signal_start": START,
            "signal_end_exclusive": END,
        },
    )
    atomic(
        job_dir / "status.json",
        {
            "job_id": JOB,
            "state": "COMPLETED",
            "raw_total": 2,
            "tier_a_total": 1,
            "coins": [
                {
                    "symbol": "AAVEUSDT",
                    "state": "COMPLETED",
                    "runner_run_id": RUN,
                    "artifact_reference": f"coin_runs/AAVEUSDT/{RUN}",
                    "raw_total": 2,
                    "tier_a_total": 1,
                }
            ],
        },
    )
    atomic(run_dir / "summary.json", {"run_id": RUN, "raw_total": 2, "tier_a_total": 1})
    atomic(
        run_dir / "run_manifest.json",
        {
            "run_id": RUN,
            "selected_symbol": "AAVEUSDT",
            "strategy_id": STRATEGY_VERSION,
            "signal_start": START,
            "signal_end_exclusive": END,
            "source_commit_pin": "f16ae32",
        },
    )
    lines = [
        json.dumps(
            {
                "signal_id": "raw-1",
                "symbol": "AAVEUSDT",
                "timeframe": "30m",
                "direction": "LONG",
                "tier_a": False,
                "entry_price": 10.0,
                "tp_price": 10.1,
                "sl_price": 9.9,
                "candle_open_time": "2026-08-01T01:00:00Z",
                "candle_close_time": "2026-08-01T01:00:00Z",
                "strategy_version": STRATEGY_VERSION,
            }
        ),
        json.dumps(
            {
                "signal_id": "tier-1",
                "symbol": "AAVEUSDT",
                "timeframe": "30m",
                "direction": "LONG",
                "tier_a": True,
                "entry_price": 10.0,
                "tp_price": 10.1,
                "sl_price": 9.9,
                "candle_open_time": "2026-08-01T02:00:00Z",
                "candle_close_time": "2026-08-01T02:00:00Z",
                "strategy_version": STRATEGY_VERSION,
            }
        ),
    ]
    (run_dir / "signals.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_eval(root: Path) -> None:
    directory = root / EVAL
    coin = directory / "coin_runs" / "AAVEUSDT"
    coin.mkdir(parents=True)
    atomic(
        directory / "request.json",
        {
            "evaluation_id": EVAL,
            "source_job_id": JOB,
            "exit_policy": "NO_BE50",
            "signal_strategy_version": STRATEGY_VERSION,
        },
    )
    atomic(
        directory / "status.json",
        {
            "evaluation_id": EVAL,
            "source_job_id": JOB,
            "state": "COMPLETED",
            "combined_summary": {
                "signals": 1,
                "wins": 1,
                "losses": 0,
                "open": 0,
                "win_rate_pct": 100.0,
                "gross_profit_pct": 1.2,
                "gross_loss_pct": 0.0,
                "total_pnl_pct": 1.2,
                "be50_activated_count": 0,
            },
        },
    )
    (coin / "outcomes.jsonl").write_text(
        json.dumps(
            {
                "signal_id": "tier-1",
                "symbol": "AAVEUSDT",
                "display_result": "WIN",
                "outcome": "WIN",
                "pnl_pct_gross": 1.2,
                "exit_reason": "TP",
                "exit_time": "2026-08-01T03:00:00Z",
                "be_activated": False,
                "duration_seconds": 3600,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_join_by_signal_id_only(tmp_path):
    jobs = tmp_path / "jobs"
    evals = tmp_path / "evals"
    jobs.mkdir()
    evals.mkdir()
    _write_job(jobs)
    _write_eval(evals)
    env = {
        "STOCH_FADE_RESEARCH_JOBS_ROOT": str(jobs),
        "STOCH_FADE_RESEARCH_EVALUATIONS_ROOT": str(evals),
    }
    payload, code = load_job_signals(JOB, environ=env, evaluation_id=EVAL)
    assert code == 200
    rows = payload["rows"]
    assert len(rows) == 1
    assert rows[0]["signal_id"] == "tier-1"
    assert rows[0]["source"] == "FROZEN_RESEARCH_EVALUATION"
    assert rows[0]["outcomes_computed"] is True
    assert rows[0]["result"] == "WIN"
    assert rows[0]["pnl_pct"] == 1.2
    assert payload["execution_dedup_applied"] is False
    assert payload["summary"]["outcomes_computed"] is True


def test_catalog_no_implicit_latest(tmp_path):
    evals = tmp_path / "evals"
    evals.mkdir()
    _write_eval(evals)
    env = {"STOCH_FADE_RESEARCH_EVALUATIONS_ROOT": str(evals)}
    payload = catalog_response(env, JOB)
    assert payload["implicit_latest"] is False
    assert payload["count"] == 1
    oc, code = load_outcomes(EVAL, environ=env)
    assert code == 200
    assert oc["rows"][0]["signal_id"] == "tier-1"


def test_catalog_skips_legacy_be50_evaluation(tmp_path):
    evals = tmp_path / "evals"
    evals.mkdir()
    _write_eval(evals)
    legacy = evals / "dddddddddddddddddddddddddddddddd"
    legacy.mkdir()
    atomic(legacy / "request.json", {"exit_policy": "FROZEN_BE50", "source_job_id": JOB})
    atomic(legacy / "status.json", {"state": "COMPLETED", "source_job_id": JOB})
    env = {"STOCH_FADE_RESEARCH_EVALUATIONS_ROOT": str(evals)}
    payload = catalog_response(env, JOB)
    assert payload["count"] == 1
    assert payload["evaluations"][0]["evaluation_id"] == EVAL
    oc, code = load_outcomes("dddddddddddddddddddddddddddddddd", environ=env)
    assert code == 409
    assert oc["error"] == "EVALUATION_POLICY_MISMATCH"

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from stoch_fade_research_jobs.config import CAUSAL_MANIFEST_HASH, CONFIRMATION_POLICY, STRATEGY_VERSION
from stoch_fade_research_jobs.feed import (
    SOURCE,
    catalog_response,
    frozen_strategy_requires_job,
    load_job_signals,
    parse_job_id,
)
from stoch_universe_51.jsonio import write_json_atomic as atomic

JOB = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RUN = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
START = "2026-08-01T00:00:00Z"
END = "2026-08-02T00:00:00Z"
EXAMPLE = "8e86d1527a4749a79531d787cf67a032"
REPO = Path(__file__).resolve().parents[3]
EXAMPLE_DIR = REPO / "results" / "stoch_fade_research_jobs" / EXAMPLE


def _env(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    return {"STOCH_FADE_RESEARCH_JOBS_ROOT": str(root)}, root


def _signal(symbol, *, tier_a, ts, sid):
    return {
        "signal_id": sid,
        "symbol": symbol,
        "timeframe": "30m",
        "direction": "LONG",
        "signal_type": "wave_fade",
        "tier_a": tier_a,
        "is_q4": tier_a,
        "trend_bucket": "TREND_ALIGNED",
        "entry_valid": True,
        "entry_price": 10.0,
        "entry_time": ts.replace("00Z", "01Z") if ts.endswith("00Z") else ts,
        "tp_price": 10.1,
        "sl_price": 9.9,
        "candle_open_time": ts,
        "candle_close_time": ts,
        "confirmation_available_at": ts,
        "generated_at": ts,
        "strategy_version": STRATEGY_VERSION,
    }


def _write_job(root: Path, *, state="COMPLETED", extra_coins=None):
    job_dir = root / JOB
    run_dir = job_dir / "coin_runs" / "AAVEUSDT" / RUN
    run_dir.mkdir(parents=True)
    atomic(
        job_dir / "request.json",
        {
            "job_id": JOB,
            "fixed_strategy_version": STRATEGY_VERSION,
            "confirmation_policy": CONFIRMATION_POLICY,
            "causal_manifest_hash": CAUSAL_MANIFEST_HASH,
            "selected_symbols": ["AAVEUSDT"],
            "signal_start": START,
            "signal_end_exclusive": END,
        },
    )
    coins = [
        {
            "symbol": "AAVEUSDT",
            "state": "COMPLETED",
            "runner_run_id": RUN,
            "artifact_reference": f"coin_runs/AAVEUSDT/{RUN}",
            "raw_total": 2,
            "tier_a_total": 1,
        }
    ]
    if extra_coins:
        coins.extend(extra_coins)
    atomic(
        job_dir / "status.json",
        {
            "job_id": JOB,
            "state": state,
            "created_at": "2026-08-15T15:50:26Z",
            "finished_at": "2026-08-15T15:51:27Z",
            "successful_coins": 1,
            "failed_coins": 0,
            "raw_total": 2,
            "tier_a_total": 1,
            "coins": coins,
        },
    )
    atomic(
        run_dir / "summary.json",
        {"run_id": RUN, "raw_total": 2, "tier_a_total": 1},
    )
    atomic(
        run_dir / "run_manifest.json",
        {
            "run_id": RUN,
            "selected_symbol": "AAVEUSDT",
            "strategy_id": STRATEGY_VERSION,
            "confirmation_policy": CONFIRMATION_POLICY,
            "causal_manifest_hash": CAUSAL_MANIFEST_HASH,
            "signal_start": START,
            "signal_end_exclusive": END,
            "source_commit_pin": "f16ae32",
        },
    )
    lines = [
        json.dumps(_signal("AAVEUSDT", tier_a=False, ts="2026-08-01T01:00:00Z", sid="raw-1")),
        json.dumps(_signal("AAVEUSDT", tier_a=True, ts="2026-08-01T02:00:00Z", sid="tier-1")),
    ]
    (run_dir / "signals.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return job_dir


def test_parse_job_id():
    assert parse_job_id(JOB) == JOB
    assert parse_job_id("not-a-job") is None
    assert parse_job_id("../" + JOB) is None
    assert parse_job_id(JOB + "/x") is None


def test_catalog_skips_running(tmp_path):
    env, root = _env(tmp_path)
    _write_job(root, state="RUNNING")
    payload = catalog_response(env)
    assert payload["jobs"] == []
    assert payload["implicit_latest"] is False


def test_catalog_completed(tmp_path):
    env, root = _env(tmp_path)
    _write_job(root)
    payload = catalog_response(env)
    assert payload["count"] == 1
    job = payload["jobs"][0]
    assert job["job_id"] == JOB
    assert job["state"] == "COMPLETED"
    assert job["raw_total"] == 2
    assert job["tier_a_total"] == 1
    assert job["outcome_evaluation_enabled"] is False
    blob = json.dumps(payload)
    assert "/home/" not in blob
    assert "signals.jsonl" not in blob


def test_signals_default_tier_a(tmp_path):
    env, root = _env(tmp_path)
    _write_job(root)
    payload, code = load_job_signals(JOB, environ=env)
    assert code == 200
    assert payload["source"] == SOURCE
    assert payload["outcomes_computed"] is False
    assert payload["execution_dedup_applied"] is False
    assert payload["pagination"]["total"] == 1
    row = payload["rows"][0]
    assert row["signal_id"] == "tier-1"
    assert row["tier_a"] is True
    assert "result" not in row
    assert "pnl_pct" not in row
    assert row["outcomes_computed"] is False
    assert row["plan_status"] == "PLANNED_NO_OUTCOME"
    assert row["job_signal_start"] == START
    assert row["job_signal_end_exclusive"] == END
    assert payload["summary"]["wins"] is None
    assert payload["summary"]["open"] is None
    assert payload["summary"]["total_pnl"] is None


def test_signals_raw_filter(tmp_path):
    env, root = _env(tmp_path)
    _write_job(root)
    payload, code = load_job_signals(JOB, environ=env, tier_a="all")
    assert code == 200
    assert payload["pagination"]["total"] == 2


def test_invalid_and_unselectable(tmp_path):
    env, root = _env(tmp_path)
    _write_job(root, state="QUEUED")
    payload, code = load_job_signals("zzz", environ=env)
    assert code == 400
    payload, code = load_job_signals(JOB, environ=env)
    assert code == 409
    assert payload["error"] == "JOB_NOT_SELECTABLE"


def test_frozen_strategy_not_collector():
    assert frozen_strategy_requires_job(STRATEGY_VERSION) is True
    assert frozen_strategy_requires_job("wave_fade_no_be50_v1") is False


def test_auth_routes_include_catalog_and_signals():
    text = (Path(__file__).resolve().parents[2] / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/api/stoch/frozen-fade-jobs")' in text
    assert '@app.post("/api/stoch/frozen-fade-jobs")' in text
    assert '@app.get("/api/stoch/frozen-fade-jobs/{job_id}/signals")' in text
    chunk = text.split('@app.get("/api/stoch/frozen-fade-jobs")')[1].split('@app.get("/api/stoch/profits")')[0]
    assert chunk.count("require_auth") >= 6


def test_job_plan_horizon_not_open_now():
    from datetime import timedelta

    from research_charts.stoch_backtester import DEFAULT_OPEN_WIDTH, signal_to_position_spec

    spec = signal_to_position_spec(
        {
            "signal_id": "tier-1",
            "symbol": "AAVEUSDT",
            "direction": "LONG",
            "timeframe": "30m",
            "entry_price": 10.0,
            "tp_price": 10.1,
            "sl_price": 9.9,
            "entry_time": "2026-08-01T02:01:00Z",
            "source": SOURCE,
            "outcomes_computed": False,
        }
    )
    assert spec is not None
    assert spec["end"] - spec["start"] == DEFAULT_OPEN_WIDTH
    assert DEFAULT_OPEN_WIDTH == timedelta(hours=4)


def test_example_job_readonly_if_present():
    if not EXAMPLE_DIR.is_dir():
        return
    env = {"STOCH_FADE_RESEARCH_JOBS_ROOT": str(EXAMPLE_DIR.parent)}
    files = [
        EXAMPLE_DIR / "status.json",
        EXAMPLE_DIR / "combined_summary.json",
        EXAMPLE_DIR / "request.json",
    ]
    before = {str(p): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in files}
    cat = catalog_response(env)
    ids = [j["job_id"] for j in cat["jobs"]]
    assert EXAMPLE in ids
    payload, code = load_job_signals(EXAMPLE, environ=env)
    assert code == 200
    assert payload["job"]["state"] == "COMPLETED"
    assert payload["summary"]["raw_total"] == 56
    assert payload["summary"]["tier_a_total"] == 6
    assert payload["job"]["identity_kind"] in ("CAUSAL_CANONICAL", "LEGACY_WAVE_END_NON_CAUSAL", "UNKNOWN")
    after = {str(p): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in files}
    assert after == before

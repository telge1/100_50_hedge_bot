"""Tests for reproducible research-run storage."""

from __future__ import annotations

import copy
import os
import uuid
from pathlib import Path

import pytest

from research.regime_scanner.candle_sources import REGIME_ENV_FILE, load_regime_db_env_file
from research.regime_scanner.research_runs.baseline_runner import run_baseline_research
from research.regime_scanner.research_runs.compare import compare_runs
from research.regime_scanner.research_runs.fingerprint import build_run_fingerprint
from research.regime_scanner.research_runs.hashing import combined_output_hash, json_hash
from research.regime_scanner.research_runs.normalize import (
    normalize_structure_events,
    normalize_trend_states,
    structure_event_key,
    trend_state_event_key,
)
from research.regime_scanner.research_runs.parameters import (
    assert_no_secrets_in_parameters,
    build_baseline_parameter_set,
    parameter_hash,
)
from research.regime_scanner.research_runs.schema import (
    HASH_NOT_EXPORTED,
    RESEARCH_SCHEMA_STATEMENTS,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
)
from research.regime_scanner.research_runs.store_memory import InMemoryResearchStore, new_run_id
from research.regime_scanner.research_runs.store_mysql import MySQLResearchStore
from research.regime_scanner.timeframes import ensure_utc_timestamp


def _params(**kwargs):
    return build_baseline_parameter_set(**kwargs)


def test_parameter_serialization_deterministic() -> None:
    a = _params().to_canonical_dict()
    b = _params().to_canonical_dict()
    assert a == b
    assert json_hash(a) == json_hash(b)


def test_parameter_hash_differs_for_different_sets() -> None:
    base = parameter_hash(_params())
    other = parameter_hash(_params(history_candles=200))
    assert base != other


def test_secrets_not_in_parameters() -> None:
    payload = _params().to_canonical_dict()
    assert_no_secrets_in_parameters(payload)
    with pytest.raises(ValueError):
        assert_no_secrets_in_parameters({"db_password": "x"})


def test_run_fingerprint_deterministic_and_ignores_run_id() -> None:
    params = _params()
    start = ensure_utc_timestamp("2026-03-01T00:00:00Z").to_pydatetime()
    end = ensure_utc_timestamp("2026-03-08T00:00:00Z").to_pydatetime()
    warm = ensure_utc_timestamp("2025-12-27T00:00:00Z").to_pydatetime()
    fp1 = build_run_fingerprint(
        params=params,
        start_time=start,
        end_time=end,
        warmup_start=warm,
        decision_time=None,
        code_version="abc123",
        candle_hash_5m="h5",
        candle_hash_15m="h15",
        candle_hash_30m="h30",
    )
    fp2 = build_run_fingerprint(
        params=params,
        start_time=start,
        end_time=end,
        warmup_start=warm,
        decision_time=None,
        code_version="abc123",
        candle_hash_5m="h5",
        candle_hash_15m="h15",
        candle_hash_30m="h30",
    )
    assert fp1 == fp2
    assert new_run_id() != new_run_id()


def test_schema_statements_idempotent_and_separate() -> None:
    blob = "\n".join(RESEARCH_SCHEMA_STATEMENTS)
    assert "research_runs" in blob
    assert "research_parameter_sets" in blob
    assert "market_candles" not in blob
    assert "DROP TABLE" not in blob.upper()


def test_event_keys_deterministic() -> None:
    key = trend_state_event_key(timestamp="2026-03-01T00:05:00+00:00", state="uptrend", transition_reason="x")
    assert key == trend_state_event_key(
        timestamp="2026-03-01T00:05:00+00:00", state="uptrend", transition_reason="x"
    )
    skey = structure_event_key(
        timestamp="2026-03-01T00:05:00+00:00",
        event_type="bullish_bos",
        direction="bullish",
        reference_pivot_time="2026-03-01T00:00:00+00:00",
        price=1.5,
    )
    assert "bullish_bos" in skey


def test_duplicate_events_rejected_in_memory_store() -> None:
    store = InMemoryResearchStore()
    run_id = new_run_id()
    store.create_running_run({"run_id": run_id, "parameter_set_id": 1})
    dup = [
        {
            "event_key": "k1",
            "timestamp": "2026-03-01T00:05:00+00:00",
            "state": "uptrend",
            "previous_state": None,
            "direction": "bullish",
            "strength": 1.0,
            "transition_reason": None,
            "confirmation_count": None,
            "protective_high": None,
            "protective_low": None,
            "metadata_json": {},
        },
        {
            "event_key": "k1",
            "timestamp": "2026-03-01T00:10:00+00:00",
            "state": "uptrend",
            "previous_state": None,
            "direction": "bullish",
            "strength": 1.0,
            "transition_reason": None,
            "confirmation_count": None,
            "protective_high": None,
            "protective_low": None,
            "metadata_json": {},
        },
    ]
    with pytest.raises(ValueError):
        store.save_completed_run(
            run_id=run_id,
            updates={"finished_at": "2026-03-01"},
            trend_states=dup,
            structure_events=[],
            signals=[],
            metrics=[],
        )


def test_failed_run_does_not_save_results() -> None:
    store = InMemoryResearchStore()

    class BoomStore(InMemoryResearchStore):
        def save_completed_run(self, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("db write failed")

    boom = BoomStore()
    boom.parameter_sets = store.parameter_sets
    boom.parameter_set_ids = store.parameter_set_ids
    boom.runs = store.runs
    run_id = new_run_id()
    boom.create_running_run({"run_id": run_id, "parameter_set_id": 1, "status": RUN_STATUS_RUNNING})
    boom.mark_failed(run_id, error_type="RuntimeError", error_message="db write failed")
    assert boom.get_run(run_id)["status"] == RUN_STATUS_FAILED
    assert run_id not in boom.trend_states


def test_compare_runs_detects_divergence() -> None:
    store = InMemoryResearchStore()
    a = new_run_id()
    b = new_run_id()
    for rid, h in ((a, "hash_a"), (b, "hash_b")):
        store.runs[rid] = {
            "run_id": rid,
            "run_fingerprint": h,
            "parameter_set_id": 1,
            "symbol": "APTUSDT",
            "started_at": "2026-03-01",
            "trend_state_hash": h,
            "structure_event_hash": h,
            "signal_hash": HASH_NOT_EXPORTED,
            "combined_output_hash": h,
            "candle_hash_5m": "c",
            "candle_hash_15m": "c",
            "candle_hash_30m": "c",
        }
        store.parameter_sets["p"] = {"id": 1, "parameter_hash": "ph"}
    store.trend_states[a] = [{"event_key": "1"}]
    store.trend_states[b] = [{"event_key": "1"}, {"event_key": "2"}]
    out = compare_runs(store, a, b)
    assert out["equivalent"] is False
    assert out["first_divergence"] is not None


def test_combined_hash_skips_not_exported() -> None:
    h = combined_output_hash(
        trend_state_hash="aa",
        structure_event_hash="bb",
        price_action_hash=HASH_NOT_EXPORTED,
        momentum_hash=HASH_NOT_EXPORTED,
        signal_hash=HASH_NOT_EXPORTED,
    )
    h2 = combined_output_hash(
        trend_state_hash="aa",
        structure_event_hash="bb",
        price_action_hash=None,
        momentum_hash=None,
        signal_hash=None,
    )
    assert h == h2


@pytest.mark.skipif(
    not REGIME_ENV_FILE.exists(),
    reason="regime DB env file not present",
)
def test_mysql_schema_init_idempotent() -> None:
    load_regime_db_env_file()
    from research.regime_scanner.mysql_candle_store.config import load_regime_db_config

    store = MySQLResearchStore(load_regime_db_config())
    try:
        candles_before = store.count_candles()
        validation_before = store.count_validation_runs()
        store.init_schema()
        store.init_schema()
        assert store.count_candles() == candles_before
        assert store.count_validation_runs() == validation_before
    finally:
        store.close()


@pytest.mark.skipif(
    not REGIME_ENV_FILE.exists(),
    reason="regime DB env file not present",
)
def test_mysql_baseline_runs_equivalent_without_pipeline() -> None:
    """Integration: two identical trend/structure runs (pipeline skipped for CI time)."""
    load_regime_db_env_file()
    from research.regime_scanner.mysql_candle_store.config import load_regime_db_config

    store = MySQLResearchStore(load_regime_db_config())
    try:
        store.init_schema()
        candles_before = store.count_candles()
        validation_before = store.count_validation_runs()
        r1 = run_baseline_research(
            store,
            include_pipeline=False,
            data_source="mysql",
        )
        r2 = run_baseline_research(
            store,
            include_pipeline=False,
            data_source="mysql",
        )
        assert r1["run_id"] != r2["run_id"]
        assert r1["run_fingerprint"] == r2["run_fingerprint"]
        assert r1["parameter_hash"] == r2["parameter_hash"]
        assert r1["hashes"]["trend_state_hash"] == r2["hashes"]["trend_state_hash"]
        cmp = compare_runs(store, r1["run_id"], r2["run_id"])
        assert cmp["equivalent"] is True
        assert store.count_candles() == candles_before
        assert store.count_validation_runs() == validation_before
        row = store.get_run(r1["run_id"])
        assert row is not None
        assert row["status"] == RUN_STATUS_COMPLETED
    finally:
        store.close()


def test_normalize_trend_states_stable_sort() -> None:
    class Snap:
        def to_dict(self):
            return {
                "decision_time": "2026-03-01T00:10:00+00:00",
                "current_state": "uptrend",
                "previous_state": "range",
                "active_reasons": ["r1"],
                "state_confidence": 0.5,
                "structure_5m": {"bias": "bullish"},
            }

    rows = normalize_trend_states([Snap(), Snap()])
    assert len(rows) == 2
    assert rows[0]["timestamp"] <= rows[1]["timestamp"]


def test_parameter_set_reused_in_memory() -> None:
    store = InMemoryResearchStore()
    params = _params()
    ph = parameter_hash(params)
    id1 = store.ensure_parameter_set(parameter_hash=ph, scanner_name="regime_scanner", params=params)
    id2 = store.ensure_parameter_set(parameter_hash=ph, scanner_name="regime_scanner", params=params)
    assert id1 == id2

"""Tests for backfill recovery, atomic JSON, and modality contracts."""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.btc_doge_research.atomic_json import atomic_write_json, read_json, read_json_lenient
from research.btc_doge_research.backfill_plan import build_backfill_plan, _eligibility
from research.btc_doge_research.full_history_contracts import (
    IMPORTABLE_MODALITIES,
    ModalityContractError,
    SHADOW_ARCHIVE_PRODUCER_ID,
    ob_producer_for_hour,
)
from research.btc_doge_research.full_history_runner import _assert_importable_modality, _filter_plan
from research.btc_doge_research.modality_coverage import _ch_modality_segment
from research.btc_doge_research.run_state import acquire_runner_lock, read_progress, release_runner_lock, status_snapshot
from research.btc_doge_research.segment_loader import SegmentContext, load_segment
from research.btc_doge_research.contracts import stable_hash as contract_stable_hash


def test_candles_coverage_only_never_importable():
    row = {"modality": "CANDLES", "status": "READY", "exclusion_reason": ""}
    eligibility, reason = _eligibility(row)
    assert eligibility == "COVERAGE_ONLY"
    assert reason == "CANDLES_TRACKED_NOT_IMPORTED"
    with pytest.raises(ModalityContractError):
        _assert_importable_modality("CANDLES")


def test_candles_not_in_import_denominator():
    plan = build_backfill_plan()
    importable = [r for r in plan if r.get("import_eligible")]
    assert all(r["modality"] != "CANDLES" for r in importable)
    coverage = [r for r in plan if r["eligibility"] == "COVERAGE_ONLY"]
    assert coverage and all(r["modality"] == "CANDLES" for r in coverage)


def test_filter_plan_excludes_candles():
    plan = build_backfill_plan()
    work = _filter_plan(plan, symbol=None, start=None, end=None)
    assert all(r["modality"] in IMPORTABLE_MODALITIES for r in work)


def test_unknown_modality_contract_error_before_loader():
    ctx = SegmentContext(
        symbol="BTCUSDT",
        modality="CANDLES",
        segment_start=datetime(2026, 7, 19, tzinfo=timezone.utc),
        segment_end=datetime(2026, 7, 20, tzinfo=timezone.utc),
        batch_id="x",
        build_id="y",
        contract_version="v",
        producer_id="p",
        source_semantics_version="s",
        source_fingerprint="f",
    )
    with pytest.raises(ModalityContractError):
        load_segment(None, ctx, datetime.now(timezone.utc))


def test_atomic_write_json_roundtrip(tmp_path: Path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"a": 1, "b": [2, 3]})
    assert read_json(target) == {"a": 1, "b": [2, 3]}


def test_atomic_write_concurrent_threads(tmp_path: Path):
    target = tmp_path / "progress.json"
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            for j in range(20):
                atomic_write_json(target, {"worker": i, "step": j})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    payload = read_json(target)
    assert "worker" in payload and "step" in payload


def _concurrent_writer(path_str: str, worker_id: int) -> None:
    path = Path(path_str)
    for i in range(15):
        atomic_write_json(path, {"worker": worker_id, "i": i})


def test_atomic_write_concurrent_processes(tmp_path: Path):
    target = tmp_path / "progress.json"
    path_str = str(target)
    processes = [multiprocessing.Process(target=_concurrent_writer, args=(path_str, w)) for w in range(3)]
    for proc in processes:
        proc.start()
    for proc in processes:
        proc.join()
        assert proc.exitcode == 0
    payload = read_json(target)
    assert isinstance(payload, dict)


def test_read_json_lenient_trailing_fragment(tmp_path: Path):
    target = tmp_path / "broken.json"
    target.write_text('{"completed": 1}\n\n  "updated_at": "x"\n}\n', encoding="utf-8")
    payload, corrupted = read_json_lenient(target)
    assert payload == {"completed": 1}
    assert corrupted is True


def test_runner_lock_second_acquire_fails(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    monkeypatch.setattr("research.btc_doge_research.run_state.RUN_STATE_DIR", run_dir)
    release_runner_lock()
    first = acquire_runner_lock()
    assert first["acquired"] is True
    second = acquire_runner_lock()
    assert second["acquired"] is False
    assert second["reason"] == "ALREADY_RUNNING"
    release_runner_lock()


def test_ob_producer_shadow_after_queue_full():
    hour = datetime(2026, 8, 31, 18, tzinfo=timezone.utc)
    producer, _ = ob_producer_for_hour(hour, file_exists=True)
    assert producer == SHADOW_ARCHIVE_PRODUCER_ID


def test_modality_partial_oi():
    day = datetime(2026, 8, 27, tzinfo=timezone.utc)
    ch = {"trade_count": 1000, "oi_count": 17268, "oi_unique": 17268, "candle_count": 1440, "liq_count": 10}
    seg = _ch_modality_segment("BTCUSDT", day, "OPEN_INTEREST", ch)
    assert seg["status"] == "PARTIAL"


def test_stable_hash_rejects_nan():
    with pytest.raises(ValueError):
        contract_stable_hash({"x": float("nan")})

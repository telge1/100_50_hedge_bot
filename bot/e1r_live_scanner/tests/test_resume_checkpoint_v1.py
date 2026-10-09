"""P3 resume prototype. Fast tests only — no warmup replay."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bot.e1r_live_scanner.processing import SymbolProcessor
from bot.e1r_live_scanner.config import ScannerConfig
from bot.e1r_live_scanner.resume_checkpoint import (
    CHECKPOINT_USED_FOR_STARTUP,
    MISSING_REBUILD_REQUIRED,
    RESTORED_FIELDS,
    atomic_write_json,
    compare_restored_state,
    load_resume_checkpoint,
    payload_state_hash,
    prime_e1r_walker_to_checkpoint,
    prime_frozen_walker_to_checkpoint,
    restore_symbol_state_from_checkpoint_v1,
    resume_and_replay_tail_v1,
)

_PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
_LIVE_CKPT = (
    Path(__file__).resolve().parents[1] / "runtime" / "checkpoints" / "APTUSDT.json"
)


def _opens(n: int = 4) -> list[datetime]:
    return [_PANE_FROM + timedelta(minutes=15 * i) for i in range(n)]


def _decision(opens: list[datetime], index: int) -> datetime:
    from bot.e1r_live_scanner.resume_checkpoint import _close_15m

    return _close_15m(opens[index])


def _episode() -> dict:
    return {
        "start_time": "2026-06-01T00:15:00+00:00",
        "close": 1.0,
        "ema200": 0.9,
        "cluster_2_pool_ids": ["c2"],
        "cluster_3_pool_ids": ["c3"],
        "variant": "E1R",
        "reclaims": [{"result": "EMA_RECLAIM_SUCCESS"}],
    }


def _reclaim_events() -> list[dict]:
    return [
        {
            "dip_time": "2026-06-02T00:15:00+00:00",
            "dip_close": 1.1,
            "result": "EMA_RECLAIM_SUCCESS",
            "cluster_2_pool_ids": ["c2"],
            "cluster_3_pool_ids": ["c3"],
        }
    ]


def _document(opens: list[datetime], index: int = 1, *, resume: bool = False, **meta_overrides) -> dict:
    decision = _decision(opens, index)
    e1r: dict = {
        "e1r_state": "ACTIVE",
        "e1r_machine": {decision.isoformat(): "ACTIVE"},
        "pending": None,
        "reclaim_count": 4,
    }
    if resume:
        e1r = {
            "snapshot_version": 2,
            "e1r_state": "ACTIVE",
            "e1r_machine": {decision.isoformat(): "ACTIVE"},
            "pending": None,
            "e1r_ep": _episode(),
            "reclaim_events": _reclaim_events(),
            "sim_from": "2026-04-01T00:00:00+00:00",
            "sim_to": decision.isoformat(),
        }
    payload = {
        "last_processed_15m_close": decision.isoformat(),
        "seen_no_guard": ["pool-b", "pool-a"],
        "seen_with_guard": ["pool-c"],
        "e1r": e1r,
        "floor_guard": {
            "block_until": "2026-10-06T12:00:00+00:00",
            "session_floor_top": 1.5,
            "session_floor_bottom": 1.4,
            "last_reason": "A1_touch",
        },
        "frozen_pool_diagnostic": {
            "available_pool_ids": ["p1", "p2"],
            "available_rows_hash": "abc",
            "candle_index": index,
            "frozen_config_version": 1,
        },
        "lld_cache_note": "not_serialized_in_p2",
    }
    if resume:
        payload["e1r_lld_diagnostic"] = {
            "pool_ids": ["e1"],
            "rows_hash": "eee",
            "candle_index": index,
        }
    metadata = {
        "schema_version": 2 if resume else 1,
        "symbol": "APTUSDT",
        "timeframe": "15m",
        "pane_from": _PANE_FROM.isoformat(),
        "checkpoint_decision_close": decision.isoformat(),
        "checkpoint_candle_index": index,
        "frozen_config_version": 1,
        "pane_first_open": _PANE_FROM.isoformat(),
        "pane_last_open": opens[-1].isoformat(),
        "strict_complete_buckets": True,
        "scanner_config_hash": "test",
        "created_at": "2026-10-06T12:47:36+00:00",
        "code_git_sha": None,
        "state_hash": payload_state_hash(payload),
    }
    metadata.update(meta_overrides)
    if "state_hash" not in meta_overrides:
        metadata["state_hash"] = payload_state_hash(payload)
    return {"metadata": metadata, "payload": payload}


def _write(tmp_path: Path, document: dict, name: str = "APTUSDT.json") -> Path:
    path = tmp_path / name
    atomic_write_json(path, document)
    return path


def test_valid_checkpoint_load(tmp_path):
    opens = _opens()
    path = _write(tmp_path, _document(opens))
    loaded = load_resume_checkpoint(path, pane_opens=opens, expected_symbol="APTUSDT")
    assert loaded.status == "VALID"
    assert loaded.readiness == "VALID_FOR_DIAGNOSTIC"
    assert loaded.resume_ready is False
    assert loaded.reasons == []
    assert loaded.resume_blockers == ["schema_v1_not_resumable"]


def test_corrupted_checkpoint_is_invalid(tmp_path):
    path = tmp_path / "APTUSDT.json"
    path.write_text("{not json", encoding="utf-8")
    loaded = load_resume_checkpoint(path, pane_opens=_opens())
    assert loaded.status == "INVALID"
    assert loaded.reasons == ["json_decode"]


def test_wrong_frozen_config_version_is_invalid(tmp_path):
    opens = _opens()
    path = _write(tmp_path, _document(opens, frozen_config_version=99))
    loaded = load_resume_checkpoint(path, pane_opens=opens)
    assert loaded.status == "INVALID"
    assert "frozen_config_version" in loaded.reasons
    assert "state_hash" not in loaded.reasons


def test_wrong_pane_from_is_invalid(tmp_path):
    opens = _opens()
    path = _write(tmp_path, _document(opens, pane_from="2026-01-01T00:00:00+00:00"))
    loaded = load_resume_checkpoint(path, pane_opens=opens)
    assert loaded.status == "INVALID"
    assert "pane_from" in loaded.reasons


def test_state_hash_mismatch_is_invalid(tmp_path):
    opens = _opens()
    doc = _document(opens)
    doc["metadata"]["state_hash"] = "0" * 64
    path = _write(tmp_path, doc)
    loaded = load_resume_checkpoint(path, pane_opens=opens)
    assert loaded.status == "INVALID"
    assert "state_hash" in loaded.reasons


def test_restore_seen_sets(tmp_path):
    opens = _opens()
    loaded = load_resume_checkpoint(_write(tmp_path, _document(opens, resume=True)), pane_opens=opens)
    assert loaded.resume_ready is True
    report = restore_symbol_state_from_checkpoint_v1(loaded)
    assert report.state.seen_no_guard == {"pool-a", "pool-b"}
    assert report.state.seen_with_guard == {"pool-c"}
    assert report.state.frozen_lld_cache == {}
    assert report.state.lld_cache == {}


def test_e1r_ep_and_reclaim_round_trip(tmp_path):
    opens = _opens()
    loaded = load_resume_checkpoint(_write(tmp_path, _document(opens, resume=True)), pane_opens=opens)
    engine = restore_symbol_state_from_checkpoint_v1(loaded).state.e1r_engine
    assert engine.e1r_state == "ACTIVE"
    assert engine.e1r_ep == _episode()
    assert engine.reclaim_events == _reclaim_events()
    assert engine.snapshot()["e1r_machine"] == loaded.payload["e1r"]["e1r_machine"]
    assert engine.pending is None


def test_active_without_episode_is_not_resume_ready(tmp_path):
    opens = _opens()
    doc = _document(opens, resume=True)
    doc["payload"]["e1r"]["e1r_ep"] = None
    doc["metadata"]["state_hash"] = payload_state_hash(doc["payload"])
    loaded = load_resume_checkpoint(_write(tmp_path, doc), pane_opens=opens)
    assert loaded.status == "INVALID"
    assert loaded.resume_ready is False
    assert "e1r_ep_required" in loaded.reasons
    with pytest.raises(ValueError, match="not resume_ready"):
        restore_symbol_state_from_checkpoint_v1(loaded)


def test_schema_v2_complete_is_resume_ready(tmp_path):
    opens = _opens()
    loaded = load_resume_checkpoint(_write(tmp_path, _document(opens, resume=True)), pane_opens=opens)
    assert loaded.status == "VALID"
    assert loaded.readiness == "VALID_FOR_RESUME"
    assert loaded.resume_ready is True
    assert loaded.metadata["schema_version"] == 2


def test_restore_floor_fields(tmp_path):
    opens = _opens()
    loaded = load_resume_checkpoint(_write(tmp_path, _document(opens, resume=True)), pane_opens=opens)
    guard = restore_symbol_state_from_checkpoint_v1(loaded).state.floor_guard
    assert guard.block_until == datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    assert guard.session_floor_top == 1.5
    assert guard.session_floor_bottom == 1.4
    assert guard.last_reason == "A1_touch"
    assert guard._candles_4h is None


def test_frozen_and_e1r_prime_parity(tmp_path):
    opens = _opens()
    loaded = load_resume_checkpoint(_write(tmp_path, _document(opens, resume=True)), pane_opens=opens)
    report = restore_symbol_state_from_checkpoint_v1(loaded)
    frozen = {
        "result": SimpleNamespace(
            available_pool_ids=["p1", "p2"],
            available_rows_hash="abc",
            candle_index=1,
        )
    }
    e1r = {"pool_ids": ["e1"], "rows_hash": "eee"}
    parity = compare_restored_state(loaded, report, frozen, e1r)
    assert parity["frozen_available_pool_ids"] == "MATCH"
    assert parity["frozen_rows_hash"] == "MATCH"
    assert parity["checkpoint_candle_index"] == "MATCH"
    assert parity["e1r_lld_pool_ids"] == "MATCH"
    assert parity["e1r_lld_rows_hash"] == "MATCH"
    assert parity["e1r_ep"] == "MATCH"
    assert parity["reclaim_events"] == "MATCH"
    assert parity["frozen_walker_object"] == "NOT_RESTORED"
    assert parity["e1r_lld_cache"] == "NOT_RESTORED"
    assert parity["last_processed_15m_close"] == "MATCH"
    assert "e1r_ep" in RESTORED_FIELDS
    assert "frozen_pool_walker" in MISSING_REBUILD_REQUIRED


def test_startup_production_path_ignores_checkpoint():
    assert CHECKPOINT_USED_FOR_STARTUP is False
    root = Path(__file__).resolve().parents[3]
    processing = (root / "bot/e1r_live_scanner/processing.py").read_text(encoding="utf-8")
    runner = (root / "bot/e1r_live_scanner/runner.py").read_text(encoding="utf-8")
    for banned in (
        "load_resume_checkpoint",
        "restore_symbol_state_from_checkpoint_v1",
        "prime_frozen_walker_to_checkpoint",
        "prime_e1r_walker_to_checkpoint",
        "resume_and_replay_tail_v1",
    ):
        assert banned not in processing
        assert banned not in runner
    load_src = inspect.getsource(SymbolProcessor.load_state)
    assert "checkpoint" not in load_src


def test_restore_refuses_invalid_checkpoint(tmp_path):
    path = tmp_path / "APTUSDT.json"
    path.write_text("[]", encoding="utf-8")
    loaded = load_resume_checkpoint(path, pane_opens=_opens())
    with pytest.raises(ValueError, match="not resume_ready"):
        restore_symbol_state_from_checkpoint_v1(loaded)


def test_schema_v1_restore_is_refused(tmp_path):
    opens = _opens()
    loaded = load_resume_checkpoint(_write(tmp_path, _document(opens)), pane_opens=opens)
    assert loaded.readiness == "VALID_FOR_DIAGNOSTIC"
    with pytest.raises(ValueError, match="not resume_ready"):
        restore_symbol_state_from_checkpoint_v1(loaded)


def _bars(opens: list[datetime]) -> list[dict]:
    return [
        {
            "open_time": open_time,
            "close_time": _decision(opens, index),
            "candle_index": index,
            "close": 1.0,
            "low": 0.9,
            "high": 1.1,
        }
        for index, open_time in enumerate(opens)
    ]


def test_tail_replay_processes_only_bars_after_checkpoint(tmp_path):
    opens = _opens()
    loaded = load_resume_checkpoint(_write(tmp_path, _document(opens, resume=True)), pane_opens=opens)
    bars = _bars(opens)
    decision = bars[1]["close_time"]
    calls: list[datetime] = []

    def _fake(st, bar, emit_signals=False):
        calls.append(bar["close_time"])
        st.seen_no_guard.add("tail:" + bar["close_time"].isoformat())
        return []

    proc = SymbolProcessor(ScannerConfig(live=False, write_resume_checkpoint=False))
    with patch.object(proc, "_process_one_bar", side_effect=_fake), patch(
        "bot.e1r_live_scanner.resume_checkpoint.prime_frozen_walker_to_checkpoint",
        return_value={
            "seconds": 0.0,
            "result": SimpleNamespace(
                available_pool_ids=["p1", "p2"],
                available_rows_hash="abc",
                candle_index=1,
            ),
        },
    ), patch(
        "bot.e1r_live_scanner.resume_checkpoint.prime_e1r_walker_to_checkpoint",
        return_value={
            "seconds": 0.0,
            "config_seconds": 0.0,
            "config": object(),
            "pool_ids": ["e1"],
            "rows_hash": "eee",
            "built_through": 1,
        },
    ):
        out = resume_and_replay_tail_v1(
            "APTUSDT",
            loaded,
            pane=[object(), object(), object(), object()],
            tail_end=bars[-1]["close_time"],
            processor=proc,
            bars15=bars,
            lld_cfg=object(),
        )
    assert calls == [bars[2]["close_time"], bars[3]["close_time"]]
    assert all(close > decision for close in calls)
    assert out["bars_at_or_before_checkpoint_processed"] == 0
    assert set(out["seen_no_guard_before_tail"]) == {"pool-a", "pool-b"}
    assert "pool-a" in out["state"].seen_no_guard
    assert "pool-b" in out["state"].seen_no_guard
    tail_tags = {item for item in out["state"].seen_no_guard if str(item).startswith("tail:")}
    assert tail_tags == {"tail:" + bars[2]["close_time"].isoformat(), "tail:" + bars[3]["close_time"].isoformat()}


def test_frozen_prime_is_one_kernel_call():
    sentinel = SimpleNamespace(available_pool_ids=["p"], available_rows_hash="h", candle_index=1)
    with patch(
        "bot.e1r_live_scanner.short_pools.available_for_short_entry",
        return_value=sentinel,
    ) as call:
        out = prime_frozen_walker_to_checkpoint(
            "APTUSDT",
            [object()],
            datetime(2026, 2, 10, 0, 30, tzinfo=timezone.utc),
            cache={},
        )
    call.assert_called_once()
    assert out["method"] == "available_for_short_entry_once"
    assert out["result"] is sentinel


def test_e1r_prime_is_one_scanner_call():
    with patch(
        "dashboard.research_charts.lld_research_kernel.ui_lld_config",
        return_value=object(),
    ), patch(
        "dashboard.research_charts.lld_research_kernel.scanner_pools_for_index",
        return_value=([], [], None),
    ) as call, patch(
        "bot.e1r_live_scanner.resume_checkpoint.walker_built_through",
        return_value=1,
    ):
        out = prime_e1r_walker_to_checkpoint([object(), object()], 1, cache={})
    call.assert_called_once()
    assert call.call_args.args[2] == 1
    assert out["method"] == "scanner_pools_for_index_once"
    assert out["built_through"] == 1


def test_on_disk_apt_checkpoint_hash_holds():
    if not _LIVE_CKPT.is_file():
        pytest.skip("no APT checkpoint")
    loaded = load_resume_checkpoint(_LIVE_CKPT, expected_symbol="APTUSDT")
    assert loaded.reasons == ["pane_not_loaded"]
    assert loaded.metadata["created_at"].startswith("2026-10-06T12:47:36")
    raw = json.loads(_LIVE_CKPT.read_text(encoding="utf-8"))
    assert payload_state_hash(raw["payload"]) == raw["metadata"]["state_hash"]

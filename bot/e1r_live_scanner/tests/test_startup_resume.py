"""Startup resume cutover (flag default off). No ClickHouse / no live runtime."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bot.e1r_live_scanner.config import ScannerConfig, ensure_runtime_paths
from bot.e1r_live_scanner.processing import SymbolProcessor
from bot.e1r_live_scanner.resume_checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    StartupResumeResult,
    atomic_write_json,
    load_resume_checkpoint,
    payload_state_hash,
    scanner_config_hash,
    try_startup_resume,
)
from bot.e1r_live_scanner.state import Readiness, SymbolState
from bot.e1r_live_scanner.warmup_metrics import WarmupMetrics

_PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)


def _bars(n: int = 5) -> list[dict]:
    out = []
    for i in range(n):
        open_time = _PANE_FROM + timedelta(minutes=15 * i)
        close_time = open_time + timedelta(minutes=15)
        out.append(
            {
                "open_time": open_time,
                "close_time": close_time,
                "candle_index": i,
                "close": 1.0 + i * 0.01,
                "low": 0.9,
                "high": 1.1,
            }
        )
    return out


def _episode() -> dict:
    return {
        "start_time": "2026-06-01T00:15:00+00:00",
        "cluster_2_pool_ids": ["c2"],
        "cluster_3_pool_ids": ["c3"],
        "variant": "E1R",
    }


def _schema2_document(
    opens: list[datetime],
    index: int = 2,
    symbol: str = "APTUSDT",
    cfg: ScannerConfig | None = None,
    **meta_overrides,
) -> dict:
    from bot.e1r_live_scanner.resume_checkpoint import _close_15m

    decision = _close_15m(opens[index])
    payload = {
        "last_processed_15m_close": decision.isoformat(),
        "seen_no_guard": ["a"],
        "seen_with_guard": ["b"],
        "e1r": {
            "snapshot_version": 2,
            "e1r_state": "ACTIVE",
            "e1r_machine": {decision.isoformat(): "ACTIVE"},
            "pending": None,
            "e1r_ep": _episode(),
            "reclaim_events": [],
            "sim_from": "2026-04-01T00:00:00+00:00",
            "sim_to": decision.isoformat(),
        },
        "floor_guard": {
            "block_until": None,
            "session_floor_top": None,
            "session_floor_bottom": None,
            "last_reason": None,
        },
        "frozen_pool_diagnostic": {
            "available_pool_ids": ["p1"],
            "available_rows_hash": "abc",
            "candle_index": index,
        },
        "e1r_lld_diagnostic": {"pool_ids": ["e1"], "rows_hash": "eee", "candle_index": index},
        "lld_cache_note": "not_serialized_in_p2",
    }
    cfg = cfg or ScannerConfig(checkpoint_dir=Path("/tmp/unused"))
    metadata = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "symbol": symbol,
        "timeframe": "15m",
        "pane_from": _PANE_FROM.isoformat(),
        "checkpoint_decision_close": decision.isoformat(),
        "checkpoint_candle_index": index,
        "frozen_config_version": 1,
        "pane_first_open": opens[0].isoformat(),
        "pane_last_open": opens[-1].isoformat(),
        "strict_complete_buckets": True,
        "scanner_config_hash": scanner_config_hash(cfg),
        "created_at": "2026-10-06T12:00:00+00:00",
        "state_hash": payload_state_hash(payload),
    }
    metadata.update(meta_overrides)
    if "state_hash" not in meta_overrides:
        metadata["state_hash"] = payload_state_hash(payload)
    return {"metadata": metadata, "payload": payload}


def _state_with_pane(n: int = 5) -> SymbolState:
    bars = _bars(n)
    st = SymbolState(symbol="APTUSDT", readiness=Readiness.WARMING)
    st.bars15 = bars
    st.candles15 = [SimpleNamespace(timestamp=b["open_time"]) for b in bars]
    st.by_open = {b["open_time"]: b for b in bars}
    st.market = {"4h": {"bars": []}, "15m": {"bars": []}}
    st.lld_cfg = object()
    st.strict_complete_buckets = True
    return st


def test_default_flags_on_for_productive_cutover():
    ensure_runtime_paths()
    cfg = ScannerConfig()
    assert cfg.use_resume_checkpoint_at_startup is True
    assert cfg.require_scanner_config_hash_match is True
    assert cfg.resume_startup_invariant_check is True


def test_feature_flag_off_uses_full_warmup_only():
    cfg = ScannerConfig(use_resume_checkpoint_at_startup=False)
    proc = SymbolProcessor(cfg)
    st = _state_with_pane()
    with patch.object(proc, "warmup", return_value=None) as warm:
        with patch("bot.e1r_live_scanner.processing.try_startup_resume") as try_resume:
            proc.warmup_from_checkpoint_or_full(st)
    warm.assert_called_once_with(st)
    try_resume.assert_not_called()


def test_valid_schema2_resume_skips_full_warmup(tmp_path):
    cfg = ScannerConfig(
        use_resume_checkpoint_at_startup=True,
        write_resume_checkpoint=True,
        checkpoint_dir=tmp_path,
    )
    proc = SymbolProcessor(cfg)
    st = _state_with_pane()
    loaded_meta = {"schema_version": 2, "checkpoint_decision_close": st.bars15[2]["close_time"].isoformat()}
    ok = StartupResumeResult(
        ok=True,
        loaded=SimpleNamespace(metadata=loaded_meta),
        tail_bars_processed=2,
        tail_seconds=0.1,
        legacy_prime_seconds=0.0,
        load_seconds=0.01,
        restore_seconds=0.01,
        prime_seconds=0.02,
    )
    with patch("bot.e1r_live_scanner.processing.try_startup_resume", return_value=ok):
        with patch.object(proc, "warmup") as warm:
            metrics = proc.warmup_from_checkpoint_or_full(st)
    warm.assert_not_called()
    assert metrics is not None
    assert metrics.startup_mode == "resume"
    assert metrics.resume_used is True


@pytest.mark.parametrize(
    "fallback_reason",
    [
        "file_missing",
        "json_decode",
        "schema_v1_not_resumable",
        "e1r_ep_required",
        "symbol",
        "scanner_config_hash",
        "checkpoint_after_latest_closed_bar",
        "startup_invariant_failed",
        "restore_error:ValueError",
        "prime_error:RuntimeError",
    ],
)
def test_resume_failure_falls_back_to_full_warmup(tmp_path, fallback_reason: str):
    cfg = ScannerConfig(use_resume_checkpoint_at_startup=True, checkpoint_dir=tmp_path)
    proc = SymbolProcessor(cfg)
    st = _state_with_pane()
    fresh = _state_with_pane()
    fresh.readiness = Readiness.WARMING
    base_metrics = WarmupMetrics(
        symbol="APTUSDT",
        warmup_started_at="t0",
        warmup_finished_at="t1",
        bars_processed=5,
        first_close=None,
        last_close=None,
        pane_load_seconds=0.0,
        replay_seconds=1.0,
        legacy_prime_seconds=0.0,
        total_seconds=1.0,
    )
    fail = StartupResumeResult(ok=False, fallback_reason=fallback_reason, load_seconds=0.01)
    with patch("bot.e1r_live_scanner.processing.try_startup_resume", return_value=fail):
        with patch.object(proc, "load_state", return_value=fresh) as load_state:
            with patch.object(proc, "warmup", return_value=base_metrics) as warm:
                metrics = proc.warmup_from_checkpoint_or_full(st)
    load_state.assert_called_once()
    warm.assert_called_once()
    assert metrics is not None
    assert metrics.startup_mode == "fallback_full"
    assert metrics.resume_fallback_reason == fallback_reason
    assert st.bars15 == fresh.bars15


def test_fallback_replaces_partial_state(tmp_path):
    cfg = ScannerConfig(use_resume_checkpoint_at_startup=True, checkpoint_dir=tmp_path)
    proc = SymbolProcessor(cfg)
    st = _state_with_pane()
    st.seen_no_guard = {"poison"}
    fresh = _state_with_pane()
    fresh.seen_no_guard = set()
    fail = StartupResumeResult(ok=False, fallback_reason="startup_invariant_failed")
    with patch("bot.e1r_live_scanner.processing.try_startup_resume", return_value=fail):
        with patch.object(proc, "load_state", return_value=fresh):
            with patch.object(proc, "warmup", return_value=WarmupMetrics(
                symbol="APTUSDT",
                warmup_started_at="a",
                warmup_finished_at="b",
                bars_processed=1,
                first_close=None,
                last_close=None,
                pane_load_seconds=0.0,
                replay_seconds=0.0,
                legacy_prime_seconds=0.0,
                total_seconds=0.0,
            )):
                proc.warmup_from_checkpoint_or_full(st)
    assert st.seen_no_guard == set()


def test_try_startup_resume_tail_only_replays_after_checkpoint(tmp_path):
    opens = [b["open_time"] for b in _bars(5)]
    cfg = ScannerConfig(
        checkpoint_dir=tmp_path,
        require_scanner_config_hash_match=True,
        resume_startup_invariant_check=False,
        frozen_pool_transition_parity=False,
    )
    doc = _schema2_document(opens, index=2, cfg=cfg)
    atomic_write_json(tmp_path / "APTUSDT.json", doc)
    proc = SymbolProcessor(cfg)
    st = _state_with_pane()
    calls: list[datetime] = []

    def _fake_process(target, bar, emit_signals=False):
        calls.append(bar["close_time"])
        target.last_processed_15m_close = bar["close_time"]
        return []

    proc._process_one_bar = _fake_process  # type: ignore[method-assign]
    ensure_runtime_paths()
    with patch(
        "bot.e1r_live_scanner.resume_checkpoint.prime_frozen_walker_to_checkpoint",
        return_value={"result": SimpleNamespace(available_pool_ids=["p1"], available_rows_hash="abc", candle_index=2)},
    ), patch(
        "bot.e1r_live_scanner.resume_checkpoint.prime_e1r_walker_to_checkpoint",
        return_value={"config": st.lld_cfg, "pool_ids": ["e1"], "rows_hash": "eee"},
    ):
        result = try_startup_resume(st, proc, config=cfg)
    assert result.ok is True
    assert calls == [st.bars15[3]["close_time"], st.bars15[4]["close_time"]]
    assert st.last_processed_15m_close == st.bars15[4]["close_time"]


def test_try_startup_resume_no_tail_when_at_pane_end(tmp_path):
    opens = [b["open_time"] for b in _bars(3)]
    cfg = ScannerConfig(
        checkpoint_dir=tmp_path,
        resume_startup_invariant_check=False,
        frozen_pool_transition_parity=False,
    )
    doc = _schema2_document(opens, index=2, cfg=cfg)
    atomic_write_json(tmp_path / "APTUSDT.json", doc)
    proc = SymbolProcessor(cfg)
    st = _state_with_pane(3)
    proc._process_one_bar = MagicMock(return_value=[])  # type: ignore[method-assign]
    ensure_runtime_paths()
    with patch(
        "bot.e1r_live_scanner.resume_checkpoint.prime_frozen_walker_to_checkpoint",
        return_value={"result": SimpleNamespace(available_pool_ids=["p1"], available_rows_hash="abc", candle_index=2)},
    ), patch(
        "bot.e1r_live_scanner.resume_checkpoint.prime_e1r_walker_to_checkpoint",
        return_value={"config": st.lld_cfg, "pool_ids": ["e1"], "rows_hash": "eee"},
    ):
        result = try_startup_resume(st, proc, config=cfg)
    assert result.ok is True
    assert result.tail_bars_processed == 0
    proc._process_one_bar.assert_not_called()
    assert st.last_processed_15m_close == st.bars15[2]["close_time"]


def test_schema_v1_checkpoint_not_resume_ready(tmp_path):
    opens = [b["open_time"] for b in _bars(4)]
    cfg = ScannerConfig(checkpoint_dir=tmp_path)
    doc = _schema2_document(opens, index=1, cfg=cfg)
    doc["metadata"]["schema_version"] = 1
    doc["payload"]["e1r"] = {
        "e1r_state": "ACTIVE",
        "e1r_machine": {},
        "pending": None,
        "reclaim_count": 0,
    }
    doc["metadata"]["state_hash"] = payload_state_hash(doc["payload"])
    atomic_write_json(tmp_path / "APTUSDT.json", doc)
    st = _state_with_pane(4)
    proc = SymbolProcessor(cfg)
    ensure_runtime_paths()
    result = try_startup_resume(st, proc, config=cfg)
    assert result.ok is False
    assert result.fallback_reason == "schema_v1_not_resumable"


def test_active_without_e1r_ep_blocks_resume(tmp_path):
    opens = [b["open_time"] for b in _bars(4)]
    cfg = ScannerConfig(checkpoint_dir=tmp_path)
    doc = _schema2_document(opens, index=1, cfg=cfg)
    doc["payload"]["e1r"]["e1r_ep"] = None
    doc["metadata"]["state_hash"] = payload_state_hash(doc["payload"])
    atomic_write_json(tmp_path / "APTUSDT.json", doc)
    loaded = load_resume_checkpoint(tmp_path / "APTUSDT.json", pane_opens=opens, expected_symbol="APTUSDT")
    assert loaded.resume_ready is False
    assert "e1r_ep_required" in loaded.reasons


def test_corrupt_json_fallback_reason(tmp_path):
    (tmp_path / "APTUSDT.json").write_text("{bad", encoding="utf-8")
    cfg = ScannerConfig(checkpoint_dir=tmp_path)
    st = _state_with_pane()
    result = try_startup_resume(st, SymbolProcessor(cfg), config=cfg)
    assert result.ok is False
    assert result.fallback_reason == "json_decode"


def test_scanner_config_hash_mismatch(tmp_path):
    opens = [b["open_time"] for b in _bars(4)]
    doc = _schema2_document(opens, index=1, scanner_config_hash="deadbeef")
    atomic_write_json(tmp_path / "APTUSDT.json", doc)
    cfg = ScannerConfig(checkpoint_dir=tmp_path, require_scanner_config_hash_match=True)
    result = try_startup_resume(_state_with_pane(4), SymbolProcessor(cfg), config=cfg)
    assert result.ok is False
    assert result.fallback_reason == "scanner_config_hash"


def test_resume_and_full_reference_same_end_state(tmp_path):
    """Resume tail + restore should match sequential full replay on a tiny fixture."""
    bars = _bars(5)
    opens = [b["open_time"] for b in bars]
    cfg = ScannerConfig(
        checkpoint_dir=tmp_path,
        frozen_pool_transition_parity=False,
        resume_startup_invariant_check=False,
    )
    doc = _schema2_document(opens, index=2, cfg=cfg)
    atomic_write_json(tmp_path / "APTUSDT.json", doc)

    def _step(st, bar):
        st.seen_no_guard.add(f"bar-{bar['candle_index']}")
        st.last_processed_15m_close = bar["close_time"]
        return []

    full = _state_with_pane(5)
    for bar in full.bars15:
        _step(full, bar)

    proc = SymbolProcessor(cfg)
    resume = _state_with_pane(5)
    proc._process_one_bar = lambda st, bar, emit_signals=False: _step(st, bar)  # type: ignore[method-assign]
    with patch(
        "bot.e1r_live_scanner.resume_checkpoint.prime_frozen_walker_to_checkpoint",
        return_value={"result": SimpleNamespace(available_pool_ids=["p1"], available_rows_hash="abc", candle_index=2)},
    ), patch(
        "bot.e1r_live_scanner.resume_checkpoint.prime_e1r_walker_to_checkpoint",
        return_value={"config": resume.lld_cfg, "pool_ids": ["e1"], "rows_hash": "eee"},
    ), patch(
        "bot.e1r_live_scanner.resume_checkpoint.restore_symbol_state_from_checkpoint_v1"
    ) as restore:
        restored = _state_with_pane(5)
        restored.seen_no_guard = {"bar-0", "bar-1", "bar-2"}
        restored.last_processed_15m_close = bars[2]["close_time"]
        restore.return_value = SimpleNamespace(state=restored)
        result = try_startup_resume(resume, proc, config=cfg)
    assert result.ok is True
    assert resume.seen_no_guard == full.seen_no_guard
    assert resume.last_processed_15m_close == full.last_processed_15m_close


def test_runner_uses_wrapper_not_bare_warmup():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "runner.py").read_text(encoding="utf-8")
    assert "warmup_from_checkpoint_or_full" in text
    assert "metrics = proc.warmup(st)" not in text

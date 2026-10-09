"""P1 warmup metrics + P2 read-only checkpoint (fast tests only)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_repo = Path(__file__).resolve().parents[3]
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

from bot.e1r_live_scanner.config import ScannerConfig, ensure_runtime_paths
from bot.e1r_live_scanner.e1r_v1.engine import E1REngine
from bot.e1r_live_scanner.processing import SymbolProcessor
from bot.e1r_live_scanner.resume_checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    atomic_write_json,
    build_checkpoint_document,
    checkpoint_path_for_symbol,
    floor_guard_snapshot,
    scanner_config_hash,
    write_read_only_checkpoint,
)
from bot.e1r_live_scanner.state import Readiness, SymbolState

ensure_runtime_paths()

_TRP = Path("/home/telgenbuescher/projects/trading_research_platform").is_dir()


def test_atomic_write_leaves_no_final_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "DOGEUSDT.json"

    def _boom(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("bot.e1r_live_scanner.resume_checkpoint.os.replace", _boom)
    with pytest.raises(OSError):
        atomic_write_json(path, {"ok": True})
    assert not path.is_file()


def test_atomic_write_creates_final_file(tmp_path):
    path = tmp_path / "X.json"
    atomic_write_json(path, {"a": 1})
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}


def test_floor_guard_snapshot_fields():
    from short_block_4h_guard import H4FloorGuard

    g = H4FloorGuard(
        "DOGEUSDT",
        datetime(2026, 2, 10, tzinfo=timezone.utc),
        datetime(2026, 10, 6, tzinfo=timezone.utc),
    )
    g.block_until = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    g.session_floor_top = 1.5
    g.session_floor_bottom = 1.4
    g.last_reason = "A1_touch"
    snap = floor_guard_snapshot(g)
    assert snap["block_until"].startswith("2026-10-06")
    assert snap["session_floor_top"] == 1.5
    assert snap["last_reason"] == "A1_touch"


def test_e1r_snapshot_json_serializable():
    eng = E1REngine(
        datetime(2026, 4, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 6, tzinfo=timezone.utc),
    )
    eng.e1r_state = "ACTIVE"
    eng.e1r_machine = {datetime(2026, 6, 1, tzinfo=timezone.utc): "ACTIVE"}
    raw = json.dumps(eng.snapshot(), default=str)
    assert "ACTIVE" in raw


def test_warmup_metrics_one_summary_per_symbol(tmp_path):
    cfg = ScannerConfig(
        live=False,
        write_resume_checkpoint=False,
        frozen_pool_transition_parity=False,
    )
    proc = SymbolProcessor(cfg)
    st = SymbolState(symbol="X", readiness=Readiness.WARMING)
    st.bars15 = [
        {
            "open_time": datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
            "close_time": datetime(2026, 6, 1, 0, 15, tzinfo=timezone.utc),
            "candle_index": 0,
            "close": 1.0,
            "low": 0.9,
            "high": 1.1,
        },
        {
            "open_time": datetime(2026, 6, 1, 0, 15, tzinfo=timezone.utc),
            "close_time": datetime(2026, 6, 1, 0, 30, tzinfo=timezone.utc),
            "candle_index": 1,
            "close": 1.0,
            "low": 0.9,
            "high": 1.1,
        },
    ]
    st.candles15 = []
    st.market = {"4h": {"bars": []}, "15m": {"bars": []}}
    st.strict_complete_buckets = True
    st.pane_load_seconds = 0.01
    st.lld_cfg = object()
    from short_block_4h_guard import H4FloorGuard

    st.floor_guard = H4FloorGuard(
        "X",
        datetime(2026, 2, 10, tzinfo=timezone.utc),
        datetime(2026, 10, 6, tzinfo=timezone.utc),
    )
    st.e1r_engine = E1REngine(
        datetime(2026, 4, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 6, tzinfo=timezone.utc),
    )

    with patch.object(proc, "_process_one_bar", return_value=[]):
        with patch.object(proc, "_prime_legacy_transition_cache"):
            m = proc.warmup(st, through_close=st.bars15[-1]["close_time"])
    assert m is not None
    assert m.bars_processed == 2
    assert m.first_close == st.bars15[0]["close_time"].isoformat()
    assert m.last_close == st.bars15[-1]["close_time"].isoformat()
    summary = m.as_summary()
    assert summary["event"] == "warmup_summary"
    assert summary["bars_processed"] == 2


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_checkpoint_written_after_warmup(tmp_path):
    cfg = ScannerConfig(
        live=False,
        write_resume_checkpoint=True,
        checkpoint_dir=tmp_path / "ckpt",
        frozen_pool_transition_parity=True,
    )
    proc = SymbolProcessor(cfg)
    st = proc.load_state("DOGEUSDT")
    if not st.bars15:
        pytest.skip("no pane")
    through = st.bars15[420]["close_time"]
    proc.warmup(st, through_close=through)
    path = checkpoint_path_for_symbol(cfg, "DOGEUSDT")
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    meta = doc["metadata"]
    assert meta["schema_version"] == CHECKPOINT_SCHEMA_VERSION
    assert meta["symbol"] == "DOGEUSDT"
    assert meta["checkpoint_decision_close"] == through.isoformat()
    assert meta["scanner_config_hash"] == scanner_config_hash(cfg)
    assert "state_hash" in meta
    assert sorted(doc["payload"]["seen_no_guard"]) == sorted(st.seen_no_guard)


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_frozen_checkpoint_diagnostic_matches_kernel(tmp_path):
    cfg = ScannerConfig(
        live=False,
        write_resume_checkpoint=True,
        checkpoint_dir=tmp_path / "ckpt",
        frozen_pool_transition_parity=False,
    )
    proc = SymbolProcessor(cfg)
    st = proc.load_state("DOGEUSDT")
    if not st.bars15:
        pytest.skip("no pane")
    through_bar = st.bars15[420]
    proc.warmup(st, through_close=through_bar["close_time"])
    from bot.e1r_live_scanner.short_pools import available_for_short_entry

    live = available_for_short_entry(
        st.symbol,
        st.candles15,
        through_bar["close_time"],
        frozen_lld_cache=st.frozen_lld_cache,
    )
    doc = json.loads(checkpoint_path_for_symbol(cfg, "DOGEUSDT").read_text(encoding="utf-8"))
    diag = doc["payload"]["frozen_pool_diagnostic"]
    assert diag["available_pool_ids"] == live.available_pool_ids
    assert diag["available_rows_hash"] == live.available_rows_hash


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_load_state_does_not_use_checkpoint(tmp_path):
    cfg = ScannerConfig(
        live=False,
        write_resume_checkpoint=True,
        checkpoint_dir=tmp_path / "ckpt",
        frozen_pool_transition_parity=False,
    )
    proc = SymbolProcessor(cfg)
    st1 = proc.load_state("DOGEUSDT")
    if not st1.bars15:
        pytest.skip("no pane")
    proc.warmup(st1, through_close=st1.bars15[300]["close_time"])
    assert checkpoint_path_for_symbol(cfg, "DOGEUSDT").is_file()

    st2 = proc.load_state("DOGEUSDT")
    assert st2.last_processed_15m_close is None
    assert st2.readiness in (Readiness.WARMING, Readiness.LIVE_READY)
    assert st2.seen_no_guard == set()
    with patch("bot.e1r_live_scanner.frozen_pool_transition.run_transition_parity") as rtp:
        proc.warmup(st2, through_close=st2.bars15[305]["close_time"])
        rtp.assert_not_called()


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_p0_regression_warmup_primes_once(tmp_path):
    cfg = ScannerConfig(
        live=False,
        write_resume_checkpoint=False,
        frozen_pool_transition_parity=True,
    )
    proc = SymbolProcessor(cfg)
    st = proc.load_state("DOGEUSDT")
    if not st.bars15:
        pytest.skip("no pane")
    with patch("bot.e1r_live_scanner.frozen_pool_transition.run_transition_parity") as rtp:
        with patch.object(proc, "_prime_legacy_transition_cache") as prime:
            proc.warmup(st, through_close=st.bars15[410]["close_time"])
            rtp.assert_not_called()
            prime.assert_called_once()

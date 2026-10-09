"""Phase 3B: frozen pool kernel is productive for short entry_setup."""

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
from bot.e1r_live_scanner.frozen_pool_transition import REASON_MISMATCH, TransitionParityOutcome
from bot.e1r_live_scanner.processing import SymbolProcessor

ensure_runtime_paths()

_TRP = Path("/home/telgenbuescher/projects/trading_research_platform").is_dir()


def _load_bar(symbol: str = "DOGEUSDT", min_idx: int = 500):
    proc = SymbolProcessor(
        ScannerConfig(
            live=False,
            frozen_pool_transition_parity=False,
            report_from=datetime(2026, 2, 10, tzinfo=timezone.utc),
        )
    )
    st = proc.load_state(symbol)
    if not st.bars15:
        pytest.skip("no 15m pane")
    bar = st.bars15[min(min_idx, len(st.bars15) - 1)]
    return proc, st, bar


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_frozen_becomes_authoritative_for_entry_setup(tmp_path):
    proc, st, bar = _load_bar()
    cfg = proc.config
    cfg.frozen_pool_transition_parity = True
    cfg.frozen_pool_transition_log_path = tmp_path / "t.jsonl"

    pool_a = {"pool_id": "lld:TEST:15m:upper:111", "side": "upper"}
    pool_b = {"pool_id": "lld:TEST:15m:upper:222", "side": "upper"}

    from dashboard.research_charts.frozen_pool_kernel_v1 import FrozenPoolKernelResultV1

    moment = bar["close_time"]
    fake_frozen = FrozenPoolKernelResultV1(
        symbol=st.symbol,
        timeframe="15m",
        decision_timestamp=moment,
        candle_open_timestamp=bar["open_time"],
        candle_index=bar["candle_index"],
        pane_first_timestamp=datetime(2026, 2, 10, tzinfo=timezone.utc),
        pane_last_timestamp=bar["open_time"],
        available=[pool_a],
        available_pool_ids=[pool_a["pool_id"]],
        frozen_config_version=1,
        available_rows_hash="a" * 64,
        strict_complete_buckets=True,
    )
    captured: list = []

    def _capture_baseline(*_a, pools, **_kw):
        captured.append(list(pools))
        return []

    pass_record = {
        "symbol": st.symbol,
        "decision_time": moment.isoformat(),
        "frozen_count": 1,
        "legacy_count": 1,
        "frozen_hash": "a" * 64,
        "legacy_hash": "b" * 64,
        "status": "PASS",
        "candle_index": bar["candle_index"],
        "pane_first": datetime(2026, 2, 10, tzinfo=timezone.utc).isoformat(),
        "pane_last": bar["open_time"].isoformat(),
        "config_version": 1,
    }
    with patch(
        "bot.e1r_live_scanner.short_pools.available_for_short_entry",
        return_value=fake_frozen,
    ):
        with patch(
            "bot.e1r_live_scanner.frozen_pool_transition.compare_legacy_to_frozen_transition",
            return_value=pass_record,
        ):
            with patch(
                "bot.e1r_live_scanner.processing.process_bar_baseline",
                side_effect=_capture_baseline,
            ):
                proc._process_one_bar(st, bar, emit_signals=True)

    assert captured
    assert captured[0][0]["pool_id"] == pool_a["pool_id"]


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_transition_parity_pass_allows_normal_processing(tmp_path):
    log_path = tmp_path / "transition.jsonl"
    cfg = ScannerConfig(
        live=False,
        frozen_pool_transition_parity=True,
        frozen_pool_transition_log_path=log_path,
    )
    proc = SymbolProcessor(cfg)
    st = proc.load_state("DOGEUSDT")
    if not st.bars15:
        pytest.skip("no 15m pane")
    bar = st.bars15[min(800, len(st.bars15) - 1)]
    before_no = set(st.seen_no_guard)
    before_with = set(st.seen_with_guard)
    proc._process_one_bar(st, bar, emit_signals=True)
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    record = json.loads(lines[-1])
    assert record["status"] == "PASS"
    assert record["frozen_count"] == record["legacy_count"]
    assert st.seen_no_guard >= before_no
    assert st.seen_with_guard >= before_with


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_transition_parity_mismatch_blocks_signals(tmp_path):
    from dashboard.research_charts.short_pool_oracle_v1 import ORACLE_FIXTURES, parse_decision_iso

    symbol, decision_iso = ORACLE_FIXTURES[0]
    decision = parse_decision_iso(decision_iso)
    log_path = tmp_path / "transition.jsonl"
    cfg = ScannerConfig(
        live=False,
        frozen_pool_transition_parity=True,
        frozen_pool_transition_log_path=log_path,
    )
    proc = SymbolProcessor(cfg)
    st = proc.load_state(symbol)
    bar = next(b for b in st.bars15 if b["close_time"] == decision)
    seen_no = set(st.seen_no_guard)
    seen_with = set(st.seen_with_guard)

    mismatch_record = {
        "symbol": symbol,
        "decision_time": decision.isoformat(),
        "frozen_count": 3,
        "legacy_count": 0,
        "frozen_hash": "f" * 64,
        "legacy_hash": "l" * 64,
        "status": "MISMATCH",
        "reason_code": REASON_MISMATCH,
        "only_frozen": ["a"],
        "only_legacy": ["b"],
        "candle_index": bar["candle_index"],
        "pane_first": datetime(2026, 2, 10, tzinfo=timezone.utc).isoformat(),
        "pane_last": bar["open_time"].isoformat(),
        "config_version": 1,
    }
    with patch(
        "bot.e1r_live_scanner.frozen_pool_transition.compare_legacy_to_frozen_transition",
        return_value=mismatch_record,
    ):
        rows = proc._process_one_bar(st, bar, emit_signals=True)

    assert rows == []
    assert st.seen_no_guard == seen_no
    assert st.seen_with_guard == seen_with
    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert record["status"] == "MISMATCH"
    assert record["reason_code"] == REASON_MISMATCH
    assert st.state_ts.get("frozen_pool_block_reason") == REASON_MISMATCH


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_transition_parity_flag_off_skips_legacy_compare(tmp_path):
    log_path = tmp_path / "transition.jsonl"
    cfg = ScannerConfig(
        live=False,
        frozen_pool_transition_parity=False,
        frozen_pool_transition_log_path=log_path,
    )
    proc = SymbolProcessor(cfg)
    st = proc.load_state("DOGEUSDT")
    if not st.bars15:
        pytest.skip("no 15m pane")
    bar = st.bars15[400]
    with patch("bot.e1r_live_scanner.frozen_pool_transition.compare_legacy_to_frozen_transition") as cmp:
        proc._process_one_bar(st, bar, emit_signals=False)
        cmp.assert_not_called()
    assert not log_path.exists()


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_productive_bar_path_does_not_reload_pane_from_ch():
    proc, st, bar = _load_bar()
    with patch(
        "dashboard.research_charts.frozen_pool_kernel_v1.load_pane_candles"
    ) as kernel_load:
        with patch(
            "dashboard.research_charts.frozen_pool_kernel_v1.available_at_decision_v1"
        ) as at_decision:
            proc._process_one_bar(st, bar, emit_signals=False)
            kernel_load.assert_not_called()
            at_decision.assert_not_called()


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_frozen_config_isolation_from_workspace(tmp_path):
    """Wrong ui_lld_config must not change productive pools when transition parity is off."""
    from dataclasses import replace

    from dashboard.research_charts.frozen_pool_kernel_v1 import available_from_loaded_pane_v1
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index, ui_lld_config
    from dashboard.research_charts.short_pool_oracle_v1 import ORACLE_FIXTURES, parse_decision_iso

    symbol, decision_iso = ORACLE_FIXTURES[1]
    decision = parse_decision_iso(decision_iso)
    cfg = ScannerConfig(live=False, frozen_pool_transition_parity=False)
    proc = SymbolProcessor(cfg)
    st = proc.load_state(symbol)
    bar = next(b for b in st.bars15 if b["close_time"] == decision)
    idx = bar["candle_index"]
    expected = available_from_loaded_pane_v1(symbol, st.candles15, decision, cache={})
    bad_cfg = replace(ui_lld_config("15m"), amount=1, highest_len=1, lowest_len=1)
    legacy_bad, _, _ = scanner_pools_for_index(
        st.candles15, "15m", idx, bad_cfg, cache={}
    )
    legacy_bad_ids = sorted(r["pool_id"] for r in legacy_bad)
    assert legacy_bad_ids != expected.available_pool_ids

    captured: list = []

    def _capture_baseline(*_a, pools, **_kw):
        captured.append(list(pools))
        return []

    with patch("bot.e1r_live_scanner.processing.process_bar_baseline", side_effect=_capture_baseline):
        proc._process_one_bar(st, bar, emit_signals=False)

    assert captured
    got_ids = sorted(p["pool_id"] for p in captured[0])
    assert got_ids == expected.available_pool_ids


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_warmup_emit_false_unchanged_seen_sets():
    proc, st, bar = _load_bar()
    seen_no = set(st.seen_no_guard)
    seen_with = set(st.seen_with_guard)
    proc._process_one_bar(st, bar, emit_signals=False)
    assert st.seen_no_guard == seen_no
    assert st.seen_with_guard == seen_with


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_warmup_skips_transition_parity_and_primes_legacy(tmp_path):
    log_path = tmp_path / "transition.jsonl"
    cfg = ScannerConfig(
        live=False,
        frozen_pool_transition_parity=True,
        frozen_pool_transition_log_path=log_path,
    )
    proc = SymbolProcessor(cfg)
    st = proc.load_state("DOGEUSDT")
    if not st.bars15:
        pytest.skip("no 15m pane")
    through_bar = st.bars15[420]
    with patch("bot.e1r_live_scanner.frozen_pool_transition.run_transition_parity") as rtp:
        proc.warmup(st, through_close=through_bar["close_time"])
        rtp.assert_not_called()
    assert not log_path.exists()
    from dashboard.research_charts.lld_research_kernel import CAUSAL_LLD_WALKER_KEY

    walker = st.legacy_lld_cache.get(CAUSAL_LLD_WALKER_KEY)
    assert walker is not None
    assert walker._built_through == through_bar["candle_index"]


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_emit_signals_true_runs_transition_parity(tmp_path):
    log_path = tmp_path / "transition.jsonl"
    cfg = ScannerConfig(
        live=False,
        frozen_pool_transition_parity=True,
        frozen_pool_transition_log_path=log_path,
    )
    proc, st, bar = _load_bar()
    proc.config = cfg
    with patch(
        "bot.e1r_live_scanner.frozen_pool_transition.run_transition_parity",
        return_value=TransitionParityOutcome(block_signals=False, reason_code=None, record=None),
    ) as rtp:
        proc._process_one_bar(st, bar, emit_signals=True)
        rtp.assert_called_once()
    with patch("bot.e1r_live_scanner.frozen_pool_transition.run_transition_parity") as rtp2:
        proc._process_one_bar(st, bar, emit_signals=False)
        rtp2.assert_not_called()


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_pane_extend_clears_frozen_and_legacy_caches():
    proc, st, _bar = _load_bar()
    st.frozen_lld_cache["x"] = 1
    st.legacy_lld_cache["y"] = 2
    st.lld_cache["z"] = 3
    old_len = len(st.bars15)
    market, candles15, bars15, packed, strict = proc._fetch_market_and_pane(st.symbol)
    extended = list(bars15) + [bars15[-1]]
    with patch.object(
        proc,
        "_fetch_market_and_pane",
        return_value=(market, candles15, extended, packed, strict),
    ):
        proc.refresh_candles(st)
    assert len(st.bars15) > old_len
    assert "x" not in st.frozen_lld_cache
    assert "y" not in st.legacy_lld_cache
    assert "z" not in st.lld_cache

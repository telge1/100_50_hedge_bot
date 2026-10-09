"""Phase 3A: Short frozen pool shadow parity (flag off by default)."""

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
from bot.e1r_live_scanner.frozen_pool_shadow import (
    compare_frozen_pool_shadow,
    maybe_run_frozen_pool_shadow_parity,
)
from bot.e1r_live_scanner.processing import SymbolProcessor
from bot.e1r_live_scanner.state import SymbolState

ensure_runtime_paths()


@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
def test_shadow_flag_off_does_not_call_compare(tmp_path):
    cfg = ScannerConfig(live=False, frozen_pool_shadow_parity=False)
    proc = SymbolProcessor(cfg)
    st = proc.load_state("DOGEUSDT")
    if not st.bars15:
        pytest.skip("no 15m pane")
    bar = st.bars15[min(500, len(st.bars15) - 1)]
    with patch("bot.e1r_live_scanner.frozen_pool_shadow.compare_frozen_pool_shadow") as cmp:
        proc._process_one_bar(st, bar, emit_signals=False)
        cmp.assert_not_called()


@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
def test_shadow_flag_on_passes_and_logs(tmp_path):
    log_path = tmp_path / "parity.jsonl"
    cfg = ScannerConfig(live=False, frozen_pool_shadow_parity=True, frozen_pool_parity_log_path=log_path)
    proc = SymbolProcessor(cfg)
    st = proc.load_state("DOGEUSDT")
    if not st.bars15:
        pytest.skip("no 15m pane")
    bar = st.bars15[min(500, len(st.bars15) - 1)]
    seen_no = set(st.seen_no_guard)
    seen_with = set(st.seen_with_guard)
    proc._process_one_bar(st, bar, emit_signals=False)
    assert st.seen_no_guard == seen_no
    assert st.seen_with_guard == seen_with
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    record = json.loads(lines[-1])
    assert record["status"] == "PASS"
    assert record["current_count"] == record["frozen_count"]
    assert "only_current" not in record


@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
def test_shadow_mismatch_logged(tmp_path):
    from dashboard.research_charts.short_pool_oracle_v1 import ORACLE_FIXTURES, parse_decision_iso

    log_path = tmp_path / "parity.jsonl"
    symbol, decision_iso = ORACLE_FIXTURES[0]
    decision = parse_decision_iso(decision_iso)
    proc = SymbolProcessor(ScannerConfig(live=False))
    st = proc.load_state(symbol)
    if not st.bars15:
        pytest.skip("no 15m pane")
    bar = next(b for b in st.bars15 if b["close_time"] == decision)

    def _fake_frozen(*_a, **_k):
        from dashboard.research_charts.frozen_pool_kernel_v1 import FrozenPoolKernelResultV1

        return FrozenPoolKernelResultV1(
            symbol=symbol,
            timeframe="15m",
            decision_timestamp=decision,
            candle_open_timestamp=bar["open_time"],
            candle_index=bar["candle_index"],
            pane_first_timestamp=datetime(2026, 2, 10, tzinfo=timezone.utc),
            pane_last_timestamp=bar["open_time"],
            available=[],
            available_pool_ids=["lld:FAKE:15m:upper:1"],
            frozen_config_version=1,
            available_rows_hash="0" * 64,
            strict_complete_buckets=True,
        )

    with patch(
        "dashboard.research_charts.frozen_pool_kernel_v1.available_from_loaded_pane_v1",
        side_effect=_fake_frozen,
    ):
        maybe_run_frozen_pool_shadow_parity(
            enabled=True,
            log_path=log_path,
            symbol=symbol,
            bar=bar,
            candles15=st.candles15,
            lld_cfg=st.lld_cfg,
            lld_cache=st.lld_cache,
            frozen_shadow_cache={},
        )
    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert record["status"] == "MISMATCH"
    assert record["only_frozen"] == ["lld:FAKE:15m:upper:1"]
    assert record["only_current"]


def test_compare_does_not_mutate_seen_sets():
    st = SymbolState(symbol="X")
    st.seen_no_guard = {"a"}
    st.seen_with_guard = {"b"}
    # compare_frozen_pool_shadow needs real candles — skip if no CH
    if not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir():
        pytest.skip("TRP/CH not available")
    proc = SymbolProcessor(ScannerConfig(live=False))
    st2 = proc.load_state("DOGEUSDT")
    if not st2.bars15:
        pytest.skip("no 15m pane")
    bar = st2.bars15[100]
    before_no = set(st2.seen_no_guard)
    before_with = set(st2.seen_with_guard)
    compare_frozen_pool_shadow(
        st2.symbol,
        bar,
        candles15=st2.candles15,
        lld_cfg=st2.lld_cfg,
        lld_cache={},
        frozen_shadow_cache={},
    )
    assert st2.seen_no_guard == before_no
    assert st2.seen_with_guard == before_with

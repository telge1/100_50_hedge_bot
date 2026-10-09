"""Live report window + seen_pool_ids ordering (no strategy change)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from bot.long_v1_live_scanner.config import LIVE_REPORT_TO, REPO_ROOT, ScannerConfig, ensure_runtime_paths

_EMPTY_COMMON = SimpleNamespace(available=[])
_COMMON_PATCH = "bot.long_v1_live_scanner.processing.common_causal_pools_from_loaded_pane"

ensure_runtime_paths()
_dash = REPO_ROOT / "backtester/short/dashboard"
if str(_dash) not in sys.path:
    sys.path.insert(0, str(_dash))
from bot.long_v1_live_scanner.processing import SymbolProcessor
from bot.long_v1_live_scanner.state import SymbolState

OCT = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
JUN = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
FROZEN_RT = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)


def _bar(close_time: datetime) -> dict:
    return {"close_time": close_time, "candle_index": 1, "open_time": close_time}


def _fake_sig(pool_id: str = "lld:TEST:15m:lower:1", symbol: str = "DOGEUSDT") -> dict:
    return {
        "symbol": symbol,
        "pool_id": pool_id,
        "entry_price": 1.0,
        "stop": 0.9,
        "tp": 1.1,
        "entry_time": close_time_iso(OCT),
        "ladder_pass": True,
        "decision_time": OCT.isoformat(),
        "m15_lower_2_age_h": 12.0,
        "be_trigger_pct": 0.5,
    }


def close_time_iso(t: datetime) -> str:
    return t.isoformat()


def test_effective_report_to_live_vs_frozen() -> None:
    live = ScannerConfig(live=True)
    frozen = ScannerConfig(live=False)
    assert live.effective_report_to() == LIVE_REPORT_TO
    assert frozen.effective_report_to() == frozen.report_to


@patch(_COMMON_PATCH, return_value=_EMPTY_COMMON)
@patch("bot.long_v1_live_scanner.processing.evaluate_geometry_bar")
def test_october_live_not_rejected_by_frozen_report_to(mock_eval, _mock_pools) -> None:
    mock_eval.return_value = _fake_sig("lld:DOGE:15m:lower:oct1")
    proc = SymbolProcessor(ScannerConfig(live=True))
    st = SymbolState(symbol="DOGEUSDT")
    st.by_open = {}
    st.seen_pool_ids = set()
    rows = proc._process_one_15m(st, _bar(OCT), emit_signals=True, log_ts=OCT)
    assert any(r.get("signal_status") == "ALLOWED" for r in rows)
    assert "lld:DOGE:15m:lower:oct1" in st.seen_pool_ids


@patch(_COMMON_PATCH, return_value=_EMPTY_COMMON)
@patch("bot.long_v1_live_scanner.processing.evaluate_geometry_bar")
def test_outside_frozen_window_live_false_unchanged(mock_eval, _mock_pools) -> None:
    mock_eval.return_value = _fake_sig("lld:DOGE:15m:lower:jun1")
    proc = SymbolProcessor(ScannerConfig(live=False, report_to=FROZEN_RT))
    st = SymbolState(symbol="DOGEUSDT")
    st.by_open = {}
    rows = proc._process_one_15m(
        st,
        _bar(OCT),
        emit_signals=True,
        log_ts=OCT,
        report_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        report_to=FROZEN_RT,
    )
    assert rows == []
    assert "lld:DOGE:15m:lower:jun1" not in st.seen_pool_ids


@patch(_COMMON_PATCH, return_value=_EMPTY_COMMON)
@patch("bot.long_v1_live_scanner.processing.evaluate_geometry_bar")
def test_admin_window_reject_does_not_burn_seen(mock_eval, _mock_pools) -> None:
    mock_eval.return_value = _fake_sig("lld:DOGE:15m:lower:burned")
    proc = SymbolProcessor(ScannerConfig(live=False))
    st = SymbolState(symbol="DOGEUSDT")
    st.by_open = {}
    rows = proc._process_one_15m(
        st,
        _bar(OCT),
        emit_signals=True,
        log_ts=OCT,
        report_to=FROZEN_RT,
    )
    assert rows == []
    assert "lld:DOGE:15m:lower:burned" not in st.seen_pool_ids


@patch(_COMMON_PATCH, return_value=_EMPTY_COMMON)
@patch("bot.long_v1_live_scanner.processing.evaluate_geometry_bar")
def test_dedup_after_allowed_emission(mock_eval, _mock_pools) -> None:
    sig = _fake_sig("lld:DOGE:15m:lower:once")
    mock_eval.return_value = sig
    proc = SymbolProcessor(ScannerConfig(live=True))
    st = SymbolState(symbol="DOGEUSDT")
    st.by_open = {}
    rows1 = proc._process_one_15m(st, _bar(OCT), emit_signals=True, log_ts=OCT)
    assert any(r.get("signal_status") == "ALLOWED" for r in rows1)
    rows2 = proc._process_one_15m(st, _bar(OCT), emit_signals=True, log_ts=OCT)
    assert not any(r.get("signal_status") == "ALLOWED" for r in rows2)

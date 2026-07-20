"""Phase A Emergency-Lock runner tests (synthetic + feather smoke)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.backtests.candle_loader import (
    DEFAULT_DATA_DIR,
    load_candles_for_symbol,
    symbol_to_feather_name,
)
from research.backtests.emergency_lock.config import EmergencyLockRecoveryConfig
from research.backtests.emergency_lock.phase_a_runner import (
    run_phase_a,
    write_phase_a_outputs,
)


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc).replace(minute=(i * 5) % 60, hour=i * 5 // 60)


def _candle(i: int, *, o: float, h: float, l: float, c: float) -> dict:
    return {
        "timestamp": _ts(i),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": 1.0,
    }


def _crash_series() -> list[dict]:
    """Start at 100, then crash through 90 trigger, then oscillate."""
    rows = [
        _candle(0, o=100.0, h=101.0, l=99.5, c=100.0),
        _candle(1, o=100.0, h=100.0, l=95.0, c=96.0),
        _candle(2, o=96.0, h=96.0, l=89.0, c=90.0),  # triggers lock (low<=90)
        _candle(3, o=90.0, h=90.0, l=70.0, c=72.0),  # deep dump
        _candle(4, o=72.0, h=120.0, l=72.0, c=115.0),  # sharp rally
        _candle(5, o=115.0, h=130.0, l=50.0, c=55.0),
    ]
    return rows


def test_trigger_on_low_leq_trigger() -> None:
    cfg = EmergencyLockRecoveryConfig(
        fee_rate=0.0,
        slippage_bps=0.0,
        max_candles=6,
        start_index=0,
    )
    result = run_phase_a(cfg, candles=_crash_series())
    assert result["summary"]["lock_triggered"] is True
    assert result["summary"]["lock_timestamp"] == _ts(2).isoformat()


def test_no_trigger_when_low_above_trigger() -> None:
    rows = [
        _candle(0, o=100.0, h=101.0, l=99.0, c=100.0),
        _candle(1, o=100.0, h=100.0, l=91.0, c=92.0),  # low > 90
        _candle(2, o=92.0, h=93.0, l=91.5, c=92.5),
    ]
    cfg = EmergencyLockRecoveryConfig(
        fee_rate=0.0,
        slippage_bps=0.0,
        max_candles=3,
        start_index=0,
    )
    result = run_phase_a(cfg, candles=rows)
    assert result["summary"]["lock_triggered"] is False


def test_basket_constant_after_lock_falling_prices() -> None:
    cfg = EmergencyLockRecoveryConfig(
        fee_rate=0.0,
        slippage_bps=0.0,
        max_candles=6,
        start_index=0,
        pnl_tolerance=1e-9,
    )
    result = run_phase_a(cfg, candles=_crash_series())
    assert result["summary"]["full_lock_invariant_passed"] is True
    lock_i = next(i for i, r in enumerate(result["trace"]) if r["state"] == "full_lock")
    post = [r["basket_net_pnl"] for r in result["trace"][lock_i:]]
    assert max(post) - min(post) == pytest.approx(0.0, abs=1e-9)


def test_basket_constant_after_lock_rising_prices() -> None:
    rows = [
        _candle(0, o=100.0, h=101.0, l=99.0, c=100.0),
        _candle(1, o=100.0, h=100.0, l=88.0, c=89.0),
        _candle(2, o=89.0, h=150.0, l=89.0, c=140.0),
        _candle(3, o=140.0, h=200.0, l=140.0, c=190.0),
    ]
    cfg = EmergencyLockRecoveryConfig(fee_rate=0.0, slippage_bps=0.0, max_candles=4)
    result = run_phase_a(cfg, candles=rows)
    assert result["summary"]["full_lock_invariant_passed"] is True
    lock_i = next(i for i, r in enumerate(result["trace"]) if r["state"] == "full_lock")
    post = [r["basket_net_pnl"] for r in result["trace"][lock_i:]]
    assert max(post) - min(post) == pytest.approx(0.0, abs=1e-9)


def test_post_lock_diff_only_fees_funding_rounding() -> None:
    """With fees at lock only, post-lock basket stays flat (no further costs)."""
    cfg = EmergencyLockRecoveryConfig(
        fee_rate=0.00055,
        slippage_bps=2.0,
        max_candles=6,
        funding_enabled=False,
    )
    result = run_phase_a(cfg, candles=_crash_series())
    assert result["summary"]["lock_triggered"] is True
    assert result["summary"]["maximum_post_lock_unexplained_drift"] == pytest.approx(
        0.0, abs=1e-9
    )
    assert result["summary"]["full_lock_invariant_passed"] is True


def test_deterministic_outputs(tmp_path: Path) -> None:
    cfg = EmergencyLockRecoveryConfig(
        fee_rate=0.00055,
        slippage_bps=2.0,
        max_candles=6,
        output_dir=str(tmp_path / "a"),
    )
    r1 = run_phase_a(cfg, candles=_crash_series())
    r2 = run_phase_a(copy.deepcopy(cfg), candles=_crash_series())
    assert r1["summary"]["basket_pnl_at_lock"] == r2["summary"]["basket_pnl_at_lock"]
    assert r1["trace"] == r2["trace"]
    write_phase_a_outputs(r1, tmp_path / "a")
    write_phase_a_outputs(r2, tmp_path / "b")
    assert (tmp_path / "a" / "per_bar_trace.csv").read_text(encoding="utf-8") == (
        tmp_path / "b" / "per_bar_trace.csv"
    ).read_text(encoding="utf-8")
    assert json.loads((tmp_path / "a" / "summary.json").read_text(encoding="utf-8"))[
        "basket_pnl_at_lock"
    ] == json.loads((tmp_path / "b" / "summary.json").read_text(encoding="utf-8"))[
        "basket_pnl_at_lock"
    ]


def test_summary_matches_last_trace(tmp_path: Path) -> None:
    cfg = EmergencyLockRecoveryConfig(fee_rate=0.0, slippage_bps=0.0, max_candles=6)
    result = run_phase_a(cfg, candles=_crash_series())
    write_phase_a_outputs(result, tmp_path)
    last = result["trace"][-1]
    summary = result["summary"]
    assert summary["long_qty"] == pytest.approx(last["long_qty"])
    assert summary["short_qty"] == pytest.approx(last["short_qty"])
    assert summary["final_basket_net_pnl"] == pytest.approx(last["basket_net_pnl"])
    assert summary["fees"] == pytest.approx(last["total_fees"])
    assert summary["slippage_cost"] == pytest.approx(last["slippage_cost"])


def test_reject_start_below_trigger() -> None:
    rows = [_candle(0, o=100.0, h=100.0, l=80.0, c=100.0)]
    cfg = EmergencyLockRecoveryConfig(
        fee_rate=0.0,
        slippage_bps=0.0,
        start_below_trigger_policy="reject",
        max_candles=1,
    )
    with pytest.raises(Exception, match="already at/below emergency trigger"):
        run_phase_a(cfg, candles=rows)


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="APTUSDT 5m feather missing",
)
def test_feather_loader_smoke_aptusdt() -> None:
    rows = load_candles_for_symbol("APTUSDT", timeframe="5m", limit=500)
    assert len(rows) == 500
    assert {"timestamp", "open", "high", "low", "close"} <= set(rows[0].keys())
    # Find a start that locks within 2000 bars for a tiny Phase A run.
    full = load_candles_for_symbol("APTUSDT", timeframe="5m", limit=None)
    found = None
    for i in range(0, min(len(full) - 50, 20000), 25):
        entry = float(full[i]["close"])
        trigger = entry * 0.9
        for j in range(i + 1, min(i + 2000, len(full))):
            if float(full[j]["low"]) <= trigger:
                found = i
                break
        if found is not None:
            break
    assert found is not None, "no 10% drawdown window found in APT sample"
    cfg = EmergencyLockRecoveryConfig(
        symbol="APTUSDT",
        start_index=found,
        max_candles=2500,
        fee_rate=0.00055,
        slippage_bps=2.0,
    )
    result = run_phase_a(cfg, candles=full)
    assert result["summary"]["lock_triggered"] is True
    assert result["summary"]["full_lock_invariant_passed"] is True

"""Phase B integration tests for unlock / re-lock / break-even."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.backtests.emergency_lock.config import EmergencyLockRecoveryConfig
from research.backtests.emergency_lock.phase_b_runner import (
    run_phase_b,
    write_phase_b_outputs,
)
from research.backtests.emergency_lock.signals import unlock_reference_price


def _ts(i: int) -> datetime:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return base.replace(
        hour=(i * 5) // 60,
        minute=(i * 5) % 60,
    )


def _c(i: int, *, o: float, h: float, l: float, c: float) -> dict:
    return {
        "timestamp": _ts(i),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": 1.0,
    }


def _cfg(**kwargs) -> EmergencyLockRecoveryConfig:
    base = dict(
        fee_rate=0.0,
        slippage_bps=0.0,
        unlock_rebound_pcts=(0.03, 0.05, 0.075, 0.10),
        unlock_steps=(0.10, 0.10, 0.15, 0.15),
        relock_distance_pct=0.02,
        max_failed_unlocks=2,
        cooldown_bars_after_relock=12,
        maximum_net_long_fraction=0.50,
        basket_exit_target_usdt=0.0,
        basket_exit_buffer_usdt=0.05,
        minimum_short_profit_buffer_usdt=0.0,
        minimum_distance_to_short_avg_pct=0.0,
        max_post_lock_bars=5000,
        unlock_confirmation_bars=1,
        relock_confirmation_bars=1,
    )
    base.update(kwargs)
    return EmergencyLockRecoveryConfig(**base)


def test_post_lock_low_updates_before_first_unlock() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=89.0, c=90),  # lock, post_lock_low=89
        _c(2, o=90, h=90, l=85.0, c=86),  # deeper low before unlock
        _c(3, o=86, h=86, l=85.0, c=85.5),
    ]
    result = run_phase_b(_cfg(max_candles=4), candles=rows)
    lows = [r["post_lock_low"] for r in result["trace"] if r["post_lock_low"] is not None]
    assert min(lows) == pytest.approx(85.0)
    assert result["summary"]["lock_triggered"] is True


def test_first_tranche_closes_from_full_lock_qty() -> None:
    # lock low=90 → post_lock_low=90; stage0 ref=92.7
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=93.0, l=91.0, c=92.8),  # unlock stage 0
    ]
    result = run_phase_b(_cfg(max_candles=3), candles=rows)
    actions = [a for a in result["actions"] if a["action"] == "unlock_short"]
    assert len(actions) == 1
    full_q = result["summary"]["full_lock_short_qty"]
    assert actions[0]["qty"] == pytest.approx(0.10 * full_q)
    assert result["trace"][-1]["short_qty"] == pytest.approx(full_q - actions[0]["qty"])


def test_max_net_long_guard_blocks_oversized_step() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=95.0, l=91.0, c=94),
    ]
    cfg = _cfg(
        unlock_rebound_pcts=(0.03,),
        unlock_steps=(0.10,),
        maximum_net_long_fraction=0.05,  # step 0.10 would exceed
        max_candles=3,
    )
    # validation rejects sum(steps)>max fraction
    with pytest.raises(ValueError):
        run_phase_b(cfg, candles=rows)


def test_runtime_net_long_fraction_after_unlock() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=93.0, l=91.0, c=92.8),
        _c(3, o=93, h=93.0, l=92.0, c=92.5),
    ]
    cfg = _cfg(
        unlock_rebound_pcts=(0.03, 0.05),
        unlock_steps=(0.10, 0.10),
        maximum_net_long_fraction=0.50,
        max_candles=4,
    )
    result = run_phase_b(cfg, candles=rows)
    unlocks = [a for a in result["actions"] if a["action"] == "unlock_short"]
    assert len(unlocks) == 1
    assert result["trace"][-1]["net_long_fraction"] == pytest.approx(0.10, abs=1e-9)


def test_unlock_qty_capped_to_remaining_short() -> None:
    """Four baseline stages remove exactly 50% of full-lock short qty."""
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=93.0, l=91.0, c=92.8),  # 10%
        _c(3, o=93, h=95.0, l=93.0, c=94.6),  # 10%
        _c(4, o=95, h=97.5, l=95.0, c=97.0),  # 15%
        _c(5, o=97, h=100.0, l=97.0, c=99.0),  # 15% → fraction 0.50
        _c(6, o=99, h=110.0, l=99.0, c=105.0),  # no further stages
    ]
    result = run_phase_b(
        _cfg(
            max_candles=7,
            # Keep the book open so all four unlock stages can fire.
            basket_exit_buffer_usdt=1e9,
            basket_exit_target_usdt=1e9,
        ),
        candles=rows,
    )
    unlocks = [a for a in result["actions"] if a["action"] == "unlock_short"]
    assert len(unlocks) == 4
    full_q = result["summary"]["full_lock_short_qty"]
    removed = sum(a["qty"] for a in unlocks)
    assert removed == pytest.approx(0.50 * full_q, rel=1e-9)
    last_unlock_ts = unlocks[-1]["timestamp"]
    row = next(r for r in result["trace"] if r["timestamp"] == last_unlock_ts)
    assert row["net_long_fraction"] == pytest.approx(0.50, abs=1e-9)


def test_maximum_net_long_guard_runtime() -> None:
    from research.backtests.emergency_lock.position_ledger import PositionLedger
    from research.backtests.emergency_lock.state_machine import EmergencyLockStateMachine

    cfg = _cfg(
        unlock_rebound_pcts=(0.03, 0.05),
        unlock_steps=(0.10, 0.10),
        maximum_net_long_fraction=0.10,
    )
    # Bypass config sum validation: construct SM after a 10% unlock already done.
    ledger = PositionLedger(
        long_qty=1.0, long_avg=100.0, short_qty=0.9, short_avg=95.0
    )
    sm = EmergencyLockStateMachine(cfg=cfg, ledger=ledger)
    sm.full_lock_short_qty = 1.0
    sm.next_unlock_stage = 1
    sm.state = "PARTIAL_UNLOCK"
    ok, reason = sm._unlock_guards_ok(unlock_reference=95.0, qty=0.10)
    assert ok is False
    assert reason == "maximum_net_long_fraction"


def test_short_profit_and_distance_guards() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=93.0, l=91.0, c=92.8),
    ]
    cfg = _cfg(
        minimum_short_profit_buffer_usdt=1e9,  # impossible
        max_candles=3,
    )
    result = run_phase_b(cfg, candles=rows)
    assert not [a for a in result["actions"] if a["action"] == "unlock_short"]

    cfg2 = _cfg(
        minimum_distance_to_short_avg_pct=0.50,  # need 50% below short avg
        max_candles=3,
    )
    result2 = run_phase_b(cfg2, candles=rows)
    assert not [a for a in result2["actions"] if a["action"] == "unlock_short"]


def test_relock_last_tranche_only() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=93.0, l=91.0, c=92.8),  # unlock
        # relock trigger = fill * 0.98; fill=92.7 → ~90.846
        _c(3, o=92, h=92.0, l=90.0, c=90.5),
    ]
    result = run_phase_b(
        _cfg(cooldown_bars_after_relock=0, max_candles=4), candles=rows
    )
    unlocks = [a for a in result["actions"] if a["action"] == "unlock_short"]
    relocks = [a for a in result["actions"] if a["action"] == "relock_short"]
    assert len(unlocks) == 1
    assert len(relocks) == 1
    assert relocks[0]["qty"] == pytest.approx(unlocks[0]["qty"])
    assert result["summary"]["failed_unlocks"] == 1
    assert result["summary"]["relock_count"] == 1
    # fully relocked
    assert result["trace"][-1]["long_qty"] == pytest.approx(result["trace"][-1]["short_qty"])


def test_cooldown_blocks_immediate_retry() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=93.0, l=91.0, c=92.8),
        _c(3, o=92, h=92.0, l=90.0, c=90.5),  # relock
        # rebound again immediately — should be blocked by cooldown=12
        _c(4, o=90, h=94.0, l=90.0, c=93.0),
    ]
    result = run_phase_b(
        _cfg(cooldown_bars_after_relock=12, max_candles=5), candles=rows
    )
    unlocks = [a for a in result["actions"] if a["action"] == "unlock_short"]
    assert len(unlocks) == 1
    assert result["trace"][-1]["cooldown_bars_remaining"] >= 11


def test_max_failed_unlocks_blocks_further() -> None:
    # Two fail cycles then no more unlocks
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=93.0, l=91.0, c=92.8),
        _c(3, o=92, h=92.0, l=90.0, c=90.5),
    ]
    # after relock cooldown 0, unlock again and fail again
    extra = []
    idx = 4
    for _ in range(2):
        extra += [
            _c(idx, o=90, h=93.0, l=90.5, c=92.8),
            _c(idx + 1, o=92, h=92.0, l=90.0, c=90.5),
        ]
        idx += 2
    extra.append(_c(idx, o=90, h=95.0, l=90.0, c=94.0))
    cfg = _cfg(cooldown_bars_after_relock=0, max_failed_unlocks=2, max_candles=20)
    result = run_phase_b(cfg, candles=rows + extra)
    assert result["summary"]["failed_unlocks"] >= 2
    # after 2 failures, the last rebound should not unlock
    unlocks = [a for a in result["actions"] if a["action"] == "unlock_short"]
    assert len(unlocks) == 2


def test_one_unlock_per_bar_despite_multiple_thresholds() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        # high clears stage0 (92.7) and stage1 (94.5) in one bar
        _c(2, o=91, h=96.0, l=91.0, c=95.0),
        _c(3, o=95, h=95.0, l=94.0, c=94.5),
    ]
    result = run_phase_b(_cfg(max_candles=4), candles=rows)
    unlocks = [a for a in result["actions"] if a["action"] == "unlock_short"]
    # bar2: one unlock; bar3 may unlock second if still above stage1 ref
    assert unlocks[0]["stage"] == 0
    assert sum(1 for a in unlocks if a["timestamp"] == _ts(2).isoformat()) == 1


def test_intrabar_prefers_relock_over_unlock() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=93.0, l=91.0, c=92.8),  # unlock stage0
        # same bar could print high for stage1 and low for relock — only relock
        _c(3, o=92, h=95.0, l=90.0, c=91.0),
    ]
    result = run_phase_b(
        _cfg(cooldown_bars_after_relock=0, max_candles=4), candles=rows
    )
    day3 = [a for a in result["actions"] if a["timestamp"] == _ts(3).isoformat()]
    assert len(day3) == 1
    assert day3[0]["action"] == "relock_short"


def test_added_loss_tracking() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=93.0, l=91.0, c=92.8),  # unlock → basket can move
        _c(3, o=92, h=92.0, l=70.0, c=72.0),  # dump while partially unlocked
    ]
    result = run_phase_b(_cfg(max_candles=4), candles=rows)
    assert result["summary"]["max_added_loss_after_lock"] >= 0.0
    assert result["trace"][-1]["added_loss_after_lock"] == pytest.approx(
        max(
            result["summary"]["basket_pnl_at_lock"] - result["trace"][-1]["basket_net_pnl"],
            0.0,
        )
    )


def test_max_added_loss_stop() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=93.0, l=91.0, c=92.8),
        _c(3, o=92, h=92.0, l=50.0, c=55.0),
    ]
    result = run_phase_b(
        _cfg(max_added_loss_after_lock_usdt=0.01, max_candles=4), candles=rows
    )
    assert result["summary"]["final_status"] == "STOPPED_MAX_ADDED_LOSS"
    assert result["trace"][-1]["long_qty"] > 0


def test_basket_be_rejects_when_fees_would_break_target() -> None:
    """Mark basket looks fine but projected close with fees fails target."""
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        # climb a lot so mark basket recovers after unlocks
        _c(2, o=91, h=93.0, l=91.0, c=92.8),
        _c(3, o=93, h=95.0, l=93.0, c=94.6),
        _c(4, o=95, h=98.0, l=95.0, c=97.0),
        _c(5, o=97, h=101.0, l=97.0, c=100.5),
        _c(6, o=100, h=105.0, l=100.0, c=104.0),
        _c(7, o=104, h=110.0, l=104.0, c=109.0),
    ]
    # High fees make projected exit miss target=0 even if mark basket high
    cfg = _cfg(
        fee_rate=0.05,
        slippage_bps=50.0,
        basket_exit_target_usdt=0.0,
        basket_exit_buffer_usdt=0.0,
        max_candles=8,
    )
    result = run_phase_b(cfg, candles=rows)
    # May or may not exit; if status is BE, final must be >= target
    if result["summary"]["break_even_reached"]:
        assert result["summary"]["final_net_pnl"] >= -1e-9
    else:
        # Ensure we never labeled BE incorrectly
        assert result["summary"]["final_status"] != "CLOSED_BREAK_EVEN"


def test_true_net_break_even_exit() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=93.0, l=91.0, c=92.8),
        _c(3, o=93, h=95.0, l=93.0, c=94.6),
        _c(4, o=95, h=98.0, l=95.0, c=97.0),
        _c(5, o=97, h=102.0, l=97.0, c=101.0),
        _c(6, o=101, h=108.0, l=101.0, c=107.0),
        _c(7, o=107, h=115.0, l=107.0, c=114.0),
        _c(8, o=114, h=120.0, l=114.0, c=119.0),
    ]
    cfg = _cfg(
        fee_rate=0.0,
        slippage_bps=0.0,
        basket_exit_buffer_usdt=0.0,
        max_candles=9,
    )
    result = run_phase_b(cfg, candles=rows)
    assert result["summary"]["break_even_reached"] is True
    assert result["summary"]["final_status"] == "CLOSED_BREAK_EVEN"
    assert result["summary"]["final_net_pnl"] >= -1e-9
    assert result["trace"][-1]["long_qty"] == pytest.approx(0.0)
    assert result["trace"][-1]["short_qty"] == pytest.approx(0.0)


def test_timeout() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=91.0, l=90.5, c=90.8),
        _c(3, o=90.8, h=91.0, l=90.5, c=90.7),
        _c(4, o=90.7, h=91.0, l=90.5, c=90.6),
    ]
    result = run_phase_b(_cfg(max_post_lock_bars=2, max_candles=5), candles=rows)
    assert result["summary"]["final_status"] == "STOPPED_TIMEOUT"
    assert result["trace"][-1]["long_qty"] > 0


def test_deterministic_and_summary_alignment(tmp_path: Path) -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=93.0, l=91.0, c=92.8),
        _c(3, o=92, h=92.0, l=90.0, c=90.5),
        _c(4, o=90, h=90.5, l=90.0, c=90.2),
    ]
    cfg = _cfg(cooldown_bars_after_relock=0, max_candles=5)
    r1 = run_phase_b(cfg, candles=rows)
    r2 = run_phase_b(copy.deepcopy(cfg), candles=rows)
    assert r1["summary"]["final_status"] == r2["summary"]["final_status"]
    assert r1["actions"] == r2["actions"]
    write_phase_b_outputs(r1, tmp_path / "a")
    write_phase_b_outputs(r2, tmp_path / "b")
    assert (tmp_path / "a" / "actions.csv").read_text() == (
        tmp_path / "b" / "actions.csv"
    ).read_text()
    last = r1["trace"][-1]
    s = r1["summary"]
    assert s["final_status"] == last["state"]
    assert s["failed_unlocks"] == last["failed_unlocks"]
    assert s["unlock_count"] == len(
        [a for a in r1["actions"] if a["action"] == "unlock_short"]
    )


def test_rebound_uses_only_causal_post_lock_low() -> None:
    rows = [
        _c(0, o=100, h=100, l=99.5, c=100),
        _c(1, o=100, h=100, l=90.0, c=91),
        _c(2, o=91, h=91.5, l=88.0, c=88.5),  # new post_lock_low=88
        _c(3, o=88.5, h=90.7, l=88.5, c=90.0),  # ref=88*1.03=90.64 → touch
    ]
    result = run_phase_b(_cfg(max_candles=4), candles=rows)
    assert result["trace"][2]["post_lock_low"] == pytest.approx(88.0)
    ref = unlock_reference_price(post_lock_low=88.0, rebound_pct=0.03)
    unlocks = [a for a in result["actions"] if a["action"] == "unlock_short"]
    assert len(unlocks) == 1
    assert unlocks[0]["reference_price"] == pytest.approx(ref)

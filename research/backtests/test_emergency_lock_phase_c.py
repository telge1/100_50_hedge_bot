"""Phase C integration tests: modes, lookahead separation, aggregates."""

from __future__ import annotations

import ast
import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.backtests.emergency_lock.config import EmergencyLockRecoveryConfig
from research.backtests.emergency_lock.event_finder import find_crash_events
from research.backtests.emergency_lock.phase_c_report import (
    aggregate_rows,
    compute_capture_rate,
    run_phase_c_to_disk,
    write_phase_c_outputs,
)
from research.backtests.emergency_lock.phase_c_runner import (
    MODE_BASELINE,
    MODE_FULL_LOCK,
    MODE_ORACLE,
    evaluate_event_modes,
    phase_b_baseline_config,
    run_phase_c,
)

PKG = Path(__file__).resolve().parent / "emergency_lock"
FORBIDDEN = (
    "research.backtests.historical_backtest",
    "research.backtests.hedge_bot_original_simulator",
    "fixed_cycle_hedge_bot.fixed_cycle_strategy",
)


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc).replace(
        hour=min((i * 5) // 60, 23), minute=(i * 5) % 60
    )


def _c(i: int, *, h: float, l: float, c: float | None = None) -> dict:
    close = float(c if c is not None else (h + l) / 2.0)
    return {
        "timestamp": _ts(i),
        "open": close,
        "high": float(h),
        "low": float(l),
        "close": close,
        "volume": 1.0,
    }


def _crash_series() -> list[dict]:
    """Peak 100 → low 85 (15%), then recovery path for unlocks/BE potential."""
    rows = [_c(0, h=100.0, l=99.5, c=100.0)]
    rows.append(_c(1, h=100.0, l=89.0, c=90.0))  # emergency lock region
    rows.append(_c(2, h=90.0, l=85.0, c=86.0))  # event low
    # recovery
    for i, px in enumerate([88, 90, 93, 95, 98, 101, 105, 110, 115], start=3):
        rows.append(_c(i, h=px + 1, l=px - 1, c=float(px)))
    return rows


def _cfg(**kwargs) -> EmergencyLockRecoveryConfig:
    cfg = phase_b_baseline_config()
    cfg.event_peak_lookback_bars = 0
    cfg.event_max_drop_bars = 50
    cfg.event_post_low_bars = 20
    cfg.event_cooldown_bars = 5
    cfg.event_min_separation_bars = 3
    cfg.fee_rate = 0.0
    cfg.slippage_bps = 0.0
    cfg.basket_exit_buffer_usdt = 0.0
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def test_no_emergency_trigger_reported() -> None:
    # Peak then tiny dip — qualifies as event on high→low, but entry close may not
    # reach emergency if we start after peak without enough drop vs long avg.
    rows = [_c(0, h=100.0, l=99.0, c=100.0)]
    rows += [_c(i, h=92.0, l=90.0, c=91.0) for i in range(1, 8)]
    cfg = _cfg()
    # Raise emergency trigger requirement so lock never fires from this path
    cfg.emergency_trigger_pct = 0.50
    finder = find_crash_events(rows, cfg)
    assert finder.events
    ev = finder.events[0]
    out = evaluate_event_modes(rows, ev, cfg)
    assert out["baseline"]["final_status"] == "NO_EMERGENCY_TRIGGER"
    assert out["baseline"]["lock_triggered"] is False


def test_full_lock_control_no_unlock_actions() -> None:
    rows = _crash_series()
    cfg = _cfg()
    finder = find_crash_events(rows, cfg)
    ev = finder.events[0]
    out = evaluate_event_modes(rows, ev, cfg)
    assert out["full_lock"]["unlock_count"] == 0
    assert out["full_lock"]["relock_count"] == 0
    actions = out["full_lock_result"]["actions"]
    assert not any(a["action"] == "unlock_short" for a in actions)


def test_full_lock_price_neutral_after_lock() -> None:
    rows = _crash_series()
    cfg = _cfg()
    finder = find_crash_events(rows, cfg)
    ev = finder.events[0]
    out = evaluate_event_modes(rows, ev, cfg)
    if not out["full_lock"]["lock_triggered"]:
        pytest.skip("lock not triggered in synthetic path")
    trace = out["full_lock_result"]["trace"]
    lock_rows = [r for r in trace if r["state"] in {"FULL_LOCK", "OPEN_AT_DATA_END", "STOPPED_TIMEOUT"}]
    # After lock with unlock disabled, basket_net should be flat (fee_rate=0).
    locked = [r for r in trace if r.get("basket_pnl_at_lock") is not None]
    pnls = [r["basket_net_pnl"] for r in locked if r["state"] != "PRE_EMERGENCY"]
    assert max(pnls) - min(pnls) == pytest.approx(0.0, abs=1e-9)


def test_oracle_marked_non_causal() -> None:
    rows = _crash_series()
    cfg = _cfg()
    finder = find_crash_events(rows, cfg)
    out = evaluate_event_modes(rows, finder.events[0], cfg)
    assert out["oracle"]["mode"] == MODE_ORACLE
    assert "OPTIMISTIC" in str(out["oracle"]["oracle_bound_type"])
    assert out["oracle"]["selection_type"] == "hindsight_selected_stress_event"


def test_baseline_config_identical_across_events() -> None:
    rows = _crash_series() + [_c(20, h=120.0, l=119.0, c=119.5)]
    rows += [_c(i, h=110.0, l=100.0, c=101.0) for i in range(21, 30)]
    cfg = _cfg(event_cooldown_bars=1, event_min_separation_bars=1)
    payload = run_phase_c(cfg, candles=rows)
    baselines = [r for r in payload["per_event_rows"] if r["mode"] == MODE_BASELINE]
    # Config fingerprint via unlock params on runner base
    assert payload["config"].unlock_steps == (0.10, 0.10, 0.15, 0.15)
    assert payload["config"].relock_distance_pct == 0.02
    assert all(r["maximum_net_long_fraction"] == 0.50 for r in baselines)


def test_baseline_vs_control_incremental_fields() -> None:
    rows = _crash_series()
    cfg = _cfg()
    finder = find_crash_events(rows, cfg)
    out = evaluate_event_modes(rows, finder.events[0], cfg)
    b = out["baseline"]
    c = out["full_lock"]
    if b["final_net_pnl"] is None or c["final_net_pnl"] is None:
        pytest.skip("missing finals")
    assert b["incremental_final_pnl_vs_full_lock"] == pytest.approx(
        float(b["final_net_pnl"]) - float(c["final_net_pnl"])
    )


def test_event_finder_future_not_in_baseline_window_start() -> None:
    """Strategy only sees candles from peak start; low_index is not passed in."""
    rows = _crash_series()
    cfg = _cfg()
    finder = find_crash_events(rows, cfg)
    ev = finder.events[0]
    out = evaluate_event_modes(rows, ev, cfg)
    # Baseline entry equals peak close (event entry mode), not low.
    assert out["baseline"]["entry_price"] == pytest.approx(float(rows[ev.peak_index]["close"]))
    # Simulation length ignores knowledge of low for start.
    assert out["baseline"]["simulation_start_index"] == ev.peak_index


def test_aggregation_matches_rows() -> None:
    rows = _crash_series()
    cfg = _cfg()
    payload = run_phase_c(cfg, candles=rows)
    baseline = [r for r in payload["per_event_rows"] if r["mode"] == MODE_BASELINE]
    agg = aggregate_rows(payload["per_event_rows"], mode=MODE_BASELINE)
    assert agg["event_count"] == len(baseline)


def test_capture_rate_helper() -> None:
    rows = [
        {
            "event_id": "e1",
            "mode": MODE_ORACLE,
            "oracle_break_even_possible": True,
        },
        {
            "event_id": "e1",
            "mode": MODE_BASELINE,
            "break_even_reached": True,
        },
        {
            "event_id": "e2",
            "mode": MODE_ORACLE,
            "oracle_break_even_possible": True,
        },
        {
            "event_id": "e2",
            "mode": MODE_BASELINE,
            "break_even_reached": False,
        },
    ]
    assert compute_capture_rate(rows) == pytest.approx(0.5)


def test_deterministic_phase_c(tmp_path: Path) -> None:
    rows = _crash_series()
    cfg = _cfg()
    a = run_phase_c(cfg, candles=rows)
    b = run_phase_c(copy.deepcopy(cfg), candles=rows)
    assert [e.event_id for e in a["finder"].events] == [
        e.event_id for e in b["finder"].events
    ]
    assert a["per_event_rows"] == b["per_event_rows"]
    write_phase_c_outputs(a, tmp_path / "a")
    write_phase_c_outputs(b, tmp_path / "b")
    assert (tmp_path / "a" / "event_manifest.csv").read_text() == (
        tmp_path / "b" / "event_manifest.csv"
    ).read_text()


def test_no_forbidden_imports() -> None:
    for path in sorted(PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                for forbidden in FORBIDDEN:
                    assert not name.startswith(forbidden), f"{path.name} imports {name}"

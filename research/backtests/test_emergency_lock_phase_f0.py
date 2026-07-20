"""Phase F0 integration: prefix parity, determinism, candidate gates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from research.backtests.emergency_lock.phase_f0_report import decide_candidates
from research.backtests.emergency_lock.phase_f0_runner import (
    audit_event_window,
    phase_f0_base_config,
)
from research.backtests.emergency_lock.phase_f0_speed import (
    PhaseF0Config,
    find_level_crossings,
)

PHASE_C = Path(__file__).resolve().parent / "results" / "emergency_lock" / "phase_c"


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


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


def test_prefix_parity_crossings_and_legs() -> None:
    ref = 100.0
    base = [_c(0, h=100, l=100, c=100)]
    for i in range(1, 20):
        px = 100 - i * 0.3
        base.append(_c(i, h=px + 0.2, l=px - 0.2, c=px))
    extended = base + [_c(20 + i, h=90, l=80, c=85) for i in range(30)]
    n = 15
    xs_s = find_level_crossings(
        extended[:n],
        reference_price=ref,
        levels_pct=(-0.02, -0.04, -0.06),
        start_index=0,
        end_index=n - 1,
    )
    xs_l = find_level_crossings(
        extended,
        reference_price=ref,
        levels_pct=(-0.02, -0.04, -0.06),
        start_index=0,
        end_index=len(extended) - 1,
    )
    short_keys = [(r["level_pct"], r["end_bar"]) for r in xs_s]
    long_prefix = [
        (r["level_pct"], r["end_bar"]) for r in xs_l if int(r["end_bar"]) < n
    ]
    assert short_keys == long_prefix


def test_candidate_gates_require_all_conditions() -> None:
    baseline = {
        "group_value": "R0_unfiltered",
        "median_net_attempt_pnl": 0.0,
        "mean_net_attempt_pnl": 0.0,
        "win_rate": 0.4,
        "median_added_loss": 0.5,
        "p90_added_loss": 1.0,
        "worst_added_loss": 2.0,
        "completed_count": 10,
        "sample_count": 10,
    }
    good = {
        "group_value": "R4_slowdown_ge_1.25",
        "median_net_attempt_pnl": 0.2,
        "mean_net_attempt_pnl": 0.15,
        "win_rate": 0.6,
        "median_added_loss": 0.2,
        "p90_added_loss": 0.8,
        "worst_added_loss": 1.5,
        "completed_count": 8,
        "sample_count": 8,
        "positive_events": 5,
        "negative_events": 2,
        "max_event_share_of_positive_pnl": 0.3,
        "median_incremental_pnl_vs_full_lock": 0.1,
    }
    bad = dict(good)
    bad["group_value"] = "R4_slowdown_ge_2.00"
    bad["median_net_attempt_pnl"] = -0.1
    decisions = decide_candidates([baseline, good, bad], min_sample=5)
    by = {d["variant"]: d for d in decisions}
    assert by["R4_slowdown_ge_1.25"]["phase_f1_candidate"] is True
    assert by["R4_slowdown_ge_2.00"]["phase_f1_candidate"] is False


def test_deterministic_crossings() -> None:
    candles = [
        _c(i, h=100 - i * 0.5, l=99.5 - i * 0.5, c=99.8 - i * 0.5) for i in range(10)
    ]
    a = find_level_crossings(
        candles,
        reference_price=100.0,
        levels_pct=(-0.02, -0.04),
        start_index=0,
        end_index=9,
    )
    b = find_level_crossings(
        candles,
        reference_price=100.0,
        levels_pct=(-0.02, -0.04),
        start_index=0,
        end_index=9,
    )
    assert a == b


@pytest.mark.skipif(
    not (PHASE_C / "event_manifest.csv").exists(),
    reason="Phase-C manifest missing",
)
def test_smoke_one_real_event() -> None:
    from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
    from research.backtests.emergency_lock.phase_d_runner import load_phase_c_events

    events = load_phase_c_events(PHASE_C / "event_manifest.csv")
    candles = load_candles_for_symbol(
        symbol="APTUSDT", timeframe="5m", data_dir=DEFAULT_DATA_DIR, limit=None
    )
    cfg = phase_f0_base_config()
    f0 = PhaseF0Config(fee_rate=float(cfg.fee_rate), slippage_bps=float(cfg.slippage_bps))
    ev = events[0]
    window = list(candles[ev.simulation_start_index : ev.simulation_end_index + 1])
    out = audit_event_window(window, ev, cfg, f0, oracle_possible=None)
    assert out["lock_triggered"] is True
    assert out["per_event"]["short_avg_after_lock"] is not None
    assert any(abs(float(c["level_pct"])) < 1e-15 for c in out["crossings"])

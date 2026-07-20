"""Phase D integration: common_pct ranking, prefix parity, Phase-C regression."""

from __future__ import annotations

import ast
import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.backtests.emergency_lock.phase_d_report import (
    aggregate_signal,
    decide_phase_e_candidates,
)
from research.backtests.emergency_lock.phase_d_runner import (
    MAIN_RELOCK_VARIANT,
    evaluate_event_signal,
    load_phase_c_events,
    phase_d_base_config,
    run_signal_on_window,
)
from research.backtests.emergency_lock.phase_d_signals import (
    PROTECTED_STRUCTURE_ADAPTER_AVAILABLE,
)

PKG = Path(__file__).resolve().parent / "emergency_lock"
PHASE_C_DIR = (
    Path(__file__).resolve().parent
    / "results"
    / "emergency_lock"
    / "phase_c"
)
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


def _synthetic_lock_window() -> list[dict]:
    """Entry 100 → lock near 90 → recovery with a clear swing break."""
    rows = [_c(0, h=100.0, l=99.5, c=100.0)]
    rows.append(_c(1, h=100.0, l=89.0, c=90.0))  # lock
    for i, (h, l, cl) in enumerate(
        [
            (91, 88, 89),
            (92, 88, 90),
            (93, 88, 91),
            (95, 89, 92),  # swing high candidate
            (93, 89, 90),
            (92, 88, 89),
            (91, 88, 88.5),  # confirm
            (96, 90, 95.5),  # close break
            (97, 94, 96),
            (98, 95, 97),
            (99, 96, 98),
            (100, 97, 99),
            (101, 98, 100),
            (102, 99, 101),
        ],
        start=2,
    ):
        rows.append(_c(i, h=h, l=l, c=cl))
    return rows


def test_no_forbidden_imports() -> None:
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for bad in FORBIDDEN:
                        assert bad not in alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for bad in FORBIDDEN:
                    assert not node.module.startswith(bad)


def test_protected_structure_adapter_skipped() -> None:
    assert PROTECTED_STRUCTURE_ADAPTER_AVAILABLE is False


def test_full_lock_control_unchanged_no_unlocks() -> None:
    window = _synthetic_lock_window()
    cfg = phase_d_base_config()
    cfg.fee_rate = 0.0
    cfg.slippage_bps = 0.0
    out = run_signal_on_window(window, cfg, signal_name="full_lock_control")
    assert out["summary"]["unlock_count"] == 0
    assert out["summary"]["relock_count"] == 0
    assert not any(a["action"] == "unlock_short" for a in out["actions"])


def test_common_pct_relock_still_used() -> None:
    window = _synthetic_lock_window()
    cfg = phase_d_base_config()
    cfg.fee_rate = 0.0
    cfg.slippage_bps = 0.0
    out = run_signal_on_window(
        window, cfg, signal_name="rebound_baseline", relock_variant=MAIN_RELOCK_VARIANT
    )
    assert out["summary"]["relock_variant"] == MAIN_RELOCK_VARIANT


def test_signal_invalidation_relock_path() -> None:
    window = _synthetic_lock_window()
    cfg = phase_d_base_config()
    cfg.fee_rate = 0.0
    cfg.slippage_bps = 0.0
    out = run_signal_on_window(
        window,
        cfg,
        signal_name="swing_high_break",
        relock_variant="signal_invalidation",
    )
    assert out["summary"]["relock_variant"] == "signal_invalidation"


def test_prefix_parity_no_lookahead() -> None:
    window = _synthetic_lock_window()
    extended = window + [
        _c(len(window) + i, h=200, l=190, c=195) for i in range(20)
    ]
    cfg = phase_d_base_config()
    cfg.fee_rate = 0.0
    cfg.slippage_bps = 0.0
    n = len(window) - 5
    short = run_signal_on_window(
        extended[:n], cfg, signal_name="swing_high_break"
    )
    long = run_signal_on_window(extended, cfg, signal_name="swing_high_break")
    short_ts = {a["timestamp"] for a in short["actions"]}
    long_prefix = [a for a in long["actions"] if a["timestamp"] in short_ts]

    def _norm(actions: list[dict]) -> list[tuple]:
        return [
            (
                a["timestamp"],
                a["action"],
                a.get("stage"),
                round(float(a.get("fill_price") or 0), 8),
            )
            for a in actions
        ]

    assert _norm(short["actions"]) == _norm(long_prefix)


def test_oracle_not_passed_to_signals() -> None:
    import dataclasses

    from research.backtests.emergency_lock.phase_d_signals import SignalContext

    src = (PKG / "phase_d_signals.py").read_text(encoding="utf-8")
    assert "event_low" not in src
    names = {f.name for f in dataclasses.fields(SignalContext)}
    assert "oracle_break_even_possible" not in names
    assert "event_low" not in names
    assert "low_price" not in names


def test_aggregation_matches_per_event() -> None:
    rows = [
        {
            "signal_name": "swing_high_break",
            "relock_variant": MAIN_RELOCK_VARIANT,
            "drop_bucket": "10–12.5%",
            "incremental_final_pnl_vs_full_lock": 1.0,
            "max_added_loss_after_lock": 0.5,
            "unlock_count": 1,
            "relock_count": 0,
            "break_even_reached": True,
            "bars_lock_to_break_even": 10,
            "better_than_full_lock": True,
            "worse_than_full_lock": False,
            "oracle_break_even_possible": True,
            "oracle_captured": True,
            "failed_unlocks": 0,
            "total_fees": 0.1,
            "basket_pnl_at_lock": -5.0,
        },
        {
            "signal_name": "swing_high_break",
            "relock_variant": MAIN_RELOCK_VARIANT,
            "drop_bucket": ">=15%",
            "incremental_final_pnl_vs_full_lock": -0.5,
            "max_added_loss_after_lock": 1.5,
            "unlock_count": 2,
            "relock_count": 1,
            "break_even_reached": False,
            "bars_lock_to_break_even": None,
            "better_than_full_lock": False,
            "worse_than_full_lock": True,
            "oracle_break_even_possible": False,
            "oracle_captured": False,
            "failed_unlocks": 1,
            "total_fees": 0.2,
            "basket_pnl_at_lock": -5.0,
        },
    ]
    agg = aggregate_signal(
        rows, signal_name="swing_high_break", relock_variant=MAIN_RELOCK_VARIANT
    )
    assert agg["event_count"] == 2
    assert agg["break_even_count"] == 1
    assert agg["better_than_full_lock_count"] == 1
    assert agg["worse_than_full_lock_count"] == 1
    assert agg["oracle_capture_count"] == 1
    assert agg["failed_unlock_event_rate"] == 0.5
    assert agg["total_fees"] == pytest.approx(0.3)


def test_candidate_decision_uses_exact_gates() -> None:
    rebound = {
        "signal_name": "rebound_baseline",
        "relock_variant": MAIN_RELOCK_VARIANT,
        "drop_bucket": "all",
        "better_than_full_lock_rate": 0.1,
        "worse_than_full_lock_rate": 0.8,
        "median_incremental_final_pnl_vs_full_lock": -0.5,
        "median_max_added_loss": 1.0,
        "p90_max_added_loss": 3.0,
        "failed_unlock_event_rate": 1.0,
        "oracle_capture_count": 1,
        "break_even_count": 1,
        "worst_max_added_loss": 4.0,
        "median_abs_basket_pnl_at_lock": 5.0,
    }
    good = {
        "signal_name": "swing_high_break",
        "relock_variant": MAIN_RELOCK_VARIANT,
        "drop_bucket": "all",
        "better_than_full_lock_rate": 0.6,
        "worse_than_full_lock_rate": 0.2,
        "median_incremental_final_pnl_vs_full_lock": 0.2,
        "median_max_added_loss": 0.4,
        "p90_max_added_loss": 1.0,
        "failed_unlock_event_rate": 0.2,
        "oracle_capture_count": 2,
        "break_even_count": 2,
        "worst_max_added_loss": 2.0,
        "median_abs_basket_pnl_at_lock": 5.0,
    }
    bad = {
        "signal_name": "ema_reclaim",
        "relock_variant": MAIN_RELOCK_VARIANT,
        "drop_bucket": "all",
        "better_than_full_lock_rate": 0.1,
        "worse_than_full_lock_rate": 0.5,
        "median_incremental_final_pnl_vs_full_lock": -0.1,
        "median_max_added_loss": 2.0,
        "p90_max_added_loss": 5.0,
        "failed_unlock_event_rate": 1.0,
        "oracle_capture_count": 0,
        "break_even_count": 0,
        "worst_max_added_loss": 6.0,
        "median_abs_basket_pnl_at_lock": 5.0,
    }
    decisions = decide_phase_e_candidates([rebound, good, bad])
    by = {d["signal_name"]: d for d in decisions}
    assert by["swing_high_break"]["phase_e_candidate"] is True
    assert by["ema_reclaim"]["phase_e_candidate"] is False
    assert "better_than_full_lock_rate_not_gt_worse" in by["ema_reclaim"][
        "rejection_reasons"
    ]
    assert by["ema_reclaim"]["warning_worst_loss_gt_frozen"] is True


def test_deterministic_output() -> None:
    window = _synthetic_lock_window()
    cfg = phase_d_base_config()
    cfg.fee_rate = 0.0
    cfg.slippage_bps = 0.0
    a = run_signal_on_window(window, cfg, signal_name="swing_high_break")
    b = run_signal_on_window(window, cfg, signal_name="swing_high_break")
    assert a["summary"] == b["summary"]
    assert a["actions"] == b["actions"]


@pytest.mark.skipif(
    not (PHASE_C_DIR / "event_manifest.csv").exists(),
    reason="Phase-C manifest missing",
)
def test_rebound_baseline_reproduces_phase_c_values() -> None:
    """Phase-D rebound_baseline must match Phase-C baseline per-event finals."""
    from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol

    events = load_phase_c_events(PHASE_C_DIR / "event_manifest.csv")
    assert len(events) == 14
    candles = load_candles_for_symbol(
        symbol="APTUSDT", timeframe="5m", data_dir=DEFAULT_DATA_DIR, limit=None
    )
    cfg = phase_d_base_config()
    phase_c_rows = {}
    with (PHASE_C_DIR / "baseline_per_event_summary.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            phase_c_rows[row["event_id"]] = row

    for event in events[:3]:
        out = evaluate_event_signal(
            candles,
            event,
            cfg,
            signal_name="rebound_baseline",
            relock_variant=MAIN_RELOCK_VARIANT,
            oracle_possible=None,
            full_lock_final=None,
            full_lock_min=None,
        )["row"]
        ref = phase_c_rows[event.event_id]
        assert int(out["unlock_count"]) == int(float(ref["unlock_count"]))
        assert int(out["relock_count"]) == int(float(ref["relock_count"]))
        assert float(out["final_net_pnl"]) == pytest.approx(
            float(ref["final_net_pnl"]), abs=1e-6
        )


@pytest.mark.skipif(
    not (PHASE_C_DIR / "event_manifest.csv").exists(),
    reason="Phase-C manifest missing",
)
def test_phase_d_manifest_unchanged_event_count() -> None:
    events = load_phase_c_events(PHASE_C_DIR / "event_manifest.csv")
    assert len(events) == 14
    assert all(e.selection_type == "hindsight_selected_stress_event" for e in events)

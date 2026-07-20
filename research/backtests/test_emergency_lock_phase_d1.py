"""Phase D.1 integration: controls regression, caps, prefix parity, gates."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.backtests.emergency_lock.phase_d1_policy import micro_unlock_configs
from research.backtests.emergency_lock.phase_d1_report import (
    aggregate_variant,
    decide_phase_e_gates,
)
from research.backtests.emergency_lock.phase_d1_runner import (
    phase_d1_base_config,
    run_micro_on_window,
)

PHASE_C_DIR = Path(__file__).resolve().parent / "results" / "emergency_lock" / "phase_c"
PHASE_D_DIR = Path(__file__).resolve().parent / "results" / "emergency_lock" / "phase_d"


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


def _window() -> list[dict]:
    rows = [_c(0, h=100.0, l=99.5, c=100.0)]
    rows.append(_c(1, h=100.0, l=89.0, c=90.0))
    for i, (h, l, cl) in enumerate(
        [
            (91, 88, 89),
            (92, 88, 90),
            (93, 88, 91),
            (95, 89, 92),
            (93, 89, 90),
            (92, 88, 89),
            (91, 88, 88.5),
            (96, 90, 95.5),
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


def test_micro_caps_never_exceeded() -> None:
    cfg = phase_d1_base_config()
    cfg.fee_rate = 0.0
    cfg.slippage_bps = 0.0
    window = _window()
    for name, policy in micro_unlock_configs().items():
        out = run_micro_on_window(window, cfg, policy)
        assert out["summary"]["max_open_unlock_pct"] <= policy.max_total_unlock_pct + 1e-12
        assert (
            out["summary"]["cumulative_unlock_pct_final"]
            <= policy.max_total_unlock_pct + 1e-12
        )


def test_prefix_parity_micro() -> None:
    cfg = phase_d1_base_config()
    cfg.fee_rate = 0.0
    cfg.slippage_bps = 0.0
    base = _window()
    extended = base + [_c(len(base) + i, h=200, l=190, c=195) for i in range(15)]
    policy = micro_unlock_configs()["micro_unlock_10"]
    n = len(base) - 3
    short = run_micro_on_window(extended[:n], cfg, policy)
    long = run_micro_on_window(extended, cfg, policy)
    short_ts = {a["timestamp"] for a in short["actions"]}
    long_prefix = [a for a in long["actions"] if a["timestamp"] in short_ts]

    def norm(actions: list[dict]) -> list[tuple]:
        return [
            (
                a["timestamp"],
                a["action"],
                a.get("stage"),
                round(float(a.get("fill_price") or 0), 8),
            )
            for a in actions
        ]

    assert norm(short["actions"]) == norm(long_prefix)


def test_gate_logic_exact() -> None:
    rows = []
    for eid, incr, added, be, oracle_cap, s2 in [
        ("e1", 1.0, 0.2, True, True, 0),
        ("e2", 0.5, 0.3, True, True, 0),
        ("e3", -0.1, 0.4, False, False, 0),
    ]:
        rows.append(
            {
                "event_id": eid,
                "variant": "micro_unlock_10",
                "incremental_final_pnl_vs_full_lock": incr,
                "max_added_loss_after_lock": added,
                "better_than_full_lock": incr > 0,
                "worse_than_full_lock": incr < 0,
                "break_even_reached": be,
                "oracle_break_even_possible": True,
                "oracle_captured": oracle_cap,
                "unlock_count": 1,
                "stage_2_unlock_count": s2,
                "relock_count": 0,
                "stage_1_break_even_confirmed": True,
                "total_fees": 0.1,
                "frozen_deficit_usdt": 5.0,
                "basket_pnl_at_lock": -5.0,
            }
        )
    for eid in ("e1", "e2", "e3"):
        rows.append(
            {
                "event_id": eid,
                "variant": "rebound_baseline",
                "incremental_final_pnl_vs_full_lock": -0.5,
                "max_added_loss_after_lock": 1.0,
                "better_than_full_lock": False,
                "worse_than_full_lock": True,
                "break_even_reached": False,
                "oracle_break_even_possible": True,
                "oracle_captured": False,
                "unlock_count": 2,
                "stage_2_unlock_count": 0,
                "relock_count": 2,
                "stage_1_break_even_confirmed": None,
                "total_fees": 0.2,
                "frozen_deficit_usdt": 5.0,
                "basket_pnl_at_lock": -5.0,
            }
        )
    aggs = [
        aggregate_variant(rows, "rebound_baseline"),
        aggregate_variant(rows, "micro_unlock_10"),
    ]
    for v in (
        "full_lock_control",
        "swing_break_with_ema_existing",
        "micro_unlock_10_10",
        "micro_unlock_10_15",
    ):
        aggs.append({"variant": v, "event_count": 0})
    decisions = decide_phase_e_gates(aggs, rows)
    by = {d["variant"]: d for d in decisions}
    assert by["micro_unlock_10"]["passes_better_vs_worse"] is True
    assert by["micro_unlock_10"]["passes_median_incremental_pnl"] is True


def test_deterministic_micro_output() -> None:
    cfg = phase_d1_base_config()
    cfg.fee_rate = 0.0
    cfg.slippage_bps = 0.0
    window = _window()
    policy = micro_unlock_configs()["micro_unlock_10"]
    a = run_micro_on_window(window, cfg, policy)
    b = run_micro_on_window(window, cfg, policy)
    assert a["summary"] == b["summary"]
    assert a["actions"] == b["actions"]


@pytest.mark.skipif(
    not (PHASE_D_DIR / "signal_per_event_summary.csv").exists(),
    reason="Phase D missing",
)
def test_controls_reproduce_phase_d_spot() -> None:
    from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
    from research.backtests.emergency_lock.phase_d_runner import (
        MAIN_RELOCK_VARIANT,
        evaluate_event_signal,
        load_phase_c_events,
    )

    events = load_phase_c_events(PHASE_C_DIR / "event_manifest.csv")
    candles = load_candles_for_symbol(
        symbol="APTUSDT", timeframe="5m", data_dir=DEFAULT_DATA_DIR, limit=None
    )
    cfg = phase_d1_base_config()
    phase_d = {}
    with (PHASE_D_DIR / "signal_per_event_summary.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            if row["relock_variant"] != MAIN_RELOCK_VARIANT:
                continue
            phase_d[(row["event_id"], row["signal_name"])] = row

    for event in events[:2]:
        for signal_name in (
            "full_lock_control",
            "rebound_baseline",
            "swing_break_with_ema",
        ):
            out = evaluate_event_signal(
                candles,
                event,
                cfg,
                signal_name=signal_name,
                relock_variant=MAIN_RELOCK_VARIANT,
                oracle_possible=None,
                full_lock_final=None,
                full_lock_min=None,
            )["row"]
            ref = phase_d[(event.event_id, signal_name)]
            assert float(out["final_net_pnl"]) == pytest.approx(
                float(ref["final_net_pnl"]), abs=1e-6
            )
            assert int(out["unlock_count"]) == int(float(ref["unlock_count"]))

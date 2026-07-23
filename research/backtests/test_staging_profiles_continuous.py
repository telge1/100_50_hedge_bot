"""Unit tests for continuous staging sequencer (no multi-start, no overlap)."""

from __future__ import annotations

import pytest

from research.backtests.staging_profiles_continuous import (
    check_overlap_integrity,
    first_trade_parity_rows,
    next_start_after_flat,
    run_continuous_sequence,
    safety_aggregate,
    trade_end_bar,
    validate_profiles,
)


def _fake_trade(
    *,
    coin: str,
    profile: str,
    trade_number: int,
    start_index: int,
    duration: int,
    flat: bool,
    realized: float = 1.0,
    open_mtm: float = 0.0,
) -> dict:
    end_bar = trade_end_bar(start_index=start_index, candles_processed=duration)
    return {
        "coin": coin,
        "profile": profile,
        "trade_number": trade_number,
        "trade_id": f"{coin}|{profile}|continuous|{trade_number:04d}",
        "start_bar": start_index,
        "start_index": start_index,
        "end_bar": end_bar,
        "flat_bar": end_bar if flat else None,
        "next_start_bar": next_start_after_flat(end_bar) if flat else None,
        "trade_flat": int(flat),
        "is_blocker": int(not flat),
        "active_position_at_end": int(not flat),
        "candles_processed": duration,
        "duration_candles": duration,
        "realized_pnl": realized,
        "open_mtm": 0.0 if flat else open_mtm,
        "total_pnl": realized + (0.0 if flat else open_mtm),
        "closed_pnl": realized if flat else 0.0,
        "pnl_reconcile_ok": 1,
        "economic_undercoverage_closed": 0,
        "sufficient_false_closed": 0,
        "invalid_partial": 0,
        "over_close": 0,
        "duplicate_stage": 0,
        "orphan_stage_order": 0,
        "late_stage_fill_after_exit": 0,
        "stale_generation_fill": 0,
        "final_long_qty": 0.0 if flat else 1.0,
        "final_short_qty": 0.0 if flat else 0.5,
    }


def test_validate_profiles_rejects_forbidden() -> None:
    with pytest.raises(ValueError):
        validate_profiles(["legacy", "fixed_step_1pct_equal"])
    with pytest.raises(ValueError):
        validate_profiles(["two_early_medium_full_dynamic"])
    assert validate_profiles(["legacy", "adaptive_equal"]) == (
        "legacy",
        "adaptive_equal",
    )


def test_next_entry_only_after_flat() -> None:
    calls: list[int] = []

    def runner(**kwargs):
        start = int(kwargs["start_index"])
        calls.append(start)
        n = len(calls)
        # Flat after 10 bars for first two, open on third
        flat = n < 3
        return _fake_trade(
            coin=kwargs["coin"],
            profile=kwargs["profile"],
            trade_number=kwargs["trade_number"],
            start_index=start,
            duration=10,
            flat=flat,
            realized=1.0,
            open_mtm=-5.0,
        )

    candles = [object()] * 100
    trades = run_continuous_sequence(
        coin="APTUSDT",
        profile="legacy",
        candles=candles,
        warmup=5,
        trade_runner=runner,
    )
    assert [t["start_bar"] for t in trades] == [5, 16, 27]
    assert calls == [5, 16, 27]
    assert int(trades[-1]["trade_flat"]) == 0
    for i in range(1, len(trades)):
        prev_flat = int(trades[i - 1]["flat_bar"])
        assert int(trades[i]["start_bar"]) > prev_flat


def test_no_overlap_integrity_pass() -> None:
    trades = [
        _fake_trade(
            coin="BTCUSDT",
            profile="two_early_medium",
            trade_number=1,
            start_index=10,
            duration=20,
            flat=True,
        ),
        _fake_trade(
            coin="BTCUSDT",
            profile="two_early_medium",
            trade_number=2,
            start_index=31,
            duration=5,
            flat=True,
        ),
    ]
    rows = check_overlap_integrity(trades)
    assert all(int(r["pass"]) == 1 for r in rows)
    assert all(int(r["overlap_detected"]) == 0 for r in rows)


def test_overlap_detected_when_start_inside_prior() -> None:
    trades = [
        _fake_trade(
            coin="ETHUSDT",
            profile="legacy",
            trade_number=1,
            start_index=10,
            duration=50,
            flat=True,
        ),
        _fake_trade(
            coin="ETHUSDT",
            profile="legacy",
            trade_number=2,
            start_index=40,  # inside prior
            duration=5,
            flat=True,
        ),
    ]
    rows = check_overlap_integrity(trades)
    assert int(rows[1]["overlap_detected"]) == 1
    assert int(rows[1]["pass"]) == 0


def test_pnl_reconciliation_total_equals_realized_plus_open() -> None:
    flat = _fake_trade(
        coin="APTUSDT",
        profile="adaptive_equal",
        trade_number=1,
        start_index=0,
        duration=10,
        flat=True,
        realized=3.5,
    )
    open_t = _fake_trade(
        coin="APTUSDT",
        profile="adaptive_equal",
        trade_number=2,
        start_index=11,
        duration=10,
        flat=False,
        realized=-1.0,
        open_mtm=-20.0,
    )
    assert flat["total_pnl"] == flat["realized_pnl"] + flat["open_mtm"]
    assert open_t["total_pnl"] == open_t["realized_pnl"] + open_t["open_mtm"]
    assert open_t["total_pnl"] == -21.0


def test_first_trade_parity() -> None:
    by_p = {
        "legacy": [
            _fake_trade(
                coin="APTUSDT",
                profile="legacy",
                trade_number=1,
                start_index=240,
                duration=10,
                flat=True,
            )
        ],
        "two_early_medium": [
            _fake_trade(
                coin="APTUSDT",
                profile="two_early_medium",
                trade_number=1,
                start_index=240,
                duration=50,
                flat=False,
                open_mtm=-1.0,
            )
        ],
        "adaptive_equal": [
            _fake_trade(
                coin="APTUSDT",
                profile="adaptive_equal",
                trade_number=1,
                start_index=240,
                duration=20,
                flat=True,
            )
        ],
    }
    row = first_trade_parity_rows(by_p, coin="APTUSDT")
    assert int(row["first_trade_start_parity_ok"]) == 1


def test_safety_aggregate_green() -> None:
    trades = [
        _fake_trade(
            coin="APTUSDT",
            profile="legacy",
            trade_number=1,
            start_index=0,
            duration=5,
            flat=True,
        )
    ]
    integ = check_overlap_integrity(trades)
    safety = safety_aggregate(trades, integ)
    assert int(safety["all_green"]) == 1


def test_no_stale_orders_flag_fails_integrity() -> None:
    t = _fake_trade(
        coin="APTUSDT",
        profile="legacy",
        trade_number=1,
        start_index=0,
        duration=5,
        flat=True,
    )
    t["orphan_stage_order"] = 1
    rows = check_overlap_integrity([t])
    assert int(rows[0]["stale_orders_detected"]) == 1
    assert int(rows[0]["pass"]) == 0

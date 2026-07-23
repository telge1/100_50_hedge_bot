"""Tests for large multi-coin × window TEM validation planning/resume."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from research.backtests.multicoin_price_staging_grid import (
    assert_output_dir_safe,
    atomic_write_json,
    load_checkpoint,
)
from research.backtests.simulated_order_book import SyntheticCandle
from research.backtests.two_early_medium_window_plan import (
    build_time_windows_for_coin,
    select_starts_for_window,
    window_pair_key,
    window_profile_run_key,
)


def _candles(n: int, *, start_px: float = 1.0) -> list[SyntheticCandle]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    px = start_px
    for i in range(n):
        px *= 1.0 + (0.001 if (i // 30) % 2 == 0 else -0.0008)
        out.append(
            SyntheticCandle(
                symbol="TESTUSDT",
                timestamp=base,
                open=px,
                high=px * 1.002,
                low=px * 0.998,
                close=px,
            )
        )
    return out


def test_window_pair_keys_include_window() -> None:
    assert window_pair_key("aptusdt", "early", 100) == "APTUSDT|early|100"
    assert window_profile_run_key("APTUSDT", "late", 50, "legacy") == "APTUSDT|late|50|legacy"


def test_time_windows_are_chronological_and_non_overlapping_run_ends() -> None:
    candles = _candles(30_000)
    windows = build_time_windows_for_coin("APTUSDT", candles, warmup=240)
    kinds = [w.kind for w in windows]
    assert "early" in kinds and "middle" in kinds and "late" in kinds
    assert "full_history" in kinds
    chron = [w for w in windows if w.kind in {"early", "middle", "late"}]
    for a, b in zip(chron, chron[1:]):
        assert a.run_end_index <= b.start_index_lo or a.run_end_index <= b.run_end_index
        assert a.start_index_hi < a.run_end_index
        assert a.start_index_lo <= a.start_index_hi


def test_starts_lie_within_window_bounds() -> None:
    candles = _candles(25_000)
    windows = build_time_windows_for_coin("TRXUSDT", candles)
    early = next(w for w in windows if w.kind == "early")
    rows = select_starts_for_window(
        coin="TRXUSDT",
        candles=candles,
        window=early,
        blocker_starts=[early.start_index_lo + 10],
        target_starts=20,
        seed=1,
        smoke=False,
    )
    assert len(rows) >= 10
    keys = [r["pair_key"] for r in rows]
    assert len(keys) == len(set(keys))
    for r in rows:
        assert early.start_index_lo <= int(r["start_index"]) <= early.start_index_hi
        assert int(r["run_end_index"]) == early.run_end_index
        assert int(r["max_window_candles"]) == early.run_end_index - int(r["start_index"])


def test_identical_starts_independent_of_profile_concept() -> None:
    """Selection does not take a profile argument — both profiles share the plan."""
    candles = _candles(20_000)
    windows = build_time_windows_for_coin("ATOMUSDT", candles)
    mid = next(w for w in windows if w.kind == "middle")
    a = select_starts_for_window(
        coin="ATOMUSDT", candles=candles, window=mid, blocker_starts=[], target_starts=15, seed=7
    )
    b = select_starts_for_window(
        coin="ATOMUSDT", candles=candles, window=mid, blocker_starts=[], target_starts=15, seed=7
    )
    assert [r["start_index"] for r in a] == [r["start_index"] for r in b]


def test_output_dir_safe_and_resume_checkpoint(tmp_path: Path) -> None:
    out = tmp_path / "large"
    out.mkdir()
    assert_output_dir_safe(out, resume=True)
    ck = {
        "version": 1,
        "completed_pair_keys": ["APTUSDT|early|100"],
        "completed_run_keys": ["APTUSDT|early|100|legacy"],
    }
    atomic_write_json(out / "checkpoint.json", ck)
    loaded = load_checkpoint(out / "checkpoint.json")
    assert loaded is not None
    assert "APTUSDT|early|100" in loaded["completed_pair_keys"]


def test_lookahead_regime_uses_prefix_only() -> None:
    from research.backtests.two_early_medium_multistart_starts import (
        assert_no_lookahead_features,
        select_start_points_for_coin,
    )

    candles = _candles(5000)
    pts = select_start_points_for_coin(
        coin="APTUSDT",
        candles=candles,
        historical_blocker_starts=[],
        target_total=10,
        seed=1,
        warmup=240,
        min_remaining=800,
        grid_step=400,
    )
    assert pts
    assert_no_lookahead_features(candles, pts[0].start_index, pts[0].causal_features)

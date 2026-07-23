"""Unit tests for two_early_medium causal multi-start validation helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from research.backtests.simulated_order_book import SyntheticCandle
from research.backtests.two_early_medium_multistart_metrics import (
    bootstrap_ci,
    compare_pair,
    decide,
    status_bucket,
    summarize_pairs,
)
from research.backtests.two_early_medium_multistart_starts import (
    CATEGORY_GRID,
    CATEGORY_HISTORICAL_BLOCKER,
    assert_no_lookahead_features,
    classify_regimes_at_index,
    compute_causal_feature_frame,
    eligible_indices,
    pair_key,
    profile_run_key,
    select_start_points_for_coin,
)


def _trend_candles(n: int, *, start: float = 100.0, drift: float = 0.02) -> list[SyntheticCandle]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out: list[SyntheticCandle] = []
    px = start
    for i in range(n):
        px = px * (1.0 + drift * (0.1 if i % 17 else 1.0))
        hi = px * 1.002
        lo = px * 0.998
        out.append(
            SyntheticCandle(
                symbol="TESTUSDT",
                timestamp=base,
                open=px,
                high=hi,
                low=lo,
                close=px,
            )
        )
    return out


def test_pair_and_run_keys_stable() -> None:
    assert pair_key("aptusdt", 570) == "APTUSDT|570"
    assert profile_run_key("APTUSDT", 570, "legacy") == "APTUSDT|570|legacy"


def test_eligible_respects_warmup_and_min_remaining() -> None:
    idxs = eligible_indices(1000, warmup=100, min_remaining=200)
    assert idxs[0] == 100
    assert idxs[-1] == 800
    assert all(i + 200 <= 1000 for i in idxs)


def test_regime_classification_is_causal_no_future() -> None:
    candles = _trend_candles(500, drift=0.05)
    frame_full = compute_causal_feature_frame(candles)
    frame_prefix = compute_causal_feature_frame(candles[:301])
    tags_a, feats_a = classify_regimes_at_index(frame_full, 300)
    tags_b, feats_b = classify_regimes_at_index(frame_prefix, 300)
    assert tags_a == tags_b
    assert abs(feats_a["close"] - feats_b["close"]) < 1e-12
    assert abs(feats_a["ema_slow"] - feats_b["ema_slow"]) < 1e-9


def test_start_selection_deterministic_and_includes_blocker() -> None:
    candles = _trend_candles(8000, drift=0.01)
    a = select_start_points_for_coin(
        coin="APTUSDT",
        candles=candles,
        historical_blocker_starts=[570],
        target_total=30,
        seed=20260721,
        warmup=240,
        min_remaining=1500,
        grid_step=1200,
    )
    b = select_start_points_for_coin(
        coin="APTUSDT",
        candles=candles,
        historical_blocker_starts=[570],
        target_total=30,
        seed=20260721,
        warmup=240,
        min_remaining=1500,
        grid_step=1200,
    )
    assert [p.start_index for p in a] == [p.start_index for p in b]
    assert any(CATEGORY_HISTORICAL_BLOCKER in p.categories for p in a)
    assert any(CATEGORY_GRID in p.categories for p in a)
    assert len({p.start_index for p in a}) == len(a)
    # Identical starts across "profiles" — selection does not depend on profile.
    assert_no_lookahead_features(candles, a[0].start_index, a[0].causal_features)


def test_status_bucket_and_compare_pair() -> None:
    assert status_bucket(False, True) == "legacy_open_staging_closed"
    legacy = {
        "coin": "APTUSDT",
        "start_index": 1,
        "trade_flat": 0,
        "total_pnl": -10.0,
        "closed_pnl": 0.0,
        "open_mtm": -10.0,
        "duration_candles": 100,
        "status": "open",
        "economically_valid_close": 0,
    }
    staging = {
        "coin": "APTUSDT",
        "start_index": 1,
        "trade_flat": 1,
        "total_pnl": 4.0,
        "closed_pnl": 4.0,
        "open_mtm": 0.0,
        "duration_candles": 50,
        "status": "closed",
        "coverage_class": "covered_by_basket_exit",
        "economically_valid_close": 1,
    }
    pair = compare_pair(legacy, staging, {"pair_key": "APTUSDT|1", "primary_category": "grid"})
    assert pair["better"] == "staging_better"
    assert pair["bucket"] == "legacy_open_staging_closed"
    s = summarize_pairs([pair])
    assert s["better"] == 1
    assert s["additional_valid_closes"] == 1


def test_bootstrap_and_decide_smoke() -> None:
    boot = bootstrap_ci([1.0, 2.0, 3.0, -0.5], n_boot=200, seed=1)
    assert boot["mean_ci"] is not None
    assert boot["median_ci"][0] <= boot["median_ci"][1]
    summary = {
        "better": 10,
        "worse": 3,
        "delta_total": {"sum": 100.0, "median": 1.0, "mean": 2.0},
        "neutral_pool": {"delta_total": {"sum": 20.0}},
        "exposure_drawdown_ok": True,
        "atom_regression_bounded": False,
    }
    leave = {"without_apt": 50.0}
    d = decide(summary, leave, True)
    assert d["verdict"] == "Research-Kandidat behalten"
    assert d["live_integration"] == "noch keine Live-Integration"


def test_checkpoint_resume_helpers(tmp_path: Path) -> None:
    from research.backtests.multicoin_price_staging_grid import (
        atomic_write_json,
        assert_output_dir_safe,
        load_checkpoint,
    )
    from research.backtests.run_two_early_medium_multistart_validation import _empty_checkpoint

    out = tmp_path / "ms"
    out.mkdir()
    assert_output_dir_safe(out, resume=True)
    ck = _empty_checkpoint(coins=["APTUSDT"], planned_pairs=2)
    ck["completed_pair_keys"] = ["APTUSDT|100"]
    atomic_write_json(out / "checkpoint.json", ck)
    loaded = load_checkpoint(out / "checkpoint.json")
    assert loaded is not None
    assert "APTUSDT|100" in loaded["completed_pair_keys"]

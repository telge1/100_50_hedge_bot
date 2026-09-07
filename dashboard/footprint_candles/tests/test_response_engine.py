"""AVR V1.1 unit tests — invariants, causality, dominant evidence."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from footprint_candles.response_baseline import (  # noqa: E402
    build_baseline_from_feature_rows,
)
from footprint_candles.response_contracts import (  # noqa: E402
    MIN_BASELINE_VALID_SECONDS,
    ResponseState,
    badge_for_state,
    clv,
)
from footprint_candles.response_engine import (  # noqa: E402
    RawTrade,
    SecondBucket,
    SecondSeries,
    _pick_dominant,
    aggregate_trades_to_seconds,
    classify_features,
    summarize_candle_response,
)


def _bucket(
    t: int,
    *,
    buy: float = 0.0,
    sell: float = 0.0,
    first: float = 100.0,
    last: float | None = None,
) -> SecondBucket:
    last = first if last is None else last
    return SecondBucket(
        second_ts=t,
        buy_notional=buy,
        sell_notional=sell,
        buy_size=buy / first if first else 0.0,
        sell_size=sell / first if first else 0.0,
        buy_trade_count=1 if buy > 0 else 0,
        sell_trade_count=1 if sell > 0 else 0,
        first_price=first,
        last_price=last,
        high_price=max(first, last),
        low_price=min(first, last),
    )


def _rich_baseline() -> object:
    rows = []
    for i in range(MIN_BASELINE_VALID_SECONDS):
        rows.append(
            {
                "buy_notional_rate": 1e3 + i,
                "sell_notional_rate": 1e3 + (i % 50),
                "delta_notional_rate": 0.0,
                "price_velocity_bps_per_second": 0.01 * ((i % 5) - 2),
                "buy_efficiency": 0.5,
                "sell_efficiency": 0.5,
                "response_ratio_buy": 0.5,
                "response_ratio_sell": 0.5,
                "total_notional_rate": 2e3,
                "trade_rate": 2.0,
            }
        )
    return build_baseline_from_feature_rows(
        rows, valid_second_buckets=MIN_BASELINE_VALID_SECONDS
    )


def _feats(**kwargs):
    base = {
        "insufficient_coverage": False,
        "available_at": 1000,
        "classified_at": 1000,
        "window_seconds": 15,
        "window_start": 985,
        "window_end": 1000,
        "buy_notional_rate": 1e3,
        "sell_notional_rate": 1e3,
        "delta_notional_rate": 0.0,
        "abs_delta_notional_rate": 0.0,
        "price_velocity_bps_per_second": 0.0,
        "price_move_bps": 0.0,
        "buy_efficiency": 0.0,
        "sell_efficiency": 0.0,
        "response_ratio_buy": 0.0,
        "response_ratio_sell": 0.0,
        "opposite_response_buy": 0.0,
        "opposite_response_sell": 0.0,
        "directional_move_bps_down": 0.0,
        "directional_move_bps_up": 0.0,
        "directional_velocity_bps_s_down": 0.0,
        "directional_velocity_bps_s_up": 0.0,
        "aggression_notional_rate_sell": 1e3,
        "aggression_notional_rate_buy": 1e3,
        "impact_efficiency_bps_per_million_sell": 0.0,
        "impact_efficiency_bps_per_million_buy": 0.0,
        "total_notional_rate": 2e3,
        "trade_rate": 2.0,
        "directional_dominance_sell": 0.5,
        "directional_dominance_buy": 0.5,
    }
    base.update(kwargs)
    return base


def test_buy_sell_and_first_last_tiebreak():
    trades = [
        RawTrade(50.0, "2", "Buy", 102.0, 1.0, 102.0),
        RawTrade(50.0, "1", "Buy", 100.0, 1.0, 100.0),
        RawTrade(50.0, "3", "Sell", 101.0, 1.0, 101.0),
    ]
    secs = aggregate_trades_to_seconds(trades)
    assert secs[0].first_price == 100.0
    assert secs[0].last_price == 101.0


def test_half_open_window_boundary_causality():
    """Trades in bucket T are excluded when available_at == T."""
    # Bucket 100 has a huge sell that moves price; bucket 99 quiet.
    buckets = [_bucket(t, buy=1e3, sell=1e3, first=100.0, last=100.0) for t in range(80, 100)]
    buckets.append(_bucket(100, buy=1e3, sell=5e7, first=100.0, last=99.0))
    series = SecondSeries(buckets)
    # available_at=100 → window [85,100) — excludes bucket 100
    f_before = series.features_at(100, 15, min_valid_frac=0.5)
    assert f_before is not None
    assert f_before["window_start"] == 85
    assert f_before["window_end"] == 100
    assert f_before["price_move_bps"] == pytest.approx(0.0, abs=1e-9)
    # available_at=101 → includes bucket 100
    f_after = series.features_at(101, 15, min_valid_frac=0.5)
    assert f_after is not None
    assert f_after["price_move_bps"] < 0
    assert f_after["sell_notional"] > 1e6


def test_no_lookahead_across_jump():
    buckets = []
    for t in range(0, 40):
        last = 100.0 if t < 30 else 101.0
        buckets.append(
            _bucket(t, buy=1e6 if t >= 30 else 1e3, sell=1e3, first=100.0, last=last)
        )
    series = SecondSeries(buckets)
    # Jump lives in bucket 30 → visible only for available_at > 30
    f = series.features_at(30, 15, min_valid_frac=0.5)
    assert f is not None
    assert f["price_move_bps"] == pytest.approx(0.0, abs=1e-9)
    f2 = series.features_at(31, 15, min_valid_frac=0.5)
    assert f2["price_move_bps"] > 0


def test_seller_control_high_aggression_fast_down():
    base = _rich_baseline()
    feats = _feats(
        sell_notional_rate=5e7,
        buy_notional_rate=1e3,
        aggression_notional_rate_sell=5e7,
        price_move_bps=-30.0,
        directional_move_bps_down=30.0,
        price_velocity_bps_per_second=-2.0,
        directional_velocity_bps_s_down=2.0,
        sell_efficiency=50.0,
        response_ratio_sell=50.0,
        impact_efficiency_bps_per_million_sell=50.0,
        directional_dominance_sell=0.9,
        directional_dominance_buy=0.1,
        total_notional_rate=5e7,
    )
    out = classify_features(feats, base)
    assert out["state"] == ResponseState.SELLER_CONTROL.value
    ev = out["evidence"]
    assert ev["available_at"] == 1000
    assert ev["window_seconds"] == 15
    assert ev["window_start"] == 985
    assert ev["window_end"] == 1000
    assert "down_velocity_percentile" in ev


def test_sell_absorption_flat_price():
    base = _rich_baseline()
    feats = _feats(
        sell_notional_rate=5e7,
        buy_notional_rate=1e3,
        aggression_notional_rate_sell=5e7,
        price_move_bps=0.1,
        directional_move_bps_down=0.0,
        directional_move_bps_up=0.1,
        price_velocity_bps_per_second=0.01,
        sell_efficiency=0.0,
        response_ratio_sell=0.0,
        opposite_response_sell=0.1,
        directional_dominance_sell=0.85,
        directional_dominance_buy=0.15,
        total_notional_rate=5e7,
    )
    out = classify_features(feats, base)
    assert out["state"] == ResponseState.SELL_ABSORPTION_CANDIDATE.value


def test_sell_absorption_price_rises():
    base = _rich_baseline()
    feats = _feats(
        sell_notional_rate=5e7,
        buy_notional_rate=1e3,
        price_move_bps=3.0,
        directional_move_bps_up=3.0,
        price_velocity_bps_per_second=0.2,
        sell_efficiency=0.0,
        opposite_response_sell=3.0,
        directional_dominance_sell=0.85,
        directional_dominance_buy=0.15,
        total_notional_rate=5e7,
    )
    out = classify_features(feats, base)
    assert out["state"] == ResponseState.SELL_ABSORPTION_CANDIDATE.value


def test_contradiction_guard_no_absorption_when_fast_efficient_down():
    """High sell + fast down + high efficiency must NOT be absorption."""
    base = _rich_baseline()
    feats = _feats(
        sell_notional_rate=5e7,
        buy_notional_rate=1e3,
        price_move_bps=-8.0,
        directional_move_bps_down=8.0,
        price_velocity_bps_per_second=-0.55,
        directional_velocity_bps_s_down=0.55,
        sell_efficiency=40.0,
        response_ratio_sell=40.0,
        directional_dominance_sell=0.9,
        directional_dominance_buy=0.1,
        total_notional_rate=5e7,
    )
    out = classify_features(feats, base)
    assert out["state"] == ResponseState.SELLER_CONTROL.value
    assert out["state"] != ResponseState.SELL_ABSORPTION_CANDIDATE.value


def test_buyer_control_and_absorption_symmetric():
    base = _rich_baseline()
    ctrl = classify_features(
        _feats(
            buy_notional_rate=5e7,
            sell_notional_rate=1e3,
            price_move_bps=30.0,
            directional_move_bps_up=30.0,
            price_velocity_bps_per_second=2.0,
            buy_efficiency=50.0,
            response_ratio_buy=50.0,
            directional_dominance_buy=0.9,
            directional_dominance_sell=0.1,
            total_notional_rate=5e7,
        ),
        base,
    )
    assert ctrl["state"] == ResponseState.BUYER_CONTROL.value
    abs_ = classify_features(
        _feats(
            buy_notional_rate=5e7,
            sell_notional_rate=1e3,
            price_move_bps=-2.0,
            directional_move_bps_down=2.0,
            price_velocity_bps_per_second=-0.1,
            buy_efficiency=0.0,
            opposite_response_buy=2.0,
            directional_dominance_buy=0.85,
            directional_dominance_sell=0.15,
            total_notional_rate=5e7,
        ),
        base,
    )
    assert abs_["state"] == ResponseState.BUY_ABSORPTION_CANDIDATE.value


def test_control_and_absorption_never_same_classify():
    base = _rich_baseline()
    for move, vel, eff, expect in [
        (-20, -1.5, 50, ResponseState.SELLER_CONTROL.value),
        (2, 0.1, 0.0, ResponseState.SELL_ABSORPTION_CANDIDATE.value),
    ]:
        out = classify_features(
            _feats(
                sell_notional_rate=5e7,
                buy_notional_rate=1e3,
                price_move_bps=move,
                directional_move_bps_down=max(-move, 0),
                directional_move_bps_up=max(move, 0),
                price_velocity_bps_per_second=vel,
                sell_efficiency=eff,
                response_ratio_sell=eff,
                opposite_response_sell=max(move, 0),
                directional_dominance_sell=0.9,
                directional_dominance_buy=0.1,
                total_notional_rate=5e7,
            ),
            base,
        )
        assert out["state"] == expect


def test_evidence_same_window_as_state():
    base = _rich_baseline()
    feats = _feats(
        available_at=5555,
        classified_at=5555,
        window_start=5540,
        window_end=5555,
        window_seconds=15,
        sell_notional_rate=5e7,
        buy_notional_rate=1e3,
        price_move_bps=-20,
        directional_move_bps_down=20,
        price_velocity_bps_per_second=-1.5,
        sell_efficiency=50,
        response_ratio_sell=50,
        directional_dominance_sell=0.9,
        directional_dominance_buy=0.1,
        total_notional_rate=5e7,
    )
    out = classify_features(feats, base)
    ev = out["evidence"]
    assert ev["available_at"] == 5555
    assert ev["window_start"] == 5540
    assert ev["window_end"] == 5555
    assert ev["window_seconds"] == 15


def test_dominant_tie_break_deterministic():
    weak_abs = [
        (
            100 + i,
            ResponseState.SELL_ABSORPTION_CANDIDATE.value,
            {"strength": 40, "evidence": {"available_at": 100 + i}},
        )
        for i in range(6)
    ]
    strong_ctrl = [
        (
            200,
            ResponseState.SELLER_CONTROL.value,
            {"strength": 92, "evidence": {"available_at": 200}},
        )
    ]
    dom, strength, cl = _pick_dominant(weak_abs + strong_ctrl)
    assert dom == ResponseState.SELLER_CONTROL.value
    assert cl["evidence"]["available_at"] == 200
    assert strength >= 92


def test_badges():
    assert badge_for_state(ResponseState.SELLER_CONTROL) == "S CTRL"
    assert badge_for_state(ResponseState.SELL_ABSORPTION_CANDIDATE) == "S ABS"


def test_clv_high_eq_low():
    assert clv(100, 100, 100, 100) == pytest.approx(0.5)


def test_missing_unverified_and_no_1s_arrays_in_summary():
    series = SecondSeries([])
    out = summarize_candle_response(
        candle_time=1000,
        ohlc={"open": 1, "high": 1, "low": 1, "close": 1},
        coverage="MISSING",
        series=series,
        now_unix=1300,
    )
    assert out["dominant_state"] == ResponseState.INSUFFICIENT_DATA.value
    assert out["verification"] == "UNVERIFIED"
    assert "seconds" not in out
    blob = str(out)
    assert "second_ts" not in blob


def test_unknown_stays_unverified_on_summary():
    t0 = 2_000_000_000
    hist = [_bucket(t0 - 2000 + i, buy=2e3, sell=2e3) for i in range(1800)]
    buckets = [_bucket(t0 + i, buy=1e4, sell=1e4) for i in range(50)]
    series = SecondSeries(hist + buckets)
    out = summarize_candle_response(
        candle_time=t0,
        ohlc={"open": 100, "high": 101, "low": 99, "close": 100},
        coverage="UNKNOWN",
        series=series,
        now_unix=t0 + 50,
    )
    assert out["verification"] == "UNVERIFIED"
    assert out["provisional"] is True


def test_utc_second_bucket_floor():
    trades = [RawTrade(10.999, "x", "Buy", 1.0, 1.0, 1.0)]
    assert aggregate_trades_to_seconds(trades)[0].second_ts == 10


def test_baseline_requires_900_valid_seconds():
    thin = build_baseline_from_feature_rows(
        [
            {
                "buy_notional_rate": 1.0,
                "sell_notional_rate": 1.0,
                "delta_notional_rate": 0.0,
                "price_velocity_bps_per_second": 0.0,
                "buy_efficiency": 0.0,
                "sell_efficiency": 0.0,
                "total_notional_rate": 2.0,
                "trade_rate": 1.0,
            }
        ]
        * 200,
        valid_second_buckets=200,
    )
    assert not thin.sufficient
    assert _rich_baseline().sufficient


def test_gap_invalidates_baseline():
    base = build_baseline_from_feature_rows(
        [{"buy_notional_rate": 1.0, "sell_notional_rate": 1.0, "delta_notional_rate": 0.0,
          "price_velocity_bps_per_second": 0.0, "buy_efficiency": 0.0, "sell_efficiency": 0.0,
          "total_notional_rate": 2.0, "trade_rate": 1.0}]
        * MIN_BASELINE_VALID_SECONDS,
        invalidated=True,
        invalidate_reason="gap",
        valid_second_buckets=MIN_BASELINE_VALID_SECONDS,
    )
    assert not base.sufficient


def test_vacuum_down_proxy():
    base = _rich_baseline()
    out = classify_features(
        _feats(
            buy_notional_rate=500.0,
            sell_notional_rate=500.0,
            price_move_bps=-45.0,
            directional_move_bps_down=45.0,
            price_velocity_bps_per_second=-3.0,
            directional_velocity_bps_s_down=3.0,
            sell_efficiency=10.0,
            total_notional_rate=1000.0,
            directional_dominance_sell=0.5,
            directional_dominance_buy=0.5,
        ),
        base,
    )
    assert out["state"] == ResponseState.VACUUM_DOWN_PROXY.value
    assert out["confirmation"] == "PROXY_UNCONFIRMED"


def test_thirds_and_forming_provisional():
    t0 = 2_100_000_000
    hist = [_bucket(t0 - 2000 + i, buy=2e3, sell=2e3) for i in range(1800)]
    buckets = [_bucket(t0 + i, buy=1e4, sell=1e4) for i in range(120)]
    series = SecondSeries(hist + buckets)
    out = summarize_candle_response(
        candle_time=t0,
        ohlc={"open": 100, "high": 101, "low": 99, "close": 100.5},
        coverage="UNKNOWN",
        series=series,
        now_unix=t0 + 120,
        sample_every_s=20,
    )
    assert "EARLY" in out["thirds"]
    assert "MIDDLE" in out["thirds"]
    assert "LATE" in out["thirds"]
    assert out["provisional"] is True
    assert out["thirds"]["LATE"]["provisional"] is True
    assert out["lifecycle"] == "PROVISIONAL"


def test_config_hash_stable():
    from footprint_candles.response_contracts import config_hash

    assert config_hash() == config_hash()
    assert len(config_hash()) == 16

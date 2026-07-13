"""Phase-3 Momentum confirmation tests (no entry / TP)."""

from __future__ import annotations

import json

from research.regime_scanner.momentum import (
    MomentumConfig,
    body_to_range_ratio,
    close_location_ratio,
    compute_candle_metrics,
    default_momentum_config,
    directional_body,
    evaluate_momentum_confirmation,
    initialize_momentum_state,
    range_atr_ratio,
    update_momentum_state,
)
from research.regime_scanner.point_audit import json_safe


def _pa(**kwargs) -> dict:
    base = {
        "setup_id": "setup_00001",
        "side": "long",
        "pattern_type": "higher_low",
        "setup_activation_timestamp": "2026-03-01T01:00:00+00:00",
        "structure_break_timestamp": "2026-03-01T02:00:00+00:00",
        "confirmation_level": 100.0,
        "invalidation_level": 98.0,
        "warnings": [],
        "blockers": [],
    }
    base.update(kwargs)
    return base


def _c(
    ts: str,
    *,
    o: float,
    h: float,
    l: float,
    c: float,
    volume: float = 10.0,
) -> dict:
    return {
        "timestamp": ts,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": volume,
    }


CFG = MomentumConfig(
    confirmation_window_candles=3,
    allow_confirmation_on_break_candle=True,
    min_body_to_range_ratio=0.50,
    min_close_location_ratio=0.60,
    min_range_atr_ratio=0.30,
    max_range_atr_ratio=3.00,
    volume_filter_enabled=False,
)


def _strong_long(**kwargs) -> dict:
    base = _c("2026-03-01T02:00:00+00:00", o=100.2, h=102.0, l=100.0, c=101.8)
    base.update(kwargs)
    return base


def _strong_short(**kwargs) -> dict:
    base = _c("2026-03-01T02:00:00+00:00", o=99.8, h=100.0, l=98.0, c=98.2)
    base.update(kwargs)
    return base


def test_strong_long_candle_confirms() -> None:
    state = initialize_momentum_state(_pa(side="long"), CFG)
    state = update_momentum_state(state, _strong_long(), atr=2.0)
    assert state["state"] == "momentum_confirmed"
    conf = evaluate_momentum_confirmation(state)
    assert conf is not None
    assert conf["side"] == "long"
    assert conf["confirmation_type"] == "break_candle"
    assert conf["confidence"] in {"medium", "high"}


def test_strong_short_candle_confirms() -> None:
    state = initialize_momentum_state(
        _pa(side="short", confirmation_level=100.0, pattern_type="lower_high"),
        CFG,
    )
    state = update_momentum_state(state, _strong_short(), atr=2.0)
    assert state["state"] == "momentum_confirmed"
    conf = evaluate_momentum_confirmation(state)
    assert conf["side"] == "short"


def test_doji_does_not_confirm() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:00:00+00:00", o=100.5, h=101.0, l=100.0, c=100.51),
        atr=2.0,
    )
    assert state["state"] == "waiting_for_momentum"
    assert "BODY_TO_RANGE" in (state["latest_condition_result"]["failed"])


def test_wrong_direction_does_not_confirm() -> None:
    state = initialize_momentum_state(_pa(side="long"), CFG)
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:00:00+00:00", o=101.8, h=102.0, l=100.5, c=100.6),
        atr=2.0,
    )
    assert state["state"] != "momentum_confirmed"
    assert "DIRECTIONAL_BODY" in state["latest_condition_result"]["failed"]


def test_poor_close_location_does_not_confirm() -> None:
    state = initialize_momentum_state(_pa(side="long"), CFG)
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:00:00+00:00", o=100.1, h=102.0, l=100.0, c=100.3),
        atr=2.0,
    )
    assert state["state"] != "momentum_confirmed"
    assert "CLOSE_LOCATION" in state["latest_condition_result"]["failed"]


def test_range_too_small_does_not_confirm() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:00:00+00:00", o=100.1, h=100.4, l=100.0, c=100.35),
        atr=2.0,
    )
    assert "RANGE_ATR_TOO_SMALL" in state["latest_condition_result"]["failed"]


def test_range_too_large_does_not_confirm() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:00:00+00:00", o=100.5, h=110.0, l=100.0, c=109.0),
        atr=2.0,
    )
    assert "RANGE_ATR_TOO_LARGE" in state["latest_condition_result"]["failed"]


def test_break_candle_may_confirm() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(state, _strong_long(), atr=2.0)
    assert state["age_candles"] == 0
    assert evaluate_momentum_confirmation(state)["confirmation_type"] == "break_candle"


def test_candle_1_may_confirm() -> None:
    cfg = MomentumConfig(**{**CFG.to_dict(), "allow_confirmation_on_break_candle": False})
    state = initialize_momentum_state(_pa(), cfg)
    state = update_momentum_state(state, _strong_long(), atr=2.0)
    assert state["state"] == "waiting_for_momentum"
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:05:00+00:00", o=100.2, h=102.0, l=100.0, c=101.8),
        atr=2.0,
    )
    assert state["age_candles"] == 1
    assert state["state"] == "momentum_confirmed"
    assert evaluate_momentum_confirmation(state)["candles_after_price_action_confirmation"] == 1


def test_candle_2_may_confirm() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(
        state, _c("2026-03-01T02:00:00+00:00", o=100.5, h=101.0, l=100.0, c=100.55), atr=2.0
    )
    state = update_momentum_state(
        state, _c("2026-03-01T02:05:00+00:00", o=100.5, h=101.0, l=100.0, c=100.55), atr=2.0
    )
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:10:00+00:00", o=100.2, h=102.0, l=100.0, c=101.8),
        atr=2.0,
    )
    assert state["age_candles"] == 2
    assert state["state"] == "momentum_confirmed"


def test_candle_3_may_confirm() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    for i, ts in enumerate(
        [
            "2026-03-01T02:00:00+00:00",
            "2026-03-01T02:05:00+00:00",
            "2026-03-01T02:10:00+00:00",
            "2026-03-01T02:15:00+00:00",
        ]
    ):
        if i < 3:
            candle = _c(ts, o=100.5, h=101.0, l=100.0, c=100.55)
        else:
            candle = _c(ts, o=100.2, h=102.0, l=100.0, c=101.8)
        state = update_momentum_state(state, candle, atr=2.0)
    assert state["age_candles"] == 3
    assert state["state"] == "momentum_confirmed"


def test_expires_after_window() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    for ts in [
        "2026-03-01T02:00:00+00:00",
        "2026-03-01T02:05:00+00:00",
        "2026-03-01T02:10:00+00:00",
        "2026-03-01T02:15:00+00:00",
    ]:
        state = update_momentum_state(
            state, _c(ts, o=100.5, h=101.0, l=100.0, c=100.55), atr=2.0
        )
    assert state["state"] == "expired"
    assert state["invalidation_reason"] == "MOMENTUM_WINDOW_EXPIRED"


def test_long_close_back_under_level_invalidates() -> None:
    state = initialize_momentum_state(_pa(side="long", confirmation_level=100.0), CFG)
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:00:00+00:00", o=100.5, h=101.0, l=100.0, c=100.55),
        atr=2.0,
    )
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:05:00+00:00", o=100.2, h=100.5, l=99.0, c=99.5),
        atr=2.0,
    )
    assert state["state"] == "invalidated"
    assert state["invalidation_reason"] == "CLOSE_BEYOND_STRUCTURE_LEVEL"


def test_short_close_back_above_level_invalidates() -> None:
    state = initialize_momentum_state(_pa(side="short", confirmation_level=100.0), CFG)
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:00:00+00:00", o=99.5, h=100.0, l=99.0, c=99.4),
        atr=2.0,
    )
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:05:00+00:00", o=99.8, h=101.0, l=99.5, c=100.5),
        atr=2.0,
    )
    assert state["state"] == "invalidated"


def test_wick_alone_does_not_invalidate() -> None:
    state = initialize_momentum_state(_pa(side="long", confirmation_level=100.0), CFG)
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:00:00+00:00", o=100.5, h=101.0, l=99.0, c=100.6),
        atr=2.0,
    )
    assert state["state"] == "waiting_for_momentum"


def test_high_equals_low_safe() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:00:00+00:00", o=100.0, h=100.0, l=100.0, c=100.0),
        atr=2.0,
    )
    assert state["state"] != "momentum_confirmed"
    m = state["latest_metrics"]
    assert m["body_to_range_ratio"] == 0.0
    assert m["close_location_ratio"] == 0.0


def test_missing_atr_no_false_confirmation() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(state, _strong_long(), atr=None)
    assert state["state"] != "momentum_confirmed"
    assert "RANGE_ATR" in state["latest_condition_result"]["failed"]


def test_volume_filter_disabled() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(
        state, _strong_long(volume=0.1), atr=2.0, rolling_median_volume=10.0
    )
    assert state["state"] == "momentum_confirmed"


def test_volume_filter_enabled_and_met() -> None:
    cfg = MomentumConfig(
        **{**CFG.to_dict(), "volume_filter_enabled": True, "min_volume_to_median_ratio": 1.0}
    )
    state = initialize_momentum_state(_pa(), cfg)
    state = update_momentum_state(
        state, _strong_long(volume=12.0), atr=2.0, rolling_median_volume=10.0
    )
    assert state["state"] == "momentum_confirmed"


def test_volume_filter_enabled_and_missed() -> None:
    cfg = MomentumConfig(
        **{**CFG.to_dict(), "volume_filter_enabled": True, "min_volume_to_median_ratio": 1.0}
    )
    state = initialize_momentum_state(_pa(), cfg)
    state = update_momentum_state(
        state, _strong_long(volume=5.0), atr=2.0, rolling_median_volume=10.0
    )
    assert state["state"] != "momentum_confirmed"
    assert "VOLUME" in state["latest_condition_result"]["failed"]


def test_no_double_confirmation() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(state, _strong_long(), atr=2.0)
    conf1 = evaluate_momentum_confirmation(state)
    state2 = update_momentum_state(
        state,
        _c("2026-03-01T02:05:00+00:00", o=100.2, h=103.0, l=100.0, c=102.5),
        atr=2.0,
    )
    assert state2["state"] == "momentum_confirmed"
    assert evaluate_momentum_confirmation(state2) == conf1


def test_terminal_does_not_reactivate() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:00:00+00:00", o=100.2, h=100.5, l=99.0, c=99.5),
        atr=2.0,
    )
    assert state["state"] == "invalidated"
    nxt = _strong_long()
    nxt["timestamp"] = "2026-03-01T02:05:00+00:00"
    state2 = update_momentum_state(state, nxt, atr=2.0)
    assert state2["state"] == "invalidated"
    assert evaluate_momentum_confirmation(state2) is None


def test_opposing_setup_invalidates() -> None:
    state = initialize_momentum_state(_pa(side="long"), CFG)
    state = update_momentum_state(
        state,
        _c("2026-03-01T02:00:00+00:00", o=100.5, h=101.0, l=100.0, c=100.55),
        atr=2.0,
        opposing_setup={"setup_activated": True, "setup_side": "short"},
    )
    assert state["state"] == "invalidated"
    assert state["invalidation_reason"] == "NEW_OPPOSING_SETUP"


def test_price_action_invalidation_invalidates() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(
        state,
        _strong_long(),
        atr=2.0,
        price_action_invalidated=True,
    )
    assert state["state"] == "invalidated"
    assert state["invalidation_reason"] == "PRICE_ACTION_INVALIDATED"


def test_long_short_metric_symmetry() -> None:
    long_c = _c("t", o=100, h=102, l=100, c=101.5)
    short_c = _c("t", o=100, h=100, l=98, c=98.5)
    assert abs(body_to_range_ratio(long_c) - body_to_range_ratio(short_c)) < 1e-9
    assert (
        abs(
            close_location_ratio(long_c, side="long")
            - close_location_ratio(short_c, side="short")
        )
        < 1e-9
    )
    assert directional_body(long_c, side="long") is True
    assert directional_body(short_c, side="short") is True


def test_serialization_roundtrip() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(state, _strong_long(), atr=2.0)
    encoded = json.dumps(json_safe(state), allow_nan=False)
    restored = json.loads(encoded)
    assert restored["state"] == "momentum_confirmed"
    assert restored["confirmation"]["side"] == "long"


def test_no_entry_tp_fields() -> None:
    state = initialize_momentum_state(_pa(), CFG)
    state = update_momentum_state(state, _strong_long(), atr=2.0)
    blob = json.dumps(json_safe(state), allow_nan=False)
    for key in ("entry_price", "tp_price", "stop_loss", "mae_pct", "mfe_pct", "position_size"):
        assert key not in blob


def test_default_config_volume_disabled() -> None:
    cfg = default_momentum_config()
    assert cfg.volume_filter_enabled is False
    assert cfg.confirmation_window_candles == 3
    assert cfg.allow_confirmation_on_break_candle is True


def test_range_atr_ratio_helper() -> None:
    c = _c("t", o=1, h=3, l=1, c=2.5)
    assert range_atr_ratio(c, 2.0) == 1.0
    assert range_atr_ratio(c, None) is None
    assert range_atr_ratio(c, 0.0) is None


def test_compute_metrics_invalid_ohlc() -> None:
    m = compute_candle_metrics(
        {"timestamp": "t", "open": float("nan"), "high": 1, "low": 0, "close": 1},
        side="long",
    )
    assert m["ohlc_valid"] is False

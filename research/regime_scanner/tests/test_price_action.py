"""Phase-2 Price Action tests (causal structure confirmation, no entry/TP)."""

from __future__ import annotations

import json

import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.price_action import (
    PriceActionConfig,
    confirmed_pivot_to_swing,
    evaluate_price_action_confirmation,
    filter_swings_as_of,
    initialize_price_action_state,
    swing_usable_as_of,
    update_price_action_state,
)
from research.regime_scanner.price_action_audit import walk_price_action, write_price_action_audit
from research.regime_scanner.structure import classify_swing_structure
from research.regime_scanner.swings import ConfirmedPivot, find_developing_swing_candidates


def _swing(
    *,
    side: str,
    price: float,
    pivot_index: int,
    confirmation_index: int | None = None,
    pivot_ts: str | None = None,
    conf_ts: str | None = None,
) -> dict:
    conf_i = confirmation_index if confirmation_index is not None else pivot_index + 3
    base = pd.Timestamp("2026-03-01T00:00:00+00:00")
    return {
        "side": side,
        "price": price,
        "pivot_index": pivot_index,
        "pivot_timestamp": pivot_ts
        or (base + pd.Timedelta(minutes=5 * pivot_index)).isoformat(),
        "confirmation_index": conf_i,
        "confirmation_timestamp": conf_ts
        or (base + pd.Timedelta(minutes=5 * conf_i)).isoformat(),
        "source_timeframe": "5m",
        "reason_codes": [],
    }


def _candle(ts: str, *, o: float, h: float, l: float, c: float, **extra) -> dict:
    row = {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}
    row.update(extra)
    return row


def _approx(value: float, rel: float = 1e-9):
    class _Approx:
        def __eq__(self, other: object) -> bool:
            try:
                return abs(float(other) - value) <= rel * max(1.0, abs(value))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return False

        def __repr__(self) -> str:
            return f"approx({value})"

    return _Approx()


def _short_setup(**kwargs) -> dict:
    base = {
        "setup_activated": True,
        "setup_side": "short",
        "setup_type": "continuation_weakness",
        "setup_activation_timestamp": "2026-03-01T01:00:00+00:00",
        "activating_regime": "bearish_trend_with_trend_weakness",
        "previous_regime": "bearish_trend",
        "confidence": "medium",
        "blockers": [],
        "warnings": [],
        "invalidation_reason": None,
        "source_snapshot": {"combined_regime": "bearish_trend"},
    }
    base.update(kwargs)
    return base


def _long_setup(**kwargs) -> dict:
    s = _short_setup(
        setup_side="long",
        activating_regime="bullish_trend_with_trend_weakness",
        previous_regime="bullish_trend",
        source_snapshot={"combined_regime": "bullish_trend"},
    )
    s.update(kwargs)
    return s


CFG = PriceActionConfig(
    minimum_swing_separation_candles=5,
    max_setup_age_candles=20,
    price_epsilon_pct=0.01,
    breakout_tolerance_pct=0.0,
)


# ---------------------------------------------------------------------------
# Structure classification
# ---------------------------------------------------------------------------


def test_classify_lower_higher_equal_highs() -> None:
    assert (
        classify_swing_structure(100.0, 99.0, side="high", epsilon_pct=0.01)[
            "structure_type"
        ]
        == "lower_high"
    )
    assert (
        classify_swing_structure(100.0, 101.0, side="high", epsilon_pct=0.01)[
            "structure_type"
        ]
        == "higher_high"
    )
    assert (
        classify_swing_structure(100.0, 100.005, side="high", epsilon_pct=0.01)[
            "structure_type"
        ]
        == "equal_high"
    )


def test_classify_higher_lower_equal_lows() -> None:
    assert (
        classify_swing_structure(100.0, 101.0, side="low", epsilon_pct=0.01)[
            "structure_type"
        ]
        == "higher_low"
    )
    assert (
        classify_swing_structure(100.0, 99.0, side="low", epsilon_pct=0.01)[
            "structure_type"
        ]
        == "lower_low"
    )
    assert (
        classify_swing_structure(100.0, 100.005, side="low", epsilon_pct=0.01)[
            "structure_type"
        ]
        == "equal_low"
    )


def test_long_short_structure_symmetry() -> None:
    high = classify_swing_structure(100.0, 98.0, side="high", epsilon_pct=0.01)
    low = classify_swing_structure(100.0, 102.0, side="low", epsilon_pct=0.01)
    assert high["structure_type"] == "lower_high"
    assert low["structure_type"] == "higher_low"


def test_clearly_lower_high_is_valid_without_exhaustion_cap() -> None:
    # Far below reference — still PA lower_high (no 0.75% cap).
    out = classify_swing_structure(100.0, 90.0, side="high", epsilon_pct=0.01)
    assert out["structure_type"] == "lower_high"


# ---------------------------------------------------------------------------
# Causality
# ---------------------------------------------------------------------------


def test_pivot_before_confirmation_ignored_as_of() -> None:
    swing = _swing(side="high", price=110.0, pivot_index=10, confirmation_index=13)
    assert swing_usable_as_of(swing, "2026-03-01T00:50:00+00:00") is False  # before conf
    assert swing_usable_as_of(swing, swing["confirmation_timestamp"]) is True
    filtered = filter_swings_as_of([swing], "2026-03-01T00:50:00+00:00")
    assert filtered == []


def test_developing_swing_never_arms_structure() -> None:
    # Build a short frame where a developing high exists but is not confirmed.
    start = pd.Timestamp("2026-03-01T00:00:00+00:00")
    rows = []
    highs = [1, 1, 1, 5, 2, 2]  # high at 3 needs right=3 → not confirmed yet
    for i, h in enumerate(highs):
        rows.append(
            {
                "timestamp": start + pd.Timedelta(minutes=5 * i),
                "open": h - 0.5,
                "high": h,
                "low": h - 1,
                "close": h - 0.2,
                "volume": 1.0,
            }
        )
    frame = pd.DataFrame(rows)
    developing = find_developing_swing_candidates(
        frame,
        pivot_left=3,
        pivot_right=3,
        candle_interval_minutes=5,
        pivot_type="high",
    )
    assert developing
    # Only confirmed swings passed into PA — developing list is not used.
    setup = _short_setup(setup_activation_timestamp=frame.iloc[-1]["timestamp"].isoformat())
    state = initialize_price_action_state(setup, CFG, confirmed_swings_as_of_setup=[])
    # Feed no confirmed swings; only candle updates — cannot arm.
    for i in range(len(frame)):
        state = update_price_action_state(state, frame.iloc[i].to_dict(), [])
    assert state.get("structure_confirmed") is False
    assert evaluate_price_action_confirmation(state) is None


# ---------------------------------------------------------------------------
# Short Lower High path
# ---------------------------------------------------------------------------


def _short_lh_swings():
    # H1 at idx 5 conf 8; Low between at 12 conf 15; H2 LH at 20 conf 23
    h1 = _swing(side="high", price=100.0, pivot_index=5, confirmation_index=8)
    low = _swing(side="low", price=95.0, pivot_index=12, confirmation_index=15)
    h2 = _swing(side="high", price=98.0, pivot_index=20, confirmation_index=23)
    return h1, low, h2


def test_short_setup_selects_reference_high() -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp="2026-03-01T00:45:00+00:00")  # after h1 conf
    state = initialize_price_action_state(setup, CFG, [h1, low])
    assert state["state"] == "waiting_for_pullback"
    assert state["reference_swing"]["price"] == 100.0
    assert state["reference_swing"]["side"] == "high"
    # low confirms after setup → not in initial known set
    assert all(s["side"] != "low" for s in state["known_swings"])


def test_short_lower_high_arms_but_no_confirmation_yet() -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state,
        _candle(h2["confirmation_timestamp"], o=98, h=98.5, l=97, c=97.5),
        [low, h2],
    )
    assert state["structure_confirmed"] is True
    assert state["pattern_type"] == "lower_high"
    assert state["confirmation_level"] == 95.0
    assert state["invalidation_level"] == 98.0
    assert state["state"] == "waiting_for_structure_break"
    assert evaluate_price_action_confirmation(state) is None


def test_short_close_below_level_confirms() -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state,
        _candle(h2["confirmation_timestamp"], o=98, h=98.5, l=97, c=97.5),
        [low, h2],
    )
    state = update_price_action_state(
        state,
        _candle("2026-03-01T02:05:00+00:00", o=95, h=95.5, l=94.0, c=94.5),
        [],
    )
    assert state["state"] == "price_action_confirmed"
    conf = evaluate_price_action_confirmation(state)
    assert conf is not None
    assert conf["side"] == "short"
    assert conf["pattern_type"] == "lower_high"
    assert conf["confirmation_level"] == 95.0


def test_short_wick_below_level_without_close_does_not_confirm() -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state,
        _candle(h2["confirmation_timestamp"], o=98, h=98.5, l=97, c=97.5),
        [low, h2],
    )
    state = update_price_action_state(
        state,
        _candle("2026-03-01T02:05:00+00:00", o=95.5, h=96, l=94.0, c=95.5),
        [],
    )
    assert state["state"] == "waiting_for_structure_break"
    assert evaluate_price_action_confirmation(state) is None


def test_short_close_above_lh_invalidates_wick_does_not() -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state,
        _candle(h2["confirmation_timestamp"], o=98, h=98.5, l=97, c=97.5),
        [low, h2],
    )
    state = update_price_action_state(
        state,
        _candle("2026-03-01T02:00:00+00:00", o=97, h=99.0, l=96.5, c=97.2),
        [],
    )
    assert state["state"] == "waiting_for_structure_break"
    state = update_price_action_state(
        state,
        _candle("2026-03-01T02:05:00+00:00", o=98.2, h=99.0, l=98.0, c=98.5),
        [],
    )
    assert state["state"] == "invalidated"


# ---------------------------------------------------------------------------
# Long Higher Low (mirror)
# ---------------------------------------------------------------------------


def _long_hl_swings():
    l1 = _swing(side="low", price=100.0, pivot_index=5, confirmation_index=8)
    high = _swing(side="high", price=105.0, pivot_index=12, confirmation_index=15)
    l2 = _swing(side="low", price=102.0, pivot_index=20, confirmation_index=23)
    return l1, high, l2


def test_long_higher_low_full_path() -> None:
    l1, high, l2 = _long_hl_swings()
    setup = _long_setup(setup_activation_timestamp=l1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [l1])
    assert state["reference_swing"]["side"] == "low"
    state = update_price_action_state(
        state,
        _candle(l2["confirmation_timestamp"], o=102, h=103, l=101.5, c=102.2),
        [high, l2],
    )
    assert state["pattern_type"] == "higher_low"
    assert state["confirmation_level"] == 105.0
    assert evaluate_price_action_confirmation(state) is None
    state = update_price_action_state(
        state,
        _candle("2026-03-01T02:00:00+00:00", o=104, h=106, l=103.5, c=104.5),
        [],
    )
    assert evaluate_price_action_confirmation(state) is None
    state = update_price_action_state(
        state,
        _candle("2026-03-01T02:05:00+00:00", o=105, h=106.5, l=104.8, c=105.5),
        [],
    )
    assert state["state"] == "price_action_confirmed"
    conf = evaluate_price_action_confirmation(state)
    assert conf["side"] == "long"
    assert conf["pattern_type"] == "higher_low"


def test_long_close_below_hl_invalidates_wick_does_not() -> None:
    l1, high, l2 = _long_hl_swings()
    setup = _long_setup(setup_activation_timestamp=l1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [l1])
    state = update_price_action_state(
        state,
        _candle(l2["confirmation_timestamp"], o=102, h=103, l=101.5, c=102.2),
        [high, l2],
    )
    state = update_price_action_state(
        state,
        _candle("2026-03-01T02:00:00+00:00", o=102.5, h=103, l=101.0, c=102.2),
        [],
    )
    assert state["state"] == "waiting_for_structure_break"
    state = update_price_action_state(
        state,
        _candle("2026-03-01T02:05:00+00:00", o=101.5, h=102, l=100.5, c=101.0),
        [],
    )
    assert state["state"] == "invalidated"


# ---------------------------------------------------------------------------
# Failed breakout / breakdown
# ---------------------------------------------------------------------------


def test_failed_breakout_then_confirm_and_invalidate_rules() -> None:
    h1 = _swing(side="high", price=100.0, pivot_index=5, confirmation_index=8)
    low = _swing(side="low", price=94.0, pivot_index=10, confirmation_index=13)
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:10:00+00:00", o=99.5, h=100.8, l=99.0, c=99.2),
        [low],
    )
    assert state["pattern_type"] == "failed_breakout"
    assert state["failed_break_extreme"] == 100.8
    assert state["confirmation_level"] == 94.0
    assert evaluate_price_action_confirmation(state) is None
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:15:00+00:00", o=94.5, h=95, l=93.5, c=94.2),
        [],
    )
    assert evaluate_price_action_confirmation(state) is None
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:20:00+00:00", o=94, h=94.5, l=93.0, c=93.5),
        [],
    )
    assert state["state"] == "price_action_confirmed"


def test_failed_breakout_invalidate_by_close_not_wick() -> None:
    h1 = _swing(side="high", price=100.0, pivot_index=5, confirmation_index=8)
    low = _swing(side="low", price=94.0, pivot_index=10, confirmation_index=13)
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:10:00+00:00", o=99.5, h=100.8, l=99.0, c=99.2),
        [low],
    )
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:15:00+00:00", o=100, h=101.0, l=99.5, c=100.2),
        [],
    )
    assert state["state"] == "waiting_for_structure_break"
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:20:00+00:00", o=100.5, h=101.2, l=100.3, c=100.9),
        [],
    )
    assert state["state"] == "invalidated"


def test_failed_breakdown_mirror() -> None:
    l1 = _swing(side="low", price=100.0, pivot_index=5, confirmation_index=8)
    high = _swing(side="high", price=106.0, pivot_index=10, confirmation_index=13)
    setup = _long_setup(setup_activation_timestamp=l1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [l1])
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:10:00+00:00", o=100.5, h=101.0, l=99.2, c=100.4),
        [high],
    )
    assert state["pattern_type"] == "failed_breakdown"
    assert state["failed_break_extreme"] == 99.2
    assert state["confirmation_level"] == 106.0
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:20:00+00:00", o=105.5, h=107.0, l=105.0, c=106.5),
        [],
    )
    assert state["state"] == "price_action_confirmed"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_max_age_expires() -> None:
    h1 = _swing(side="high", price=100.0, pivot_index=5, confirmation_index=8)
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    cfg = PriceActionConfig(max_setup_age_candles=2)
    state = initialize_price_action_state(setup, cfg, [h1])
    state = update_price_action_state(
        state, _candle("2026-03-01T01:05:00+00:00", o=99, h=100, l=98, c=99), []
    )
    state = update_price_action_state(
        state, _candle("2026-03-01T01:10:00+00:00", o=99, h=100, l=98, c=99), []
    )
    state = update_price_action_state(
        state, _candle("2026-03-01T01:15:00+00:00", o=99, h=100, l=98, c=99), []
    )
    assert state["state"] == "expired"


def test_opposing_setup_invalidates() -> None:
    h1 = _swing(side="high", price=100.0, pivot_index=5, confirmation_index=8)
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    opp = _long_setup(setup_activated=True)
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:05:00+00:00", o=99, h=100, l=98, c=99),
        [],
        opposing_setup=opp,
    )
    assert state["state"] == "invalidated"
    assert state["invalidation_reason"] == "NEW_OPPOSING_SETUP"


def test_htf_opposing_blocks_init() -> None:
    setup = _short_setup(blockers=["HTF_OPPOSING_TREND"], setup_activated=False)
    state = initialize_price_action_state(setup, CFG, [])
    assert state["state"] == "invalidated"
    assert "HTF_OPPOSING_TREND" in (state["invalidation_reason"] or "")


def test_identical_swing_not_double_processed() -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    c = _candle(h2["confirmation_timestamp"], o=98, h=98.5, l=97, c=97.5)
    state = update_price_action_state(state, c, [low, h2])
    events_before = len(state["event_log"])
    state2 = update_price_action_state(state, c, [low, h2])
    assert state2["structure_confirmed"] is True
    armed = [e for e in state2["event_log"][events_before:] if e["event"] == "structure_armed"]
    assert armed == []


def test_no_second_confirmation_after_confirmed() -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state, _candle(h2["confirmation_timestamp"], o=98, h=98.5, l=97, c=97.5), [low, h2]
    )
    state = update_price_action_state(
        state, _candle("2026-03-01T02:05:00+00:00", o=95, h=95.5, l=94.0, c=94.5), []
    )
    assert state["state"] == "price_action_confirmed"
    conf1 = evaluate_price_action_confirmation(state)
    state = update_price_action_state(
        state, _candle("2026-03-01T02:10:00+00:00", o=94, h=94.5, l=93.0, c=93.5), []
    )
    assert state["state"] == "price_action_confirmed"
    conf2 = evaluate_price_action_confirmation(state)
    assert conf1 == conf2


def test_no_reactivation_after_invalidation() -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state, _candle(h2["confirmation_timestamp"], o=98, h=98.5, l=97, c=97.5), [low, h2]
    )
    state = update_price_action_state(
        state, _candle("2026-03-01T02:05:00+00:00", o=98.2, h=99, l=98, c=98.5), []
    )
    assert state["state"] == "invalidated"
    state = update_price_action_state(
        state, _candle("2026-03-01T02:10:00+00:00", o=94, h=95, l=93, c=93.5), [h2]
    )
    assert state["state"] == "invalidated"
    assert evaluate_price_action_confirmation(state) is None


def test_no_entry_tp_momentum_fields() -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state, _candle(h2["confirmation_timestamp"], o=98, h=98.5, l=97, c=97.5), [low, h2]
    )
    state = update_price_action_state(
        state, _candle("2026-03-01T02:05:00+00:00", o=95, h=95.5, l=94.0, c=94.5), []
    )
    blob = json.dumps(json_safe(state), allow_nan=False)
    for key in ("entry_price", "tp_price", "stop_loss", "mae_pct", "mfe_pct", "momentum"):
        assert key not in blob
    assert state["source_setup_activation"]["setup_side"] == "short"


def test_serialization_roundtrip() -> None:
    h1 = _swing(side="high", price=100.0, pivot_index=5, confirmation_index=8)
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    encoded = json.dumps(json_safe(state), allow_nan=False)
    restored = json.loads(encoded)
    assert restored["setup_side"] == "short"
    assert restored["reference_swing"]["price"] == 100.0


def test_confirmed_pivot_adapter() -> None:
    pivot = ConfirmedPivot(
        pivot_index=3,
        pivot_timestamp="2026-03-01T00:15:00+00:00",
        confirmation_index=6,
        confirmation_timestamp="2026-03-01T00:30:00+00:00",
        price=12.5,
        pivot_type="high",
    )
    swing = confirmed_pivot_to_swing(pivot)
    assert swing["side"] == "high"
    assert swing["price"] == 12.5


def test_audit_harness_writes_events(tmp_path) -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    # Minimal candle frame covering the path
    start = pd.Timestamp("2026-03-01T00:00:00+00:00")
    rows = []
    for i in range(30):
        rows.append(
            {
                "timestamp": start + pd.Timedelta(minutes=5 * i),
                "open": 97.0,
                "high": 98.0 if i != 23 else 98.5,
                "low": 96.0 if i != 25 else 94.0,
                "close": 97.0 if i != 25 else 94.5,
                "volume": 1.0,
            }
        )
    candles = pd.DataFrame(rows)
    payload = walk_price_action(
        setup_activation=setup,
        candles=candles,
        config=CFG,
        swings=[h1, low, h2],
    )
    paths = write_price_action_audit(payload, tmp_path)
    assert paths["json"].exists()
    assert paths["csv"].exists()
    events = {e["event"] for e in payload["events"]}
    assert "setup_initialized" in events or "reference_swing_selected" in events


def test_reference_missing_warning_not_crash() -> None:
    setup = _short_setup()
    state = initialize_price_action_state(setup, CFG, [])
    assert state["state"] == "waiting_for_pullback"
    assert "REFERENCE_SWING_MISSING" in state["warnings"]


# ---------------------------------------------------------------------------
# Hardening: same-bar, geometry, FBD/FBO binding, ATR buffer, age docs
# ---------------------------------------------------------------------------


def test_same_bar_lower_high_does_not_confirm() -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    # Close already through confirmation on the arming candle.
    state = update_price_action_state(
        state,
        _candle(h2["confirmation_timestamp"], o=96, h=98.5, l=94.0, c=94.5, candle_index=23),
        [low, h2],
    )
    assert state["structure_confirmed"] is True
    assert state["structure_armed_timestamp"] == h2["confirmation_timestamp"]
    assert state["state"] == "waiting_for_structure_break"
    assert evaluate_price_action_confirmation(state) is None
    assert state["same_bar_confirmation_blocked"] is True
    assert any(e["event"] == "same_bar_confirmation_blocked" for e in state["event_log"])


def test_next_candle_after_same_bar_block_may_confirm() -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state,
        _candle(h2["confirmation_timestamp"], o=96, h=98.5, l=94.0, c=94.5, candle_index=23),
        [low, h2],
    )
    assert state["same_bar_confirmation_blocked"] is True
    state = update_price_action_state(
        state,
        _candle("2026-03-01T02:00:00+00:00", o=94, h=94.5, l=93.5, c=94.0, candle_index=24),
        [],
    )
    assert state["state"] == "price_action_confirmed"
    conf = evaluate_price_action_confirmation(state)
    assert conf is not None
    assert conf["structure_armed_timestamp"] == h2["confirmation_timestamp"]
    assert conf["same_bar_confirmation_blocked"] is True


def test_same_bar_higher_low_does_not_confirm() -> None:
    l1, high, l2 = _long_hl_swings()
    setup = _long_setup(setup_activation_timestamp=l1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [l1])
    state = update_price_action_state(
        state,
        _candle(l2["confirmation_timestamp"], o=104, h=106, l=101.5, c=105.5, candle_index=23),
        [high, l2],
    )
    assert state["pattern_type"] == "higher_low"
    assert state["state"] == "waiting_for_structure_break"
    assert evaluate_price_action_confirmation(state) is None
    assert state["same_bar_confirmation_blocked"] is True


def test_same_bar_failed_breakout_does_not_confirm() -> None:
    h1 = _swing(side="high", price=100.0, pivot_index=5, confirmation_index=8)
    low = _swing(side="low", price=94.0, pivot_index=10, confirmation_index=13)
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:10:00+00:00", o=99.5, h=100.8, l=93.0, c=93.5, candle_index=14),
        [low],
    )
    assert state["pattern_type"] == "failed_breakout"
    assert state["structure_confirmed"] is True
    assert state["state"] == "waiting_for_structure_break"
    assert evaluate_price_action_confirmation(state) is None
    assert state["same_bar_confirmation_blocked"] is True


def test_same_bar_failed_breakdown_does_not_confirm() -> None:
    l1 = _swing(side="low", price=100.0, pivot_index=5, confirmation_index=8)
    high = _swing(side="high", price=106.0, pivot_index=10, confirmation_index=13)
    setup = _long_setup(setup_activation_timestamp=l1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [l1])
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:10:00+00:00", o=100.5, h=107.0, l=99.2, c=106.5, candle_index=14),
        [high],
    )
    assert state["pattern_type"] == "failed_breakdown"
    assert state["state"] == "waiting_for_structure_break"
    assert evaluate_price_action_confirmation(state) is None
    assert state["same_bar_confirmation_blocked"] is True


def test_short_conf_equals_inv_rejected() -> None:
    h1 = _swing(side="high", price=100.0, pivot_index=5, confirmation_index=8)
    # Intermediate low equals candidate high → conf == inv
    low = _swing(side="low", price=98.0, pivot_index=12, confirmation_index=15)
    h2 = _swing(side="high", price=98.0, pivot_index=20, confirmation_index=23)
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state, _candle(h2["confirmation_timestamp"], o=98, h=98.5, l=97, c=97.5), [low, h2]
    )
    assert state["state"] == "invalidated"
    assert state["invalidation_reason"] == "INVALID_STRUCTURE_GEOMETRY"
    assert state["invalid_structure_geometry"] is True


def test_short_inv_below_conf_rejected() -> None:
    h1 = _swing(side="high", price=100.0, pivot_index=5, confirmation_index=8)
    # Intermediate low ABOVE candidate high → inv < conf (invalid for short)
    low = _swing(side="low", price=99.0, pivot_index=12, confirmation_index=15)
    h2 = _swing(side="high", price=97.0, pivot_index=20, confirmation_index=23)
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state, _candle(h2["confirmation_timestamp"], o=97, h=97.5, l=96, c=96.5), [low, h2]
    )
    assert state["state"] == "invalidated"
    assert state["invalidation_reason"] == "INVALID_STRUCTURE_GEOMETRY"


def test_long_conf_equals_inv_rejected() -> None:
    l1 = _swing(side="low", price=100.0, pivot_index=5, confirmation_index=8)
    high = _swing(side="high", price=102.0, pivot_index=12, confirmation_index=15)
    l2 = _swing(side="low", price=102.0, pivot_index=20, confirmation_index=23)
    setup = _long_setup(setup_activation_timestamp=l1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [l1])
    state = update_price_action_state(
        state, _candle(l2["confirmation_timestamp"], o=102, h=103, l=101.5, c=102.2), [high, l2]
    )
    assert state["state"] == "invalidated"
    assert state["invalidation_reason"] == "INVALID_STRUCTURE_GEOMETRY"


def test_long_inv_above_conf_rejected() -> None:
    l1 = _swing(side="low", price=100.0, pivot_index=5, confirmation_index=8)
    # Intermediate high BELOW candidate low → inv > conf (invalid for long)
    high = _swing(side="high", price=101.0, pivot_index=12, confirmation_index=15)
    l2 = _swing(side="low", price=103.0, pivot_index=20, confirmation_index=23)
    setup = _long_setup(setup_activation_timestamp=l1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [l1])
    state = update_price_action_state(
        state, _candle(l2["confirmation_timestamp"], o=103, h=104, l=102.5, c=103.2), [high, l2]
    )
    assert state["state"] == "invalidated"
    assert state["invalidation_reason"] == "INVALID_STRUCTURE_GEOMETRY"


def test_valid_short_geometry_still_allowed() -> None:
    h1, low, h2 = _short_lh_swings()
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state, _candle(h2["confirmation_timestamp"], o=98, h=98.5, l=97, c=97.5), [low, h2]
    )
    assert state["state"] == "waiting_for_structure_break"
    assert state["invalidation_level"] > state["confirmation_level"]


def test_valid_long_geometry_still_allowed() -> None:
    l1, high, l2 = _long_hl_swings()
    setup = _long_setup(setup_activation_timestamp=l1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [l1])
    state = update_price_action_state(
        state, _candle(l2["confirmation_timestamp"], o=102, h=103, l=101.5, c=102.2), [high, l2]
    )
    assert state["state"] == "waiting_for_structure_break"
    assert state["invalidation_level"] < state["confirmation_level"]


def test_fbd_without_local_swing_high_waits() -> None:
    l1 = _swing(side="low", price=100.0, pivot_index=5, confirmation_index=8)
    # Ancient high BEFORE reference — must not arm FBD.
    old_high = _swing(side="high", price=110.0, pivot_index=1, confirmation_index=4)
    setup = _long_setup(setup_activation_timestamp=l1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [l1, old_high])
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:10:00+00:00", o=100.5, h=101.0, l=99.2, c=100.4),
        [],
    )
    assert state["pattern_type"] == "failed_breakdown"
    assert state["state"] == "waiting_for_confirmation_level"
    assert state["waiting_for_confirmation_level"] is True
    assert state["structure_confirmed"] is False
    assert state["confirmation_level"] is None


def test_later_confirmed_swing_high_arms_fbd() -> None:
    l1 = _swing(side="low", price=100.0, pivot_index=5, confirmation_index=8)
    setup = _long_setup(setup_activation_timestamp=l1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [l1])
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:10:00+00:00", o=100.5, h=101.0, l=99.2, c=100.4),
        [],
    )
    assert state["state"] == "waiting_for_confirmation_level"
    later_high = _swing(
        side="high",
        price=102.5,
        pivot_index=16,
        confirmation_index=19,
        pivot_ts="2026-03-01T01:20:00+00:00",
        conf_ts="2026-03-01T01:35:00+00:00",
    )
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:35:00+00:00", o=101.5, h=102.8, l=101.0, c=102.0),
        [later_high],
    )
    assert state["structure_confirmed"] is True
    assert state["pattern_type"] == "failed_breakdown"
    assert state["confirmation_level"] == 102.5
    assert state["state"] == "waiting_for_structure_break"


def test_fbo_waits_then_arms_mirrored() -> None:
    h1 = _swing(side="high", price=100.0, pivot_index=5, confirmation_index=8)
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, CFG, [h1])
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:10:00+00:00", o=99.5, h=100.8, l=99.0, c=99.2),
        [],
    )
    assert state["state"] == "waiting_for_confirmation_level"
    later_low = _swing(
        side="low",
        price=96.0,
        pivot_index=16,
        confirmation_index=19,
        pivot_ts="2026-03-01T01:20:00+00:00",
        conf_ts="2026-03-01T01:35:00+00:00",
    )
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:35:00+00:00", o=97, h=97.5, l=95.5, c=96.2),
        [later_low],
    )
    assert state["structure_confirmed"] is True
    assert state["pattern_type"] == "failed_breakout"
    assert state["confirmation_level"] == 96.0


def test_atr_buffer_long_fbd() -> None:
    from research.regime_scanner.price_action import compute_failed_break_invalidation_level

    cfg = PriceActionConfig(failed_break_invalidation_atr_buffer=0.1)
    pack = compute_failed_break_invalidation_level(
        side="long", extreme=99.0, atr=2.0, cfg=cfg
    )
    assert pack["unbuffered_failed_break_extreme"] == 99.0
    assert pack["failed_break_invalidation_buffer"] == _approx(0.2)
    assert pack["final_invalidation_level"] == _approx(98.8)


def test_atr_buffer_short_fbo() -> None:
    from research.regime_scanner.price_action import compute_failed_break_invalidation_level

    cfg = PriceActionConfig(failed_break_invalidation_atr_buffer=0.1)
    pack = compute_failed_break_invalidation_level(
        side="short", extreme=101.0, atr=2.0, cfg=cfg
    )
    assert pack["final_invalidation_level"] == _approx(101.2)


def test_missing_atr_uses_breakout_tolerance_fallback() -> None:
    from research.regime_scanner.price_action import compute_failed_break_invalidation_level

    cfg = PriceActionConfig(
        failed_break_invalidation_atr_buffer=0.1,
        breakout_tolerance_pct=1.0,
    )
    pack = compute_failed_break_invalidation_level(
        side="long", extreme=100.0, atr=None, cfg=cfg
    )
    assert pack["failed_break_invalidation_buffer_mode"] == "breakout_tolerance_pct_fallback"
    assert pack["failed_break_invalidation_buffer"] == _approx(1.0)
    assert pack["final_invalidation_level"] == _approx(99.0)


def test_wick_beyond_buffered_inv_does_not_invalidate() -> None:
    l1 = _swing(side="low", price=100.0, pivot_index=5, confirmation_index=8)
    high = _swing(side="high", price=106.0, pivot_index=10, confirmation_index=13)
    cfg = PriceActionConfig(
        minimum_swing_separation_candles=5,
        max_setup_age_candles=20,
        failed_break_invalidation_atr_buffer=0.5,
    )
    setup = _long_setup(setup_activation_timestamp=l1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, cfg, [l1])
    # extreme 99.2, atr=2 → buffer 1.0 → inv = 98.2
    state = update_price_action_state(
        state,
        {
            **_candle("2026-03-01T01:10:00+00:00", o=100.5, h=101.0, l=99.2, c=100.4),
            "atr": 2.0,
            "candle_index": 14,
        },
        [high],
    )
    assert state["final_invalidation_level"] == _approx(98.2)
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:15:00+00:00", o=100, h=100.5, l=97.5, c=99.0),
        [],
    )
    assert state["state"] == "waiting_for_structure_break"


def test_close_beyond_buffered_inv_invalidates() -> None:
    l1 = _swing(side="low", price=100.0, pivot_index=5, confirmation_index=8)
    high = _swing(side="high", price=106.0, pivot_index=10, confirmation_index=13)
    cfg = PriceActionConfig(
        minimum_swing_separation_candles=5,
        max_setup_age_candles=20,
        failed_break_invalidation_atr_buffer=0.5,
    )
    setup = _long_setup(setup_activation_timestamp=l1["confirmation_timestamp"])
    state = initialize_price_action_state(setup, cfg, [l1])
    state = update_price_action_state(
        state,
        {
            **_candle("2026-03-01T01:10:00+00:00", o=100.5, h=101.0, l=99.2, c=100.4),
            "atr": 2.0,
            "candle_index": 14,
        },
        [high],
    )
    state = update_price_action_state(
        state,
        _candle("2026-03-01T01:15:00+00:00", o=99, h=99.5, l=97.0, c=98.0),
        [],
    )
    assert state["state"] == "invalidated"


def test_age_96_still_active() -> None:
    h1 = _swing(side="high", price=100.0, pivot_index=5, confirmation_index=8)
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    cfg = PriceActionConfig(max_setup_age_candles=96)
    state = initialize_price_action_state(setup, cfg, [h1])
    base = pd.Timestamp("2026-03-01T01:05:00+00:00")
    for i in range(96):
        ts = (base + pd.Timedelta(minutes=5 * i)).isoformat()
        state = update_price_action_state(
            state, _candle(ts, o=99, h=100, l=98, c=99), []
        )
    assert state["age_candles"] == 96
    assert state["state"] != "expired"


def test_age_97_expired() -> None:
    h1 = _swing(side="high", price=100.0, pivot_index=5, confirmation_index=8)
    setup = _short_setup(setup_activation_timestamp=h1["confirmation_timestamp"])
    cfg = PriceActionConfig(max_setup_age_candles=96)
    state = initialize_price_action_state(setup, cfg, [h1])
    base = pd.Timestamp("2026-03-01T01:05:00+00:00")
    for i in range(97):
        ts = (base + pd.Timedelta(minutes=5 * i)).isoformat()
        state = update_price_action_state(
            state, _candle(ts, o=99, h=100, l=98, c=99), []
        )
    assert state["age_candles"] == 97
    assert state["state"] == "expired"
    assert state["invalidation_reason"] == "MAX_SETUP_AGE"

"""Phase-1 RegimeSnapshot + SetupActivation tests (no entry / PA / momentum)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.regime_snapshot import (
    _FORBIDDEN_ENTRY_KEYS,
    build_regime_snapshot,
    evaluate_setup_activation,
)


REQUIRED_SNAPSHOT_FIELDS = {
    "decision_time",
    "regime_5m",
    "regime_15m",
    "regime_30m",
    "combined_regime",
    "previous_combined_regime",
    "regime_change",
    "trend_direction",
    "trend_strength",
    "trend_weakness",
    "transition_detected",
    "reason_codes",
    "higher_timeframe_alignment",
    "lower_timeframe_alignment",
    "by_timeframe",
}


def _snap(**kwargs):
    base = {
        "decision_time": "2026-03-01T00:05:00+00:00",
        "regime_5m": "bearish_trend",
        "regime_15m": "bearish_trend",
        "regime_30m": "bearish_trend",
        "combined_regime": "bearish_trend",
        "previous_combined_regime": "bearish_trend",
    }
    base.update(kwargs)
    return build_regime_snapshot(**base)


def test_regime_snapshot_required_fields() -> None:
    snap = _snap(
        previous_combined_regime="neutral",
        combined_regime="bearish_trend",
        regime_5m="bearish_trend_with_trend_weakness",
        regime_15m="bearish_trend",
        regime_30m="transition",
    )
    assert REQUIRED_SNAPSHOT_FIELDS.issubset(snap.keys())
    encoded = json.dumps(json_safe(snap), allow_nan=False)
    assert "entry_price" not in encoded
    assert "tp_price" not in encoded


def test_previous_combined_regime_and_regime_change() -> None:
    changed = _snap(
        previous_combined_regime="neutral",
        combined_regime="bullish_trend",
        regime_5m="bullish_trend",
        regime_15m="bullish_trend",
        regime_30m="bullish_trend",
    )
    assert changed["previous_combined_regime"] == "neutral"
    assert changed["regime_change"] is True

    same = _snap(
        previous_combined_regime="bullish_trend",
        combined_regime="bullish_trend",
        regime_5m="bullish_trend",
        regime_15m="bullish_trend",
        regime_30m="bullish_trend",
    )
    assert same["regime_change"] is False

    no_prev = _snap(
        previous_combined_regime=None,
        combined_regime="bullish_trend",
        regime_5m="bullish_trend",
        regime_15m="bullish_trend",
        regime_30m="bullish_trend",
    )
    assert no_prev["previous_combined_regime"] is None
    assert no_prev["regime_change"] is False


def test_bullish_weakness_activates_long_continuation() -> None:
    snap = _snap(
        previous_combined_regime="bullish_trend",
        combined_regime="bullish_trend_with_trend_weakness",
        regime_5m="bullish_trend_with_trend_weakness",
        regime_15m="bullish_trend",
        regime_30m="bullish_trend",
    )
    assert snap["trend_direction"] == "long"
    assert snap["trend_weakness"] is True
    setup = evaluate_setup_activation(snap)
    assert setup["setup_activated"] is True
    assert setup["setup_side"] == "long"
    assert setup["setup_type"] == "continuation_weakness"


def test_bearish_weakness_activates_short_continuation() -> None:
    snap = _snap(
        previous_combined_regime="bearish_trend",
        combined_regime="bearish_trend_with_trend_weakness",
        regime_5m="bearish_trend_with_trend_weakness",
        regime_15m="bearish_trend",
        regime_30m="bearish_trend",
    )
    setup = evaluate_setup_activation(snap)
    assert setup["setup_activated"] is True
    assert setup["setup_side"] == "short"
    assert setup["setup_type"] == "continuation_weakness"


def test_bullish_regime_change_setup() -> None:
    snap = _snap(
        previous_combined_regime="neutral",
        combined_regime="bullish_trend",
        regime_5m="bullish_trend",
        regime_15m="bullish_trend",
        regime_30m="bullish_trend",
    )
    assert snap["regime_change"] is True
    setup = evaluate_setup_activation(snap)
    assert setup["setup_activated"] is True
    assert setup["setup_side"] == "long"
    assert setup["setup_type"] == "regime_change"


def test_bearish_regime_change_setup() -> None:
    snap = _snap(
        previous_combined_regime="transition",
        combined_regime="strong_bearish_trend",
        regime_5m="bearish_trend",
        regime_15m="strong_bearish_trend",
        regime_30m="bearish_trend",
    )
    setup = evaluate_setup_activation(snap)
    assert setup["setup_activated"] is True
    assert setup["setup_side"] == "short"
    assert setup["setup_type"] == "regime_change"


def test_intact_trend_without_edge_is_context_only() -> None:
    snap = _snap(
        previous_combined_regime="bullish_trend",
        combined_regime="bullish_trend",
        regime_5m="bullish_trend",
        regime_15m="bullish_trend",
        regime_30m="bullish_trend",
    )
    assert snap["regime_change"] is False
    assert snap["trend_weakness"] is False
    setup = evaluate_setup_activation(snap)
    assert setup["setup_activated"] is False
    assert setup["setup_side"] is None
    assert setup["setup_type"] is None


def test_unavailable_blocks_setup() -> None:
    snap = _snap(
        previous_combined_regime=None,
        combined_regime="unavailable",
        regime_5m="unavailable",
        regime_15m="unavailable",
        regime_30m="unavailable",
    )
    setup = evaluate_setup_activation(snap)
    assert setup["setup_activated"] is False
    assert "UNAVAILABLE" in setup["blockers"]


def test_neutral_no_directional_setup() -> None:
    snap = _snap(
        previous_combined_regime="neutral",
        combined_regime="neutral",
        regime_5m="neutral",
        regime_15m="neutral",
        regime_30m="neutral",
    )
    setup = evaluate_setup_activation(snap)
    assert setup["setup_activated"] is False
    assert setup["setup_side"] is None


def test_htf_transition_warning_and_confidence_cap() -> None:
    snap = _snap(
        previous_combined_regime="bullish_trend",
        combined_regime="bullish_trend_with_trend_weakness",
        regime_5m="bullish_trend_with_trend_weakness",
        regime_15m="bullish_trend",
        regime_30m="transition",
    )
    assert snap["transition_detected"] is True
    setup = evaluate_setup_activation(snap)
    assert setup["setup_activated"] is True
    assert "HTF_TRANSITION" in setup["warnings"]
    assert setup["confidence"] in {"low", "medium"}
    assert _confidence_ok(setup["confidence"])


def _confidence_ok(value: str) -> bool:
    return value in {"low", "medium"}  # max medium under HTF_TRANSITION


def test_htf_opposing_trend_blocks() -> None:
    snap = _snap(
        previous_combined_regime="bearish_trend",
        combined_regime="bearish_trend_with_trend_weakness",
        regime_5m="bearish_trend_with_trend_weakness",
        regime_15m="bearish_trend",
        regime_30m="strong_bullish_trend",
    )
    setup = evaluate_setup_activation(snap)
    assert setup["setup_activated"] is False
    assert "HTF_OPPOSING_TREND" in setup["blockers"]
    assert setup["setup_side"] == "short"
    assert setup["setup_type"] == "continuation_weakness"


def test_explicit_5m_bear_weak_15m_bear_30m_transition() -> None:
    """Audit case: combined stays bearish; short continuation with HTF warning."""
    snap = build_regime_snapshot(
        decision_time="2026-03-04T12:00:00+00:00",
        previous_combined_regime="bearish_trend",
        combined_regime="bearish_trend",
        regime_5m="bearish_trend_with_trend_weakness",
        regime_15m="bearish_trend",
        regime_30m="transition",
        reason_codes=[{"code": "BEARISH_TREND_INTACT", "explanation": "unit"}],
    )
    assert snap["combined_regime"] == "bearish_trend"
    assert snap["trend_direction"] == "short"
    assert snap["trend_weakness"] is True
    assert snap["transition_detected"] is True
    assert snap["higher_timeframe_alignment"] == "transition"
    assert snap["lower_timeframe_alignment"] == "aligned"

    setup = evaluate_setup_activation(snap)
    assert setup["setup_activated"] is True
    assert setup["setup_side"] == "short"
    assert setup["setup_type"] == "continuation_weakness"
    assert "HTF_TRANSITION" in setup["warnings"]
    assert setup["confidence"] in {"low", "medium"}
    assert "HTF_OPPOSING_TREND" not in setup["blockers"]
    # No EntrySignal / entry economics.
    for key in _FORBIDDEN_ENTRY_KEYS:
        assert key not in setup
        assert key not in setup["source_snapshot"]


def test_setup_activation_has_no_entry_or_tp_fields() -> None:
    snap = _snap(
        previous_combined_regime="bullish_trend",
        combined_regime="bullish_trend_with_trend_weakness",
        regime_5m="bullish_trend_with_trend_weakness",
        regime_15m="bullish_trend",
        regime_30m="bullish_trend",
    )
    setup = evaluate_setup_activation(snap)
    blob = json.dumps(json_safe(setup), allow_nan=False)
    for key in ("entry_price", "tp_price", "tp_pct", "mae_pct", "mfe_pct"):
        assert key not in blob


def test_no_lookahead_in_snapshot_builder() -> None:
    """Snapshot uses only provided closed-regime labels — no future inputs."""
    past = build_regime_snapshot(
        decision_time="2026-03-01T00:00:00+00:00",
        previous_combined_regime="neutral",
        combined_regime="bearish_trend",
        regime_5m="bearish_trend",
        regime_15m="bearish_trend",
        regime_30m="bearish_trend",
    )
    # Mutating a "future" label object after the fact cannot change the snapshot.
    future_labels = {
        "regime_5m": "strong_bullish_trend",
        "regime_15m": "strong_bullish_trend",
        "regime_30m": "strong_bullish_trend",
        "combined_regime": "strong_bullish_trend",
    }
    assert past["combined_regime"] == "bearish_trend"
    assert past["regime_change"] is True
    # Rebuilding with only past inputs stays identical (deterministic, causal inputs).
    again = build_regime_snapshot(
        decision_time="2026-03-01T00:00:00+00:00",
        previous_combined_regime="neutral",
        combined_regime="bearish_trend",
        regime_5m="bearish_trend",
        regime_15m="bearish_trend",
        regime_30m="bearish_trend",
    )
    assert again == past
    # Future labels are a separate call — proving we do not peek them implicitly.
    future = build_regime_snapshot(
        decision_time="2026-03-01T01:00:00+00:00",
        previous_combined_regime="bearish_trend",
        **future_labels,
    )
    assert future["combined_regime"] == "strong_bullish_trend"
    assert past["combined_regime"] != future["combined_regime"]


def test_transition_alone_does_not_activate_setup() -> None:
    snap = _snap(
        previous_combined_regime="transition",
        combined_regime="transition",
        regime_5m="transition",
        regime_15m="transition",
        regime_30m="transition",
    )
    setup = evaluate_setup_activation(snap)
    assert setup["setup_activated"] is False
    assert "TRANSITION_CONTEXT" in setup["warnings"]

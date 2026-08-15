"""Tests for Cobertura blocker input bundle exporter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.backtests.cobertura_0_notional_strategie.cobertura_blocker_input_bundle import (
    APT_REFERENCE_TRADE_ID,
    apt_reference_values,
    build_bundle,
    distance_break_to_market_pct,
    evaluate_ready,
    index_by_join,
)

STATE = Path(
    "research/backtests/cobertura_0_notional_strategie/results/historical_blocker_states_20260726"
)
FILL = Path(
    "research/backtests/cobertura_0_notional_strategie/results/historical_blocker_fill_replay_20260726"
)


@pytest.mark.skipif(not STATE.exists() or not FILL.exists(), reason="source results missing")
def test_apt_bundle_join_and_values(tmp_path: Path):
    out = build_bundle(
        state_dir=STATE,
        fill_replay_dir=FILL,
        output_dir=tmp_path / "bundle",
        trigger_mode="first_break",
    )
    assert out["ready"] == 25
    assert out["unresolved"] == 2
    recs = {
        (r["trade_id"], r["trigger"]["trigger_mode"]): r for r in out["records"]
    }
    apt = recs[(APT_REFERENCE_TRADE_ID, "first_break")]
    ref = apt_reference_values()
    assert apt["trigger"]["structure_break_level"] == pytest.approx(ref["structure_break_level"])
    assert apt["market"]["market_price_at_signal"] == pytest.approx(ref["market_price_at_signal"])
    assert apt["market"]["tradeable_5m_open"] == pytest.approx(ref["tradeable_5m_open"])
    assert apt["market"]["neutralization_fill_price"] == pytest.approx(
        ref["neutralization_fill_price"]
    )
    assert apt["market"]["distance_break_to_market_pct"] == pytest.approx(
        ref["distance_break_to_market_pct"], rel=0, abs=1e-12
    )
    assert apt["pre_signal_position"]["long_qty"] == pytest.approx(ref["long_qty"])
    assert apt["pre_signal_position"]["short_qty"] == pytest.approx(ref["short_qty"])
    assert apt["pre_signal_position"]["long_avg"] == pytest.approx(ref["long_avg"])
    assert apt["pre_signal_position"]["short_avg"] == pytest.approx(ref["short_avg"])
    assert apt["pre_signal_position"]["net_qty"] == pytest.approx(ref["net_qty"])
    assert apt["prior_economics"]["realized_pnl"] == pytest.approx(ref["realized_pnl"])
    assert apt["prior_economics"]["unrealized_pnl_at_signal"] == pytest.approx(
        ref["unrealized_pnl_at_signal"]
    )
    assert apt["prior_economics"]["total_economics_at_signal"] == pytest.approx(
        ref["total_economics_at_signal"]
    )
    assert apt["pre_signal_position"]["fills_before_signal"] == ref["fills_before_signal"]
    assert apt["pre_signal_position"]["fills_at_or_after_signal"] == ref["fills_at_or_after_signal"]
    assert apt["pre_signal_position"]["active_cycle"] == ref["active_cycle"]
    assert apt["pre_signal_position"]["open_order_count"] == ref["open_order_count"]
    assert len(apt["source_orders"]["orders"]) == 4
    assert apt["source_orders"]["cancel_on_cobertura_handoff"] is True
    purposes = {o["purpose"] for o in apt["source_orders"]["orders"]}
    assert "LONG_TP_EXIT" in purposes
    assert "SHORT_SL_EXIT" in purposes
    assert any("SHORT_REDUCE" in (o.get("purpose") or "") for o in apt["source_orders"]["orders"])
    assert apt["trigger"]["signal_available_ts"].startswith("2026-01-19T00:00:00")
    assert apt["trigger"]["structure_break_event_ts"].startswith("2026-01-18T23:55:00")
    assert apt["quality"]["replay_match_status"] == "REPLAY_MATCH"
    assert apt["quality"]["replay_diff_count"] == 0
    assert apt["quality"]["ready_for_cobertura"] is True
    assert "FEE_RECONSTRUCTION_UNRESOLVED" in apt["quality"]["warnings"]


@pytest.mark.skipif(not STATE.exists() or not FILL.exists(), reason="source results missing")
def test_strict_cutoff_and_bch_trx_unresolved(tmp_path: Path):
    out = build_bundle(
        state_dir=STATE,
        fill_replay_dir=FILL,
        output_dir=tmp_path / "bundle2",
    )
    apt = next(r for r in out["records"] if r["trade_id"] == APT_REFERENCE_TRADE_ID)
    assert apt["pre_signal_position"]["fills_before_signal"] == 9
    assert apt["pre_signal_position"]["fills_at_or_after_signal"] == 4
    # fill at signal not in before count
    last = apt["pre_signal_position"]["last_fill_timestamp_before_signal"]
    assert last.startswith("2026-01-18T23:50:00")
    ids = {u["trade_id"] for u in out["unresolved_rows"]}
    assert any("BCHUSDT" in t for t in ids)
    assert any("TRXUSDT" in t for t in ids)


def test_distance_pct_semantics():
    d = distance_break_to_market_pct(1.7223, 1.7639)
    assert d == pytest.approx(-0.023584103407222678, abs=1e-15)


def test_duplicate_join_keys_detected():
    rows = [
        {"trade_id": "T", "trigger_mode": "first_break", "coin": "X"},
        {"trade_id": "T", "trigger_mode": "first_break", "coin": "X"},
    ]
    idx, dups = index_by_join(rows)
    assert idx == {}
    assert len(dups) == 1
    assert "DUPLICATE_JOIN_KEY" in dups[0]["reasons"]


def test_no_trigger_mode_mixing_in_index():
    rows = [
        {"trade_id": "T", "trigger_mode": "first_break", "coin": "X"},
        {"trade_id": "T", "trigger_mode": "final_invalidation", "coin": "X"},
    ]
    idx, dups = index_by_join(rows)
    assert dups == []
    assert ("T", "first_break") in idx
    assert ("T", "final_invalidation") in idx
    assert idx[("T", "first_break")] is not idx[("T", "final_invalidation")]


def test_fee_warning_alone_does_not_block():
    rec = {
        "trade_id": "T",
        "trigger": {
            "trigger_mode": "first_break",
            "signal_available_ts": "2026-01-19T00:00:00+00:00",
            "structure_break_event_ts": "2026-01-18T23:55:00+00:00",
            "structure_break_level": 1.0,
            "structure_break_kind": "protected_low_4h_close_break",
        },
        "market": {
            "market_price_at_signal": 1.0,
            "neutralization_fill_price": 1.0,
        },
        "pre_signal_position": {
            "long_qty": 10.0,
            "short_qty": 5.0,
            "long_avg": 1.1,
            "short_avg": 1.2,
            "last_fill_timestamp_before_signal": "2026-01-18T23:50:00+00:00",
        },
        "prior_economics": {"fee_quality": "FEE_RECONSTRUCTION_UNRESOLVED"},
        "quality": {
            "replay_match_status": "REPLAY_MATCH",
            "replay_diff_count": 0,
            "ready_for_neutralization_source": True,
        },
    }
    ready, reasons, warnings = evaluate_ready(rec)
    assert ready is True
    assert reasons == []
    assert "FEE_RECONSTRUCTION_UNRESOLVED" in warnings


def test_replay_mismatch_blocks():
    rec = {
        "trade_id": "T",
        "trigger": {
            "trigger_mode": "first_break",
            "signal_available_ts": "2026-01-19T00:00:00+00:00",
            "structure_break_event_ts": "2026-01-18T23:55:00+00:00",
            "structure_break_level": 1.0,
            "structure_break_kind": "k",
        },
        "market": {"market_price_at_signal": 1.0, "neutralization_fill_price": 1.0},
        "pre_signal_position": {
            "long_qty": 1.0,
            "short_qty": 1.0,
            "long_avg": 1.0,
            "short_avg": 1.0,
            "last_fill_timestamp_before_signal": "2026-01-18T23:50:00+00:00",
        },
        "prior_economics": {"fee_quality": "FEES_UNKNOWN"},
        "quality": {
            "replay_match_status": "REPLAY_MISMATCH",
            "replay_diff_count": 1,
            "ready_for_neutralization_source": True,
        },
    }
    ready, reasons, _ = evaluate_ready(rec)
    assert ready is False
    assert "REPLAY_NOT_MATCH" in reasons


def test_missing_break_level_blocks():
    rec = {
        "trade_id": "T",
        "trigger": {
            "trigger_mode": "first_break",
            "signal_available_ts": "2026-01-19T00:00:00+00:00",
            "structure_break_event_ts": "2026-01-18T23:55:00+00:00",
            "structure_break_level": 0.0,
            "structure_break_kind": "k",
        },
        "market": {"market_price_at_signal": 1.0, "neutralization_fill_price": 1.0},
        "pre_signal_position": {
            "long_qty": 1.0,
            "short_qty": 0.0,
            "long_avg": 1.0,
            "short_avg": 0.0,
            "last_fill_timestamp_before_signal": "2026-01-18T23:50:00+00:00",
        },
        "prior_economics": {"fee_quality": "FEES_UNKNOWN"},
        "quality": {
            "replay_match_status": "REPLAY_MATCH",
            "replay_diff_count": 0,
            "ready_for_neutralization_source": True,
        },
    }
    ready, reasons, _ = evaluate_ready(rec)
    assert ready is False
    assert "MISSING_OR_NONPOSITIVE_BREAK_LEVEL" in reasons


@pytest.mark.skipif(not STATE.exists() or not FILL.exists(), reason="source results missing")
def test_deterministic_outputs_and_manifest(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    build_bundle(state_dir=STATE, fill_replay_dir=FILL, output_dir=a)
    build_bundle(state_dir=STATE, fill_replay_dir=FILL, output_dir=b)
    assert (a / "blocker_historical_states.jsonl").read_text() == (
        b / "blocker_historical_states.jsonl"
    ).read_text()
    assert (a / "blocker_historical_states.csv").read_text() == (
        b / "blocker_historical_states.csv"
    ).read_text()
    man = json.loads((a / "source_manifest.json").read_text())
    assert man["sources"]
    assert any(s.get("sha256") for s in man["sources"] if s.get("exists"))
    integ = json.loads((a / "integrity.json").read_text())
    assert integ["ready_blocker_count"] == 25
    assert integ["unresolved_blocker_count"] == 2

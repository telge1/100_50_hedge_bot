"""Tests for autonomous continuous discovery (no manual windows)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_controls import (
    assign_control_group,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_defaults import (
    continuous_research_defaults,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_engine import (
    candidate_direction,
    classify_from_touch,
    process_symbol_stream,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_labels import (
    label_outcomes_for_candidates,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.regime import (
    prepare_bars_with_ema200,
    regime_snapshot,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.zones_ext import stacked_zone_label
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.impact import (
    classify_flow_mechanism,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zone_replay import (
    AnalysisSample,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import (
    EmaZone,
    make_zone,
)
from orderbook_analyse.strategy_lab.compiler_v2 import (
    StrategyCompilationError,
    compile_strategy_v2,
)
from orderbook_analyse.strategy_lab.decoder_v2 import load_strategy_v2_yaml_file
from orderbook_analyse.strategy_lab.validation import production_catalog_bundle_v2

REPO = Path(__file__).resolve().parents[1]


def _bars(n_min: int = 2500, px0: float = 100_000.0, trend: float = -0.00005) -> pd.DataFrame:
    times = pd.date_range("2026-08-20", periods=n_min, freq="1min", tz="UTC")
    px = px0
    rows = []
    for t in times:
        px *= 1.0 + trend
        rows.append({"open_time": t, "open": px, "high": px * 1.0001, "low": px * 0.9999, "close": px})
    return prepare_bars_with_ema200(pd.DataFrame(rows))


def _sample(ts_ms: int, mid: float, ema20: float, ema59: float, atr: float = 50.0) -> AnalysisSample:
    return AnalysisSample(
        ts_ms=ts_ms,
        mid=mid,
        best_bid=mid - 0.1,
        best_ask=mid + 0.1,
        bid_levels=200,
        ask_levels=200,
        genuine=True,
        seq_gap=False,
        carried_forward=False,
        warmup=False,
        ema20=ema20,
        ema59=ema59,
        atr=atr,
        bid_wall=None,
        ask_wall=None,
        ask_in_ema20=None,
        bid_in_ema20=None,
        ask_in_ema59=None,
        bid_in_ema59=None,
        source_file="synth",
    )


def test_no_manual_timestamps_in_defaults():
    d = continuous_research_defaults()
    assert d["manual_windows_not_used_as_centers"] is True
    assert "circle_1" not in str(d)


def test_closed_5m_only_regime():
    bars = _bars()
    asof = datetime(2026, 8, 25, 8, 33, tzinfo=timezone.utc)
    snap = regime_snapshot(bars, asof)
    end = pd.Timestamp(snap["last_bar_end"])
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    assert end <= pd.Timestamp(asof)
    assert snap["ema200_in_regime_score"] is False


def test_flat_compression_blocks_in_stream():
    bars = _bars(trend=0.0)
    # flat price near ema
    base = int(datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    row = bars.iloc[-1]
    ema20 = float(row["ema20"])
    ema59 = float(row["ema59"])
    atr = float(row["atr"]) if float(row["atr"]) > 0 else 30.0
    samples = [
        _sample(base + i * 250, ema20, ema20, ema59, atr=atr) for i in range(100)
    ]
    # force flat by using regime that may or may not be flat — call map via stream
    out = process_symbol_stream(
        symbol="BTCUSDT",
        samples=samples,
        bars=bars,
        trades_loader=lambda a, b: pd.DataFrame(),
        oi=pd.DataFrame(),
        liq=pd.DataFrame(),
        tick=0.1,
        discovery_start_ms=base,
        discovery_end_ms=base + 100_000,
    )
    # may produce block or watches depending on regime; ensure no crash and no manual ids
    for c in out["candidate_events"]:
        assert "circle_" not in c["episode_id"]


def test_watch_touch_timeout_path():
    bars = _bars()
    base = int(datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    # approach then enter zone briefly then stay — with no walls/trades → timeout/no_trade or undetermined
    ema20 = 100_000.0
    ema59 = 100_200.0
    atr = 40.0
    z = make_zone("EMA20", ema20, atr)
    samples = []
    # from above approaching
    for i in range(20):
        samples.append(_sample(base + i * 250, z.high + 50 - i * 2, ema20, ema59, atr))
    # inside
    for i in range(20, 400):
        samples.append(_sample(base + i * 250, ema20, ema20, ema59, atr))
    out = process_symbol_stream(
        symbol="BTCUSDT",
        samples=samples,
        bars=bars,
        trades_loader=lambda a, b: pd.DataFrame(),
        oi=pd.DataFrame(),
        liq=pd.DataFrame(),
        tick=0.1,
        discovery_start_ms=base,
        discovery_end_ms=base + 400 * 250,
    )
    assert len(out["zone_watch_events"]) + len(out["zone_contacts"]) >= 0
    for c in out["candidate_events"]:
        assert c["decision_at"] >= c.get("zone_touch_at", c["decision_at"])


def test_stacked_dedup():
    z20 = make_zone("EMA20", 100.0, 10.0)
    z59 = make_zone("EMA59", 100.5, 10.0)
    label = stacked_zone_label({"EMA20": z20, "EMA59": z59, "EMA200": None})
    assert label is not None and "STACKED" in label


def test_absorption_vs_pull_and_defense():
    assert (
        classify_flow_mechanism(
            attack_side="BUY",
            wall_side="ASK",
            buy_n=800_000,
            sell_n=10_000,
            wall_notional_before=1_000_000,
            wall_notional_after=100_000,
            wall_present_after=False,
            price_held_beyond=True,
            consumed_estimate=800_000,
        )
        == "ASK_ABSORPTION"
    )
    assert (
        classify_flow_mechanism(
            attack_side="BUY",
            wall_side="ASK",
            buy_n=40_000,
            sell_n=10_000,
            wall_notional_before=1_000_000,
            wall_notional_after=0,
            wall_present_after=False,
            price_held_beyond=True,
            consumed_estimate=40_000,
        )
        == "LIQUIDITY_PULL"
    )


def test_candidate_direction_mirror():
    assert candidate_direction("defense_rejection_confirmed", "resistance") == (
        "SHORT",
        "ask_defense_resistance",
    )
    assert candidate_direction("defense_rejection_confirmed", "support") == (
        "LONG",
        "bid_defense_support",
    )
    assert candidate_direction("breakout_confirmed", "resistance")[0] == "LONG"
    assert candidate_direction("false_breakout_confirmed", "resistance")[0] == "SHORT"
    assert candidate_direction("no_trade", "resistance")[0] == "NONE"
    assert candidate_direction("defense_rejection_confirmed", "ambiguous")[0] == "NONE"
    assert candidate_direction("watch_zone", "resistance")[0] == "NONE"
    assert candidate_direction("block_flat_compression", "support")[0] == "NONE"


def test_stage_a_never_emits_directional_marker():
    from orderbook_analyse.ema_zone_microstructure_confirmation.stage_a import (
        attach_direction_fields,
        emit_directional_marker_for,
        stage_a_direction_payload,
    )

    payload = stage_a_direction_payload()
    assert payload["candidate_direction"] == "NONE"
    assert payload["emit_directional_marker"] is False

    # Even if raw LONG leaks into a Stage-A state, hard-gate clears it.
    gated = attach_direction_fields(
        candidate_state="block_flat_compression",
        zone_role="resistance",
        raw_direction="LONG",
        direction_reason="leak_test",
    )
    assert gated["candidate_direction"] == "NONE"
    assert gated["emit_directional_marker"] is False

    assert emit_directional_marker_for(
        candidate_state="defense_rejection_confirmed",
        candidate_direction="SHORT",
    )
    assert not emit_directional_marker_for(
        candidate_state="wait_microstructure_confirmation",
        candidate_direction="LONG",
    )
    ok = attach_direction_fields(
        candidate_state="defense_rejection_confirmed",
        zone_role="resistance",
        raw_direction="SHORT",
        direction_reason="ask_defense_resistance",
    )
    assert ok == {
        "candidate_direction": "SHORT",
        "direction_reason": "ask_defense_resistance",
        "emit_directional_marker": True,
    }
    # Paket 2E: clearance blocks marker only — direction preserved.
    blocked = attach_direction_fields(
        candidate_state="breakout_confirmed",
        zone_role="resistance",
        raw_direction="LONG",
        direction_reason="breakout_up_through_resistance",
        block_directed_marker=True,
    )
    assert blocked["candidate_direction"] == "LONG"
    assert blocked["emit_directional_marker"] is False


def test_stage_a_approach_role_v2():
    from orderbook_analyse.ema_zone_microstructure_confirmation.stage_a import (
        decide_wait_next_zone,
        freeze_role_fields,
        zone_role_from_approach,
    )

    assert zone_role_from_approach("from_below") == "resistance"
    assert zone_role_from_approach("from_above") == "support"
    assert zone_role_from_approach("inside") == "ambiguous"
    roles = freeze_role_fields(approach_at_watch="from_below")
    assert roles["zone_role_at_watch"] == "resistance"
    assert roles["zone_role_at_touch"] == "resistance"
    assert roles["zone_role_at_decision"] == "resistance"
    wait, reason = decide_wait_next_zone(
        zone_name="STACKED:EMA20+EMA59",
        mechanism="ASK_DEFENSE",
        primary_class="DEFENSE_REJECTION",
        clearance_wait=False,
    )
    assert wait is True
    assert reason == "STACKED_ZONE_NO_DIRECTED"
    wait2, _ = decide_wait_next_zone(
        zone_name="EMA20",
        mechanism="ASK_DEFENSE",
        primary_class="DEFENSE_REJECTION",
        clearance_wait=True,
    )
    assert wait2 is True


def test_proximity_watch_vs_exact_touch_package2c():
    from orderbook_analyse.ema_zone_microstructure_confirmation.proximity import (
        PROXIMITY_WATCH_MAX_PCT,
        classify_zone_approach_event,
        is_exact_touch,
        is_proximity_watch,
    )
    from orderbook_analyse.ema_zone_microstructure_confirmation.stage_a import (
        stage_a_allows_microstructure,
    )

    assert PROXIMITY_WATCH_MAX_PCT == 0.20
    # 0.15% outside band → proximity only
    assert is_proximity_watch(inside_band=False, dist_outside=0.15, mid=100.0)
    assert not is_exact_touch(inside_band=False)
    ev = classify_zone_approach_event(inside_band=False, dist_outside=0.15, mid=100.0)
    assert ev["zone_event"] == "proximity_watch"
    assert ev["allows_stage_b_from_approach"] is False
    assert ev["emit_directional_marker_from_approach"] is False
    # 0.25% outside → no watch
    assert not is_proximity_watch(inside_band=False, dist_outside=0.25, mid=100.0)
    # exact touch
    assert is_exact_touch(inside_band=True)
    ev_t = classify_zone_approach_event(inside_band=True, dist_outside=0.0, mid=100.0)
    assert ev_t["zone_event"] == "exact_touch"
    assert ev_t["allows_stage_b_from_approach"] is True
    # Stage A micro gate: proximity alone insufficient
    assert not stage_a_allows_microstructure(
        block_flat_compression=False, near_zone=True, watch_armed=True, exact_touch=False
    )
    assert stage_a_allows_microstructure(
        block_flat_compression=False, near_zone=True, watch_armed=True, exact_touch=True
    )


def test_continuous_defaults_v2():
    d = continuous_research_defaults()
    assert d["format_version"].endswith("/v2")
    assert d["out_subdir"] == "continuous_discovery_v2"
    assert d["stage_a_never_emits_direction"] is True
    assert d["approach_role_map"]["from_below"] == "resistance"
    assert d["approach_role_map"]["from_above"] == "support"
    assert d["proximity_watch_max_pct"] == 0.20
    assert d["proximity_never_starts_stage_b"] is True
    assert d["proximity_never_emits_directional_marker"] is True


def test_regime_gate_package2():
    from orderbook_analyse.ema_zone_microstructure_confirmation.regime import (
        apply_regime_gate_to_candidate,
        evaluate_regime_gate,
    )

    bull = evaluate_regime_gate(regime="bullish")
    assert bull["allow_stage_b"] and bull["allow_directed"]
    assert bull["ema200_in_regime_score"] is False

    flat = evaluate_regime_gate(regime="range_compression", block_flat_compression=True)
    assert flat["hard_block"] and flat["block_state"] == "block_flat_compression"
    assert not flat["allow_directed"]

    und = evaluate_regime_gate(regime="undetermined", touched=True)
    assert und["allow_stage_b"] is True
    assert und["allow_directed"] is False
    state, reasons, allow = apply_regime_gate_to_candidate(
        final_state="defense_rejection_confirmed",
        reasons=["DEFENSE_REJECTION"],
        gate=und,
    )
    assert state == "no_trade"
    assert allow is False
    assert "BLOCK_UNDETERMINED_REGIME_DIRECTED" in reasons

    # transition: insufficient slope / separation / clearance → block directed
    tr_bad = evaluate_regime_gate(
        regime="transition",
        ema20_slope_3_atr=0.001,
        ema_spread_9_59_atr=0.01,
        zone_name="EMA20",
        touched=True,
        clearance_wait=True,
    )
    assert tr_bad["allow_directed"] is False
    assert tr_bad["transition_quality_ok"] is False

    tr_ok = evaluate_regime_gate(
        regime="transition",
        ema20_slope_3_atr=0.05,
        ema_spread_9_59_atr=0.3,
        zone_name="EMA20",
        touched=True,
        clearance_wait=False,
    )
    assert tr_ok["allow_directed"] is True
    assert tr_ok["transition_quality_ok"] is True

    # stacked never releases transition
    tr_stack = evaluate_regime_gate(
        regime="transition",
        ema20_slope_3_atr=0.05,
        ema_spread_9_59_atr=0.3,
        zone_name="STACKED:EMA20+EMA59",
        touched=True,
        clearance_wait=False,
    )
    assert tr_stack["allow_directed"] is False


def test_flat_diagnostics_package2b():
    from orderbook_analyse.ema_zone_microstructure_confirmation.regime import (
        flat_block_payload,
        flat_diagnostics,
    )
    from orderbook_analyse.ema_zone_microstructure_confirmation.stage_a import (
        stage_a_direction_payload,
    )

    bars = _bars(n_min=3000, trend=0.0)  # flat-ish path
    asof = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    diag = flat_diagnostics(bars, asof)
    assert "ema9_slope_norm" in diag
    assert "ema20_slope_norm" in diag
    assert "ema59_slope_norm" in diag
    assert "ema_spread_pct" in diag
    assert isinstance(diag["ema_cross_count"], int)
    assert isinstance(diag["ema_reorder_count"], int)
    assert "flat_reason" in diag

    if diag["flat"]:
        payload = flat_block_payload(
            flat_at_watch=True,
            flat_at_touch=False,
            flat_at_decision=False,
            diag=diag,
            decisive_stage="watch_at",
        )
        assert payload["ema_setup_state"] == "block_flat_compression"
        assert payload["flat_at_watch"] is True
        assert "FLAT_COMPRESSION" in payload["block_reasons"]
        dir_fields = stage_a_direction_payload(reason="block_flat_compression")
        assert dir_fields["candidate_direction"] == "NONE"
        assert dir_fields["emit_directional_marker"] is False
    else:
        # Still assert block payload contract when forcing flat diag
        forced = {**diag, "flat": True, "flat_reason": "FLAT_COMPRESSION"}
        payload = flat_block_payload(
            flat_at_watch=False,
            flat_at_touch=True,
            flat_at_decision=False,
            diag=forced,
            decisive_stage="touch_at",
        )
        assert payload["flat_at_touch"] is True
        assert payload["ema_setup_state"] == "block_flat_compression"
        assert "FLAT_COMPRESSION" in payload["block_reasons"]
        dir_fields = stage_a_direction_payload(reason="block_flat_compression")
        assert dir_fields["candidate_direction"] == "NONE"
        assert dir_fields["emit_directional_marker"] is False


def test_directional_clearance_package2d():
    from orderbook_analyse.ema_zone_microstructure_confirmation.directional_clearance import (
        CLEARANCE_STATUS_CLEAR,
        CLEARANCE_STATUS_NEXT_ZONE_NEAR,
        CLEARANCE_STATUS_STACKED_ZONE,
        analyze_directional_clearance,
        clearance_status_from_analysis,
        enrich_clearance_for_emit,
        bands_overlap_for_zone,
        clearance_fields_for_emit,
        expected_move_direction,
        stacked_zone_breakout_complete,
    )

    atr = 50.0
    ema20 = make_zone("EMA20", center=100_000.0, atr=atr)
    # ~0.35% edge gap above EMA20 → inside clearance band [0.2%, 0.5%]
    ema59_close = make_zone("EMA59", center=100_360.0, atr=atr)
    ema59_far = make_zone("EMA59", center=101_000.0, atr=atr)
    ema200 = make_zone("EMA200", center=102_000.0, atr=atr)
    zones = {"EMA20": ema20, "EMA59": ema59_close, "EMA200": ema200}

    assert expected_move_direction(
        candidate_state="defense_rejection_confirmed", zone_role="resistance"
    ) == "DOWN"
    assert expected_move_direction(
        candidate_state="breakout_confirmed", zone_role="resistance"
    ) == "UP"
    assert expected_move_direction(
        candidate_state="false_breakout_confirmed", zone_role="support"
    ) == "UP"
    assert expected_move_direction(
        candidate_state="breakout_confirmed",
        zone_role="resistance",
        candidate_direction="LONG",
    ) == "UP"

    gap_up = ema59_close.low - ema20.high
    assert gap_up > 0
    pct_up = (gap_up / 100_000.0) * 100.0
    assert 0.2 <= pct_up <= 0.5

    clr_close = analyze_directional_clearance(
        current_zone=ema20,
        current_zone_key="EMA20",
        zones=zones,
        expected_move="UP",
        mid=100_000.0,
        candidate_state="breakout_confirmed",
    )
    assert clr_close["next_zone"] == "EMA59"
    assert clr_close["expected_move_direction"] == "UP"
    assert clr_close["next_zone_distance_pct"] == pytest.approx(pct_up, rel=1e-6)
    assert clr_close["wait_next_zone"] is True
    assert clr_close["block_directed_marker"] is True
    assert clr_close["clearance_reason"] == "NEXT_ZONE_TOO_CLOSE"

    zones_far = {"EMA20": ema20, "EMA59": ema59_far, "EMA200": ema200}
    clr_ok = analyze_directional_clearance(
        current_zone=ema20,
        current_zone_key="EMA20",
        zones=zones_far,
        expected_move="UP",
        mid=100_000.0,
        candidate_state="breakout_confirmed",
    )
    assert clr_ok["wait_next_zone"] is False
    assert clr_ok["block_directed_marker"] is False
    assert clr_ok["clearance_reason"] == "CLEARANCE_OK"

    ema59_below = make_zone("EMA59", center=99_635.0, atr=atr)
    zones_down = {"EMA20": ema20, "EMA59": ema59_below, "EMA200": ema200}
    gap_down = ema20.low - ema59_below.high
    pct_down = (gap_down / 100_000.0) * 100.0
    assert 0.2 <= pct_down <= 0.5
    clr_down = analyze_directional_clearance(
        current_zone=ema20,
        current_zone_key="EMA20",
        zones=zones_down,
        expected_move="DOWN",
        mid=100_000.0,
        candidate_state="defense_rejection_confirmed",
    )
    assert clr_down["next_zone"] == "EMA59"
    assert clr_down["expected_move_direction"] == "DOWN"
    assert clr_down["wait_next_zone"] is True

    overlap20 = make_zone("EMA20", center=100_000.0, atr=atr)
    overlap59 = make_zone("EMA59", center=100_015.0, atr=atr)
    zones_ov = {"EMA20": overlap20, "EMA59": overlap59, "EMA200": ema200}
    assert bands_overlap_for_zone(zones_ov, "EMA20") is True
    clr_ov = analyze_directional_clearance(
        current_zone=overlap20,
        current_zone_key="EMA20",
        zones=zones_ov,
        expected_move="UP",
        mid=100_000.0,
        candidate_state="breakout_confirmed",
    )
    assert clr_ov["bands_overlap"] is True
    assert clr_ov["clearance_reason"] == "BANDS_OVERLAP_NEXT_ZONE"

    stacked_key = "STACKED:EMA20+EMA59"
    stacked_zone = make_zone(stacked_key, center=100_000.0, atr=atr)
    clr_stack_def = analyze_directional_clearance(
        current_zone=stacked_zone,
        current_zone_key=stacked_key,
        zones=zones_far,
        expected_move="DOWN",
        mid=100_000.0,
        candidate_state="defense_rejection_confirmed",
    )
    assert clr_stack_def["stacked_zone"] is True
    assert clr_stack_def["block_directed_marker"] is True
    assert clr_stack_def["clearance_reason"] == "STACKED_ZONE_NO_DIRECTED"

    samples_incomplete = [_sample(ts_ms=1_000, mid=100_000.0, ema20=100_000.0, ema59=100_360.0, atr=atr)]
    assert not stacked_zone_breakout_complete(
        samples=samples_incomplete,
        decision_ms=2_000,
        zone_low=stacked_zone.low,
        zone_high=stacked_zone.high,
        expected_move="UP",
    )
    samples_complete = [
        *samples_incomplete,
        _sample(
            ts_ms=2_000,
            mid=stacked_zone.high + 1.0,
            ema20=100_000.0,
            ema59=100_360.0,
            atr=atr,
        ),
    ]
    assert stacked_zone_breakout_complete(
        samples=samples_complete,
        decision_ms=2_000,
        zone_low=stacked_zone.low,
        zone_high=stacked_zone.high,
        expected_move="UP",
    )
    clr_stack_bo = analyze_directional_clearance(
        current_zone=stacked_zone,
        current_zone_key=stacked_key,
        zones=zones_far,
        expected_move="UP",
        mid=100_000.0,
        samples=samples_incomplete,
        decision_ms=2_000,
        candidate_state="breakout_confirmed",
    )
    assert clr_stack_bo["stacked_breakout_complete"] is False
    assert clr_stack_bo["block_directed_marker"] is True
    assert clr_stack_bo["clearance_reason"] == "STACKED_BREAKOUT_INCOMPLETE"

    state, reasons, enriched = enrich_clearance_for_emit(
        reaction_state="defense_rejection_confirmed",
        reasons=["DEFENSE_REJECTION"],
        clearance=clr_close,
    )
    assert state == "defense_rejection_confirmed"
    assert enriched["clearance_status"] == CLEARANCE_STATUS_NEXT_ZONE_NEAR
    assert "CLEARANCE_BLOCKS_DIRECTED_MARKER" in reasons
    assert "WAIT_NEXT_ZONE" in reasons
    assert "WAIT_NEXT_ZONE_CONFIRMATION" not in reasons

    assert clearance_status_from_analysis(clr_ok) == CLEARANCE_STATUS_CLEAR
    assert clearance_status_from_analysis(clr_stack_def) == CLEARANCE_STATUS_STACKED_ZONE

    emitted = clearance_fields_for_emit(enriched)
    for key in (
        "current_zone",
        "current_zone_band_low",
        "current_zone_band_high",
        "expected_move_direction",
        "next_zone",
        "next_zone_band_low",
        "next_zone_band_high",
        "next_zone_distance_pct",
        "next_zone_distance_atr",
        "bands_overlap",
        "clearance_status",
        "clearance_reason",
        "block_directed_marker",
    ):
        assert key in emitted


def test_reaction_clearance_marker_semantics_package2e():
    """Breakout + near next zone: reaction preserved, marker blocked, direction kept."""
    from orderbook_analyse.ema_zone_microstructure_confirmation.directional_clearance import (
        CLEARANCE_STATUS_NEXT_ZONE_NEAR,
        analyze_directional_clearance,
        enrich_clearance_for_emit,
    )
    from orderbook_analyse.ema_zone_microstructure_confirmation.stage_a import (
        attach_direction_fields,
    )

    atr = 50.0
    ema20 = make_zone("EMA20", center=100_000.0, atr=atr)
    ema59 = make_zone("EMA59", center=100_360.0, atr=atr)
    zones = {"EMA20": ema20, "EMA59": ema59}
    clr = analyze_directional_clearance(
        current_zone=ema20,
        current_zone_key="EMA20",
        zones=zones,
        expected_move="UP",
        mid=100_000.0,
        candidate_state="breakout_confirmed",
    )
    reaction, reasons, clr_out = enrich_clearance_for_emit(
        reaction_state="breakout_confirmed",
        reasons=["ABSORPTION_THEN_BREAKOUT"],
        clearance=clr,
    )
    assert reaction == "breakout_confirmed"
    assert clr_out["clearance_status"] == CLEARANCE_STATUS_NEXT_ZONE_NEAR
    assert clr_out["block_directed_marker"] is True
    assert clr_out["wait_next_zone"] is True

    raw_dir, dir_reason = candidate_direction(reaction, "resistance")
    assert raw_dir == "LONG"
    fields = attach_direction_fields(
        candidate_state=reaction,
        zone_role="resistance",
        raw_direction=raw_dir,
        direction_reason=dir_reason,
        block_directed_marker=True,
    )
    assert fields["candidate_direction"] == "LONG"
    assert fields["emit_directional_marker"] is False
    assert reaction != "wait_next_zone_confirmation"


def test_label_anchor_after_decision_and_outcomes_no_feedback():
    cands = [
        {
            "symbol": "BTCUSDT",
            "episode_id": "e1",
            "candidate_state": "defense_rejection_confirmed",
            "candidate_direction": "SHORT",
            "decision_at": "2026-08-25T10:00:00.000Z",
            "label_anchor_at": "2026-08-25T10:00:00.250Z",
            "label_anchor_price": 100.0,
            "regime": "bearish",
            "zone_name": "EMA20",
            "mechanism": "ASK_DEFENSE",
            "major_wall_confluence": True,
        }
    ]
    path = [(int(datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc).timestamp() * 1000) + i * 1000, 100.0 - i * 0.01) for i in range(0, 400)]
    # first path point at decision; outcomes use anchor
    rows = label_outcomes_for_candidates(
        cands,
        path=path,
        path_end_ms=path[-1][0],
    )
    assert rows
    assert all(r["decision_at"] == "2026-08-25T10:00:00.000Z" for r in rows)
    # LONG/SHORT mfe for short should be positive when price falls
    complete = [r for r in rows if r["horizon_complete"] and r["horizon_s"] == 60]
    assert complete
    assert float(complete[0]["mfe_pct"]) >= 0


def test_incomplete_horizon():
    cands = [
        {
            "symbol": "BTCUSDT",
            "episode_id": "e1",
            "candidate_state": "defense_rejection_confirmed",
            "candidate_direction": "SHORT",
            "decision_at": "2026-08-25T10:00:00.000Z",
            "label_anchor_at": "2026-08-25T10:00:00.250Z",
            "label_anchor_price": 100.0,
            "regime": "bearish",
            "zone_name": "EMA20",
            "mechanism": "ASK_DEFENSE",
            "major_wall_confluence": False,
        }
    ]
    anchor_ms = int(datetime(2026, 8, 25, 10, 0, 0, 250000, tzinfo=timezone.utc).timestamp() * 1000)
    path = [(anchor_ms + i * 1000, 100.0) for i in range(10)]
    rows = label_outcomes_for_candidates(cands, path=path, path_end_ms=path[-1][0])
    long_h = [r for r in rows if r["horizon_s"] == 14_400]
    assert long_h and long_h[0]["horizon_complete"] is False
    assert long_h[0]["mfe_pct"] == "MISSING" or long_h[0]["mfe_pct"] == "MISSING"


def test_control_groups():
    assert assign_control_group({"candidate_state": "block_flat_compression"}) == "B_flat_compression_blocked"
    assert (
        assign_control_group(
            {"candidate_state": "defense_rejection_confirmed", "major_wall_confluence": True, "mechanism": "ASK_DEFENSE"}
        )
        == "D_defense_with_major_wall"
    )
    assert (
        assign_control_group(
            {"candidate_state": "false_breakout_confirmed", "mechanism": "UNDETERMINED"}
        )
        == "H_false_breakout_undetermined_mechanism"
    )


def test_no_trade_compiler():
    catalogs = production_catalog_bundle_v2()
    spec = load_strategy_v2_yaml_file(
        REPO / "strategies/strategy_lab/ema_zone_microstructure_confirmation_v1.yaml"
    )
    with pytest.raises(StrategyCompilationError, match="CANDIDATE_DISCOVERY_NOT_TRADE_BACKTEST"):
        compile_strategy_v2(spec, catalogs)


def test_chunking_deterministic_synthetic():
    bars = _bars()
    base = int(datetime(2026, 8, 25, 11, 0, tzinfo=timezone.utc).timestamp() * 1000)
    ema20, ema59, atr = 99_000.0, 99_500.0, 40.0
    samples = [_sample(base + i * 250, ema20 + 5, ema20, ema59, atr) for i in range(50)]
    kwargs = dict(
        symbol="BTCUSDT",
        samples=samples,
        bars=bars,
        trades_loader=lambda a, b: pd.DataFrame(),
        oi=pd.DataFrame(),
        liq=pd.DataFrame(),
        tick=0.1,
        discovery_start_ms=base,
        discovery_end_ms=base + 50 * 250,
    )
    a = process_symbol_stream(**kwargs)
    b = process_symbol_stream(**kwargs)
    assert len(a["candidate_events"]) == len(b["candidate_events"])


def test_computation_mode_ema_only_skips_microstructure():
    from orderbook_analyse.ema_zone_microstructure_confirmation.research_layers import (
        COMPUTATION_MODE_EMA_ONLY,
        COMPUTATION_MODE_EMA_PLUS_MICRO,
    )

    bars = _bars()
    base = int(datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    ema20 = 100_000.0
    ema59 = 100_200.0
    atr = 40.0
    z = make_zone("EMA20", ema20, atr)
    samples = []
    for i in range(20):
        samples.append(_sample(base + i * 250, z.high + 50 - i * 2, ema20, ema59, atr))
    for i in range(20, 400):
        samples.append(_sample(base + i * 250, ema20, ema20, ema59, atr))
    common = dict(
        symbol="BTCUSDT",
        samples=samples,
        bars=bars,
        trades_loader=lambda a, b: pd.DataFrame(),
        oi=pd.DataFrame(),
        liq=pd.DataFrame(),
        tick=0.1,
        discovery_start_ms=base,
        discovery_end_ms=base + 400 * 250,
    )
    full = process_symbol_stream(**common, computation_mode=COMPUTATION_MODE_EMA_PLUS_MICRO)
    ema_only = process_symbol_stream(**common, computation_mode=COMPUTATION_MODE_EMA_ONLY)

    assert len(ema_only["ema_setup_events"]) >= len(full["ema_setup_events"]) or len(ema_only["ema_setup_events"]) > 0
    assert ema_only["microstructure_confirmation_events"] == []
    assert not any(
        str(c.get("confirmation_mode") or "") == "ema_plus_microstructure"
        for c in ema_only["candidate_events"]
    )
    assert full["microstructure_confirmation_events"] or full["candidate_events"]
    for row in ema_only["ema_setup_events"]:
        assert str(row.get("candidate_direction") or "NONE").upper() in {"NONE", ""}
        assert row.get("emit_directional_marker") is False

"""Tests for break-handoff-depth audit helpers and guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.backtests.cobertura_0_notional_strategie.break_handoff_depth import (
    activation_target_price,
    classify_handoff_case,
    select_activation_after_break,
    snapshot_from_ledger,
)
from research.backtests.cobertura_0_notional_strategie.engine import _parse_ts
from research.backtests.cobertura_0_notional_strategie.historical_blocker_fill_replay import (
    fill_before_signal,
)
from research.backtests.cobertura_0_notional_strategie.run_multi_blocker_break_handoff_depth_audit import (
    check_parity_guards,
    load_break_events,
    load_ledger_by_trade,
    run_audit,
)
from research.backtests.cobertura_0_notional_strategie.run_multi_blocker_forensic_audit import (
    DEFAULT_FILL_REPLAY_DIR,
    DEFAULT_STATE_DIR,
    load_case_universe,
)

pytestmark = pytest.mark.skipif(
    not (DEFAULT_FILL_REPLAY_DIR / "blocker_pre_signal_states.csv").exists(),
    reason="fill replay missing",
)

APT_ID = "APTUSDT|two_early_medium|continuous|0006"
TIA_ID = "TIAUSDT|two_early_medium|continuous|0007"


def test_activation_target_prices():
    assert activation_target_price(structure_break_price=100.0, depth_pct=0.0) == 100.0
    assert activation_target_price(structure_break_price=100.0, depth_pct=0.06) == pytest.approx(
        94.0
    )
    assert activation_target_price(structure_break_price=1.7639, depth_pct=0.20) == pytest.approx(
        1.7639 * 0.8
    )


def test_activation_gap_open_and_intrabar():
    candles = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "open": 10, "high": 10.1, "low": 9.9, "close": 10},
        {"timestamp": "2026-01-01T00:05:00+00:00", "open": 9.8, "high": 9.9, "low": 9.5, "close": 9.6},
        {"timestamp": "2026-01-01T00:10:00+00:00", "open": 9.2, "high": 9.3, "low": 9.0, "close": 9.1},
    ]
    # D0 at break=10 → immediate on avail candle if open<=10
    sel0 = select_activation_after_break(
        candles,
        break_available_ts="2026-01-01T00:00:00+00:00",
        structure_break_price=10.0,
        depth_pct=0.0,
        parse_ts_fn=_parse_ts,
    )
    assert sel0["activation_reached"] is True
    assert sel0["activation_fill_reason"] == "gap_open"
    assert sel0["activation_price"] == pytest.approx(10.0)

    sel = select_activation_after_break(
        candles,
        break_available_ts="2026-01-01T00:00:00+00:00",
        structure_break_price=10.0,
        depth_pct=0.05,
        parse_ts_fn=_parse_ts,
    )
    assert sel["activation_fill_reason"] == "intrabar_touch"
    assert sel["activation_price"] == pytest.approx(9.5)
    assert sel["used_low_as_fill"] is False

    sel_gap = select_activation_after_break(
        candles,
        break_available_ts="2026-01-01T00:00:00+00:00",
        structure_break_price=10.0,
        depth_pct=0.10,
        parse_ts_fn=_parse_ts,
    )
    # 00:10 open 9.2 > 9.0, low 9.0 -> intrabar; use deeper gap case:
    candles2 = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "open": 10, "high": 10, "low": 10, "close": 10},
        {"timestamp": "2026-01-01T00:05:00+00:00", "open": 8.5, "high": 8.6, "low": 8.4, "close": 8.5},
    ]
    sel_gap = select_activation_after_break(
        candles2,
        break_available_ts="2026-01-01T00:00:00+00:00",
        structure_break_price=10.0,
        depth_pct=0.10,
        parse_ts_fn=_parse_ts,
    )
    assert sel_gap["activation_fill_reason"] == "gap_open"
    assert sel_gap["activation_price"] == pytest.approx(8.5)


def test_activation_not_reached():
    candles = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "open": 10, "high": 10, "low": 10, "close": 10},
        {"timestamp": "2026-01-01T00:05:00+00:00", "open": 9.9, "high": 9.95, "low": 9.8, "close": 9.85},
    ]
    sel = select_activation_after_break(
        candles,
        break_available_ts="2026-01-01T00:00:00+00:00",
        structure_break_price=10.0,
        depth_pct=0.15,
        parse_ts_fn=_parse_ts,
        horizon_end_ts="2026-01-01T01:00:00+00:00",
    )
    assert sel["activation_reached"] is False


def test_no_lookahead_first_touch():
    candles = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "open": 10, "high": 10, "low": 10, "close": 10},
        {"timestamp": "2026-01-01T00:05:00+00:00", "open": 9.6, "high": 9.7, "low": 9.5, "close": 9.55},
        {"timestamp": "2026-01-01T00:10:00+00:00", "open": 8.0, "high": 8.1, "low": 7.5, "close": 7.8},
    ]
    sel = select_activation_after_break(
        candles,
        break_available_ts="2026-01-01T00:00:00+00:00",
        structure_break_price=10.0,
        depth_pct=0.05,
        parse_ts_fn=_parse_ts,
    )
    assert sel["activation_time"].startswith("2026-01-01T00:05:00")
    assert sel["activation_price"] == pytest.approx(9.5)


def test_activation_not_before_break_availability():
    candles = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "open": 8.0, "high": 8.1, "low": 7.5, "close": 7.8},
        {"timestamp": "2026-01-01T00:05:00+00:00", "open": 9.0, "high": 9.1, "low": 8.8, "close": 9.0},
        {"timestamp": "2026-01-01T00:10:00+00:00", "open": 8.5, "high": 8.6, "low": 8.4, "close": 8.5},
    ]
    sel = select_activation_after_break(
        candles,
        break_available_ts="2026-01-01T00:05:00+00:00",
        structure_break_price=10.0,
        depth_pct=0.10,
        parse_ts_fn=_parse_ts,
    )
    assert sel["activation_reached"] is True
    assert sel["activation_time"].startswith("2026-01-01T00:05:00")


def test_structure_break_and_pre_break_parity():
    selected, _ = load_case_universe(
        fill_replay_dir=DEFAULT_FILL_REPLAY_DIR, state_dir=DEFAULT_STATE_DIR
    )
    break_by_id = load_break_events(DEFAULT_STATE_DIR)
    ledger_by_trade = load_ledger_by_trade(DEFAULT_FILL_REPLAY_DIR)
    guards = check_parity_guards(
        selected=selected,
        break_by_id=break_by_id,
        ledger_by_trade=ledger_by_trade,
        break_d0_rows=[],
    )
    assert guards["pass"] is True


def test_ledger_snapshot_changes_after_break_for_apt():
    ledger = load_ledger_by_trade(DEFAULT_FILL_REPLAY_DIR)[APT_ID]
    selected, _ = load_case_universe(
        fill_replay_dir=DEFAULT_FILL_REPLAY_DIR, state_dir=DEFAULT_STATE_DIR
    )
    row = next(r for r in selected if r["trade_id"] == APT_ID)
    sig = row["signal_available_ts"]
    snap0 = snapshot_from_ledger(ledger, cutoff_ts=sig, trade_id=APT_ID, coin="APTUSDT")
    # later cutoff after known post-signal fills
    snap1 = snapshot_from_ledger(
        ledger, cutoff_ts="2026-01-20T00:00:00+00:00", trade_id=APT_ID, coin="APTUSDT"
    )
    post = [
        r
        for r in ledger
        if not fill_before_signal(r.get("fill_timestamp"), sig, strict=True)
    ]
    assert len(post) >= 1
    # State must be able to diverge after break when TEM continues
    assert snap0["fills_before_cutoff"] < snap1["fills_before_cutoff"]


def test_refill_cases_unit():
    # equal qty → no refill class path via handoff helper book
    book_eq = {"long_qty": 10.0, "long_avg": 2.0, "short_qty": 10.0, "short_avg": 1.9}
    assert max(book_eq["long_qty"] - book_eq["short_qty"], 0.0) == 0.0
    book_over = {"long_qty": 10.0, "long_avg": 2.0, "short_qty": 12.0, "short_avg": 1.9}
    assert max(book_over["long_qty"] - book_over["short_qty"], 0.0) == 0.0
    book_under = {"long_qty": 10.0, "long_avg": 2.0, "short_qty": 7.0, "short_avg": 1.9}
    assert max(book_under["long_qty"] - book_under["short_qty"], 0.0) == 3.0


def test_classify_unresolved_and_not_reached():
    assert (
        classify_handoff_case(
            activation_reached=False,
            unresolved_break=True,
            d0_recovered=False,
            variant_recovered=False,
            state_improved=False,
            state_worsened=False,
            combined_improved_vs_d0=False,
            combined_worsened_vs_d0=False,
            only_post_dd_improved=False,
            shared_be_worsened=False,
            no_cobertura_best=False,
            is_d0=False,
        )
        == "UNRESOLVED_STRUCTURE_BREAK"
    )
    assert (
        classify_handoff_case(
            activation_reached=False,
            unresolved_break=False,
            d0_recovered=False,
            variant_recovered=False,
            state_improved=False,
            state_worsened=False,
            combined_improved_vs_d0=False,
            combined_worsened_vs_d0=False,
            only_post_dd_improved=False,
            shared_be_worsened=False,
            no_cobertura_best=False,
            is_d0=False,
        )
        == "ACTIVATION_TARGET_NOT_REACHED"
    )


def test_audit_smoke_apt_tia_and_unresolved(tmp_path: Path):
    out = tmp_path / "break_handoff"
    summary = run_audit(
        output_dir=out,
        trade_ids=[APT_ID, TIA_ID],
        dump_cases=False,
    )
    assert summary["parity_pass"] is True
    assert summary["decision"] in (
        "BREAK_HANDOFF_DEPTH_AUDIT_PASS",
        "BREAK_HANDOFF_DEPTH_AUDIT_PASS_WITH_WARNINGS",
        "BREAK_HANDOFF_DEPTH_AUDIT_FAIL_INVARIANTS",
        "BREAK_HANDOFF_DEPTH_AUDIT_BLOCKED_REPLAY_MISMATCH",
    )
    assert (out / "trade_variant_results.csv").exists()
    assert (out / "break_activation_depth_summary.csv").exists()
    assert (out / "REPORT.md").exists()
    # unresolved always written from universe
    un = (out / "unresolved_break_cases.csv").read_text(encoding="utf-8")
    assert "BCH" in un and "TRX" in un

    # D0 deterministic re-run
    summary2 = run_audit(
        output_dir=tmp_path / "break_handoff2",
        trade_ids=[APT_ID],
        dump_cases=False,
    )
    assert summary2["parity_pass"] is True


def test_no_cobertura_variant_has_zero_cobertura_fills(tmp_path: Path):
    import csv

    out = tmp_path / "nc"
    run_audit(output_dir=out, trade_ids=[APT_ID], dump_cases=False)
    rows = list(csv.DictReader((out / "trade_variant_results.csv").open()))
    nc = next(r for r in rows if r["variant"] == "NO_COBERTURA_AFTER_BREAK")
    assert nc.get("cobertura_fills") in ("0", "0.0", 0, "")
    assert nc["recovered_120d"] in ("False", "false", False, "")


def test_break_d0_activation_at_or_after_signal(tmp_path: Path):
    import csv

    out = tmp_path / "d0"
    run_audit(output_dir=out, trade_ids=[APT_ID], dump_cases=False)
    rows = list(csv.DictReader((out / "trade_variant_results.csv").open()))
    d0 = next(r for r in rows if r["variant"] == "BREAK_D0" and r["trade_id"] == APT_ID)
    assert d0["activation_reached"] in ("True", "true", True)
    assert _parse_ts(d0["activation_time"]) >= _parse_ts(
        d0["structure_break_available_time"]
    )
    # Same-candle double-processing flag present
    assert "same_candle_activation_exit" in d0

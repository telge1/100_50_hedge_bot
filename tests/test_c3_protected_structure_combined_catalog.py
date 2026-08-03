"""Tests for combined Protected-Low + Protected-High structure catalog."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from orderbook_analyse.c3_protected_structure_combined_catalog import (
    COMBINED_PRIMARY_DECISIONS,
    decide_combined_primary,
    run_combined_structure_catalog,
    _candidate_origin,
    _union_find_regimes,
)


def test_origin_labels() -> None:
    assert (
        _candidate_origin(
            side_catalog="LOW", outcome="RECLAIM_CONFIRMED", cand_side="LONG"
        )
        == "PROTECTED_LOW_RECLAIM"
    )
    assert (
        _candidate_origin(
            side_catalog="LOW", outcome="BREAKDOWN_CONFIRMED", cand_side="SHORT"
        )
        == "PROTECTED_LOW_BREAKDOWN"
    )
    assert (
        _candidate_origin(
            side_catalog="HIGH", outcome="BREAKOUT_CONFIRMED", cand_side="LONG"
        )
        == "PROTECTED_HIGH_BREAKOUT"
    )
    assert (
        _candidate_origin(
            side_catalog="HIGH", outcome="RECLAIM_DOWN_CONFIRMED", cand_side="SHORT"
        )
        == "PROTECTED_HIGH_RECLAIM_DOWN"
    )


def test_no_cross_symbol_regime() -> None:
    events = [
        {
            "event_id": "APTUSDT_PL_1",
            "symbol": "APTUSDT",
            "origin": "PROTECTED_LOW",
            "break_available_at": "2026-07-26T11:50:00Z",
            "outcome": "BREAKDOWN_CONFIRMED",
            "level": 0.6,
        },
        {
            "event_id": "APTUSDT_PH_1",
            "symbol": "APTUSDT",
            "origin": "PROTECTED_HIGH",
            "break_available_at": "2026-07-26T12:00:00Z",
            "outcome": "BREAKOUT_CONFIRMED",
            "level": 0.7,
        },
        {
            "event_id": "DOGEUSDT_PL_1",
            "symbol": "DOGEUSDT",
            "origin": "PROTECTED_LOW",
            "break_available_at": "2026-07-26T11:55:00Z",
            "outcome": "RECLAIM_CONFIRMED",
            "level": 0.07,
        },
    ]
    mapping, regimes = _union_find_regimes(events)
    for rid, eids in regimes.items():
        symbols = {next(e["symbol"] for e in events if e["event_id"] == eid) for eid in eids}
        assert len(symbols) == 1, f"cross-symbol regime {rid}: {symbols}"
    # APT low+high overlap → same regime; DOGE separate
    apt_regs = {m["regime_id"] for m in mapping if m["symbol"] == "APTUSDT"}
    doge_regs = {m["regime_id"] for m in mapping if m["symbol"] == "DOGEUSDT"}
    assert apt_regs.isdisjoint(doge_regs)
    assert len(apt_regs) == 1


def test_gate_table_structure_decide() -> None:
    events = [
        {
            "event_id": f"E{i}",
            "symbol": "APTUSDT",
            "origin": "PROTECTED_LOW",
            "outcome": "BREAKDOWN_CONFIRMED",
            "data_valid": True,
        }
        for i in range(3)
    ]
    primary, _ = decide_combined_primary(
        events=events,
        combined_long_gate={"pass": False},
        combined_short_gate={"pass": False},
        low_events=events,
        high_events=[],
    )
    assert primary in COMBINED_PRIMARY_DECISIONS


def test_combined_from_existing_artefacts_if_present(tmp_path: Path) -> None:
    low = Path("results/c3_protected_low_historical_event_catalog")
    high = Path("results/c3_protected_high_historical_event_catalog")
    if not (low / "event_decisions.csv").exists():
        return
    if not (high / "event_decisions.csv").exists():
        return
    out = tmp_path / "combined"
    result = run_combined_structure_catalog(
        low_dir=low,
        high_dir=high,
        output_dir=out,
        overwrite=True,
        run_missing=False,
    )
    assert result["decision"] in COMBINED_PRIMARY_DECISIONS
    assert (out / "combined_event_inventory.csv").exists()
    assert (out / "combined_candidates.csv").exists()
    assert (out / "combined_sample_gate_evaluation.csv").exists()
    assert (out / "combined_decision.json").exists()
    inv = pd.read_csv(out / "combined_event_inventory.csv")
    assert set(inv["origin"].unique()) <= {"PROTECTED_LOW", "PROTECTED_HIGH"}
    gates = pd.read_csv(out / "combined_sample_gate_evaluation.csv")
    assert "universe" in gates.columns
    assert "side" in gates.columns
    assert set(gates["universe"]) >= {"low_only", "high_only", "combined"}

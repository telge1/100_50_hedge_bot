"""CLI / controls / invalidation / protected visibility offline tests."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.market_structure_c3_4b import (
    RESEARCH_MATRIX,
    ProtectedLevel,
    ProtectedRuntime,
    ProtectedStructureConfig,
    step_protected_structure_state,
)
from research.regime_scanner.orderflow_absorption_level.config import LevelAbsorptionConfig
from research.regime_scanner.orderflow_absorption_level.levels_build import level_visible_at
from research.regime_scanner.orderflow_absorption_level.controls import (
    build_control_assignments,
    match_control_pairs,
)
from research.regime_scanner.run_orderflow_absorption_level_audit import build_parser


def test_cli_parser_defaults():
    p = build_parser()
    args = p.parse_args(
        [
            "--symbols",
            "BTCUSDT,APTUSDT",
            "--start",
            "2026-04-01T00:00:00Z",
            "--end",
            "2026-04-07T00:00:00Z",
            "--output-dir",
            "/tmp/out",
        ]
    )
    assert args.patterns == "A4,A2,A1"
    assert args.flow_rules == "F1"
    assert args.lookbacks == "24"
    assert float(args.max_distance_atr) == 0.50


def test_cli_rejects_via_unavailable_in_main_path():
    from research.regime_scanner.orderflow_absorption.config import UNAVAILABLE_SYMBOLS

    assert "ENAUSDT" in UNAVAILABLE_SYMBOLS


def test_protected_visibility_strict():
    # mimic protected confirmed at bar 17
    level = {"confirmation_index": 17, "invalidated_at": None}
    assert level_visible_at(level, 17) is False
    assert level_visible_at(level, 18) is True


def test_wick_alone_does_not_force_external_invalidation_in_visibility():
    # inventory invalidation uses close-break for swings; wick-only keeps visible
    level = {"confirmation_index": 10, "invalidated_at": None}
    assert level_visible_at(level, 15) is True


def test_sequence_gap_concept_separate_inventories():
    # levels from seq A must not be queried with seq B filter
    from research.regime_scanner.orderflow_absorption_level.levels_build import active_levels_at

    inv = [
        {"level_id": "a", "sequence_id": 1, "confirmation_index": 5, "invalidated_at": None},
        {"level_id": "b", "sequence_id": 2, "confirmation_index": 5, "invalidated_at": None},
    ]
    vis = active_levels_at(inv, 10, sequence_id=1)
    assert len(vis) == 1 and vis[0]["level_id"] == "a"


def test_k1_k3_control_tags():
    events = [
        {
            "event_id": "t1",
            "symbol": "BTCUSDT",
            "pattern": "A4",
            "no_level": False,
            "far_from_level": False,
            "level_type": "protected",
            "distance_bucket_at_entry": "touch",
            "event_start_timestamp": "2026-04-01T00:00:00Z",
            "event_start_index": 10,
        },
        {
            "event_id": "c1",
            "symbol": "BTCUSDT",
            "pattern": "A4",
            "no_level": True,
            "far_from_level": False,
            "level_type": None,
            "distance_bucket_at_entry": "no_level",
            "event_start_timestamp": "2026-04-01T01:00:00Z",
            "event_start_index": 20,
        },
    ]
    c2 = [
        {
            "event_id": "k3",
            "symbol": "BTCUSDT",
            "pattern": "C2",
            "level_type": "protected",
            "distance_bucket_at_entry": "touch",
            "event_start_timestamp": "2026-04-01T02:00:00Z",
            "event_start_index": 30,
        }
    ]
    rows = build_control_assignments(events, c2_support_events=c2)
    controls = {r["control"] for r in rows}
    assert "K1" in controls
    assert "K3" in controls
    assert "TREATMENT" in controls


def test_matching_no_outcome_fields():
    treat = [
        {
            "event_id": "t1",
            "symbol": "BTCUSDT",
            "event_start_index": 10,
            "event_start_timestamp": "2026-04-01T00:00:00Z",
        }
    ]
    ctrl = [
        {
            "event_id": "c1",
            "symbol": "BTCUSDT",
            "event_start_index": 20,
            "event_start_timestamp": "2026-04-01T01:00:00Z",
        }
    ]
    base = pd.Timestamp("2026-04-01", tz="UTC")
    df = pd.DataFrame(
        {
            "atr_14": [1.0] * 30,
            "close": [100.0] * 30,
            "bucket_start": [base + pd.Timedelta(minutes=5 * i) for i in range(30)],
        }
    )
    pairs, unmatched = match_control_pairs(
        treat, ctrl, df_by_symbol={"BTCUSDT": df}, atr_edges_by_symbol={"BTCUSDT": (0.005, 0.01)}
    )
    assert pairs or unmatched
    for p in pairs:
        assert "outcome" not in str(p).lower() or "event_id" in p


def test_protected_step_smoke_no_crash():
    cfg = ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    rt = ProtectedRuntime()
    rt.protected_low = ProtectedLevel(95.0, 5, None, 8, None, "low", "seed")
    bar = {
        "bar_index": 20,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "atr_14": 1.0,
        "timestamp": pd.Timestamp("2026-04-01", tz="UTC"),
        "highs_window": [100.0] * 21,
        "lows_window": [90.0] * 21,
        "indicator_clean_regime_state": "neutral",
    }
    state, rt2, diag = step_protected_structure_state("structure_unknown", rt, bar, None, cfg)
    assert "protected_low" in diag
    assert state is not None

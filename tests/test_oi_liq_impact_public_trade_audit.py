"""Tests for public trade impact compression audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.classify import (
    classify_row,
    classify_window,
)
from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.constants import (
    AGGRESSIVE_NOTIONAL_SIDE,
    CATEGORY_FALLING_FLOW_LOW_IMPACT,
    CATEGORY_INVALID_OR_ZERO_FLOW,
    CATEGORY_SUSTAINED_FLOW_COMPRESSION,
    VERDICT_BLOCKED,
)
from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.outcomes import (
    adverse_extension,
    classification_window_end_second,
    compute_post_compression_outcomes,
    episode_signed_move,
)
from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.runner import run_public_trade_audit
from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.schema import (
    check_input_schema,
    required_impact_compression_columns,
)


def _complete_impact_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "cluster_id": "oildisc_cluster:BTCUSDT:LONG:2026-08-20T12:46:00Z",
        "direction": "LONG",
        "data_abort": False,
    }
    for prefix in ("first5", "last5", "first10", "last10", "first_half", "second_half"):
        row[f"{prefix}_aggressive_notional"] = 100.0
        row[f"{prefix}_impact_per_notional"] = 0.01
        row[f"{prefix}_trades_present"] = True
    row.update(overrides)
    return row


def _write_minimal_inputs(tmp_path: Path, impact_rows: list[dict[str, object]]) -> None:
    pd.DataFrame(impact_rows).to_csv(tmp_path / "impact_compression_metrics.csv", index=False)
    pd.DataFrame(
        [
            {
                "cluster_id": impact_rows[0]["cluster_id"],
                "symbol": "BTCUSDT",
                "direction": impact_rows[0]["direction"],
                "cluster_start": "2026-08-20T12:46:00Z",
                "cluster_end": "2026-08-20T12:46:00Z",
                "primary_candidate_id": "x",
                "candidate_ids": "x",
                "flush_minutes": 1,
                "data_abort": False,
                "abort_reason": "",
                "anchor_second": "2026-08-20T12:45:59Z",
                "anchor_wall_price": 100.0,
                "anchor_wall_qty": 1.0,
                "anchor_mid": 100.0,
                "anchor_spread_bps": 1.0,
                "anchor_directional_depth_l50": 10.0,
                "anchor_directional_imbalance_l50": 0.1,
                "anchor_directional_ofi": 1.0,
                "adverse_extreme_second": "2026-08-20T12:46:30Z",
                "adverse_extreme_mid": 99.0,
            }
        ]
    ).to_csv(tmp_path / "proxy_events.csv", index=False)
    pd.DataFrame(
        [
            {
                "cluster_id": impact_rows[0]["cluster_id"],
                "direction": impact_rows[0]["direction"],
                "reclaim_anchor": "PRE_FLUSH_CLOSE",
                "anchor_price": 100.0,
                "mark_minutes": 5,
                "first_1s_proxy_reclaim_at": "",
                "first_1m_close_reclaim_at": "",
                "minutes_to_1s_reclaim": "",
                "proxy_reclaim_within_mark": False,
            }
        ]
    ).to_csv(tmp_path / "proxy_reclaims.csv", index=False)
    pd.DataFrame(
        [
            {
                "control_id": "c1",
                "matched_cluster_id": impact_rows[0]["cluster_id"],
                "symbol": "BTCUSDT",
                "direction": impact_rows[0]["direction"],
                "control_minute": "2026-08-20T10:00:00Z",
                "match_distance": 0.1,
                "hour": 10,
                "control_displacement_pct": 0.1,
                "cluster_target_displacement_pct": 0.1,
            }
        ]
    ).to_csv(tmp_path / "matched_controls.csv", index=False)
    (tmp_path / "proxy_manifest.json").write_text(
        json.dumps({"cluster_count": 1, "format_version": "test"}),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "cluster_id": impact_rows[0]["cluster_id"],
                "direction": impact_rows[0]["direction"],
                "mark_minutes": 5,
                "mark_second": "2026-08-20T12:51:00Z",
                "depth_vs_anchor": 1.0,
                "depth_vs_adverse": 1.0,
                "recovery_branches": "DEPTH",
                "aggregate_depth_recovery_observed": True,
            }
        ]
    ).to_csv(tmp_path / "aggregate_l2_recovery.csv", index=False)
    pd.DataFrame(
        [
            {
                "cluster_id": impact_rows[0]["cluster_id"],
                "direction": impact_rows[0]["direction"],
                "flip_tradeflow_second": "",
                "flip_ofi_second": "",
                "flip_microprice_second": "",
                "flip_imbalance_second": "",
                "flip_component_count": 0,
                "first_any_flip_second": "",
            }
        ]
    ).to_csv(tmp_path / "orderflow_flip_metrics.csv", index=False)


def _timeline_rows(cluster_id: str, direction: str) -> pd.DataFrame:
    rows = []
    for i in range(180):
        minute = 46 + (i // 60)
        second = i % 60
        sec = f"2026-08-20T12:{minute:02d}:{second:02d}Z"
        rows.append(
            {
                "cluster_id": cluster_id,
                "symbol": "BTCUSDT",
                "direction": direction,
                "second": sec,
                "phase": "POST_CLUSTER",
                "is_genuine": True,
                "quality_flags": "",
                "mid_price": 100.0 - i * 0.1 if direction == "LONG" else 100.0 + i * 0.1,
                "microprice": 100.0,
                "spread_bps": 1.0,
                "best_bid_price": 99.9,
                "best_ask_price": 100.1,
                "dominant_wall_price": 100.0,
                "dominant_wall_qty": 1.0,
                "dominant_wall_bps_dist": 1.0,
                "wall_status": "DOMINANT_WALL_STABLE_EXACT",
                "wall_near_1tick": True,
                "wall_near_2tick": True,
                "wall_near_3tick": True,
                "directional_depth_l50": 10.0,
                "directional_imbalance_l50": 0.1,
                "directional_ofi": 1.0,
                "side_qty_added": 1.0,
                "side_qty_removed": 1.0,
                "aggressive_notional_1s": 50.0 if i < 10 else (100.0 if i < 150 else 0.0),
                "processed_updates": 1,
            }
        )
    return pd.DataFrame(rows)


def test_long_uses_aggressive_sells_semantics() -> None:
    assert AGGRESSIVE_NOTIONAL_SIDE["LONG"] == "sell_notional"


def test_short_uses_aggressive_buys_semantics() -> None:
    assert AGGRESSIVE_NOTIONAL_SIDE["SHORT"] == "buy_notional"


def test_equal_flow_falling_impact_is_sustained_compression() -> None:
    category = classify_window(
        first_notional=100.0,
        last_notional=100.0,
        first_impact=0.02,
        last_impact=0.01,
        first_trades_present=True,
        last_trades_present=True,
        data_abort=False,
    )
    assert category == CATEGORY_SUSTAINED_FLOW_COMPRESSION


def test_rising_flow_falling_impact_is_sustained_compression() -> None:
    category = classify_window(
        first_notional=100.0,
        last_notional=150.0,
        first_impact=0.02,
        last_impact=0.01,
        first_trades_present=True,
        last_trades_present=True,
        data_abort=False,
    )
    assert category == CATEGORY_SUSTAINED_FLOW_COMPRESSION


def test_falling_flow_falling_impact_not_sustained_compression() -> None:
    category = classify_window(
        first_notional=100.0,
        last_notional=50.0,
        first_impact=0.02,
        last_impact=0.01,
        first_trades_present=True,
        last_trades_present=True,
        data_abort=False,
    )
    assert category == CATEGORY_FALLING_FLOW_LOW_IMPACT


def test_zero_notional_is_invalid_without_division() -> None:
    row = classify_row(
        _complete_impact_row(first5_aggressive_notional=0.0),
        "first5_last5",
        "first5",
        "last5",
    )
    assert row["category"] == CATEGORY_INVALID_OR_ZERO_FLOW
    assert row["notional_ratio_last_over_first"] is None


def test_missing_trades_are_invalid() -> None:
    category = classify_window(
        first_notional=100.0,
        last_notional=100.0,
        first_impact=0.02,
        last_impact=0.01,
        first_trades_present=False,
        last_trades_present=True,
        data_abort=False,
    )
    assert category == CATEGORY_INVALID_OR_ZERO_FLOW


def test_outcome_starts_after_classification_window_end() -> None:
    cluster_id = "oildisc_cluster:BTCUSDT:LONG:2026-08-20T12:46:00Z"
    timeline = _timeline_rows(cluster_id, "LONG")
    end = classification_window_end_second(
        timeline,
        direction="LONG",
        window_size=5,
        last_prefix="last5",
    )
    assert end == "2026-08-20T12:48:29Z"
    classification = classify_row(
        _complete_impact_row(
            first5_impact_per_notional=0.02,
            last5_impact_per_notional=0.01,
        ),
        "first5_last5",
        "first5",
        "last5",
    )
    outcomes = compute_post_compression_outcomes(
        [classification],
        events=pd.DataFrame(
            [
                {
                    "cluster_id": cluster_id,
                    "direction": "LONG",
                    "anchor_mid": 100.0,
                }
            ]
        ),
        reclaims=pd.DataFrame(
            columns=[
                "cluster_id",
                "reclaim_anchor",
                "anchor_price",
                "mark_minutes",
            ]
        ),
        timeline_by_cluster={cluster_id: timeline},
        horizons=(1,),
    )
    assert outcomes
    assert outcomes[0]["outcome_start_second"] == end
    assert outcomes[0]["outcome_start_second"] >= "2026-08-20T12:46:09Z"


def test_long_short_outcomes_are_direction_mirrored() -> None:
    long_move = episode_signed_move(100.0, 101.0, "LONG")
    short_move = episode_signed_move(100.0, 99.0, "SHORT")
    assert long_move == pytest.approx(1.0)
    assert short_move == pytest.approx(1.0)
    assert adverse_extension(99.0, 100.0, "LONG") == pytest.approx(1.0)
    assert adverse_extension(101.0, 100.0, "SHORT") == pytest.approx(1.0)


def test_future_outcomes_do_not_change_category() -> None:
    base = classify_row(
        _complete_impact_row(
            first5_impact_per_notional=0.02,
            last5_impact_per_notional=0.01,
        ),
        "first5_last5",
        "first5",
        "last5",
    )
    mutated = classify_row(
        _complete_impact_row(
            first5_impact_per_notional=0.02,
            last5_impact_per_notional=0.01,
            first10_impact_per_notional=0.5,
            last10_impact_per_notional=0.001,
        ),
        "first5_last5",
        "first5",
        "last5",
    )
    assert base["category"] == mutated["category"]


def test_first5_last5_and_first10_last10_stay_separate() -> None:
    row = _complete_impact_row(
        first5_impact_per_notional=0.02,
        last5_impact_per_notional=0.01,
        first10_impact_per_notional=0.02,
        last10_impact_per_notional=0.02,
    )
    first5 = classify_row(row, "first5_last5", "first5", "last5")
    first10 = classify_row(row, "first10_last10", "first10", "last10")
    assert first5["category"] == CATEGORY_SUSTAINED_FLOW_COMPRESSION
    assert first10["category"] != CATEGORY_SUSTAINED_FLOW_COMPRESSION


def test_deterministic_outputs(tmp_path: Path) -> None:
    inp = tmp_path / "in"
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    inp.mkdir()
    row = _complete_impact_row(
        first5_impact_per_notional=0.02,
        last5_impact_per_notional=0.01,
    )
    _write_minimal_inputs(inp, [row])
    timeline = _timeline_rows(str(row["cluster_id"]), "LONG")
    timeline.to_csv(inp / "proxy_timeline_1s.csv", index=False)
    run_public_trade_audit(input_dir=inp, output_dir=out1)
    run_public_trade_audit(input_dir=inp, output_dir=out2)
    h1 = hashlib.sha256((out1 / "impact_classification.csv").read_bytes()).hexdigest()
    h2 = hashlib.sha256((out2 / "impact_classification.csv").read_bytes()).hexdigest()
    assert h1 == h2


def test_real_f3_schema_is_blocked(tmp_path: Path) -> None:
    source = Path("results/oi_liq_impact_l2/aggregate_wall_proxy_btc_f3")
    if not source.is_dir():
        pytest.skip("BTC F3 artifacts not present")
    schema = check_input_schema(source)
    assert schema.ok is False
    assert "first5_aggressive_notional" in schema.missing_fields
    assert "first10_impact_per_notional" in schema.missing_fields


def test_blocked_run_writes_manifest_and_verdict(tmp_path: Path) -> None:
    inp = tmp_path / "in"
    out = tmp_path / "out"
    inp.mkdir()
    pd.DataFrame(
        [
            {
                "cluster_id": "x",
                "direction": "LONG",
                "data_abort": False,
                "first5_impact_per_notional": 0.01,
                "last5_impact_per_notional": 0.005,
            }
        ]
    ).to_csv(inp / "impact_compression_metrics.csv", index=False)
    for name in (
        "proxy_events.csv",
        "proxy_reclaims.csv",
        "matched_controls.csv",
    ):
        pd.DataFrame([{"cluster_id": "x"}]).to_csv(inp / name, index=False)
    (inp / "proxy_manifest.json").write_text("{}", encoding="utf-8")
    result = run_public_trade_audit(input_dir=inp, output_dir=out)
    assert result.verdict == VERDICT_BLOCKED
    manifest = json.loads((out / "audit_manifest.json").read_text(encoding="utf-8"))
    assert manifest["verdict"] == VERDICT_BLOCKED
    assert manifest["missing_impact_compression_fields"]
    assert len(required_impact_compression_columns()) > 0

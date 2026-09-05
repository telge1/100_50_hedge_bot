"""Fast offline tests for OI/liquidation/impact/L2 discovery."""

from __future__ import annotations

import csv
import json
import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.discovery import (
    DiscoveryInputs,
    build_symbol_discovery,
    run_discovery,
)
from orderbook_analyse.oi_liq_impact_l2.discovery_io import load_discovery_inputs

UTC = timezone.utc
START = datetime(2026, 8, 20, 12, 33, tzinfo=UTC)


def _frames(*, gap_at: int | None = None, genuine_at_one: int = 50):
    minutes = pd.date_range(START, periods=6, freq="1min", tz="UTC")
    candles = pd.DataFrame(
        {
            "open_time": minutes,
            "open": [100, 100, 99, 100, 101, 101],
            "high": [101, 100, 101, 102, 102, 102],
            "low": [98, 99, 98, 99, 100, 100],
            "close": [98, 99, 100, 101, 101, 101],
            "volume": [1] * 6,
        }
    )
    trades = pd.DataFrame(
        {
            "minute": minutes[:3],
            "trade_count": [10, 20, 10],
            "buy_notional": [50.0, 50.0, 300.0],
            "sell_notional": [100.0, 200.0, 50.0],
            "buy_size": [1.0, 1.0, 3.0],
            "sell_size": [2.0, 4.0, 1.0],
            "total_notional": [150.0, 250.0, 350.0],
        }
    )
    oi = pd.DataFrame(
        {
            "minute": minutes[:3],
            "open_interest": [1000.0, 900.0, 800.0],
            "open_interest_value": [1000.0, 900.0, 800.0],
            "state_valid": [1, 1, 1],
            "samples": [12, 12, 12],
        }
    )
    liquidations = pd.DataFrame(
        {
            "minute": minutes[1:3],
            "liquidated_long_count": [2, 0],
            "liquidated_short_count": [0, 3],
            "liquidated_long_notional": [20.0, 0.0],
            "liquidated_short_notional": [0.0, 30.0],
        }
    )
    orderbook = pd.DataFrame(
        {
            "minute": minutes[:3],
            "seconds": [60, 60, 60],
            "valid_seconds": [60, 60, 60],
            "invalid_seconds": [0, 0, 0],
            "carried_forward_seconds": [10, 60 - genuine_at_one, 5],
            "genuine_seconds": [50, genuine_at_one, 55],
            "genuine_spread_bps_mean": [2.0, 1.8, 1.7],
            "genuine_imbalance_l50_mean": [-0.4, -0.1, 0.2],
            "genuine_bid_depth_l50_mean": [100.0, 150.0, 200.0],
            "genuine_ask_depth_l50_mean": [200.0, 180.0, 220.0],
            "genuine_ofi_sum": [-10.0, 20.0, -30.0],
            "genuine_bid_qty_added": [10.0, 40.0, 5.0],
            "genuine_bid_qty_removed": [20.0, 10.0, 20.0],
            "genuine_ask_qty_added": [20.0, 10.0, 50.0],
            "genuine_ask_qty_removed": [10.0, 20.0, 10.0],
            "genuine_bid_add_count": [1, 4, 1],
            "genuine_bid_remove_count": [2, 1, 2],
            "genuine_ask_add_count": [2, 1, 5],
            "genuine_ask_remove_count": [1, 2, 1],
        }
    )
    if gap_at is not None:
        orderbook = orderbook.drop(index=gap_at).reset_index(drop=True)
    return candles, trades, oi, liquidations, orderbook


def _build_result(
    candles: pd.DataFrame,
    trades: pd.DataFrame,
    oi: pd.DataFrame,
    liquidations: pd.DataFrame,
    orderbook: pd.DataFrame,
):
    return build_symbol_discovery(
        DiscoveryInputs(
            symbol="AAAUSDT",
            start=START,
            end=START + timedelta(minutes=3),
            label_horizon_minutes=2,
            candles=candles,
            trades=trades,
            open_interest=oi,
            liquidations=liquidations,
            orderbook=orderbook,
        )
    )


def _result(*, gap_at: int | None = None, genuine_at_one: int = 50):
    return _build_result(
        *_frames(gap_at=gap_at, genuine_at_one=genuine_at_one)
    )


def _row(result, minute: str, direction: str):
    return next(
        row
        for row in result.minute_features
        if row["minute"] == minute and row["direction"] == direction
    )


def test_first_minute_positive_additions_do_not_confirm_recovery() -> None:
    candles, trades, oi, liquidations, orderbook = _frames()
    orderbook.loc[0, "genuine_bid_qty_removed"] = 0.0
    orderbook.loc[0, "genuine_ask_qty_removed"] = 0.0
    result = _build_result(candles, trades, oi, liquidations, orderbook)
    for direction in ("LONG", "SHORT"):
        row = _row(result, "2026-08-20T12:33:00Z", direction)
        assert row["directional_net_add"] > 0
        assert row["directional_depth_change"] is None
        assert row["directional_imbalance_change"] is None
        assert row["directional_net_add_change"] is None
        assert row["l2_recovery_observed"] is False


def test_long_flush_compression_and_l2_recovery() -> None:
    result = _result()
    row = _row(result, "2026-08-20T12:34:00Z", "LONG")
    short = _row(result, "2026-08-20T12:34:00Z", "SHORT")
    assert row["directional_flush_observed"] is True
    assert row["impact_compression_observed"] is True
    assert row["l2_recovery_observed"] is True
    assert short["l2_recovery_observed"] is False
    assert row["stage_reached"] == "L2_RECOVERY_OBSERVED"
    assert row["liquidation_count"] == 2
    assert row["aggressive_notional"] == 200.0


def test_short_uses_short_liquidations_and_aggressive_buys() -> None:
    result = _result()
    row = _row(result, "2026-08-20T12:35:00Z", "SHORT")
    assert row["directional_flush_observed"] is True
    assert row["liquidation_count"] == 3
    assert row["liquidation_notional"] == 30.0
    assert row["aggressive_notional"] == 300.0


def test_short_recovery_does_not_automatically_confirm_long() -> None:
    candles, trades, oi, liquidations, orderbook = _frames()
    # At minute 1 only ask/resistance and short-directed imbalance/net-add improve.
    orderbook.loc[1, "genuine_bid_depth_l50_mean"] = 80.0
    orderbook.loc[1, "genuine_ask_depth_l50_mean"] = 250.0
    orderbook.loc[1, "genuine_imbalance_l50_mean"] = -0.5
    orderbook.loc[1, "genuine_bid_qty_added"] = 5.0
    orderbook.loc[1, "genuine_bid_qty_removed"] = 30.0
    orderbook.loc[1, "genuine_ask_qty_added"] = 50.0
    orderbook.loc[1, "genuine_ask_qty_removed"] = 10.0
    result = _build_result(candles, trades, oi, liquidations, orderbook)
    long = _row(result, "2026-08-20T12:34:00Z", "LONG")
    short = _row(result, "2026-08-20T12:34:00Z", "SHORT")
    assert long["l2_recovery_observed"] is False
    assert short["l2_recovery_observed"] is True
    assert long["directional_net_add"] != short["directional_net_add"]


def test_net_add_recovery_compares_with_previous_genuine_minute() -> None:
    candles, trades, oi, liquidations, orderbook = _frames()
    # Hold depth and imbalance constant. Only bid net-add improves.
    orderbook.loc[1, "genuine_bid_depth_l50_mean"] = orderbook.loc[
        0, "genuine_bid_depth_l50_mean"
    ]
    orderbook.loc[1, "genuine_imbalance_l50_mean"] = orderbook.loc[
        0, "genuine_imbalance_l50_mean"
    ]
    result = _build_result(candles, trades, oi, liquidations, orderbook)
    long = _row(result, "2026-08-20T12:34:00Z", "LONG")
    assert long["directional_depth_change"] == 0
    assert long["directional_imbalance_change"] == 0
    assert long["directional_net_add"] == 30.0
    assert long["directional_net_add_change"] == 40.0
    assert long["l2_recovery_observed"] is True


def test_carried_forward_never_confirms_l2_dynamics() -> None:
    result = _result(genuine_at_one=0)
    row = _row(result, "2026-08-20T12:34:00Z", "LONG")
    after = _row(result, "2026-08-20T12:35:00Z", "LONG")
    assert row["ob_carried_forward_rate"] == 1.0
    assert row["ob_genuine_seconds"] == 0
    assert row["l2_recovery_observed"] is False
    assert after["directional_depth_change"] is None
    assert after["directional_net_add_change"] is None
    assert after["l2_recovery_observed"] is False


def test_data_gap_resets_compression_history() -> None:
    result = _result(gap_at=1)
    gap_row = _row(result, "2026-08-20T12:34:00Z", "LONG")
    after_gap = _row(result, "2026-08-20T12:35:00Z", "SHORT")
    assert gap_row["technical_gap"] is True
    assert gap_row["stage_reached"] == "NONE"
    assert after_gap["previous_impact_per_aggressive_notional"] is None
    assert after_gap["impact_compression_observed"] is False
    assert after_gap["directional_depth_change"] is None
    assert after_gap["directional_imbalance_change"] is None
    assert after_gap["directional_net_add_change"] is None
    assert after_gap["l2_recovery_observed"] is False


def test_invalid_oi_resets_oi_delta() -> None:
    candles, trades, oi, liquidations, orderbook = _frames()
    oi.loc[1, "state_valid"] = 0
    result = _build_result(candles, trades, oi, liquidations, orderbook)
    after_invalid = _row(result, "2026-08-20T12:35:00Z", "SHORT")
    assert after_invalid["oi_delta_abs_1m"] is None
    assert after_invalid["directional_flush_observed"] is False


def test_future_labels_are_isolated_from_predictors() -> None:
    result = _result()
    assert result.candidates
    assert len(result.labels) == len(result.candidates)
    assert all(label["label_status"] == "COMPLETE" for label in result.labels)
    forbidden = {"mfe_pct", "mae_pct", "forward_return_pct", "entry_price"}
    assert forbidden.isdisjoint(result.minute_features[0])
    assert forbidden.isdisjoint(result.candidates[0])


def test_run_writes_deterministic_artifacts(tmp_path: Path) -> None:
    universe = tmp_path / "universe.json"
    universe.write_text(
        json.dumps({"name": "test", "n": 1, "symbols": ["AAAUSDT"]}),
        encoding="utf-8",
    )

    def loader(client, *, symbol, start, end, label_end):
        candles, trades, oi, liquidations, orderbook = _frames()
        return {
            "candles": candles,
            "trades": trades,
            "open_interest": oi,
            "liquidations": liquidations,
            "orderbook": orderbook,
        }

    snapshots = []
    for name in ("out1", "out2"):
        output = tmp_path / name
        result = run_discovery(
            client=object(),
            loader=loader,
            universe_path=universe,
            start=START,
            end=START + timedelta(minutes=3),
            label_horizon_minutes=2,
            output_dir=output,
        )
        assert result.symbol_count == 1
        snapshots.append(
            {path.name: path.read_bytes() for path in sorted(output.iterdir())}
        )
    assert snapshots[0] == snapshots[1]
    assert set(snapshots[0]) == {
        "discovery_manifest.json",
        "distribution_summary.json",
        "flush_candidates.csv",
        "labels_sidecar.csv",
        "minute_features.csv",
        "quality_by_symbol.csv",
    }
    with (tmp_path / "out1" / "minute_features.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 6
    manifest = json.loads((tmp_path / "out1" / "discovery_manifest.json").read_text())
    assert manifest["format_version"] == "oi_liq_impact_l2_discovery/v2"
    assert manifest["threshold_search"] is False
    assert manifest["profitability_claim"] is False
    assert manifest["source_tables"]["orderbook"] == (
        "orderbook_analysis.orderbook_features_1s_v2"
    )
    ob = manifest["orderbook_contract"]
    assert ob["parser_version"] == "ob200_v3"
    assert ob["depth"] == 200
    assert "carried_forward" in ob["genuine_seconds_condition"]
    assert "never contribute" in ob["carried_forward_policy"]
    assert ob["l2_side_by_direction"] == {
        "LONG": "bid/support",
        "SHORT": "ask/resistance",
    }
    assert "previous directional net-add flow" in ob["l2_recovery_observed"]
    direction = manifest["direction_contract"]
    assert direction["liquidation_side_by_direction"] == {
        "LONG": "LIQUIDATED_LONG",
        "SHORT": "LIQUIDATED_SHORT",
    }
    assert direction["aggressor_side_by_direction"] == {
        "LONG": "Sell",
        "SHORT": "Buy",
    }


def test_cli_import_does_not_create_database_client() -> None:
    namespace = runpy.run_path(
        "scripts/run_oi_liq_impact_l2_discovery.py",
        run_name="discovery_cli_import_test",
    )
    assert callable(namespace["build_parser"])
    assert callable(namespace["main"])


def test_orderbook_query_uses_manifest_contract_constants() -> None:
    class _Result:
        result_rows: list[tuple] = []

    class _Client:
        def __init__(self) -> None:
            self.sql: list[str] = []

        def query(self, sql, parameters, settings):
            self.sql.append(sql)
            return _Result()

    client = _Client()
    load_discovery_inputs(
        client,
        symbol="AAAUSDT",
        start=START,
        end=START + timedelta(minutes=3),
        label_end=START + timedelta(minutes=5),
    )
    orderbook_sql = client.sql[-1]
    assert "orderbook_analysis.orderbook_features_1s_v2" in orderbook_sql
    assert "parser_version = 'ob200_v3'" in orderbook_sql
    assert "depth = 200" in orderbook_sql
    assert "is_valid = 1" in orderbook_sql
    assert "carried_forward" in orderbook_sql

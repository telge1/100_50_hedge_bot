"""Integration checks for APT continuous exit quality after Phase C."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.backtests.continuous_reentry_backtest import (
    aggregate_continuous_results,
    run_continuous_reentry_backtests,
)
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.pnl_coverage_audit import apply_trade_exit_quality


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="APT feather file not available",
)
def test_apt_continuous_no_negative_pnl_closed_trades(tmp_path: Path) -> None:
    from research.backtests.candle_loader import load_candles_for_symbol

    candles = load_candles_for_symbol("APTUSDT", limit=20000)
    payload = run_continuous_reentry_backtests(
        symbol="APTUSDT",
        direction="long",
        candles=candles,
        config_source="live",
        fill_model="conservative",
        output_dir=tmp_path,
        write_json=False,
        write_csv=False,
    )
    for result in payload["results"]:
        apply_trade_exit_quality(result)
        assert result.exit_quality != "closed_negative_pnl", (
            f"trade {result.trade_number} closed negative: pnl={result.realized_pnl}"
        )
        assert result.exit_quality != "closed_undercovered_final_exit"

    aggregates = aggregate_continuous_results(payload["results"])
    assert aggregates
    row = aggregates[0]
    assert row["negative_pnl_closed_count"] == 0
    assert row["undercovered_final_exit_count"] == 0


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="APT feather file not available",
)
@pytest.mark.parametrize("start_index", [0, 8324, 9862])
def test_apt_trade_starts_do_not_close_negative_pnl(start_index: int) -> None:
    from research.backtests.candle_loader import load_candles_for_symbol

    candles = load_candles_for_symbol("APTUSDT", limit=20000)
    if start_index >= len(candles):
        pytest.skip("start index beyond loaded candles")
    window = candles[start_index : start_index + 5000]
    result = run_historical_backtest(
        "APTUSDT",
        "long",
        window,
        config_source="live",
        fill_model="conservative",
    )
    apply_trade_exit_quality(result)
    if result.final_status == "closed":
        assert result.exit_quality != "closed_negative_pnl", (
            f"start={start_index} closed negative pnl={result.realized_pnl}"
        )
        assert result.exit_quality != "closed_undercovered_final_exit"

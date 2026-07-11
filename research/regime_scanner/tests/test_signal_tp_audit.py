from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.signal_tp_audit import (
    ALL_SIGNAL_REGIMES,
    BEARISH_SIGNAL_REGIMES,
    BULLISH_SIGNAL_REGIMES,
    SIGNAL_REGIMES,
    build_arg_parser,
    build_signal_tp_summary,
    get_signal_side,
    is_entry_transition,
    observe_long_tp,
    observe_tp,
    parse_sides,
    scan_signal_tp,
    write_outputs,
)


def _ohlcv(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.reset_index(drop=True)


def test_get_signal_side_mapping() -> None:
    assert get_signal_side("bullish_trend") == "long"
    assert get_signal_side("strong_bullish_trend") == "long"
    assert get_signal_side("bearish_trend") == "short"
    assert get_signal_side("strong_bearish_trend") == "short"
    assert get_signal_side("range") is None
    assert BEARISH_SIGNAL_REGIMES == {"bearish_trend", "strong_bearish_trend"}
    assert SIGNAL_REGIMES == ALL_SIGNAL_REGIMES == BULLISH_SIGNAL_REGIMES | BEARISH_SIGNAL_REGIMES


def test_parse_sides_default_and_variants() -> None:
    assert parse_sides("long") == ("long",)
    assert parse_sides("short") == ("short",)
    assert parse_sides("long,short") == ("long", "short")
    assert parse_sides("short,long") == ("short", "long")
    assert parse_sides(None) == ("long",)
    with pytest.raises(argparse.ArgumentTypeError):
        parse_sides("both")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_sides("long,medium")


def test_long_tp_hit_next_candle() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.1,
                "low": 99.9,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.30,
                "low": 99.95,
                "close": 100.2,
                "volume": 1.0,
            },
        ]
    )
    out = observe_tp(
        candles,
        entry_index=0,
        entry_price=100.0,
        tp_pct=0.25,
        max_hold_candles=48,
        side="long",
    )
    assert out["tp_reached"] is True
    assert out["candles_to_tp"] == 1
    assert out["tp_price"] == pytest.approx(100.25)
    assert out["max_favorable_excursion_pct"] == pytest.approx(0.30)
    assert out["max_adverse_excursion_pct"] == pytest.approx(-0.05)
    wrap = observe_long_tp(
        candles,
        entry_index=0,
        entry_price=100.0,
        tp_pct=0.25,
        max_hold_candles=48,
    )
    assert wrap["tp_reached"] is True


def test_long_tp_miss_within_window() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.10,
                "low": 99.80,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:10:00Z",
                "open": 100.0,
                "high": 100.20,
                "low": 99.70,
                "close": 100.0,
                "volume": 1.0,
            },
        ]
    )
    out = observe_tp(
        candles,
        entry_index=0,
        entry_price=100.0,
        tp_pct=0.25,
        max_hold_candles=2,
        side="long",
    )
    assert out["tp_reached"] is False
    assert out["candles_to_tp"] is None
    assert out["max_favorable_excursion_pct"] == pytest.approx(0.20)
    assert out["max_adverse_excursion_pct"] == pytest.approx(-0.30)


def test_short_tp_hit_next_candle() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.1,
                "low": 99.9,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.05,
                "low": 99.75,
                "close": 99.8,
                "volume": 1.0,
            },
        ]
    )
    out = observe_tp(
        candles,
        entry_index=0,
        entry_price=100.0,
        tp_pct=0.25,
        max_hold_candles=48,
        side="short",
    )
    assert out["tp_reached"] is True
    assert out["candles_to_tp"] == 1
    assert out["tp_price"] == pytest.approx(99.75)
    assert out["max_favorable_excursion_pct"] == pytest.approx(0.25)
    assert out["max_adverse_excursion_pct"] == pytest.approx(-0.05)


def test_short_tp_miss_within_window() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.20,
                "low": 99.90,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:10:00Z",
                "open": 100.0,
                "high": 100.30,
                "low": 99.80,
                "close": 100.0,
                "volume": 1.0,
            },
        ]
    )
    out = observe_tp(
        candles,
        entry_index=0,
        entry_price=100.0,
        tp_pct=0.25,
        max_hold_candles=2,
        side="short",
    )
    assert out["tp_reached"] is False
    assert out["candles_to_tp"] is None
    assert out["max_favorable_excursion_pct"] == pytest.approx(0.20)
    assert out["max_adverse_excursion_pct"] == pytest.approx(-0.30)


def test_short_candles_to_tp_counts_from_next_candle() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.10,
                "low": 99.90,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:10:00Z",
                "open": 100.0,
                "high": 100.05,
                "low": 99.75,
                "close": 99.8,
                "volume": 1.0,
            },
        ]
    )
    out = observe_tp(
        candles,
        entry_index=0,
        entry_price=100.0,
        tp_pct=0.25,
        max_hold_candles=48,
        side="short",
    )
    assert out["tp_reached"] is True
    assert out["candles_to_tp"] == 2


def test_short_mfe_mae_formulas() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1.0,
            },
        ]
    )
    out = observe_tp(
        candles,
        entry_index=0,
        entry_price=100.0,
        tp_pct=5.0,
        max_hold_candles=1,
        side="short",
    )
    assert out["max_favorable_excursion_pct"] == pytest.approx(1.0)
    assert out["max_adverse_excursion_pct"] == pytest.approx(-1.0)


def test_short_adverse_thresholds() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 102.5,
                "low": 99.5,
                "close": 100.0,
                "volume": 1.0,
            },
        ]
    )
    out = observe_tp(
        candles,
        entry_index=0,
        entry_price=100.0,
        tp_pct=5.0,
        max_hold_candles=1,
        side="short",
    )
    assert out["touched_minus_0_50_pct"] is True
    assert out["touched_minus_1_00_pct"] is True
    assert out["touched_minus_2_00_pct"] is True


def test_observation_skips_entry_candle_for_both_sides() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.10,
                "low": 99.90,
                "close": 100.0,
                "volume": 1.0,
            },
        ]
    )
    long_out = observe_tp(
        candles, entry_index=0, entry_price=100.0, tp_pct=0.25, max_hold_candles=1, side="long"
    )
    short_out = observe_tp(
        candles, entry_index=0, entry_price=100.0, tp_pct=0.25, max_hold_candles=1, side="short"
    )
    assert long_out["tp_reached"] is False
    assert short_out["tp_reached"] is False


def _scan_with_regimes(
    candles: pd.DataFrame,
    regimes: dict[int, str],
    *,
    sides: str,
    tp_pct: float = 0.25,
    max_hold_candles: int = 48,
) -> dict:
    def regime_fn(index: int) -> dict:
        return {"combined_regime": regimes[index]}

    return scan_signal_tp(
        candles,
        history_candles=1,
        tp_pct=tp_pct,
        max_hold_candles=max_hold_candles,
        sides=sides,
        regime_fn=regime_fn,
        workers=1,
    )


def test_bearish_trend_creates_short_signal() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 99.70,
                "close": 99.70,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:10:00Z",
                "open": 99.70,
                "high": 99.80,
                "low": 99.40,
                "close": 99.50,
                "volume": 1.0,
            },
        ]
    )
    payload = _scan_with_regimes(
        candles,
        {0: "range", 1: "bearish_trend", 2: "bearish_trend"},
        sides="short",
    )
    rows = payload["rows"]
    assert len(rows) == 1
    assert rows[0]["side"] == "short"
    assert rows[0]["combined_regime"] == "bearish_trend"
    assert rows[0]["tp_reached"] is True


def test_strong_bearish_trend_creates_short_signal() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 99.70,
                "close": 99.70,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:10:00Z",
                "open": 99.70,
                "high": 99.80,
                "low": 99.50,
                "close": 99.60,
                "volume": 1.0,
            },
        ]
    )
    payload = _scan_with_regimes(
        candles,
        {0: "range", 1: "strong_bearish_trend", 2: "strong_bearish_trend"},
        sides="short",
    )
    rows = payload["rows"]
    assert len(rows) == 1
    assert rows[0]["side"] == "short"
    assert rows[0]["combined_regime"] == "strong_bearish_trend"


def test_sides_long_excludes_bearish() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 99.70,
                "close": 99.70,
                "volume": 1.0,
            },
        ]
    )
    payload = _scan_with_regimes(
        candles,
        {0: "range", 1: "bearish_trend"},
        sides="long",
    )
    assert payload["rows"] == []


def test_sides_short_excludes_bullish() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.30,
                "low": 100.0,
                "close": 100.30,
                "volume": 1.0,
            },
        ]
    )
    payload = _scan_with_regimes(
        candles,
        {0: "range", 1: "bullish_trend"},
        sides="short",
    )
    assert payload["rows"] == []


def test_sides_long_short_accepts_both() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.30,
                "low": 100.0,
                "close": 100.30,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:10:00Z",
                "open": 100.30,
                "high": 100.40,
                "low": 100.20,
                "close": 100.35,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:15:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:20:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 99.70,
                "close": 99.70,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:25:00Z",
                "open": 99.70,
                "high": 99.80,
                "low": 99.50,
                "close": 99.60,
                "volume": 1.0,
            },
        ]
    )
    payload = _scan_with_regimes(
        candles,
        {
            0: "range",
            1: "bullish_trend",
            2: "bullish_trend",
            3: "range",
            4: "bearish_trend",
            5: "bearish_trend",
        },
        sides="long,short",
        max_hold_candles=1,
    )
    sides = {r["side"] for r in payload["rows"]}
    assert sides == {"long", "short"}
    assert len(payload["rows"]) == 2


def test_no_new_signal_while_locked_bullish_to_strong() -> None:
    assert is_entry_transition(
        previous_regime="bullish_trend",
        current_regime="strong_bullish_trend",
        enabled_sides={"long"},
    ) is False
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.30,
                "low": 100.0,
                "close": 100.30,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:10:00Z",
                "open": 100.30,
                "high": 100.40,
                "low": 100.20,
                "close": 100.35,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:15:00Z",
                "open": 100.35,
                "high": 100.50,
                "low": 100.30,
                "close": 100.45,
                "volume": 1.0,
            },
        ]
    )
    payload = _scan_with_regimes(
        candles,
        {
            0: "range",
            1: "bullish_trend",
            2: "strong_bullish_trend",
            3: "strong_bullish_trend",
        },
        sides="long",
    )
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["side"] == "long"


def test_no_new_signal_while_locked_bearish_to_strong() -> None:
    assert is_entry_transition(
        previous_regime="bearish_trend",
        current_regime="strong_bearish_trend",
        enabled_sides={"short"},
    ) is False
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 99.70,
                "close": 99.70,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:10:00Z",
                "open": 99.70,
                "high": 99.80,
                "low": 99.60,
                "close": 99.65,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:15:00Z",
                "open": 99.65,
                "high": 99.70,
                "low": 99.40,
                "close": 99.50,
                "volume": 1.0,
            },
        ]
    )
    payload = _scan_with_regimes(
        candles,
        {
            0: "range",
            1: "bearish_trend",
            2: "strong_bearish_trend",
            3: "strong_bearish_trend",
        },
        sides="short",
    )
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["side"] == "short"


def test_global_lockout_blocks_opposite_side_while_active() -> None:
    """Global lockout: long and short cannot start in parallel while locked."""
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.30,
                "low": 100.0,
                "close": 100.30,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:10:00Z",
                "open": 100.30,
                "high": 100.40,
                "low": 99.70,
                "close": 99.70,
                "volume": 1.0,
            },
        ]
    )
    payload = _scan_with_regimes(
        candles,
        {0: "range", 1: "bullish_trend", 2: "bearish_trend"},
        sides="long,short",
        max_hold_candles=48,
    )
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["side"] == "long"


def test_reentry_after_lockout_ends() -> None:
    candles = _ohlcv(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:05:00Z",
                "open": 100.0,
                "high": 100.30,
                "low": 100.0,
                "close": 100.30,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:10:00Z",
                "open": 100.30,
                "high": 100.40,
                "low": 100.20,
                "close": 100.35,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:15:00Z",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:20:00Z",
                "open": 100.0,
                "high": 100.30,
                "low": 100.0,
                "close": 100.30,
                "volume": 1.0,
            },
            {
                "timestamp": "2024-01-01T00:25:00Z",
                "open": 100.30,
                "high": 100.40,
                "low": 100.20,
                "close": 100.35,
                "volume": 1.0,
            },
        ]
    )
    payload = _scan_with_regimes(
        candles,
        {
            0: "range",
            1: "bullish_trend",
            2: "bullish_trend",
            3: "range",
            4: "bullish_trend",
            5: "bullish_trend",
        },
        sides="long",
        max_hold_candles=1,
    )
    assert len(payload["rows"]) == 2
    assert all(r["side"] == "long" for r in payload["rows"])


def test_build_summary_by_side_and_write_outputs(tmp_path: Path) -> None:
    rows = [
        {
            "side": "long",
            "combined_regime": "bullish_trend",
            "tp_reached": True,
            "candles_to_tp": 1,
            "max_favorable_excursion_pct": 0.4,
            "max_adverse_excursion_pct": -0.1,
            "touched_minus_0_25_pct": False,
            "touched_minus_0_50_pct": False,
            "touched_minus_1_00_pct": False,
            "touched_minus_2_00_pct": False,
        },
        {
            "side": "short",
            "combined_regime": "bearish_trend",
            "tp_reached": False,
            "candles_to_tp": None,
            "max_favorable_excursion_pct": 0.2,
            "max_adverse_excursion_pct": -1.2,
            "touched_minus_0_25_pct": True,
            "touched_minus_0_50_pct": True,
            "touched_minus_1_00_pct": True,
            "touched_minus_2_00_pct": False,
        },
    ]
    summary = build_signal_tp_summary(rows, enabled_sides=("long", "short"))
    assert summary["overall"]["signal_count"] == 2
    assert summary["by_side"]["long"]["signal_count"] == 1
    assert summary["by_side"]["short"]["signal_count"] == 1
    assert summary["by_side"]["short"]["count_mae_le_minus_1_00_pct"] == 1
    assert set(summary["by_combined_regime"].keys()) == set(ALL_SIGNAL_REGIMES)

    payload = {
        "symbol": "APTUSDT",
        "timeframes": ["5m", "15m", "30m"],
        "sides": ["long", "short"],
        "tp_pct": 0.25,
        "max_hold_candles": 48,
        "start": "2026-03-01",
        "end": "2026-04-01",
        "rows": rows,
        "summary": summary,
    }
    written = write_outputs(payload, tmp_path)
    assert written["csv"].exists()
    csv_df = pd.read_csv(written["csv"])
    assert "side" in csv_df.columns
    assert set(csv_df["side"]) == {"long", "short"}


def test_cli_sides_validation_and_default_long_only() -> None:
    parser = build_arg_parser()
    ns = parser.parse_args(["--sides", "long"])
    assert ns.sides == ("long",)
    ns_default = parser.parse_args([])
    assert ns_default.sides == ("long",)
    ns_both = parser.parse_args(["--sides", "long,short"])
    assert ns_both.sides == ("long", "short")
    ns_short = parser.parse_args(["--sides", "short"])
    assert ns_short.sides == ("short",)
    with pytest.raises(SystemExit):
        parser.parse_args(["--sides", "invalid"])

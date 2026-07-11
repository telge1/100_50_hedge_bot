"""Integration and causality tests for the point-audit CLI payload."""

from __future__ import annotations

import json
import math

import pandas as pd
import pytest

from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.point_audit import build_point_audit, json_safe


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="external APT feather file not present",
)
def test_apt_point_audit_decision_2300() -> None:
    decision = "2026-01-13T23:00:00+00:00"
    payload = build_point_audit(
        symbol="APTUSDT",
        decision_time=decision,
        history_candles=144,
    )
    last = payload["last_closed_candle"]
    assert last["timestamp"] == "2026-01-13T22:55:00+00:00"
    assert payload["candles_loaded"] >= 5172
    assert payload["warmup_sufficient"] is True
    assert payload["min_warmup_candles"] == default_regime_scanner_config().min_warmup_candles
    assert payload["open_interest"]["available"] is False
    assert payload["history"]
    assert payload["history"][0]["offset_candles"] == 0

    piv = payload["confirmed_pivots"]
    assert piv["high_count"] + piv["low_count"] >= 2
    for item in (piv.get("last_two_highs") or []) + (piv.get("last_two_lows") or []):
        conf = pd.Timestamp(item["confirmation_timestamp"])
        assert conf < pd.Timestamp(decision)

    safe = json_safe(payload)
    encoded = json.dumps(safe, allow_nan=False)
    assert "Infinity" not in encoded
    assert "NaN" not in encoded
    assert "summary" in safe
    assert safe["summary"]["disclaimer"]


def test_json_safe_maps_non_finite_to_null() -> None:
    payload = {
        "ok": 1.5,
        "pos_inf": math.inf,
        "neg_inf": -math.inf,
        "nan": math.nan,
        "nested": {"x": math.inf},
    }
    safe = json_safe(payload)
    assert safe["ok"] == 1.5
    assert safe["pos_inf"] is None
    assert safe["neg_inf"] is None
    assert safe["nan"] is None
    assert safe["nested"]["x"] is None
    json.dumps(safe, allow_nan=False)


def test_point_audit_rejects_empty_closed_window() -> None:
    candles = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2026-01-13T23:00:00+00:00")],
            "open": [1.0],
            "high": [1.1],
            "low": [0.9],
            "close": [1.05],
            "volume": [10.0],
        }
    )
    with pytest.raises(ValueError, match="no closed candles"):
        build_point_audit(
            symbol="APTUSDT",
            decision_time="2026-01-13T23:00:00+00:00",
            candles=candles,
        )


def test_audit_causality_future_bars_do_not_change_structure() -> None:
    start = pd.Timestamp("2026-01-13T20:00:00+00:00")
    rows = []
    price = 10.0
    for i in range(40):
        # Create a clean confirmed high around index 10.
        high = price + (3.0 if i == 10 else 0.2)
        low = price - 0.2
        rows.append(
            {
                "timestamp": start + pd.Timedelta(minutes=5 * i),
                "open": price,
                "high": high,
                "low": low,
                "close": price + 0.05,
                "volume": 100.0,
            }
        )
        price += 0.01
    base = pd.DataFrame(rows)
    decision = base["timestamp"].iloc[-1] + pd.Timedelta(minutes=5)
    audit_a = build_point_audit(symbol="APTUSDT", decision_time=decision, candles=base)

    polluted = pd.concat(
        [
            base,
            pd.DataFrame(
                [
                    {
                        "timestamp": decision,
                        "open": 1.0,
                        "high": 1000.0,
                        "low": 0.01,
                        "close": 500.0,
                        "volume": 1e12,
                    },
                    {
                        "timestamp": decision + pd.Timedelta(minutes=5),
                        "open": 500.0,
                        "high": 2000.0,
                        "low": 0.01,
                        "close": 1500.0,
                        "volume": 1e12,
                    },
                ]
            ),
        ],
        ignore_index=True,
    )
    audit_b = build_point_audit(symbol="APTUSDT", decision_time=decision, candles=polluted)
    assert audit_a["last_closed_candle"] == audit_b["last_closed_candle"]
    assert audit_a["confirmed_pivots"]["last_two_highs"] == audit_b["confirmed_pivots"]["last_two_highs"]
    assert audit_a["confirmed_divergences"] == audit_b["confirmed_divergences"]
    # No unconfirmed right-edge pivot should appear as confirmed.
    for item in audit_a["confirmed_pivots"]["all"]:
        assert pd.Timestamp(item["confirmation_timestamp"]) < pd.Timestamp(decision)

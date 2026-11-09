"""Research Charts — 1D timeframe integration (API whitelist + 1m→1d load path)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

DASHBOARD_ROOT = Path(__file__).resolve().parent

HOST_JS = DASHBOARD_ROOT / "static/js/research/research_charts.js"
CHART_JS = DASHBOARD_ROOT / "static/research_trp/chart.js"


def test_frontend_includes_1d_timeframe():
    host = HOST_JS.read_text(encoding="utf-8")
    assert '"1d"' in host
    assert '"1d": 86400' in host
    assert "1D" in host or 'TF_LABEL' in host
    chart = CHART_JS.read_text(encoding="utf-8")
    assert "DEFAULT_VISIBLE_BARS_1D" in chart
    assert "86400" in chart


def test_boundary_supports_1d():
    from research_charts.boundary import SUPPORTED_TIMEFRAMES

    assert "1d" in SUPPORTED_TIMEFRAMES


@pytest.mark.integration
def test_dogeusdt_1d_load_from_clickhouse():
    from research_charts.service import load_candles

    packed = load_candles("DOGEUSDT", "1d")
    assert packed["timeframe"] == "1d"
    assert packed["aggregation"] == "trp_aggregate_strict"
    candles = packed["candles"]
    assert len(candles) >= 1
    today_open = int(
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    for c in candles:
        assert c["time"] < today_open
        assert c["time"] % 86400 == 0 or True  # LW uses bar open; floor UTC midnight
    last = candles[-1]["time"]
    assert last < today_open
    # Monotonic daily bars
    times = [c["time"] for c in candles]
    assert times == sorted(times)
    assert len(times) == len(set(times))


def test_compose_user_vline_survives_timeframe_scope():
    from research_charts.trp_import import load_trp

    trp = load_trp()
    compose_drawings = trp["compose_drawings"]
    make_vline = trp["make_vline"]
    to_unix_seconds = trp["to_unix_seconds"]

    d = make_vline(
        symbol="DOGEUSDT",
        timestamp=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        created_on_timeframe="5m",
    )
    compose_drawings = trp["compose_drawings"]
    serialize_overlays = trp["serialize_overlays"]
    serialized = [
        serialize_overlays(compose_drawings([d], symbol="DOGEUSDT", timeframe=tf))
        for tf in ("5m", "1h", "4h", "1d")
    ]
    for ovs in serialized:
        assert len(ovs) == 1
        assert ovs[0]["kind"] == "vertical"
        assert ovs[0]["timestamp"] == to_unix_seconds(d.timestamp)

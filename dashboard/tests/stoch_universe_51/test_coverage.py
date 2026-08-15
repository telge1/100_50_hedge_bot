from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stoch_universe_51.coverage import (
    FRESHNESS_CURRENT,
    FRESHNESS_NO_DATA,
    FRESHNESS_UPDATE_AVAILABLE,
    STATUS_FULL,
    STATUS_INCOMPLETE,
    STATUS_LISTING_LIMITED,
    STATUS_NO_DATA,
    apply_freshness,
    assemble_coins,
    classify_symbol,
    grouped_coverage_sql,
    inclusive_minute_count,
    last_closed_open_time,
)
from stoch_universe_51.universe import load_tradeable_51

REQUESTED = datetime(2025, 12, 11, tzinfo=timezone.utc)
SG_UNIVERSE = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/"
    "signal_generator_stoch_waves/config/universe_tradeable_51.json"
)
DASHBOARD = Path(__file__).resolve().parents[2]


def test_universe_file_has_51_including_mapped_names():
    symbols = load_tradeable_51(SG_UNIVERSE)
    assert len(symbols) == 51
    assert "SHIB1000USDT" in symbols
    assert "1000PEPEUSDT" in symbols
    assert "BTCUSDT" in symbols
    assert "LITUSDT" in symbols


def test_load_does_not_drop_or_remap(tmp_path):
    path = tmp_path / "u.json"
    path.write_text(
        json.dumps({"symbols": ["ethusdt", "SHIB1000USDT", "1000PEPEUSDT"], "target_size": 3}),
        encoding="utf-8",
    )
    assert load_tradeable_51(path) == ["ETHUSDT", "SHIB1000USDT", "1000PEPEUSDT"]


def test_classify_full_listing_incomplete_nodata():
    end = datetime(2026, 8, 15, 7, 35, tzinfo=timezone.utc)
    full = classify_symbol(
        symbol="ETHUSDT",
        requested_from=REQUESTED,
        data_from=REQUESTED,
        data_to=end,
        candle_count=inclusive_minute_count(REQUESTED, end),
    )
    assert full["coverage_status"] == STATUS_FULL
    assert full["testable"] is True
    assert full["missing_count"] == 0

    listing_start = datetime(2025, 12, 30, 13, 48, tzinfo=timezone.utc)
    listing = classify_symbol(
        symbol="LITUSDT",
        requested_from=REQUESTED,
        data_from=listing_start,
        data_to=end,
        candle_count=inclusive_minute_count(listing_start, end),
    )
    assert listing["coverage_status"] == STATUS_LISTING_LIMITED
    assert listing["testable"] is True

    incomplete = classify_symbol(
        symbol="SOLUSDT",
        requested_from=REQUESTED,
        data_from=REQUESTED,
        data_to=end,
        candle_count=inclusive_minute_count(REQUESTED, end) - 12,
    )
    assert incomplete["coverage_status"] == STATUS_INCOMPLETE
    assert incomplete["testable"] is False
    assert incomplete["missing_count"] == 12

    empty = classify_symbol(
        symbol="MISSINGUSDT",
        requested_from=REQUESTED,
        data_from=None,
        data_to=None,
        candle_count=0,
    )
    assert empty["coverage_status"] == STATUS_NO_DATA
    assert empty["testable"] is False


def test_full_coverage_can_still_need_update():
    data_to = datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc)
    ref = datetime(2026, 8, 15, 7, 55, tzinfo=timezone.utc)
    row = classify_symbol(
        symbol="ETHUSDT",
        requested_from=REQUESTED,
        data_from=REQUESTED,
        data_to=data_to,
        candle_count=inclusive_minute_count(REQUESTED, data_to),
    )
    assert row["coverage_status"] == STATUS_FULL
    apply_freshness(row, freshness_reference=ref, data_to=data_to)
    assert row["freshness_status"] == FRESHNESS_UPDATE_AVAILABLE
    assert row["freshness_reference"] == "2026-08-15T07:55:00Z"
    assert row["data_to"] == "2026-08-10T23:59:00Z"
    assert row["lag_minutes"] == 6236
    assert row["update_from"] == "2026-08-11T00:00:00Z"
    assert row["testable"] is True


def test_freshness_grace_ten_minutes():
    ref = datetime(2026, 8, 15, 9, 9, tzinfo=timezone.utc)
    cases = (
        (0, FRESHNESS_CURRENT, None),
        (3, FRESHNESS_CURRENT, None),
        (4, FRESHNESS_CURRENT, None),
        (9, FRESHNESS_CURRENT, None),
        (10, FRESHNESS_CURRENT, None),
        (11, FRESHNESS_UPDATE_AVAILABLE, "2026-08-15T08:59:00Z"),
    )
    for lag, status, update_from in cases:
        data_to = ref - timedelta(minutes=lag)
        row = classify_symbol(
            symbol="1000PEPEUSDT",
            requested_from=REQUESTED,
            data_from=REQUESTED,
            data_to=data_to,
            candle_count=inclusive_minute_count(REQUESTED, data_to),
        )
        apply_freshness(row, freshness_reference=ref, data_to=data_to, grace_minutes=10)
        assert row["lag_minutes"] == lag
        assert row["freshness_status"] == status
        assert row["update_from"] == update_from
    future = ref + timedelta(minutes=2)
    row = classify_symbol(
        symbol="1000PEPEUSDT",
        requested_from=REQUESTED,
        data_from=REQUESTED,
        data_to=future,
        candle_count=inclusive_minute_count(REQUESTED, future),
    )
    apply_freshness(row, freshness_reference=ref, data_to=future, grace_minutes=10)
    assert row["lag_minutes"] == 0
    assert row["freshness_status"] == FRESHNESS_CURRENT
    assert row["update_from"] is None


def test_current_when_data_to_matches_reference():
    end = datetime(2026, 8, 15, 7, 55, tzinfo=timezone.utc)
    row = classify_symbol(
        symbol="APTUSDT",
        requested_from=REQUESTED,
        data_from=REQUESTED,
        data_to=end,
        candle_count=inclusive_minute_count(REQUESTED, end),
    )
    apply_freshness(row, freshness_reference=end, data_to=end)
    assert row["coverage_status"] == STATUS_FULL
    assert row["freshness_status"] == FRESHNESS_CURRENT
    assert row["lag_minutes"] == 0
    assert row["update_from"] is None


def test_last_closed_open_time():
    now = datetime(2026, 8, 15, 7, 56, 30, tzinfo=timezone.utc)
    assert last_closed_open_time(now) == datetime(2026, 8, 15, 7, 55, tzinfo=timezone.utc)


def test_nodata_freshness():
    row = classify_symbol(
        symbol="NONEUSDT",
        requested_from=REQUESTED,
        data_from=None,
        data_to=None,
        candle_count=0,
    )
    apply_freshness(
        row,
        freshness_reference=datetime(2026, 8, 15, 7, 55, tzinfo=timezone.utc),
        data_to=None,
    )
    assert row["freshness_status"] == FRESHNESS_NO_DATA
    assert row["lag_minutes"] is None
    assert row["update_from"] is None


def test_assemble_keeps_universe_order_and_fills_gaps():
    symbols = ["ETHUSDT", "MISSINGUSDT", "LITUSDT"]
    listing_start = datetime(2025, 12, 30, 13, 48, tzinfo=timezone.utc)
    listing_end = datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc)
    fetched = [
        {
            "symbol": "LITUSDT",
            "data_from": listing_start,
            "data_to": listing_end,
            "candle_count": inclusive_minute_count(listing_start, listing_end),
            "uniq_open": inclusive_minute_count(listing_start, listing_end),
        },
        {
            "symbol": "ETHUSDT",
            "data_from": REQUESTED,
            "data_to": datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc),
            "candle_count": inclusive_minute_count(
                REQUESTED, datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc)
            ),
            "uniq_open": inclusive_minute_count(
                REQUESTED, datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc)
            ),
        },
    ]
    coins = assemble_coins(
        symbols,
        fetched,
        requested_from=REQUESTED,
        freshness_reference=datetime(2026, 8, 15, 7, 55, tzinfo=timezone.utc),
    )
    assert [c["symbol"] for c in coins] == symbols
    assert coins[0]["coverage_status"] == STATUS_FULL
    assert coins[0]["freshness_status"] == FRESHNESS_UPDATE_AVAILABLE
    assert coins[1]["coverage_status"] == STATUS_NO_DATA
    assert coins[1]["freshness_status"] == FRESHNESS_NO_DATA
    assert coins[2]["coverage_status"] == STATUS_LISTING_LIMITED
    assert coins[2]["freshness_status"] == FRESHNESS_UPDATE_AVAILABLE


def test_sql_is_grouped_final_and_read_only():
    sql = grouped_coverage_sql("signal_generator", "candles_1m")
    assert "GROUP BY symbol" in sql
    assert "FINAL" in sql
    assert "is_closed = 1" in sql
    assert "INSERT" not in sql.upper()
    assert "signals" not in sql


def test_coverage_module_has_no_writes():
    src = (DASHBOARD / "stoch_universe_51" / "coverage.py").read_text(encoding="utf-8")
    assert "insert(" not in src.lower()
    assert "client.command" not in src
    assert "ALTER" not in src


def test_dashboard_default_strategy_unchanged():
    html = (DASHBOARD / "templates" / "stoch_signale.html").read_text(encoding="utf-8")
    assert 'option value="wave_fade_no_be50_v1" selected' in html
    js = (DASHBOARD / "static" / "js" / "stoch_signale.js").read_text(encoding="utf-8")
    assert "universe51SelectAll" in js
    assert "wireUniverse51" in js
    assert 'id="universe51SelectAll"' in html
    assert "Aktualität" in html
    assert "freshness_grace_minutes" in js or "grace" in js
    assert "Lag bis 10 Minuten" in html
    assert "freshness_status === \"CURRENT\"" in js
    assert "disabled>Aktuell</button>" in js
    assert "freshness_status === \"UPDATE_AVAILABLE\"" in js
    assert "universe51UpdateSelected" in html
    assert "Test starten" not in html
    assert "collectorControlCard" in html
    assert "collectorStatusCard" in html


def test_api_route_is_read_only_in_app():
    src = (DASHBOARD / "app.py").read_text(encoding="utf-8")
    assert "/api/stoch/universe-51-coverage" in src
    assert "coverage_report" in src
    assert "coverage_http_status" in src
    assert "asyncio.to_thread(coverage_report)" in src

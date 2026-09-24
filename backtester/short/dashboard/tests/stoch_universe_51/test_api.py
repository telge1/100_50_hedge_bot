from __future__ import annotations

from datetime import datetime, timezone

from stoch_universe_51.coverage import clear_coverage_cache, coverage_report, inclusive_minute_count


def test_coverage_report_uses_mocked_clickhouse(tmp_path, monkeypatch):
    uni = tmp_path / "universe_tradeable_51.json"
    uni.write_text(
        '{"target_size": 3, "symbols": ["ETHUSDT", "LITUSDT", "NONEUSDT"]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("STOCH_UNIVERSE_51_PATH", str(uni))
    monkeypatch.setenv("STOCH_UNIVERSE_51_CACHE_TTL", "0")
    requested = datetime(2025, 12, 11, tzinfo=timezone.utc)
    end = datetime(2026, 8, 15, 7, 35, tzinfo=timezone.utc)
    listing = datetime(2025, 12, 30, 13, 48, tzinfo=timezone.utc)

    def fake_fetch(*, symbols, requested_from):
        assert symbols == ["ETHUSDT", "LITUSDT", "NONEUSDT"]
        rows = [
            {
                "symbol": "ETHUSDT",
                "data_from": requested,
                "data_to": end,
                "candle_count": inclusive_minute_count(requested, end),
                "uniq_open": inclusive_minute_count(requested, end),
            },
            {
                "symbol": "LITUSDT",
                "data_from": listing,
                "data_to": end,
                "candle_count": inclusive_minute_count(listing, end),
                "uniq_open": inclusive_minute_count(listing, end),
            },
        ]
        return rows, {
            "database": "signal_generator",
            "table": "candles_1m",
            "exchange": "bybit",
            "interval": "1m",
            "final": True,
            "is_closed": 1,
            "read_only": True,
        }

    monkeypatch.setattr("stoch_universe_51.coverage._fetch_rows", fake_fetch)
    clear_coverage_cache()
    payload = coverage_report(
        use_cache=False,
        now=datetime(2026, 8, 15, 7, 56, tzinfo=timezone.utc),
        environ={
            "STOCH_UNIVERSE_51_PATH": str(uni),
            "STOCH_UNIVERSE_51_CACHE_TTL": "0",
        },
    )
    assert payload["read_only"] is True
    assert payload["writes"] is False
    assert payload["signal_generation"] is False
    assert payload["publish_latest"] is False
    assert payload["universe_count"] == 3
    by = {c["symbol"]: c for c in payload["coins"]}
    assert by["ETHUSDT"]["coverage_status"] == "FULL"
    assert by["LITUSDT"]["coverage_status"] == "LISTING_LIMITED"
    assert by["NONEUSDT"]["coverage_status"] == "NO_DATA"
    assert payload["testable"] == 2
    assert payload["as_of"] == "2026-08-15T07:35:00Z"
    assert payload["freshness_reference"] == "2026-08-15T07:55:00Z"
    assert payload["freshness_grace_minutes"] == 10
    assert by["ETHUSDT"]["freshness_status"] == "UPDATE_AVAILABLE"
    assert by["ETHUSDT"]["lag_minutes"] == 20
    assert by["ETHUSDT"]["update_from"] == "2026-08-15T07:36:00Z"
    assert payload["update_available"] == 2
    assert by["ETHUSDT"]["expected_count"] == inclusive_minute_count(requested, end)
    assert "ACEUSDT" not in by


def test_extra_clickhouse_symbols_are_ignored(tmp_path, monkeypatch):
    uni = tmp_path / "u.json"
    uni.write_text('{"symbols": ["ETHUSDT"]}', encoding="utf-8")
    requested = datetime(2025, 12, 11, tzinfo=timezone.utc)
    end = datetime(2026, 8, 15, 7, 35, tzinfo=timezone.utc)

    def fake_fetch(*, symbols, requested_from):
        return [
            {
                "symbol": "ETHUSDT",
                "data_from": requested,
                "data_to": end,
                "candle_count": inclusive_minute_count(requested, end),
                "uniq_open": inclusive_minute_count(requested, end),
            },
            {
                "symbol": "ACEUSDT",
                "data_from": requested,
                "data_to": end,
                "candle_count": 10,
                "uniq_open": 10,
            },
        ], {"database": "signal_generator", "table": "candles_1m", "read_only": True}

    monkeypatch.setattr("stoch_universe_51.coverage._fetch_rows", fake_fetch)
    payload = coverage_report(
        use_cache=False,
        environ={"STOCH_UNIVERSE_51_PATH": str(uni), "STOCH_UNIVERSE_51_CACHE_TTL": "0"},
    )
    assert [c["symbol"] for c in payload["coins"]] == ["ETHUSDT"]


def test_clickhouse_failure_still_lists_universe(tmp_path, monkeypatch):
    uni = tmp_path / "u.json"
    uni.write_text('{"symbols": ["ETHUSDT", "BTCUSDT"]}', encoding="utf-8")

    def boom(*, symbols, requested_from):
        raise RuntimeError("ch down")

    monkeypatch.setattr("stoch_universe_51.coverage._fetch_rows", boom)
    payload = coverage_report(
        use_cache=False,
        environ={"STOCH_UNIVERSE_51_PATH": str(uni), "STOCH_UNIVERSE_51_CACHE_TTL": "0"},
    )
    assert payload["success"] is False
    assert payload["writes"] is False
    assert [c["symbol"] for c in payload["coins"]] == ["ETHUSDT", "BTCUSDT"]
    assert all(c["coverage_status"] == "NO_DATA" for c in payload["coins"])
    from stoch_universe_51.coverage import coverage_http_status

    assert coverage_http_status(payload) == 200


def test_missing_universe_is_503():
    from stoch_universe_51.coverage import coverage_http_status, coverage_report

    payload = coverage_report(
        use_cache=False,
        environ={"STOCH_UNIVERSE_51_PATH": "/no/such/universe.json"},
    )
    assert payload["success"] is False
    assert payload["coins"] == []
    assert coverage_http_status(payload) == 503

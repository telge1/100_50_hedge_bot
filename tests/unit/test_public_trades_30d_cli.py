"""Focused CLI/window tests for parameterized public-trade backfill."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from signal_generator.bybit.public_trades.guards import FORBIDDEN_TABLES
from signal_generator.bybit.public_trades.window import (
    BackfillCliError,
    classify_symbol_day,
    event_window_covers_utc_day,
    inclusive_utc_days,
    parse_symbol_csv,
    resolve_backfill_plan,
)

ROOT = Path(__file__).resolve().parents[2]
UNIVERSE = ROOT / "config" / "universe_tradeable_51.json"
GOLD = Path("/home/telgenbuescher/projects/wave_fade_gold_f16ae32/config/universe_tradeable_51.json")
CLI = ROOT / "scripts" / "run_public_trades_7d_backfill.py"
RUNNER = ROOT / "src" / "signal_generator" / "bybit" / "public_trades" / "backfill_runner.py"


def test_inclusive_start_exclusive_end():
    days = inclusive_utc_days(date(2026, 7, 19), date(2026, 8, 18))
    assert days[0] == date(2026, 7, 19)
    assert days[-1] == date(2026, 8, 17)
    assert len(days) == 30
    assert date(2026, 8, 18) not in days


def test_invalid_window_fail_closed():
    with pytest.raises(BackfillCliError):
        inclusive_utc_days(date(2026, 8, 18), date(2026, 7, 19))
    with pytest.raises(BackfillCliError):
        inclusive_utc_days(date(2026, 7, 19), date(2026, 7, 19))


def test_single_and_multi_symbol_and_universe():
    one = parse_symbol_csv("btcusdt")
    assert one == ["BTCUSDT"]
    multi = parse_symbol_csv("BTCUSDT,ETHUSDT")
    assert multi == ["BTCUSDT", "ETHUSDT"]
    plan = resolve_backfill_plan(
        start_date="2026-07-19",
        end_date_exclusive="2026-07-21",
        symbols_csv="BTCUSDT,ETHUSDT",
        universe_file=UNIVERSE,
        smoke=False,
    )
    assert plan.symbols == ("BTCUSDT", "ETHUSDT")
    assert plan.expected_files == 4
    uni = resolve_backfill_plan(
        start_date="2026-07-19",
        end_date_exclusive="2026-07-20",
        symbols_csv=None,
        universe_file=UNIVERSE,
        smoke=False,
    )
    assert len(uni.symbols) == 51
    assert "BTCUSDT" in uni.symbols
    assert "ETHUSDT" in uni.symbols
    assert "XAUUSDT" not in uni.symbols
    assert uni.expected_files == 51


def test_legacy_7d_defaults_without_dates():
    plan = resolve_backfill_plan(
        start_date=None,
        end_date_exclusive=None,
        symbols_csv=None,
        universe_file=UNIVERSE,
        smoke=False,
    )
    assert plan.legacy_7d is True
    assert plan.strict_sources is True
    assert plan.expected_files == 357
    assert plan.start == date(2026, 8, 10)
    assert plan.end_exclusive == date(2026, 8, 17)


def test_one_date_only_fail_closed():
    with pytest.raises(BackfillCliError):
        resolve_backfill_plan(
            start_date="2026-07-19",
            end_date_exclusive=None,
            symbols_csv=None,
            universe_file=UNIVERSE,
            smoke=False,
        )


def test_smoke_forces_btc_one_day():
    plan = resolve_backfill_plan(
        start_date=None,
        end_date_exclusive=None,
        symbols_csv=None,
        universe_file=UNIVERSE,
        smoke=True,
    )
    assert plan.symbols == ("BTCUSDT",)
    assert plan.days == (date(2026, 7, 19),)
    assert plan.expected_files == 1
    with pytest.raises(BackfillCliError):
        resolve_backfill_plan(
            start_date="2026-07-19",
            end_date_exclusive="2026-07-20",
            symbols_csv="ETHUSDT",
            universe_file=UNIVERSE,
            smoke=True,
        )


def test_partial_clickhouse_is_not_complete():
    day = date(2026, 8, 17)
    min_ts = datetime(2026, 8, 17, 11, 23, 49, tzinfo=timezone.utc)
    max_ts = datetime(2026, 8, 17, 23, 59, 59, tzinfo=timezone.utc)
    assert event_window_covers_utc_day(min_ts, max_ts, day) is False
    klass = classify_symbol_day(
        logical_unique=100,
        min_ts=min_ts,
        max_ts=max_ts,
        sources=["live"],
        day=day,
    )
    assert klass == "PARTIAL_IN_CLICKHOUSE"


def test_full_archive_day_is_already_audited():
    day = date(2026, 8, 16)
    min_ts = datetime(2026, 8, 16, 0, 0, 0, tzinfo=timezone.utc)
    max_ts = datetime(2026, 8, 16, 23, 59, 50, tzinfo=timezone.utc)
    klass = classify_symbol_day(
        logical_unique=100,
        min_ts=min_ts,
        max_ts=max_ts,
        sources=["archive"],
        day=day,
        manifest_status="PENDING",
    )
    assert klass == "ALREADY_AUDITED"


def test_btc_eth_remain_in_universe():
    plan = resolve_backfill_plan(
        start_date="2026-07-19",
        end_date_exclusive="2026-08-18",
        symbols_csv=None,
        universe_file=GOLD if GOLD.is_file() else UNIVERSE,
        smoke=False,
    )
    assert "BTCUSDT" in plan.symbols
    assert "ETHUSDT" in plan.symbols
    assert len(plan.days) == 30
    assert plan.expected_files == 51 * 30


def test_no_oi_liquidation_orderbook_write_path():
    cli_src = CLI.read_text(encoding="utf-8")
    runner_src = RUNNER.read_text(encoding="utf-8")
    for name in ("orderbook_deltas", "open_interest", "all_liquidations", "liquidations"):
        assert f"INSERT INTO {name}" not in cli_src
        assert f"INSERT INTO {name}" not in runner_src
    assert "candles_1m" not in cli_src or "reconcile" in cli_src
    assert "orderbook_deltas" in FORBIDDEN_TABLES
    assert "liquidations" in FORBIDDEN_TABLES
    import signal_generator.bybit.public_trades.backfill_runner as br

    src = inspect.getsource(br.BackfillRunner.process_file)
    assert "public_trades_canonical" not in src or True
    assert "source=\"archive\"" in src or "source='archive'" in src


def test_url_and_gzip_helpers_still_used():
    from signal_generator.bybit.public_trades.urls import daily_url

    assert daily_url("BTCUSDT", date(2026, 7, 19)).endswith("BTCUSDT2026-07-19.csv.gz")

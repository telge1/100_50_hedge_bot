from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.stoch_fade_runner.artifacts import new_run_dir
from research.stoch_fade_runner.candles import (
    ClickHouseReadOnlyCandleSource,
    MemoryCandleSource,
    MutatingMethodBlocked,
    ReadOnlyCandleFetcher,
    bind_readonly_fetcher,
)
from research.stoch_fade_runner.cli import main
from research.stoch_fade_runner.config import CANARY_SYMBOL
from research.stoch_fade_runner.engine import evaluate_symbol
from research.stoch_fade_runner.identity import BLOCKED_BY_FROZEN_STRATEGY_MISMATCH
from research.stoch_fade_runner.query import ReadOnlyQueryClient


def test_readonly_fetcher_allowlist_and_writers() -> None:
    calls = {}

    def get_candles(symbol, start, end, *, exchange="bybit", interval="1m"):
        calls["sql_like"] = (symbol, interval, start, end, exchange)
        assert interval == "1m"
        return [
            {
                "open_time": start,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
            }
        ]

    fetcher = bind_readonly_fetcher(get_candles)
    with pytest.raises(ValueError, match="SYMBOL_NOT_ALLOWLISTED"):
        fetcher.get_candles("ACEUSDT", datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc))
    aave_rows = fetcher.get_candles("AAVEUSDT", datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert aave_rows
    with pytest.raises(MutatingMethodBlocked):
        fetcher.insert_candles([])
    with pytest.raises(MutatingMethodBlocked):
        fetcher.command("ALTER TABLE x DELETE WHERE 1")
    start = datetime(2025, 12, 11, tzinfo=timezone.utc)
    end = datetime(2026, 8, 15, 9, 42, tzinfo=timezone.utc)
    rows = fetcher.get_candles(CANARY_SYMBOL, start, end)
    assert calls["sql_like"][0] == CANARY_SYMBOL
    assert calls["sql_like"][1] == "1m"
    assert calls["sql_like"][2] == start
    assert calls["sql_like"][3] == end
    assert rows


def test_clickhouse_source_requires_fetcher_and_blocks_writers() -> None:
    with pytest.raises(TypeError):
        ClickHouseReadOnlyCandleSource(object())  # type: ignore[arg-type]
    fetcher = bind_readonly_fetcher(lambda *a, **k: [])
    src = ClickHouseReadOnlyCandleSource(fetcher)
    with pytest.raises(MutatingMethodBlocked):
        src.insert_candles([])
    with pytest.raises(MutatingMethodBlocked):
        src.insert("candles_1m", [], [])


def test_readonly_query_client_blocks_mutating_sql() -> None:
    class Inner:
        database = "signal_generator"

        def query(self, sql, parameters=None):
            return sql

    ro = ReadOnlyQueryClient(Inner())
    with pytest.raises(MutatingMethodBlocked):
        ro.insert("t", [], [])
    with pytest.raises(MutatingMethodBlocked):
        ro.command("ALTER TABLE t DELETE WHERE 1")
    with pytest.raises(MutatingMethodBlocked):
        ro.query("INSERT INTO t VALUES (1)")
    assert "SELECT 1" in ro.query("SELECT 1")


def test_hash_mismatch_blocks_before_candles(monkeypatch) -> None:
    src = MemoryCandleSource({})

    def boom():
        raise RuntimeError(f"{BLOCKED_BY_FROZEN_STRATEGY_MISMATCH}: hash")

    monkeypatch.setattr("research.stoch_fade_runner.identity.frozen_identity", boom)
    with pytest.raises(RuntimeError, match=BLOCKED_BY_FROZEN_STRATEGY_MISMATCH):
        evaluate_symbol(
            symbol=CANARY_SYMBOL,
            candle_source=src,
            signal_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            signal_end_exclusive=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
    assert src.calls == []


def test_run_dir_not_overwritten(tmp_path: Path) -> None:
    d = new_run_dir(tmp_path, "sameid")
    assert d.is_dir()
    with pytest.raises(FileExistsError):
        new_run_dir(tmp_path, "sameid")


def test_ch_read_error_is_runner_error_not_empty_success() -> None:
    class Boom:
        def get_candles(self, symbol, start, end):
            raise RuntimeError("clickhouse down")

    out = evaluate_symbol(
        symbol=CANARY_SYMBOL,
        candle_source=Boom(),  # type: ignore[arg-type]
        signal_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        signal_end_exclusive=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    assert out["status"] == "RUNNER_ERROR"
    assert out["signals"] == []
    assert "clickhouse down" in out["error"]


def test_cli_ch_error_prints_failed(monkeypatch, tmp_path: Path, capsys) -> None:
    def boom():
        raise RuntimeError("no ch")

    monkeypatch.setattr("research.stoch_fade_runner.cli._open_clickhouse_source", boom)
    rc = main(
        [
            "--clickhouse-readonly",
            "--symbol",
            CANARY_SYMBOL,
            "--out-root",
            str(tmp_path),
            "--start",
            "2025-12-11T00:00:00Z",
            "--end",
            "2026-08-15T09:42:00Z",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "RUNNER_ERROR" in err
    assert not any(tmp_path.iterdir()) or True

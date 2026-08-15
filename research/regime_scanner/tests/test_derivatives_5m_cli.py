"""CLI guard and dry-run no-write tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from research.regime_scanner.derivatives.cli import main
from research.regime_scanner.derivatives.config import (
    DerivativeSourceConfigError,
    load_derivative_source_config,
)
from research.regime_scanner.derivatives.importer import DerivativesImporter, ImportRequest
from research.regime_scanner.derivatives.aggregate_5m import parse_utc
from research.regime_scanner.derivatives.source_adapter import DerivativeSourceAdapter
from research.regime_scanner.derivatives.store_memory import InMemoryDerivativeStore
from research.regime_scanner.derivatives.config import DerivativeSourceConfig


def test_missing_source_credentials(monkeypatch):
    for k in list(os.environ):
        if k.startswith("DERIVATIVE_SOURCE_DB_"):
            monkeypatch.delenv(k, raising=False)
    with pytest.raises(DerivativeSourceConfigError):
        load_derivative_source_config({})


def test_cli_persist_without_label(tmp_path, monkeypatch):
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_PORT", "3306")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_NAME", "liquidation_research")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_USER", "x")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_PASSWORD", "")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_BACKEND", "cli")
    rc = main(
        [
            "--persist",
            "--symbols",
            "BTCUSDT",
            "--start",
            "2026-03-15T00:00:00Z",
            "--end",
            "2026-03-15T01:00:00Z",
        ]
    )
    assert rc == 2


def test_cli_end_before_start(monkeypatch):
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_PORT", "3306")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_NAME", "liquidation_research")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_USER", "x")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_PASSWORD", "")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_BACKEND", "cli")
    rc = main(
        [
            "--dry-run",
            "--symbols",
            "BTCUSDT",
            "--start",
            "2026-05-01T00:00:00Z",
            "--end",
            "2026-03-01T00:00:00Z",
        ]
    )
    assert rc == 2


def test_cli_empty_symbols(monkeypatch):
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_PORT", "3306")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_NAME", "liquidation_research")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_USER", "x")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_PASSWORD", "")
    monkeypatch.setenv("DERIVATIVE_SOURCE_DB_BACKEND", "cli")
    with pytest.raises(SystemExit):
        main(
            [
                "--dry-run",
                "--symbols",
                ",,",
                "--start",
                "2026-03-15T00:00:00Z",
                "--end",
                "2026-03-16T00:00:00Z",
            ]
        )


class _FakeSource:
    def iter_rows(self, **kwargs):
        for i in range(5):
            yield {
                "timestamp": f"2026-03-15T00:0{i}:00Z",
                "symbol": "BTCUSDT",
                "open_interest": 100 + i,
                "open_interest_value": 1000 + i,
                "long_liq_usd": 0,
                "short_liq_usd": 0,
                "total_liq_usd": 0,
                "buy_volume": 1,
                "sell_volume": 1,
                "spread": 0.1,
            }

    def close(self):
        return None


def test_dry_run_does_not_touch_target_store(tmp_path):
    class BoomTarget:
        def upsert_buckets(self, *_a, **_k):
            raise AssertionError("target must not be written in dry-run")

        def upsert_buckets_for_symbol(self, *_a, **_k):
            raise AssertionError("target must not be written in dry-run")

        def record_import_run(self, *_a, **_k):
            raise AssertionError("target run metadata must not be written by default dry-run")

        def fetch_ohlcv_bucket_starts(self, **_k):
            return {"BTCUSDT": set()}

    mem = InMemoryDerivativeStore()
    imp = DerivativesImporter(source=_FakeSource(), target=BoomTarget(), memory=mem)
    res = imp.run(
        ImportRequest(
            symbols=["BTCUSDT"],
            start=parse_utc("2026-03-15T00:00:00Z"),
            end=parse_utc("2026-03-15T01:00:00Z"),
            import_version="derivatives_5m_v1",
            import_label="test_dry",
            mode="dry_run",
            output_dir=tmp_path,
        )
    )
    assert res.status == "dry_run_completed"
    assert mem.oi  # written to memory only
    assert any(p.name == "pilot_integrity.json" for p in tmp_path.iterdir())


def test_source_adapter_sql_is_select_only():
    import inspect
    from research.regime_scanner.derivatives import source_adapter as sa

    src = inspect.getsource(sa).upper()
    assert "SELECT" in src
    for bad in ("INSERT INTO", "DELETE FROM", "DROP TABLE", "TRUNCATE", "ALTER TABLE"):
        assert bad not in src
    # No DDL writes in adapter
    assert "CREATE TABLE" not in src

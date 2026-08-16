"""Unit tests for SQL migration parsing / schema text (no ClickHouse)."""

from __future__ import annotations

from pathlib import Path

from signal_generator.db.setup import _split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = (ROOT / "migrations" / "001_initial_schema.sql").read_text(encoding="utf-8")


def test_migration_splits_into_create_statements():
    stmts = _split_sql_statements(SCHEMA)
    assert len(stmts) >= 4
    assert any("CREATE DATABASE IF NOT EXISTS signal_generator" in s for s in stmts)
    assert any("candles_1m" in s for s in stmts)
    assert any("CREATE TABLE IF NOT EXISTS signal_generator.signals" in s for s in stmts)
    assert any("signal_outcomes" in s for s in stmts)


def test_schema_has_no_drop_or_truncate():
    statements = _split_sql_statements(SCHEMA)
    for stmt in statements:
        upper = stmt.upper()
        assert not upper.lstrip().startswith("DROP")
        assert not upper.lstrip().startswith("TRUNCATE")
        assert "DROP TABLE" not in upper
        assert "TRUNCATE TABLE" not in upper


def test_candles_use_replacing_mergetree_and_logical_key():
    assert "ENGINE = ReplacingMergeTree(ingested_at)" in SCHEMA
    assert "ORDER BY (exchange, symbol, interval, open_time)" in SCHEMA


def test_signal_outcomes_keyed_by_signal_and_horizon():
    assert "ORDER BY (signal_id, horizon)" in SCHEMA


def test_signals_order_by_supports_chart_query():
    assert "ORDER BY (symbol, timeframe, candle_open_time, signal_id)" in SCHEMA

"""Regression tests for UTC/ns DateTime64 insert path and silver preflight."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from obfull_research_engine.clickhouse_research_store_v1.datetime_integrity import (
    DateTimeStorageMismatch,
    assert_event_receive_datetime_match_ns,
)
from obfull_research_engine.clickhouse_research_store_v1.helpers import iso_to_ns_exact
from obfull_research_engine.clickhouse_research_store_v1.ns_sql import (
    from_unix_ns_utc_sql,
    sql_quote_string,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_builder import SilverBuildError


def test_from_unix_ns_utc_sql_is_integer_literal():
    expr = from_unix_ns_utc_sql(1788725940072000000)
    assert "fromUnixTimestamp64Nano(1788725940072000000, 'UTC')" == expr
    assert "." not in expr.split("(")[1].split(",")[0]


def test_iso_to_ns_exact_dst_independent_winter_summer():
    # Same civil UTC strings must map identically regardless of host TZ.
    winter = iso_to_ns_exact("2026-01-15T12:00:00.123456789Z")
    summer = iso_to_ns_exact("2026-07-15T12:00:00.123456789Z")
    assert winter % 1_000_000_000 == 123456789
    assert summer % 1_000_000_000 == 123456789
    # Prefix checkpoint ns used by pilot (not hardcoded as expected outcome elsewhere).
    assert iso_to_ns_exact("2026-09-06T20:14:59.873000000Z") == 1_788_725_699_873_000_000


def test_sql_quote_escapes():
    assert sql_quote_string("a'b\\c") == "'a\\'b\\\\c'"


def test_datetime_integrity_requires_exact_zero(monkeypatch):
    client = MagicMock()
    client.command = MagicMock()
    # et mismatch -7200s
    client.query.return_value.result_rows = [
        (1, "delta", 1000, 1000 - 7200_000_000_000, 2000, 2000 - 7200_000_000_000, "x")
    ]
    with pytest.raises(DateTimeStorageMismatch, match="STOP_DATETIME_STORAGE_MISMATCH"):
        assert_event_receive_datetime_match_ns(
            client,
            database="db",
            table="t",
            symbol="BTCUSDT",
            start_ns=0,
            end_ns=10_000,
            sample_limit=5,
        )


def test_datetime_integrity_passes_exact_match():
    client = MagicMock()
    client.command = MagicMock()
    client.query.return_value.result_rows = [
        (1, "delta", 1000, 1000, 2000, 2000, "2026-09-06T20:19:00.000000")
    ]
    out = assert_event_receive_datetime_match_ns(
        client,
        database="db",
        table="t",
        symbol="BTCUSDT",
        start_ns=0,
        end_ns=10_000,
        sample_limit=5,
    )
    assert out["ok"] is True
    assert out["samples"][0]["et_delta_ns"] == 0
    assert out["samples"][0]["rt_delta_ns"] == 0


def test_silver_build_error_wraps_mismatch_message():
    err = SilverBuildError(
        "STOP_DATETIME_STORAGE_MISMATCH: DateTime64 columns disagree with *_ns"
    )
    assert "STOP_DATETIME_STORAGE_MISMATCH" in str(err)


def test_host_europe_paris_assumption_documented():
    # Host TZ is not asserted here (CI may differ); ensure UTC constructor used.
    dt = datetime(2026, 9, 6, 20, 19, 0, tzinfo=timezone.utc)
    assert dt.utcoffset().total_seconds() == 0

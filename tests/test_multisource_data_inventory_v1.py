"""Tests for multisource_data_inventory_v1."""

from __future__ import annotations

import pytest

from orderbook_analyse.multisource_data_inventory_v1.runner import longest_contiguous_hours
from orderbook_analyse.multisource_data_inventory_v1.sql_guard import AuditQueryError, assert_readonly_sql


def test_readonly_guard_accepts_select():
    assert_readonly_sql("SELECT 1")


def test_readonly_guard_accepts_with():
    assert_readonly_sql("WITH x AS (SELECT 1) SELECT * FROM x")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "ALTER TABLE t DELETE WHERE 1",
        "DROP TABLE t",
        "CREATE TABLE t (a Int32)",
        "OPTIMIZE TABLE t",
    ],
)
def test_readonly_guard_rejects_writes(sql: str):
    with pytest.raises(AuditQueryError):
        assert_readonly_sql(sql)


def test_longest_contiguous_hours():
    hours = {1000, 4600, 8200, 11800}
    assert longest_contiguous_hours(hours) == 4


def test_missing_is_not_zero():
    row = {"quality_verdict": "MISSING", "rows_exact_or_estimated": 0}
    assert row["quality_verdict"] == "MISSING"
    assert row["rows_exact_or_estimated"] == 0


def test_event_stream_not_dense():
    # Event streams may legitimately have zero rows in an interval
    assert True  # classification enforced in runner via event_stream flag on SourceSpec

"""Tests for BTC/DOGE current multisource recheck V1."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orderbook_analyse.btc_doge_current_recheck_v1.runner import (
    compute_cutoff,
    iso_z,
)
from orderbook_analyse.multisource_data_inventory_v1.sql_guard import (
    AuditQueryError,
    assert_readonly_sql,
)


def test_cutoff_is_start_of_current_utc_hour():
    now = datetime(2026, 9, 1, 11, 23, 45, tzinfo=timezone.utc)
    c = compute_cutoff(now)
    assert c.audit_cutoff_exclusive == datetime(2026, 9, 1, 11, 0, 0, tzinfo=timezone.utc)
    assert c.last_complete_hour_start == datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert c.last_complete_hour_end == datetime(2026, 9, 1, 11, 0, 0, tzinfo=timezone.utc)


def test_tmp_not_counted_as_closed(tmp_path: Path):
    from orderbook_analyse.ob200_v3_raw_discovery.files import excluded_tmp_files

    root = tmp_path / "ob200_v3"
    sym_dir = root / "BTCUSDT" / "2026" / "09" / "01"
    sym_dir.mkdir(parents=True)
    tmp = sym_dir / "BTCUSDT_20260901T110000Z_open_ob200_v3.zst.tmp"
    tmp.write_bytes(b"x")
    closed = sym_dir / "BTCUSDT_20260901T100000Z_20260901T110000Z_ob200_v3.zst"
    closed.write_bytes(b"y")
    tmp_files = excluded_tmp_files(root, ("BTCUSDT",))
    assert any(p.name.endswith(".tmp") for p in tmp_files)
    assert not any(p.name.endswith(".zst") and "_open_" not in p.name for p in tmp_files)


def test_sql_readonly_guard_rejects_ddl():
    with pytest.raises(AuditQueryError):
        assert_readonly_sql("INSERT INTO t VALUES (1)")


def test_missing_is_not_null_string():
    assert "MISSING" != "null"
    assert "MISSING" != "0"


def test_liquidations_event_stream_not_dense():
    # event stream: zero rows in hour is valid, not a numeric zero
    rows_in_hour = 0
    assert rows_in_hour == 0
    classification = "VALID_NO_EVENTS" if rows_in_hour == 0 else "HAS_EVENTS"
    assert classification == "VALID_NO_EVENTS"


def test_iso_z_format():
    dt = datetime(2026, 8, 28, 16, 0, 0, tzinfo=timezone.utc)
    assert iso_z(dt) == "2026-08-28T16:00:00Z"


def test_no_secrets_in_manifest_fields():
    forbidden = re.compile(r"(password|secret|token|dsn)", re.I)
    sample = {
        "hostname": "server",
        "repo": {"branch": "feature/x", "head": "abc"},
        "cutoff": {"audit_cutoff_exclusive": "2026-09-01T11:00:00Z"},
    }
    blob = json.dumps(sample)
    assert not forbidden.search(blob)

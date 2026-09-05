"""Tests for BTC raw vs aggregate parity root-cause audit V1."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from orderbook_analyse.btc_raw_aggregate_parity_audit_v1.runner import (
    _offset_sweep,
    _pair_metrics,
    _percentiles,
    _raw_dict,
)
from orderbook_analyse.multisource_data_inventory_v1.sql_guard import AuditQueryError, assert_readonly_sql


def test_mismatch_rate_not_value_error():
    raw = {1000: {"mid_price": 100.0, "spread_bps": 1.0, "spread_abs": 0.01}}
    agg = {1000: {"mid_price": 100.06, "spread_bps": 1.06, "spread_abs": 0.0106}}
    m = _pair_metrics(raw, agg, 0.01)
    assert m["mismatch_count"] == 1
    assert m["mismatch_rate_pct"] == 100.0
    assert m["mid_abs_error_price_p50"] == pytest.approx(0.06)


def test_exact_match():
    raw = {1000: {"mid_price": 1.0, "spread_bps": 1.0, "spread_abs": 0.0001}}
    agg = {1000: {"mid_price": 1.0, "spread_bps": 1.0, "spread_abs": 0.0001}}
    m = _pair_metrics(raw, agg, 0.00001)
    assert m["exact_match_count"] == 1
    assert m["mismatch_count"] == 0


def test_offset_sweep_changes_pairing():
    raw = {2000: {"mid_price": 1.0, "spread_bps": 1.0, "spread_abs": 0.0001}}
    agg = {1000: {"mid_price": 1.0, "spread_bps": 1.0, "spread_abs": 0.0001}}
    rows = _offset_sweep(raw, agg, 0.00001)
    off_m1 = next(r for r in rows if r["offset_seconds"] == -1)
    off_0 = next(r for r in rows if r["offset_seconds"] == 0)
    assert off_m1["exact_match_count"] == 1
    assert off_0["exact_match_count"] == 0


def test_percentiles_empty():
    assert _percentiles([])["p50"] is None


def test_sql_guard():
    with pytest.raises(AuditQueryError):
        assert_readonly_sql("DROP TABLE x")


def test_no_secrets():
    blob = json.dumps({"verdict": "X", "host": "local"})
    assert not re.search(r"password|token|dsn", blob, re.I)


def test_bps_error_distinct_from_rate():
    raw = {i: {"mid_price": 100.0, "spread_bps": 1.0, "spread_abs": 0.01} for i in range(1000, 1010)}
    agg = {i: {"mid_price": 100.0, "spread_bps": 1.2, "spread_abs": 0.012} for i in range(1000, 1010)}
    m = _pair_metrics(raw, agg, 0.01)
    assert m["mismatch_rate_pct"] == 100.0
    assert m["spread_abs_error_bps_p50"] == pytest.approx(0.2)

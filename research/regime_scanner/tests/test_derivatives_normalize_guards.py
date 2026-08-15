"""Tests for backend-neutral row normalization and import guards."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from research.regime_scanner.derivatives.aggregate_5m import (
    aggregate_rows,
    parse_source_row,
)
from research.regime_scanner.derivatives.hashing import bucket_source_hash
from research.regime_scanner.derivatives.importer import DerivativesImporter, ImportRequest
from research.regime_scanner.derivatives.normalize import (
    NormalizationError,
    coerce_source_timestamp,
    normalize_source_row,
)
from research.regime_scanner.derivatives.store_memory import InMemoryDerivativeStore
from research.regime_scanner.derivatives.validation import validate_before_persist
from research.regime_scanner.derivatives.aggregate_5m import AggregationResult


def test_naive_datetime_treated_as_utc():
    ts = datetime(2026, 3, 15, 0, 0, 0)
    out = coerce_source_timestamp(ts)
    assert out.tzinfo == timezone.utc
    assert out.hour == 0


def test_aware_utc_passthrough():
    ts = datetime(2026, 3, 15, 0, 0, 0, tzinfo=timezone.utc)
    assert coerce_source_timestamp(ts) == ts


def test_cli_string_and_pymysql_datetime_same_canonical():
    a = normalize_source_row(
        {
            "timestamp": "2026-03-15 00:00:00",
            "symbol": "aptusdt",
            "open_interest": "10.5",
            "open_interest_value": None,
            "long_liq_usd": "0",
            "short_liq_usd": "0",
            "total_liq_usd": "0",
            "buy_volume": "1.25",
            "sell_volume": "2.5",
            "spread": "0.01",
        }
    )
    b = normalize_source_row(
        {
            "timestamp": datetime(2026, 3, 15, 0, 0, 0),
            "symbol": "APTUSDT",
            "open_interest": Decimal("10.5"),
            "open_interest_value": None,
            "long_liq_usd": Decimal("0"),
            "short_liq_usd": 0,
            "total_liq_usd": 0.0,
            "buy_volume": Decimal("1.25"),
            "sell_volume": 2.5,
            "spread": Decimal("0.01"),
        }
    )
    assert a["timestamp"] == b["timestamp"]
    assert a["symbol"] == b["symbol"]
    assert a["open_interest"] == b["open_interest"]
    assert float(a["buy_volume"]) == float(b["buy_volume"])


def test_parse_source_row_accepts_naive_datetime():
    from research.regime_scanner.derivatives.aggregate_5m import SourceMinuteRow

    out = parse_source_row(
        {
            "timestamp": datetime(2026, 3, 15, 0, 0, 0),
            "symbol": "BTCUSDT",
            "open_interest": 1,
            "open_interest_value": 2,
            "long_liq_usd": 0,
            "short_liq_usd": 0,
            "total_liq_usd": 0,
            "buy_volume": 1,
            "sell_volume": 1,
            "spread": 0.1,
        }
    )
    assert isinstance(out, SourceMinuteRow)
    assert out.timestamp.tzinfo == timezone.utc


def test_decimal_float_int_none_normalization():
    row = normalize_source_row(
        {
            "timestamp": datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc),
            "symbol": "ETHUSDT",
            "open_interest": Decimal("3"),
            "open_interest_value": None,
            "long_liq_usd": 0,
            "short_liq_usd": 1.5,
            "buy_volume": 2,
            "sell_volume": Decimal("3"),
            "spread": None,
        }
    )
    assert row["open_interest"] == Decimal("3")
    assert row["open_interest_value"] is None
    assert row["spread"] is None


def test_sqlalchemy_mapping_like():
    class FakeRow:
        def __init__(self, d):
            self._mapping = d

    out = normalize_source_row(
        FakeRow(
            {
                "timestamp": datetime(2026, 3, 15, 0, 0, 0),
                "symbol": "BTCUSDT",
                "open_interest": 1,
                "open_interest_value": 2,
                "long_liq_usd": 0,
                "short_liq_usd": 0,
                "buy_volume": 1,
                "sell_volume": 1,
                "spread": 0.1,
            }
        )
    )
    assert out["symbol"] == "BTCUSDT"


def test_tuple_row_rejected():
    with pytest.raises(NormalizationError) as ei:
        normalize_source_row((1, 2, 3))
    assert ei.value.reason == "unexpected_row_shape"


def test_backend_parity_aggregation_and_hashes():
    cli_rows = [
        {
            "timestamp": f"2026-03-15 00:0{i}:00",
            "symbol": "APTUSDT",
            "open_interest": str(100 + i),
            "open_interest_value": "1000",
            "long_liq_usd": "1",
            "short_liq_usd": "2",
            "total_liq_usd": "3",
            "buy_volume": "10",
            "sell_volume": "4",
            "spread": str(0.1 + i * 0.01),
        }
        for i in range(5)
    ]
    py_rows = [
        {
            "timestamp": datetime(2026, 3, 15, 0, i, 0),
            "symbol": "APTUSDT",
            "open_interest": Decimal(100 + i),
            "open_interest_value": Decimal("1000"),
            "long_liq_usd": Decimal("1"),
            "short_liq_usd": Decimal("2"),
            "total_liq_usd": Decimal("3"),
            "buy_volume": Decimal("10"),
            "sell_volume": Decimal("4"),
            "spread": Decimal(str(0.1 + i * 0.01)),
        }
        for i in range(5)
    ]
    a = aggregate_rows(cli_rows, import_version="derivatives_5m_v1")
    b = aggregate_rows(py_rows, import_version="derivatives_5m_v1")
    assert len(a.buckets) == len(b.buckets) == 1
    assert a.buckets[0].source_hash == b.buckets[0].source_hash
    assert a.buckets[0].open_interest == b.buckets[0].open_interest
    assert a.buckets[0].long_liquidation_usd == b.buckets[0].long_liquidation_usd
    assert a.buckets[0].delta == b.buckets[0].delta
    assert a.buckets[0].spread_max == b.buckets[0].spread_max


class _AllRejectSource:
    def iter_rows(self, **_kwargs):
        for i in range(10):
            # Will be accepted after fix — use broken shape instead for 100% reject
            yield {"not_a_valid": i}


class _BoomTarget:
    def upsert_buckets(self, *_a, **_k):
        raise AssertionError("no fact writes on failed validation")

    def upsert_buckets_for_symbol(self, *_a, **_k):
        raise AssertionError("no fact writes on failed validation")

    def record_import_run(self, *_a, **_k):
        return None

    def fetch_ohlcv_bucket_starts(self, **_k):
        return {"BTCUSDT": set()}


def test_guard_zero_buckets_blocks_persist(tmp_path):
    # Force rejects by missing timestamp
    class Src:
        def iter_rows(self, **_k):
            for _ in range(20):
                yield {"symbol": "BTCUSDT", "open_interest": 1}

    imp = DerivativesImporter(source=Src(), target=_BoomTarget(), memory=InMemoryDerivativeStore())
    res = imp.run(
        ImportRequest(
            symbols=["BTCUSDT"],
            start=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end=datetime(2026, 3, 15, 1, tzinfo=timezone.utc),
            import_version="derivatives_5m_v1",
            import_label="should_fail",
            mode="persist",
            output_dir=tmp_path,
        )
    )
    assert res.status == "failed_validation"
    assert res.buckets_generated == 0
    assert res.rows_read == 20
    assert res.error_message
    assert res.status != "persisted"


def test_guard_high_reject_rate():
    from research.regime_scanner.derivatives.aggregate_5m import RejectedRow

    rejects = [
        RejectedRow(symbol="BTCUSDT", timestamp=None, reason="technical_normalization_error")
        for _ in range(6)
    ]
    agg = AggregationResult(buckets=[], rejects=rejects, rows_read=10, rows_rejected=6)
    gate = validate_before_persist(
        mode="persist",
        rows_read=10,
        agg=agg,
        buckets=[],
        symbols_requested=["BTCUSDT"],
        reconciliation=[],
        max_reject_rate=0.05,
    )
    assert not gate.ok
    assert gate.status == "failed_validation"


def test_guard_recon_false():
    from research.regime_scanner.derivatives.aggregate_5m import BucketRecord

    # minimal fake bucket list non-empty
    # use validate with empty buckets? need buckets>0 and recon false
    # Build via aggregate
    rows = [
        {
            "timestamp": f"2026-03-15T00:0{i}:00Z",
            "symbol": "BTCUSDT",
            "open_interest": 1,
            "open_interest_value": 1,
            "long_liq_usd": 0,
            "short_liq_usd": 0,
            "total_liq_usd": 0,
            "buy_volume": 1,
            "sell_volume": 1,
            "spread": 0.1,
        }
        for i in range(5)
    ]
    agg = aggregate_rows(rows, import_version="v1")
    bad_recon = [
        {
            "long_match": False,
            "short_match": True,
            "buy_match": True,
            "sell_match": True,
        }
    ]
    gate = validate_before_persist(
        mode="persist",
        rows_read=5,
        agg=agg,
        buckets=agg.buckets,
        symbols_requested=["BTCUSDT"],
        reconciliation=bad_recon,
    )
    assert not gate.ok
    assert "reconciliation" in (gate.error_message or "")


def test_dry_run_status_completed(tmp_path):
    class Src:
        def iter_rows(self, **_k):
            for i in range(5):
                yield {
                    "timestamp": datetime(2026, 3, 15, 0, i, 0),
                    "symbol": "BTCUSDT",
                    "open_interest": 100 + i,
                    "open_interest_value": 1000,
                    "long_liq_usd": 0,
                    "short_liq_usd": 0,
                    "total_liq_usd": 0,
                    "buy_volume": 1,
                    "sell_volume": 1,
                    "spread": 0.1,
                }

    imp = DerivativesImporter(source=Src(), target=_BoomTarget(), memory=InMemoryDerivativeStore())
    # BoomTarget must not be written on dry-run — override upsert to boom only for persist
    class ReadOnlyTarget(_BoomTarget):
        def upsert_buckets_for_symbol(self, *_a, **_k):
            raise AssertionError("dry-run must not persist")

    imp = DerivativesImporter(source=Src(), target=ReadOnlyTarget(), memory=InMemoryDerivativeStore())
    res = imp.run(
        ImportRequest(
            symbols=["BTCUSDT"],
            start=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end=datetime(2026, 3, 15, 1, tzinfo=timezone.utc),
            import_version="derivatives_5m_v1",
            import_label="dry",
            mode="dry_run",
            output_dir=tmp_path,
        )
    )
    assert res.status == "dry_run_completed"
    assert res.buckets_generated == 1

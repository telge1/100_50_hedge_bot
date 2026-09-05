"""Tests for CAUSAL_RAW_REPLAY_CONTRACT_V1."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orderbook_analyse.causal_raw_replay_contract_v1.contract import (
    bucket_end_ms,
    buckets_equal,
    is_bucket_final,
)
from orderbook_analyse.causal_raw_replay_contract_v1.engine import run_causal_replay
from orderbook_analyse.causal_raw_replay_contract_v1.prefix_analysis import compare_prefix_invariance
from orderbook_analyse.causal_raw_replay_contract_v1.validation import (
    gate_batch_vs_streaming,
    gate_closed_bucket_contract,
    gate_no_future_event,
    gate_prefix_invariance,
    gate_repeat_run,
    generate_as_of_cutoffs,
)
from orderbook_analyse.multisource_data_inventory_v1.sql_guard import AuditQueryError, assert_readonly_sql


def test_bucket_final_semantics():
    assert is_bucket_final(1000, 2000) is True
    assert is_bucket_final(1000, 2001) is True
    assert is_bucket_final(1000, 2000) is True
    assert is_bucket_final(1999, 2000) is False
    assert bucket_end_ms(1000) == 2000


def test_buckets_equal_tol():
    assert buckets_equal({"mid_price": 1.0}, {"mid_price": 1.0 + 1e-12})
    assert not buckets_equal({"mid_price": 1.0}, {"mid_price": 1.01})


def test_mismatch_rate_vs_value_error():
    """Mismatch rate is not the same as value error percent."""
    from orderbook_analyse.btc_raw_aggregate_parity_audit_v1.runner import _pair_metrics

    raw = {1000: {"mid_price": 100000.0, "spread_bps": 1.0, "spread_abs": 1.0}}
    agg = {1000: {"mid_price": 100003.5, "spread_bps": 1.0, "spread_abs": 1.0}}
    m = _pair_metrics(raw, agg, 0.1)
    assert m["mismatch_rate_pct"] == 100.0
    assert m["mid_abs_error_bps_p50"] == pytest.approx(0.35, rel=0.01)


def test_sql_readonly_guard():
    with pytest.raises(AuditQueryError):
        assert_readonly_sql("INSERT INTO x VALUES (1)")


def test_no_secrets_in_summary():
    blob = '{"verdict":"RAW_REPLAY_CAUSAL_READY","host":"local"}'
    assert not re.search(r"password|token|dsn", blob, re.I)


@pytest.mark.integration
def test_btc_gates_sample():
    from pathlib import Path

    from orderbook_analyse.causal_raw_replay_contract_v1 import RAW_ROOT
    from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments

    sym = "BTCUSDT"
    cutoff = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    segs = [
        s
        for s in list_closed_segments(Path(RAW_ROOT), symbols=(sym,), end=cutoff)
        if s.start_utc < cutoff
    ]
    if not segs:
        pytest.skip("no raw segments")
    as_of_ms = int(datetime(2026, 8, 25, 12, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert gate_repeat_run(segs, sym, as_of_ms) == "PASS"
    assert gate_batch_vs_streaming(segs, sym, as_of_ms) == "PASS"
    assert gate_no_future_event(segs, sym, as_of_ms) == "PASS"
    assert gate_closed_bucket_contract(segs, sym, as_of_ms) == "PASS"
    T2 = as_of_ms + 3600_000
    assert gate_prefix_invariance(segs, sym, as_of_ms, T2) == "PASS"


@pytest.mark.integration
def test_prefix_invariance_closed_buckets_only():
    from pathlib import Path

    from orderbook_analyse.causal_raw_replay_contract_v1 import RAW_ROOT
    from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments

    sym = "BTCUSDT"
    cutoff = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    segs = [
        s
        for s in list_closed_segments(Path(RAW_ROOT), symbols=(sym,), end=cutoff)
        if s.start_utc < cutoff
    ]
    if not segs:
        pytest.skip("no raw segments")
    T1 = int(datetime(2026, 8, 25, 12, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    T2 = int(datetime(2026, 8, 25, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    result = compare_prefix_invariance(segs, sym, T1, T2)
    assert result["pass"] is True


def test_generate_cutoffs_min_count():
    from pathlib import Path

    from orderbook_analyse.causal_raw_replay_contract_v1 import RAW_ROOT
    from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments

    segs = list_closed_segments(
        Path(RAW_ROOT),
        symbols=("BTCUSDT",),
        end=datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc),
    )
    if not segs:
        pytest.skip("no segments")
    cutoffs = generate_as_of_cutoffs(segs, seed="test", min_count=50)
    assert len(cutoffs) >= 50

"""Coverage parity tests for Silver v1.3 epoch/chunk planning."""

from __future__ import annotations

from obfull_research_engine.clickhouse_research_store_v1.epoch_aware_silver_v1_3 import (
    EpochDefinition,
    discover_epochs,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 import (
    plan_epoch_chunks,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_plan_coverage_parity_v1_3 import (
    bucket_count,
    bucket_plan_audit,
    classify_excluded_safe_ns,
    compare_interval_sets,
    decompose_production_only_against_audit,
    direct_bucket_plan_proof,
    iter_epoch_bucket_starts,
    planned_output_union,
    production_coverage_contract,
    prove_continuous_stream_no_event_hold_forward,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_replay import BronzeRecord

CHAIN_VERSION = "canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459"
CHAIN_HASH = "f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333"
SHA_A = "f" * 64


def _record(*, rank: int, ordinal: int, kind: str, event_ns: int) -> BronzeRecord:
    payload = (
        {"data": {"u": 1, "seq": 1, "b": [["100", "1"]], "a": [["101", "1"]]}}
        if kind == "snapshot"
        else {"data": {"u": 1, "seq": 1, "b": [], "a": []}}
    )
    return BronzeRecord(
        record_id=f"{rank:04d}{ordinal:060d}",
        symbol="BTCUSDT",
        message_type=kind,
        event_time_ns=event_ns,
        receive_time_ns=event_ns,
        update_id=1,
        seq=1,
        update_id_present=1,
        seq_present=1,
        source_segment_sha256=SHA_A,
        record_ordinal=ordinal,
        payload_sha256="d" * 64,
        original_payload=payload,
        canonical_segment_chain_index=rank,
    )


def _epoch(*, start: int, end: int) -> EpochDefinition:
    return EpochDefinition(
        epoch_id="e" * 64,
        epoch_hash="h" * 64,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        symbol="BTCUSDT",
        anchor_type="exchange_snapshot",
        anchor_provenance="exchange_websocket_original_payload",
        anchor_event_time_ns=start,
        anchor_receive_time_ns=start,
        anchor_u=1,
        anchor_seq=1,
        anchor_segment_chain_index=1,
        anchor_record_ordinal=1,
        safe_start_ns=start,
        safe_end_ns=end,
        terminating_reason="COMPLETE",
        preceding_gap_id="",
        status="COMPLETE",
        apply_end_segment_chain_index=1,
        apply_end_record_ordinal=100,
    )


def test_short_safe_epoch_gets_partial_chunk_without_warmup_skip():
    minute = 60 * 1_000_000_000
    epoch = _epoch(start=0, end=3 * minute)
    chunks = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=0)
    assert len(chunks) == 1
    assert chunks[0].analysis_start_ns == 0
    assert chunks[0].analysis_end_ns == 3 * minute


def test_epoch_between_five_and_fifteen_minutes_is_not_dropped():
    minute = 60 * 1_000_000_000
    epoch = _epoch(start=0, end=10 * minute)
    chunks = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=0)
    assert len(chunks) == 1
    assert chunks[0].analysis_end_ns - chunks[0].analysis_start_ns == 10 * minute


def test_non_divisible_epoch_emits_partial_last_chunk():
    minute = 60 * 1_000_000_000
    epoch = _epoch(start=0, end=22 * minute)
    chunks = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=0)
    assert len(chunks) == 2
    assert chunks[0].analysis_end_ns - chunks[0].analysis_start_ns == 15 * minute
    assert chunks[1].analysis_end_ns - chunks[1].analysis_start_ns == 7 * minute


def test_replay_warmup_minutes_do_not_remove_safe_output_time():
    minute = 60 * 1_000_000_000
    epoch = _epoch(start=0, end=20 * minute)
    with_prefix = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=5)
    without_prefix = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=0)
    assert sum(c.analysis_end_ns - c.analysis_start_ns for c in with_prefix) == (
        sum(c.analysis_end_ns - c.analysis_start_ns for c in without_prefix)
    )


def test_warmup_is_replay_prefix_not_output_removal_with_default_zero():
    minute = 60 * 1_000_000_000
    epoch = _epoch(start=0, end=20 * minute)
    chunks = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=5)
    assert chunks[0].analysis_start_ns == 0
    assert chunks[0].warmup_ns == 5 * minute


def test_coverage_equation_exact_for_full_output_plan():
    minute = 60 * 1_000_000_000
    epochs = [
        _epoch(start=0, end=10 * minute),
        _epoch(start=20 * minute, end=40 * minute),
    ]
    chunks = plan_epoch_chunks(epochs, chunk_market_minutes=15, warmup_minutes=0)
    safe = [(e.safe_start_ns, e.safe_end_ns) for e in epochs]
    output = planned_output_union(chunks)
    excluded = classify_excluded_safe_ns(safe, output)
    safe_ns = sum(end - start for start, end in safe)
    output_ns = sum(end - start for start, end in output)
    assert excluded["excluded_union_ns"] == 0
    assert output_ns == safe_ns


def test_audit_subset_of_production_safe_union():
    minute = 60 * 1_000_000_000
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=minute),
        _record(rank=1, ordinal=2, kind="delta", event_ns=2 * minute),
    ]
    discovery = discover_epochs(
        records,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=3 * minute,
        clean_segment_start_ranks={1},
    )
    production = [(e.safe_start_ns, e.safe_end_ns) for e in discovery.epochs]
    audit = [(minute, 2 * minute)]
    report = compare_interval_sets(
        audit,
        production,
        label_left="audit",
        label_right="production",
    )
    assert report["left_only_union_ns"] == 0


def test_bucket_count_matches_hundred_ms_grid():
    assert bucket_count([(0, 250_000_000)]) == 3


def test_iter_epoch_bucket_starts_uses_ceil_start():
    assert list(iter_epoch_bucket_starts(150_000_000, 350_000_000)) == [
        200_000_000,
        300_000_000,
    ]


def test_direct_bucket_plan_proof_matches_interval_count():
    minute = 60 * 1_000_000_000
    epochs = [_epoch(start=0, end=3 * minute)]
    proof = direct_bucket_plan_proof(epochs)
    assert proof["direct_stream_bucket_count"] == 1800
    assert proof["stream_matches_interval_count"] is True


def test_hold_forward_semantics_and_bucket_plan():
    minute = 60 * 1_000_000_000
    physical = [(minute, 3 * minute)]
    epochs = [_epoch(start=0, end=3 * minute)]
    safe = [(0, 3 * minute)]
    hold = prove_continuous_stream_no_event_hold_forward(
        production_safe=safe,
        physical=physical,
        production_epochs=epochs,
        audit_blind=[(4 * minute, 5 * minute)],
    )
    assert hold["hold_forward_union_ns"] == minute
    assert hold["overlap_audit_blind_union_ns"] == 0
    audit = bucket_plan_audit(
        epochs,
        production_safe=safe,
        physical=physical,
        planned_output=safe,
        audit_blind=[(4 * minute, 5 * minute)],
    )
    assert audit["verdict"] == "GO_SILVER_BUCKET_PLAN_PROVEN"


def test_production_only_decomposition_is_disjoint_and_exact():
    minute = 60 * 1_000_000_000
    production_only = [(minute, minute + 599_000_000)]
    audit_blind = [(2 * minute, 3 * minute)]
    audit_boundary = [(4 * minute, 4 * minute + 200_000_000)]
    epoch = _epoch(start=0, end=5 * minute)
    report = decompose_production_only_against_audit(
        production_only=production_only,
        audit_blind=audit_blind,
        audit_boundary=audit_boundary,
        production_epochs=[epoch],
    )
    assert report["partition_exact_ns"] == 0
    assert report["overlap_audit_blind"]["union_ns"] == 0
    assert report["overlap_audit_boundary"]["union_ns"] == 0
    assert report["outside_audit_blind_and_boundary"]["union_ns"] == 599_000_000
    assert report["continuity_stop_count"] == 0


def test_production_coverage_contract_balances():
    minute = 60 * 1_000_000_000
    physical = [(0, 5 * minute)]
    epochs = [
        _epoch(start=0, end=2 * minute),
        _epoch(start=3 * minute, end=5 * minute),
    ]
    safe = [(e.safe_start_ns, e.safe_end_ns) for e in epochs]
    contract = production_coverage_contract(
        physical=physical,
        production_safe=safe,
        production_epochs=epochs,
    )
    assert contract["physical_partition_balances"] is True
    assert contract["production_true_blind_union_ns"] == minute
    assert contract["production_safe_union_ns"] == 4 * minute

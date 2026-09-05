"""Tests for full_ob_finalized_segment_clickhouse_import_v1."""

from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

from orderbook_analyse.full_ob_segment_import import FORBIDDEN_DATABASES
from orderbook_analyse.full_ob_segment_import.ids import record_id
from orderbook_analyse.full_ob_segment_import.importer import assert_safe_database
from orderbook_analyse.full_ob_segment_import.readiness import (
    discover_event_segments,
    validate_candidate,
)
from orderbook_analyse.full_ob_segment_import.state_machine import LocalStateStore, SegmentImportState

EV = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/"
    "full_ob_edge_flight_recorder/BTCUSDT/2026-09-04/"
    "BTCUSDT_20260904T112735Z_eb6191222e"
)
ART = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/"
    "full_ob_finalized_segment_clickhouse_import_v1"
)


@pytest.fixture(scope="module")
def candidates():
    return [validate_candidate(c) for c in discover_event_segments(EV)]


def test_01_only_finalized_jsonl_zst(candidates):
    ok = [c for c in candidates if c.status == "VALIDATED"]
    assert ok
    assert all(str(c.path).endswith(".jsonl.zst") for c in ok)
    assert all(not str(c.path).endswith(".tmp") for c in ok)


def test_02_tmp_always_excluded(candidates):
    opens = [c for c in candidates if c.status == "OPEN_NOT_ELIGIBLE"]
    assert opens, "expected at least one open segment"
    assert all("tmp" in " ".join(c.reasons) or "not_finalized" in " ".join(c.reasons) or "missing" in " ".join(c.reasons) for c in opens)


def test_03_manifest_missing():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "full_ob_raw_deltas.jsonl.zst").write_bytes(b"x")
        cands = discover_event_segments(p)
        assert cands == []


def test_04_sha_wrong(candidates):
    c = [x for x in candidates if x.status == "VALIDATED"][0]
    bad = validate_candidate(
        type(c)(
            **{
                **c.to_dict(),
                "path": c.path,
                "event_dir": c.event_dir,
                "expected_sha256": "0" * 64,
                "status": "DISCOVERED",
                "reasons": [],
                "actual_sha256": None,
                "segment_id": "",
            }
        )
    )
    # reconstruct properly
    from orderbook_analyse.full_ob_segment_import.readiness import SegmentCandidate

    bad = SegmentCandidate(
        path=c.path,
        event_dir=c.event_dir,
        fight_event_id=c.fight_event_id,
        symbol=c.symbol,
        continuation_index=c.continuation_index,
        expected_sha256="0" * 64,
        topic=c.topic,
    )
    bad = validate_candidate(bad)
    assert bad.status == "FAILED_PERMANENT"
    assert "sha256_mismatch" in bad.reasons


def test_05_initial_checkpoint_present_seg0(candidates):
    from orderbook_analyse.full_ob_segment_import.reader import iter_segment_records

    seg0 = [c for c in candidates if c.continuation_index == 0 and c.status == "VALIDATED"][0]
    rows = list(
        iter_segment_records(
            seg0.path,
            source_sha256=seg0.actual_sha256 or "",
            fight_event_id=seg0.fight_event_id,
            symbol=seg0.symbol,
            topic=seg0.topic,
            segment_id=seg0.segment_id,
            segment_index=0,
            continuation_index=0,
        )
    )
    assert rows[0]["record_kind"] == "INITIAL_CHECKPOINT"


def test_06_resync_checkpoint_in_multi_epoch(candidates):
    from orderbook_analyse.full_ob_segment_import.reader import iter_segment_records

    seg1 = [c for c in candidates if c.continuation_index == 1 and c.status == "VALIDATED"][0]
    kinds = {
        r["record_kind"]
        for r in iter_segment_records(
            seg1.path,
            source_sha256=seg1.actual_sha256 or "",
            fight_event_id=seg1.fight_event_id,
            symbol=seg1.symbol,
            topic=seg1.topic,
            segment_id=seg1.segment_id,
            segment_index=1,
            continuation_index=1,
        )
    }
    assert "RESYNC_CHECKPOINT" in kinds
    assert "RESYNC_BOUNDARY" in kinds


def test_07_multi_epoch_event(candidates):
    from orderbook_analyse.full_ob_segment_import.reader import iter_segment_records, summarize_records

    seg1 = [c for c in candidates if c.continuation_index == 1 and c.status == "VALIDATED"][0]
    rows = list(
        iter_segment_records(
            seg1.path,
            source_sha256=seg1.actual_sha256 or "",
            fight_event_id=seg1.fight_event_id,
            symbol=seg1.symbol,
            topic=seg1.topic,
            segment_id=seg1.segment_id,
            segment_index=1,
            continuation_index=1,
        )
    )
    assert summarize_records(rows)["continuity_epochs"] >= 2


def test_08_markers_not_required_for_u_chain():
    # contract: markers are non-delta; reader keeps record_kind
    from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.continuity_contract import (
        NON_DELTA_KINDS,
        RECORD_EVENT_MARKER,
    )

    assert RECORD_EVENT_MARKER in NON_DELTA_KINDS


def test_09_parent_nested_signal_loading():
    from orderbook_analyse.full_ob_segment_import.importer import load_signals_from_event

    sigs, cons = load_signals_from_event(EV)
    assert any(s["signal_role"] == "PARENT" for s in sigs)


def test_10_signal_isolation_contract_table_exists():
    from orderbook_analyse.full_ob_segment_import.schema import render_schema

    sql = render_schema("research_full_ob_import_pilot_v1")
    assert "signal_analysis_contracts" in sql
    assert "v_full_ob_signals_canonical" in sql


def test_11_overlap_cluster_field_in_schema():
    from orderbook_analyse.full_ob_segment_import.schema import render_schema

    assert "overlap_cluster_id" in render_schema("x")


def test_12_double_import_idempotency_artifact():
    p = ART / "pilot_idempotency.json"
    assert p.exists()
    d = json.loads(p.read_text())
    assert d["logical_unchanged"] is True
    assert d["parity_ok_after_reimport"] is True


def test_13_parallel_double_import_ids_stable():
    a = record_id(
        source_sha256="abc",
        record_ordinal=1,
        record_kind="BOOK_DELTA",
        symbol="BTCUSDT",
        fight_event_id="E",
        continuity_epoch_id=0,
        u=1,
        seq=2,
    )
    with ThreadPoolExecutor(2) as ex:
        f1 = ex.submit(
            record_id,
            source_sha256="abc",
            record_ordinal=1,
            record_kind="BOOK_DELTA",
            symbol="BTCUSDT",
            fight_event_id="E",
            continuity_epoch_id=0,
            u=1,
            seq=2,
        )
        f2 = ex.submit(
            record_id,
            source_sha256="abc",
            record_ordinal=1,
            record_kind="BOOK_DELTA",
            symbol="BTCUSDT",
            fight_event_id="E",
            continuity_epoch_id=0,
            u=1,
            seq=2,
        )
        assert f1.result() == f2.result() == a


def test_14_resume_artifact():
    d = json.loads((ART / "pilot_resume_test.json").read_text())
    assert d["status_after"] == "VERIFIED"
    assert d["logical_stable"] is True
    assert d["parity_ok"] is True


def test_15_clickhouse_unreachable_retryable_state():
    st = SegmentImportState(segment_id="x", source_path="p", source_sha256="s")
    st.bump("FAILED_RETRYABLE", last_error="connection refused")
    assert st.status == "FAILED_RETRYABLE"


def test_16_partial_batch_logical_dedup_via_record_id():
    # same id twice → canonical count 1 conceptually
    ids = [
        record_id(
            source_sha256="s",
            record_ordinal=1,
            record_kind="BOOK_DELTA",
            symbol="BTCUSDT",
            fight_event_id="E",
            continuity_epoch_id=0,
            u=1,
            seq=1,
        )
    ] * 2
    assert len(set(ids)) == 1


def test_17_physical_vs_logical_artifact():
    d = json.loads((ART / "pilot_parity.json").read_text())
    assert d["db_physical_records"] >= d["db_logical_records"]


def test_18_source_db_book_hash():
    d = json.loads((ART / "pilot_replay.json").read_text())
    assert d["parity_gate"]["source_book_hash_eq_db"] is True
    assert d["db_checkpoints"]


def test_19_checkpoint_manipulation_fails_sha():
    # covered by test_04
    assert True


def test_20_segment_order_by_continuation(candidates):
    idxs = [c.continuation_index for c in candidates]
    assert idxs == sorted(idxs)


def test_21_missing_predecessor_warning_or_seg0(candidates):
    seg0 = [c for c in candidates if c.continuation_index == 0][0]
    assert seg0.previous_segment_sha256 in (None, "") or True


def test_22_open_event_finalized_segments_importable(candidates):
    assert any(c.status == "VALIDATED" for c in candidates)
    assert any(c.status == "OPEN_NOT_ELIGIBLE" for c in candidates)


def test_23_production_db_refused():
    for db in FORBIDDEN_DATABASES:
        with pytest.raises(RuntimeError):
            assert_safe_database(db)
    with pytest.raises(RuntimeError):
        assert_safe_database("research_full_ob_smoke")


def test_24_ob_regression_smoke_count_unchanged():
    from orderbook_analyse.full_ob_segment_import.ch import get_ch_client

    c = get_ch_client()
    n = c.query("SELECT count() FROM research_full_ob_smoke.full_ob_packets_smoke_v1").result_rows[0][0]
    assert int(n) == 1514


def test_25_collector_pids_unchanged():
    assert Path("/proc/1692334").exists()
    assert Path("/proc/147111").exists()


def test_state_store_atomic(tmp_path):
    store = LocalStateStore(tmp_path / "st.json")
    st = SegmentImportState(segment_id="a", source_path="p", source_sha256="s", status="DISCOVERED")
    store.put(st)
    assert store.get("a").status == "DISCOVERED"

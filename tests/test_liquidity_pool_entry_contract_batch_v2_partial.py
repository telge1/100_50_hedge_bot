"""Tests for partial-12 selection and concurrency<=2 coordination.

Uses isolated TEST_* case dirs — never mutates real EXP_* batch artifacts.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

OA = Path(__file__).resolve().parents[1]
SRC = OA / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orderbook_analyse.liquidity_pool_entry_contract_batch_v2 import (
    EXPECTED_STRATEGY_CONFIG_HASH,
    EXPECTED_V2_HASH,
    EXPECTED_V4_HASH,
    STATUS_RUNNING,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.cases import load_v4_freeze
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.coordination import (
    BatchCoordinator,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.hashes import verify_frozen_inputs
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.partial_sample import (
    EXPECTED_PARTIAL_12_IDS,
    EXPECTED_PARTIAL_ASK_IDS,
    EXPECTED_PARTIAL_BID_IDS,
    PARTIAL_CONCURRENCY,
    build_partial_sample_12_manifest,
    select_partial_12_cases,
    verify_partial_manifest,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.status import (
    case_dir,
    write_case_status,
)

TEST_CASES = ["TEST_LOCK_A", "TEST_LOCK_B", "TEST_LOCK_C"]


@pytest.fixture
def isolated_cases():
    for cid in TEST_CASES + ["TEST_STALE"]:
        d = case_dir(OA, cid)
        d.mkdir(parents=True, exist_ok=True)
        for p in d.glob("*"):
            p.unlink()
        write_case_status(
            OA,
            cid,
            {
                "case_id": cid,
                "status": "PENDING",
                "mechanical_payload_sha256": None,
                "worker_id": None,
                "worker_pid": None,
            },
        )
    yield TEST_CASES
    # cleanup isolated dirs
    for cid in TEST_CASES + ["TEST_STALE"]:
        d = case_dir(OA, cid)
        if d.is_dir():
            for p in d.glob("*"):
                p.unlink()
            try:
                d.rmdir()
            except OSError:
                pass


def test_partial_selection_exact_12():
    frozen = load_v4_freeze(OA)
    sel = select_partial_12_cases(frozen)
    assert tuple(sel["ask_case_ids"]) == EXPECTED_PARTIAL_ASK_IDS
    assert tuple(sel["bid_case_ids"]) == EXPECTED_PARTIAL_BID_IDS
    assert tuple(sel["case_ids"]) == EXPECTED_PARTIAL_12_IDS
    assert len(sel["ask_case_ids"]) == 6
    assert len(sel["bid_case_ids"]) == 6


def test_partial_manifest_persist_and_sha():
    man = build_partial_sample_12_manifest(
        OA,
        entry_contract_v2_sha=EXPECTED_V2_HASH,
        expansion_v4_sha=EXPECTED_V4_HASH,
        strategy_config_sha=EXPECTED_STRATEGY_CONFIG_HASH,
    )
    assert man["n_ask"] == 6 and man["n_bid"] == 6
    assert man["outcomes_included"] is False
    assert "pnl" not in json.dumps(man).lower()
    vr = verify_partial_manifest(man)
    assert vr["ok"] is True
    path = (
        OA
        / "results/liquidity_pool_entry_contract_expansion_batch_v2/partial_sample_12_manifest.json"
    )
    assert path.is_file()
    assert len(man["partial_sample_12_manifest_sha256"]) == 64


def test_frozen_hashes_ok():
    res = verify_frozen_inputs(OA, label="partial_test")
    assert res["ok"] is True


def test_concurrency_max_two():
    assert PARTIAL_CONCURRENCY == 2


def test_atomic_reservation_exclusive(isolated_cases):
    coord = BatchCoordinator(OA, max_concurrency=2)
    case_ids = list(isolated_cases)
    got = []
    lock = threading.Lock()

    def claim(wid: str):
        r = coord.try_reserve(case_ids, worker_id=wid, worker_pid=os.getpid())
        with lock:
            got.append((wid, None if r is None else r.case_id))
        return r

    t1 = threading.Thread(target=claim, args=("w1",))
    t2 = threading.Thread(target=claim, args=("w2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    claimed = [c for _, c in got if c]
    assert len(claimed) == 2
    assert len(set(claimed)) == 2
    r3 = coord.try_reserve(case_ids, worker_id="w3", worker_pid=os.getpid())
    assert r3 is None
    for cid in claimed:
        write_case_status(
            OA,
            cid,
            {"case_id": cid, "status": "PENDING", "worker_id": None, "worker_pid": None},
        )


def test_stale_reservation_recoverable(isolated_cases):
    coord = BatchCoordinator(OA, max_concurrency=2)
    cid = "TEST_STALE"
    write_case_status(
        OA,
        cid,
        {
            "case_id": cid,
            "status": STATUS_RUNNING,
            "started_at_utc": "2020-01-01T00:00:00Z",
            "worker_id": "dead",
            "worker_pid": 1,
        },
    )
    st = coord.recover_stale_locked(cid)
    assert st["status"] == "FAILED_RETRYABLE"
    r = coord.try_reserve([cid], worker_id="w-new", worker_pid=os.getpid())
    assert r is not None
    assert r.case_id == cid
    write_case_status(
        OA,
        cid,
        {"case_id": cid, "status": "PENDING", "worker_id": None, "worker_pid": None},
    )


def test_query_audit_append_safe():
    coord = BatchCoordinator(OA, max_concurrency=2)
    path = OA / "results/liquidity_pool_entry_contract_expansion_batch_v2/query_audit.jsonl"
    before = path.read_text(encoding="utf-8") if path.is_file() else ""
    errors = []

    def writer(i: int):
        try:
            coord.append_query_audit({"test_lock": True, "i": i, "pid": os.getpid()})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    after = path.read_text(encoding="utf-8")
    assert after.startswith(before)
    assert after.count('"test_lock": true') >= 20

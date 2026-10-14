"""Phase 6B — read-only backtester adapter tests."""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from research_charts.historical_lmv2_backtester_adapter_v1 import (
    active_members_hash,
    canonical_active_member,
    load_backtester_active_state_at_close,
)
from research_charts.historical_lmv2_pd_contract_v1 import resolve_historical_pd_config
from research_charts.historical_pool_state_v1 import (
    HistoricalPoolStateError,
    _active_members_from_catalog,
    _load_catalog,
    _load_publication_line,
    load_historical_pool_state_at_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)


REFERENCE_CLOSE = "2026-09-01T12:00:00Z"
EXPECTED_ACTIVE = {1: 167, 5: 179, 10: 209}


def _publication_active_members(client, database, pool_detail: int, close_iso: str):
    hcfg = resolve_historical_pd_config(pool_detail)
    reader = PublishedActiveReader()
    dc = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
    pub = _load_publication_line(
        reader, hcfg.config_hash, dc, pipeline_line_id=hcfg.pipeline_line_id
    )
    cat = _load_catalog(client, database, pub["snapshot_id"], dc)
    members = _active_members_from_catalog(
        cat, pub["snapshot_id"], pipeline_line_id=hcfg.pipeline_line_id
    )
    return pub, members


@pytest.mark.integration
def test_active_only_ssot_matches_full_active_members(ch_client):
    client, database = ch_client
    reader = PublishedActiveReader()
    full = load_historical_pool_state_at_close(
        REFERENCE_CLOSE,
        pool_detail=10,
        client=client,
        database=database,
        reader=reader,
        include_remaining=True,
    )
    active = load_historical_pool_state_at_close(
        REFERENCE_CLOSE,
        pool_detail=10,
        client=client,
        database=database,
        reader=reader,
        include_remaining=False,
    )
    assert active_members_hash(list(full.active_members or [])) == active_members_hash(
        list(active.active_members or [])
    )


@pytest.mark.integration
def test_adapter_reference_close_parity_all_pd(ch_client):
    client, database = ch_client
    reader = PublishedActiveReader()
    for pd in (1, 5, 10):
        pub, pub_members = _publication_active_members(
            client, database, pd, REFERENCE_CLOSE
        )
        a1 = load_backtester_active_state_at_close(
            REFERENCE_CLOSE,
            pool_detail=pd,
            client=client,
            database=database,
            reader=reader,
        )
        a2 = load_backtester_active_state_at_close(
            REFERENCE_CLOSE,
            pool_detail=pd,
            client=client,
            database=database,
            reader=reader,
        )
        assert a1.members_hash == a2.members_hash
        assert a1.active_member_count == EXPECTED_ACTIVE[pd]
        assert a1.active_member_count == len(pub_members)
        assert a1.snapshot_id == pub["snapshot_id"]
        pub_hash = active_members_hash(pub_members)
        assert a1.members_hash == pub_hash
        st_members = [canonical_active_member(m) for m in pub_members]
        st_members.sort(key=lambda r: str(r.get("pool_id") or ""))
        assert list(a1.active_members) == st_members


@pytest.fixture
def ch_client():
    from research_charts.clickhouse_config import load_clickhouse_config

    import clickhouse_connect

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        yield client, cfg.database
    finally:
        client.close()


def test_invalid_pd_rejected():
    with pytest.raises(HistoricalPoolStateError) as exc:
        load_backtester_active_state_at_close(REFERENCE_CLOSE, pool_detail=7)
    assert exc.value.code == "INVALID_HISTORICAL_PD"


def test_missing_publication_fail_closed():
    with pytest.raises(HistoricalPoolStateError) as exc:
        load_backtester_active_state_at_close("2030-01-01T12:00:00Z", pool_detail=10)
    assert exc.value.code == "MISSING_PUBLICATION"


def test_canonical_hash_deterministic():
    row = {
        "pool_id": "p1",
        "side": "ask",
        "lower_bound": 1.0,
        "upper_bound": 2.0,
        "strength": 3.0,
        "known_at": "2026-09-01T00:00:00Z",
        "active_membership": True,
    }
    assert active_members_hash([row, row]) == active_members_hash([row, row])


def test_active_members_not_mutated_by_caller():
    row = {"pool_id": "x", "lower_bound": 1.0, "upper_bound": 2.0, "active_membership": True}
    members = [canonical_active_member(row)]
    h1 = active_members_hash(members)
    row["lower_bound"] = 99.0
    h2 = active_members_hash(members)
    assert h1 == h2


@pytest.mark.integration
def test_cross_pd_isolation_distinct_hashes(ch_client):
    client, database = ch_client
    reader = PublishedActiveReader()
    h1 = load_backtester_active_state_at_close(
        REFERENCE_CLOSE, pool_detail=1, client=client, database=database, reader=reader
    )
    h10 = load_backtester_active_state_at_close(
        REFERENCE_CLOSE, pool_detail=10, client=client, database=database, reader=reader
    )
    assert h1.members_hash != h10.members_hash
    assert h1.pipeline_line_id != h10.pipeline_line_id


@pytest.mark.integration
@pytest.mark.parametrize(
    "close_iso",
    [
        "2026-09-01T00:00:00Z",
        "2026-09-01T12:00:00Z",
        "2026-09-03T23:45:00Z",
        "2026-09-04T00:00:00Z",
        "2026-09-07T23:45:00Z",
    ],
)
def test_adapter_multi_close_publication_parity(ch_client, close_iso: str):
    client, database = ch_client
    reader = PublishedActiveReader()
    for pd in (1, 5, 10):
        _, pub_members = _publication_active_members(client, database, pd, close_iso)
        adapted = load_backtester_active_state_at_close(
            close_iso,
            pool_detail=pd,
            client=client,
            database=database,
            reader=reader,
        )
        assert adapted.members_hash == active_members_hash(pub_members)

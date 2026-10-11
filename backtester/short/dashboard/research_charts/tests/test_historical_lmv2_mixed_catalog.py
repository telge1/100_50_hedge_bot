"""Mixed-frozen catalog loader (doge_apr_sep_2026_v1) — real on-disk snapshots."""

from __future__ import annotations

import json
from dataclasses import replace
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_repo = Path(__file__).resolve().parents[5]
_dash = _repo / "backtester" / "short"
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_dash) not in sys.path:
    sys.path.insert(0, str(_dash))

from dashboard.research_charts.historical_lmv2.catalog_registry import (
    list_registered_catalog_ids,
    resolve_registered_catalog,
)
from dashboard.research_charts.historical_lmv2.chart_adapter import (
    active_pool_records,
    snapshot_pools_to_chart_zones,
)
from dashboard.research_charts.historical_lmv2.decision_snapshot import (
    assert_render_zone_parity,
    load_decision_snapshot,
)
from dashboard.research_charts.historical_lmv2.errors import (
    FrozenInputMismatchError,
    ProvenanceMismatchError,
    ProvenanceMissingError,
    RequestFrozenMismatchError,
    Sha256MismatchError,
    SnapshotMissingError,
    SnapshotValidationError,
)
from dashboard.research_charts.historical_lmv2.mixed_catalog import MixedFrozenProductionCatalog
from dashboard.research_charts.historical_lmv2.provenance_lookup import (
    ProvenanceEntry,
    ProvenanceLookup,
    relative_dest_for,
)
from dashboard.research_charts.historical_lmv2.provider import (
    HistoricalLmV2PoolStore,
    validate_frozen_input,
    validate_snapshot_payload,
)
from dashboard.research_charts.historical_lmv2.decision_snapshot import validate_snapshot_active_state

UTC = timezone.utc
MIXED_ID = "doge_apr_sep_2026_v1"
FROZEN_NEW = "84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93"
FROZEN_CFAE = "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b"
FROZEN_FE549 = "fe549309ef047ea092bd1fa3eba5c16fae647db7f3fad95dc567b47c66ebcb66"

APRIL = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
JULY = datetime(2026, 7, 7, 7, 0, tzinfo=UTC)
SEPTEMBER = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def mixed_catalog() -> MixedFrozenProductionCatalog:
    cat = resolve_registered_catalog(MIXED_ID)
    assert isinstance(cat, MixedFrozenProductionCatalog)
    return cat


def test_catalog_registered():
    assert MIXED_ID in list_registered_catalog_ids()


def test_mixed_frozen_april_load(mixed_catalog):
    store = HistoricalLmV2PoolStore(mixed_catalog)
    payload = store.load_payload("15m", 10, APRIL)
    assert (payload.get("frozen_input") or {}).get("snapshot_id") == FROZEN_NEW


def test_mixed_frozen_july_load(mixed_catalog):
    store = HistoricalLmV2PoolStore(mixed_catalog)
    payload = store.load_payload("15m", 10, JULY)
    assert (payload.get("frozen_input") or {}).get("snapshot_id") == FROZEN_CFAE


def test_mixed_frozen_september_4h_load(mixed_catalog):
    store = HistoricalLmV2PoolStore(mixed_catalog)
    payload = store.load_payload("4h", 10, SEPTEMBER)
    assert (payload.get("frozen_input") or {}).get("snapshot_id") == FROZEN_FE549


def test_unauthorized_expected_frozen_fails():
    with pytest.raises(RequestFrozenMismatchError):
        load_decision_snapshot(
            catalog_id=MIXED_ID,
            symbol="DOGEUSDT",
            timeframe="15m",
            pool_detail=10,
            decision_close=APRIL,
            expected_frozen_input_id="deadbeef" * 8,
        )


def test_wrong_expected_frozen_for_path_fails(mixed_catalog):
    with pytest.raises(RequestFrozenMismatchError):
        load_decision_snapshot(
            catalog_id=MIXED_ID,
            symbol="DOGEUSDT",
            timeframe="15m",
            pool_detail=10,
            decision_close=JULY,
            expected_frozen_input_id=FROZEN_NEW,
        )


def test_missing_snapshot_fails(mixed_catalog):
    store = HistoricalLmV2PoolStore(mixed_catalog)
    with pytest.raises(SnapshotMissingError):
        store.load_payload("15m", 10, datetime(2026, 4, 1, 0, 1, tzinfo=UTC))


def test_wrong_tf_pd_fails(mixed_catalog):
    store = HistoricalLmV2PoolStore(mixed_catalog)
    path = store.snapshot_path("15m", 10, APRIL)
    payload = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(SnapshotValidationError):
        validate_snapshot_payload(payload, timeframe="15m", pool_detail=5, stem=path.stem)


def test_invalid_active_fingerprint_fails(mixed_catalog):
    store = HistoricalLmV2PoolStore(mixed_catalog)
    path = store.snapshot_path("15m", 10, APRIL)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["active_state_fingerprint"] = "bad"
    with pytest.raises(SnapshotValidationError):
        validate_snapshot_active_state(
            payload,
            symbol="DOGEUSDT",
            timeframe="15m",
            pool_detail=10,
            decision_close=APRIL,
        )


def test_sha256_mismatch_fails(mixed_catalog):
    store = HistoricalLmV2PoolStore(mixed_catalog)
    path = store.snapshot_path("15m", 10, APRIL)
    rel = relative_dest_for(mixed_catalog.production_root, path)
    entry = mixed_catalog.provenance.get(rel)
    bad = ProvenanceLookup(
        entries={rel: ProvenanceEntry(source_frozen_id=entry.source_frozen_id, dest_sha256="0" * 64)}
    )
    broken = replace(mixed_catalog, provenance=bad)
    with pytest.raises(Sha256MismatchError):
        HistoricalLmV2PoolStore(broken).load_payload("15m", 10, APRIL)


def test_provenance_missing_fails(mixed_catalog):
    store = HistoricalLmV2PoolStore(mixed_catalog)
    path = store.snapshot_path("15m", 10, APRIL)
    rel = relative_dest_for(mixed_catalog.production_root, path)
    empty = ProvenanceLookup(entries={})
    broken = replace(mixed_catalog, provenance=empty)
    with pytest.raises(ProvenanceMissingError):
        HistoricalLmV2PoolStore(broken).load_payload("15m", 10, APRIL)


def test_provenance_frozen_mismatch_fails(mixed_catalog):
    store = HistoricalLmV2PoolStore(mixed_catalog)
    path = store.snapshot_path("15m", 10, JULY)
    rel = relative_dest_for(mixed_catalog.production_root, path)
    entry = mixed_catalog.provenance.get(rel)
    bad = ProvenanceLookup(
        entries={rel: ProvenanceEntry(source_frozen_id=FROZEN_NEW, dest_sha256=entry.dest_sha256)}
    )
    broken = replace(mixed_catalog, provenance=bad)
    with pytest.raises(ProvenanceMismatchError):
        HistoricalLmV2PoolStore(broken).load_payload("15m", 10, JULY)


def test_backtester_chart_parity_april_july_sep(mixed_catalog):
    cases = [
        (APRIL, "15m", 10, FROZEN_NEW),
        (JULY, "15m", 10, FROZEN_CFAE),
        (SEPTEMBER, "4h", 10, FROZEN_FE549),
    ]
    for dc, tf, pd, fid in cases:
        api = load_decision_snapshot(
            catalog_id=MIXED_ID,
            symbol="DOGEUSDT",
            timeframe=tf,
            pool_detail=pd,
            decision_close=dc,
            expected_frozen_input_id=fid,
        )
        store = HistoricalLmV2PoolStore(mixed_catalog)
        payload = store.load_payload(tf, pd, dc)
        legacy = store.load_legacy_pools(tf, pd, dc)
        assert api["frozen_input_id"] == fid
        zones = snapshot_pools_to_chart_zones(
            payload, decision_close=dc, symbol="DOGEUSDT", timeframe=tf, pool_detail=pd
        )
        assert_render_zone_parity(payload, zones)
        assert {p["pool_id"] for p in legacy} == set(api["active_pool_ids"])


def test_legacy_catalog_regression_still_strict():
    cat = resolve_registered_catalog("doge_multi_month_2026")
    assert not getattr(cat, "mixed_frozen", False)
    store = HistoricalLmV2PoolStore(cat)
    payload = store.load_payload("15m", 10, JULY)
    bad = dict(payload)
    bad["frozen_input"] = dict(payload["frozen_input"])
    bad["frozen_input"]["snapshot_id"] = FROZEN_NEW
    with pytest.raises(FrozenInputMismatchError):
        validate_frozen_input(bad, cat)

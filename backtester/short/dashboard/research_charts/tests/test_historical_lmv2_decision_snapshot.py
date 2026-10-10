"""Phase 1: backtester provider vs chart adapter parity on frozen snapshots."""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_repo = Path(__file__).resolve().parents[5]
_dash = _repo / "backtester" / "short"
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_dash) not in sys.path:
    sys.path.insert(0, str(_dash))

from dashboard.research_charts.historical_lmv2.catalog import assert_month_allowed
from dashboard.research_charts.historical_lmv2.provider import combo_folder
from dashboard.research_charts.historical_lmv2.catalog_registry import resolve_registered_catalog
from dashboard.research_charts.historical_lmv2.chart_adapter import (
    active_pool_records,
    snapshot_pools_to_chart_zones,
    zone_geometry_by_pool_id,
)
from dashboard.research_charts.historical_lmv2.decision_snapshot import (
    assert_render_zone_parity,
    load_decision_snapshot,
    parse_decision_close,
    validate_snapshot_active_state,
)
from dashboard.research_charts.historical_lmv2.errors import (
    ManifestGateError,
    RequestFrozenMismatchError,
    SnapshotMissingError,
    SnapshotValidationError,
    UnknownCatalogError,
)
from dashboard.research_charts.historical_lmv2.provider import HistoricalLmV2PoolStore
from dashboard.research_charts.historical_lmv2.strategy_paths import ensure_strategy_paths

UTC = timezone.utc
CATALOG_ID = "doge_multi_month_2026"
FROZEN_JULY = "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b"
TRADE4_POOL = "lldv2:DOGEUSDT:15m:micro:upper:1783406700"
TRADE4_DECISION = datetime(2026, 7, 7, 7, 0, tzinfo=UTC)
TRADE4_ENTRY_OPEN = datetime(2026, 7, 7, 6, 45, tzinfo=UTC)


def _api_snapshot():
    return load_decision_snapshot(
        catalog_id=CATALOG_ID,
        symbol="DOGEUSDT",
        timeframe="15m",
        pool_detail=10,
        decision_close=TRADE4_DECISION,
        expected_frozen_input_id=FROZEN_JULY,
    )


def _provider_payload():
    cat = resolve_registered_catalog(CATALOG_ID)
    store = HistoricalLmV2PoolStore(cat)
    return store.load_payload("15m", 10, TRADE4_DECISION)


def test_strategy_decision_close_is_bar_close_not_open():
    """Backtester loads pools at bar close_time (strategy_loop), not entry_open."""
    ensure_strategy_paths()
    from find_short_entry_15m_v1 import entry_setup

    payload = _provider_payload()
    store = HistoricalLmV2PoolStore(resolve_registered_catalog(CATALOG_ID))
    pools = store.load_legacy_pools("15m", 10, TRADE4_DECISION)
    bar = {
        "open_time": TRADE4_ENTRY_OPEN,
        "close_time": TRADE4_DECISION,
        "open": 0.075,
        "high": 0.0755,
        "low": 0.0752,
        "close": 0.07543,
    }
    setup = entry_setup(pools, bar, TRADE4_DECISION)
    assert setup is not None
    pool, _upper, _lower, _gap = setup
    assert pool["pool_id"] == TRADE4_POOL


def test_trade4_provider_vs_chart_service_parity():
    provider_payload = _provider_payload()
    api_body = _api_snapshot()

    provider_records = active_pool_records(provider_payload)
    api_pools = api_body["pools"]
    assert len(provider_records) == api_body["active_pool_count"]
    assert {r["pool_id"] for r in provider_records} == set(api_body["active_pool_ids"])

    by_id = {p["pool_id"]: p for p in api_pools}
    for row in provider_records:
        pid = str(row["pool_id"])
        api_row = by_id[pid]
        assert float(row["bottom_price"]) == float(api_row["bottom_price"])
        assert float(row["top_price"]) == float(api_row["top_price"])
        assert str(row["side"]) == str(api_row["side"])
        assert row.get("strength_class") == api_row.get("strength_class")
        assert str(row.get("known_at")) == str(api_row.get("known_at"))

    assert api_body["active_state_fingerprint"] == str(
        provider_payload.get("active_state_fingerprint") or ""
    )
    assert api_body["frozen_input_id"] == FROZEN_JULY
    assert parse_decision_close(api_body["decision_close"]) == TRADE4_DECISION
    assert TRADE4_POOL in api_body["active_pool_ids"]

    zgeom = zone_geometry_by_pool_id(api_body["zones"])
    for row in provider_records:
        pid = str(row["pool_id"])
        zb, zt = zgeom[pid]
        assert zb == float(row["bottom_price"])
        assert zt == float(row["top_price"])
    assert api_body["parity_validation"]["status"] == "PASS"


def test_independent_zone_mapping_matches_payload():
    payload = _provider_payload()
    zones = snapshot_pools_to_chart_zones(
        payload,
        decision_close=TRADE4_DECISION,
        symbol="DOGEUSDT",
        timeframe="15m",
        pool_detail=10,
    )
    assert_render_zone_parity(payload, zones)


def test_unknown_catalog_fail_closed():
    with pytest.raises(UnknownCatalogError):
        load_decision_snapshot(
            catalog_id="not_a_real_catalog",
            symbol="DOGEUSDT",
            timeframe="15m",
            pool_detail=10,
            decision_close=TRADE4_DECISION,
            expected_frozen_input_id=FROZEN_JULY,
        )


def test_wrong_frozen_id_fail_closed():
    with pytest.raises(RequestFrozenMismatchError):
        load_decision_snapshot(
            catalog_id=CATALOG_ID,
            symbol="DOGEUSDT",
            timeframe="15m",
            pool_detail=10,
            decision_close=TRADE4_DECISION,
            expected_frozen_input_id="deadbeef",
        )


def test_missing_snapshot_fail_closed():
    # In-catalog month but no published JSON for this close.
    with pytest.raises(SnapshotMissingError):
        load_decision_snapshot(
            catalog_id=CATALOG_ID,
            symbol="DOGEUSDT",
            timeframe="15m",
            pool_detail=10,
            decision_close=datetime(2026, 7, 7, 7, 1, tzinfo=UTC),
            expected_frozen_input_id=FROZEN_JULY,
        )


def test_partial_month_manifest_gate():
    cat = resolve_registered_catalog(CATALOG_ID)
    with pytest.raises(ManifestGateError):
        assert_month_allowed(cat, "2026-04", combo_folder("15m", 10))


def test_wrong_pool_detail_no_file():
    with pytest.raises(SnapshotMissingError):
        load_decision_snapshot(
            catalog_id=CATALOG_ID,
            symbol="DOGEUSDT",
            timeframe="15m",
            pool_detail=99,
            decision_close=TRADE4_DECISION,
            expected_frozen_input_id=FROZEN_JULY,
        )


def _minimal_payload() -> dict:
    dc = TRADE4_DECISION
    pid = "lldv2:DOGEUSDT:15m:micro:upper:1"
    return {
        "symbol": "DOGEUSDT",
        "timeframe": "15m",
        "pool_detail": 10,
        "decision_close": dc.isoformat(),
        "active_state_fingerprint": "a" * 64,
        "active_pool_ids": [pid],
        "active_sides": ["upper"],
        "pools": {
            pid: {
                "pool_id": pid,
                "side": "upper",
                "top_price": 0.1,
                "bottom_price": 0.09,
                "known_at": (dc - timedelta(hours=1)).isoformat(),
                "strength": 1.0,
                "strength_class": "Weak",
            }
        },
    }


def test_known_at_in_future_rejected():
    p = _minimal_payload()
    pid = p["active_pool_ids"][0]
    p["pools"][pid]["known_at"] = (TRADE4_DECISION + timedelta(minutes=5)).isoformat()
    with pytest.raises(SnapshotValidationError):
        validate_snapshot_active_state(
            p,
            symbol="DOGEUSDT",
            timeframe="15m",
            pool_detail=10,
            decision_close=TRADE4_DECISION,
        )


def test_duplicate_active_pool_id_rejected():
    p = _minimal_payload()
    p["active_pool_ids"] = [p["active_pool_ids"][0], p["active_pool_ids"][0]]
    p["active_sides"] = ["upper", "upper"]
    with pytest.raises(SnapshotValidationError):
        validate_snapshot_active_state(
            p,
            symbol="DOGEUSDT",
            timeframe="15m",
            pool_detail=10,
            decision_close=TRADE4_DECISION,
        )


def test_missing_pool_id_rejected():
    p = _minimal_payload()
    p["active_pool_ids"].append("lldv2:DOGEUSDT:15m:micro:upper:missing")
    p["active_sides"].append("upper")
    with pytest.raises(ValueError):
        active_pool_records(p)


def test_manipulated_bounds_rejected_on_render():
    p = _provider_payload()
    bad = copy.deepcopy(p)
    pid = bad["active_pool_ids"][0]
    bad["pools"][pid]["top_price"] = float(bad["pools"][pid]["top_price"]) + 0.001
    zones = snapshot_pools_to_chart_zones(
        bad,
        decision_close=TRADE4_DECISION,
        symbol="DOGEUSDT",
        timeframe="15m",
        pool_detail=10,
    )
    with pytest.raises(SnapshotValidationError):
        assert_render_zone_parity(p, zones)


def test_strength_class_preserved_in_api_pools():
    body = _api_snapshot()
    row = next(p for p in body["pools"] if p["pool_id"] == TRADE4_POOL)
    provider = _provider_payload()["pools"][TRADE4_POOL]
    assert row["strength_class"] == provider["strength_class"]
    assert row["strength"] == provider["strength"]

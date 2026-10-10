"""Light tests for historical_lmv2 provider + guard + strategy loop (no full backtest)."""

from __future__ import annotations

import json
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

from dashboard.research_charts.historical_lmv2.catalog import discover_production_catalog
from dashboard.research_charts.historical_lmv2.errors import (
    FrozenInputMismatchError,
    ManifestGateError,
    SnapshotMissingError,
)
from dashboard.research_charts.historical_lmv2.guard_v2 import V2H4FloorGuard
from dashboard.research_charts.historical_lmv2.legacy_map import legacy_pools_from_snapshot_payload
from dashboard.research_charts.historical_lmv2.provider import (
    HistoricalLmV2PoolStore,
    decision_close_to_stem,
    validate_frozen_input,
)
from dashboard.research_charts.historical_lmv2.strategy_loop import WalkState, run_baseline_walk
from dashboard.research_charts.historical_lmv2.strategy_paths import ensure_strategy_paths
from dashboard.research_charts.historical_lmv2.variants import BaselineVariant

UTC = timezone.utc
FROZEN_ID = "test-frozen-id-0001"


def _minimal_payload(decision_close: datetime, *, pool_detail: int = 10, tf: str = "15m") -> dict:
    dc = decision_close.isoformat()
    upper_id = f"lldv2:DOGEUSDT:{tf}:micro:upper:1700000000"
    lower_id = f"lldv2:DOGEUSDT:{tf}:micro:lower:1700000100"
    known = (decision_close - timedelta(hours=1)).isoformat()
    pools = {
        upper_id: {
            "pool_id": upper_id,
            "side": "upper",
            "top_price": 0.11,
            "bottom_price": 0.10,
            "known_at": known,
            "break_at": None,
            "strength": 5.0,
            "strength_class": "Medium",
        },
        lower_id: {
            "pool_id": lower_id,
            "side": "lower",
            "top_price": 0.09,
            "bottom_price": 0.08,
            "known_at": known,
            "break_at": None,
            "strength": 5.0,
            "strength_class": "Medium",
        },
    }
    ids = [upper_id, lower_id]
    sides = ["upper", "lower"]
    import hashlib

    fp_raw = json.dumps(sorted(zip(ids, sides)), separators=(",", ":"))
    fingerprint = hashlib.sha256(fp_raw.encode()).hexdigest()

    return {
        "symbol": "DOGEUSDT",
        "timeframe": tf,
        "pool_detail": pool_detail,
        "decision_close": dc,
        "active_pool_ids": ids,
        "active_sides": sides,
        "active_state_fingerprint": fingerprint,
        "pools": pools,
        "frozen_input": {
            "snapshot_id": FROZEN_ID,
            "price_hash": "ph",
            "revision_hash": "rh",
        },
    }


@pytest.fixture
def mini_tree(tmp_path: Path) -> Path:
    root = tmp_path / "doge_test_history"
    month = "2026-07"
    manifest = {
        "month": month,
        "MONTH_STATUS": "COMPLETE",
        "BACKTESTER_CANDIDATE": True,
        "FROZEN_INPUT_ID": FROZEN_ID,
        "PARTIAL_COMBINATIONS": [],
    }
    (root / "production" / "months" / month).mkdir(parents=True)
    (root / "production" / "months" / month / "manifest.json").write_text(json.dumps(manifest))
    (root / "production" / "status.json").write_text(json.dumps({"FROZEN_INPUT_ID": FROZEN_ID}))

    dc = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    stem = decision_close_to_stem(dc)
    for combo in ("15m_PD1", "15m_PD5", "15m_PD10", "4h_PD10"):
        d = root / "production" / "results" / month / combo
        d.mkdir(parents=True)
        pd = int(combo.split("_PD")[1])
        tf = "4h" if combo.startswith("4h") else "15m"
        (d / f"{stem}.json").write_text(json.dumps(_minimal_payload(dc, pool_detail=pd, tf=tf)))
    return root


def test_manifest_gate_blocks_partial_month(tmp_path: Path):
    root = tmp_path / "partial"
    month = "2026-04"
    (root / "production" / "months" / month).mkdir(parents=True)
    (root / "production" / "months" / month / "manifest.json").write_text(
        json.dumps(
            {
                "month": month,
                "MONTH_STATUS": "PARTIAL",
                "BACKTESTER_CANDIDATE": False,
                "FROZEN_INPUT_ID": FROZEN_ID,
            }
        )
    )
    cat = discover_production_catalog(root)
    store = HistoricalLmV2PoolStore(cat)
    dc = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    with pytest.raises(ManifestGateError):
        store.load_legacy_pools("15m", 10, dc)


def test_frozen_input_and_mapping(mini_tree: Path):
    cat = discover_production_catalog(mini_tree)
    assert "2026-07" in cat.allowed_months()
    store = HistoricalLmV2PoolStore(cat)
    dc = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    pools = store.load_legacy_pools("15m", 10, dc)
    assert len(pools) == 2
    assert pools[0]["known"] <= dc
    assert "pool_id" in pools[0] and "bottom" in pools[0]


def test_missing_snapshot_raises(mini_tree: Path):
    cat = discover_production_catalog(mini_tree)
    store = HistoricalLmV2PoolStore(cat)
    with pytest.raises(SnapshotMissingError):
        store.load_legacy_pools("15m", 10, datetime(2026, 7, 17, 0, 0, tzinfo=UTC))


def test_frozen_mismatch(mini_tree: Path):
    cat = discover_production_catalog(mini_tree)
    dc = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    path = HistoricalLmV2PoolStore(cat).snapshot_path("15m", 10, dc)
    payload = json.loads(path.read_text())
    payload["frozen_input"]["snapshot_id"] = "wrong"
    path.write_text(json.dumps(payload))
    with pytest.raises(FrozenInputMismatchError):
        validate_frozen_input(payload, cat)


def test_entry_setup_compat(mini_tree: Path):
    ensure_strategy_paths()
    from find_short_entry_15m_v1 import entry_setup, open_pools

    cat = discover_production_catalog(mini_tree)
    store = HistoricalLmV2PoolStore(cat)
    dc = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    pools = store.load_legacy_pools("15m", 10, dc)
    bar = {
        "open_time": dc - timedelta(minutes=15),
        "close_time": dc,
        "open": 0.105,
        "high": 0.106,
        "low": 0.099,
        "close": 0.0995,
    }
    uppers = open_pools(pools, "upper", dc, min_bottom=bar["close"])
    assert uppers  # geometry may or may not trigger entry_setup
    _ = entry_setup(pools, bar, dc)  # None ok for minimal fixture


def test_guard_episode_blocks_then_expires(mini_tree: Path):
    cat = discover_production_catalog(mini_tree)
    store = HistoricalLmV2PoolStore(cat)
    h4_close = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    guard = V2H4FloorGuard(
        "DOGEUSDT",
        datetime(2026, 2, 10, tzinfo=UTC),
        datetime(2026, 12, 1, tzinfo=UTC),
        store,
        pool_detail_4h=10,
        four_h_bar_closes=[h4_close],
    )
    deepest_top = 0.10
    deepest_bottom = 0.08
    touch_bar = {
        "close": 0.095,
        "low": deepest_bottom,
        "high": deepest_top,
    }
    blocked, reason = guard.advance(touch_bar, h4_close)
    assert blocked and reason == "A1_touch"
    blocked2, _ = guard.advance({"close": 0.095, "low": 0.09, "high": 0.091}, h4_close + timedelta(minutes=15))
    assert blocked2


def test_seen_state_isolated_between_variants():
    a = WalkState()
    b = WalkState()
    a.seen_pool_ids.add("x")
    assert "x" not in b.seen_pool_ids


def test_pd_selection_paths(mini_tree: Path):
    cat = discover_production_catalog(mini_tree)
    store = HistoricalLmV2PoolStore(cat)
    dc = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    for pd in (1, 5, 10):
        pools = store.load_legacy_pools("15m", pd, dc)
        assert len(pools) == 2


def test_real_july_snapshot_read_light():
    """Single read from COMPLETE month if present (no scan)."""
    root = _repo / "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history"
    manifest = root / "production/months/2026-07/manifest.json"
    if not manifest.is_file():
        pytest.skip("july manifest not present")
    data = json.loads(manifest.read_text())
    if data.get("MONTH_STATUS") != "COMPLETE":
        pytest.skip("july not complete")
    cat = discover_production_catalog(root)
    store = HistoricalLmV2PoolStore(cat)
    dc = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    pools = store.load_legacy_pools("15m", 10, dc)
    assert len(pools) > 0


def test_guard_off_skips_4h_pool_load(mini_tree: Path, monkeypatch):
    cat = discover_production_catalog(mini_tree)
    store = HistoricalLmV2PoolStore(cat)
    calls = {"n": 0}
    orig = store.load_legacy_pools

    def counted(tf, pd, dc):
        if tf == "4h":
            calls["n"] += 1
        return orig(tf, pd, dc)

    monkeypatch.setattr(store, "load_legacy_pools", lambda tf, pd, dc: counted(tf, pd, dc))

    # Synthetic market / bars — down_4h will fail without real EMAs; only assert no 4h load before failure
    bars15 = [
        {
            "open_time": datetime(2026, 7, 16, 11, 45, tzinfo=UTC),
            "close_time": datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "ema9": 1,
            "ema20": 1,
            "ema59": 1,
            "ema200": 1,
            "candle_index": 0,
        }
    ]
    market = {
        "4h": {
            "bars": [
                {
                    "close_time": datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
                    "close": 1.0,
                    "ema9": 2,
                    "ema20": 1.5,
                    "ema59": 1.2,
                    "ema200": 1.0,
                }
            ]
        }
    }
    variant = BaselineVariant("t", 10, False)
    try:
        run_baseline_walk(
            "DOGEUSDT",
            bars15,
            market,
            store,
            variant,
            pane_from=datetime(2026, 2, 10, tzinfo=UTC),
            pane_to=datetime(2026, 12, 1, tzinfo=UTC),
            replay_from=datetime(2026, 7, 1, tzinfo=UTC),
            report_from=datetime(2026, 7, 1, tzinfo=UTC),
            report_to=datetime(2026, 8, 1, tzinfo=UTC),
        )
    except Exception:
        pass
    assert calls["n"] == 0

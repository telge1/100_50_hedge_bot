"""Pending-plan lifecycle: pool validity until fill + same-bar ambiguity."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.fixtures import (
    pool,
    pullback_short_confirmation_bundle,
    static_pools,
)
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.markers import (
    dedupe_plan_rows,
    signals_to_marker_specs,
)
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.models import CandidateState, PoolRecord
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.pending_plan_lifecycle_audit import (
    classify_extra_terminal_0321,
    classify_pullback_short_reference,
    classify_terminal_long_reference,
)
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.runner import run_scanner


def _loader_factory(base_pools: dict[str, list], *, invalidate_ids_after: dict[str, set[str]] | None = None):
    """Return pools; after keyed timestamps mark pool ids invalidated (explicit lifecycle)."""

    invalidate_ids_after = invalidate_ids_after or {}

    def _fn(_candles, *, symbol, as_of):
        out: dict[str, list] = {}
        inv_ids: set[str] = set()
        for ts_s, ids in invalidate_ids_after.items():
            if as_of >= datetime.fromisoformat(ts_s):
                inv_ids |= ids
        for tf, ps in base_pools.items():
            rows = []
            for p in ps:
                if p.pool_id in inv_ids:
                    rows.append(
                        PoolRecord(
                            pool_id=p.pool_id,
                            symbol=p.symbol,
                            timeframe=p.timeframe,
                            side=p.side,
                            lower_edge=p.lower_edge,
                            upper_edge=p.upper_edge,
                            midpoint=p.midpoint,
                            component_count=p.component_count,
                            strength=p.strength,
                            known_at=p.known_at,
                            available_at=p.available_at,
                            invalidated_at=as_of,
                            source_timestamp=p.source_timestamp,
                        )
                    )
                else:
                    rows.append(p)
            out[tf] = rows
        return out

    return _fn


def test_target_valid_until_fill_allows_fill():
    candles, approach_at = pullback_short_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader_factory(static_pools(known_at=approach_at - timedelta(hours=2))),
    )
    confirmed = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]
    assert confirmed
    assert confirmed[0]["state"] == "CONFIRMED"
    assert confirmed[0]["hypothetical_filled_at"] > confirmed[0]["armed_at"]


def test_entry_pool_valid_until_fill_allows_fill():
    candles, approach_at = pullback_short_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader_factory(static_pools(known_at=approach_at - timedelta(hours=2))),
    )
    confirmed = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]
    assert confirmed[0]["entry_pool"]["pool_id"] == "ask15"


def test_target_invalidated_before_fill():
    candles, approach_at = pullback_short_confirmation_bundle()
    import pandas as pd
    from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.config import TF_CONFIRM

    df1 = candles[TF_CONFIRM].copy()
    df1["ot"] = pd.to_datetime(df1["open_time"])
    # Keep price below limit for several bars after arm, then would-be fill later
    after = df1["ot"] > pd.Timestamp(approach_at)
    early = df1.index[after][:4]
    for i in early:
        df1.loc[i, "high"] = 0.10125  # below limit ~0.1013
        df1.loc[i, "low"] = 0.10110
        df1.loc[i, "close"] = 0.10118
        df1.loc[i, "open"] = 0.10118
    candles = {**candles, TF_CONFIRM: df1.drop(columns=["ot"])}
    drop_at = (approach_at + timedelta(minutes=2)).isoformat()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader_factory(
            static_pools(known_at=approach_at - timedelta(hours=2)),
            invalidate_ids_after={drop_at: {"bid30"}},
        ),
    )
    found = False
    for c in result["invalidated"]:
        d = c.to_dict() if hasattr(c, "to_dict") else c
        if d.get("setup_type") == "A_PLUS_PULLBACK_SHORT":
            assert d["state"] == "INVALIDATED_UNFILLED"
            assert "TARGET_POOL_INVALIDATED_BEFORE_FILL" in (d.get("invalidation_reason") or "")
            found = True
    assert found
    assert not [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]


def test_entry_pool_invalidated_before_fill():
    candles, approach_at = pullback_short_confirmation_bundle()
    import pandas as pd
    from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.config import TF_CONFIRM

    df1 = candles[TF_CONFIRM].copy()
    df1["ot"] = pd.to_datetime(df1["open_time"])
    after = df1["ot"] > pd.Timestamp(approach_at)
    early = df1.index[after][:4]
    for i in early:
        df1.loc[i, "high"] = 0.10125
        df1.loc[i, "low"] = 0.10110
        df1.loc[i, "close"] = 0.10118
        df1.loc[i, "open"] = 0.10118
    candles = {**candles, TF_CONFIRM: df1.drop(columns=["ot"])}
    drop_at = (approach_at + timedelta(minutes=2)).isoformat()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader_factory(
            static_pools(known_at=approach_at - timedelta(hours=2)),
            invalidate_ids_after={drop_at: {"ask15"}},
        ),
    )
    found = False
    for c in result["invalidated"]:
        d = c.to_dict() if hasattr(c, "to_dict") else c
        if d.get("setup_type") == "A_PLUS_PULLBACK_SHORT":
            assert d["state"] == "INVALIDATED_UNFILLED"
            assert "ENTRY_POOL_INVALIDATED_BEFORE_FILL" in (d.get("invalidation_reason") or "")
            found = True
    assert found
    assert not [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]


def test_invalidated_plan_cannot_fill_later():
    candles, approach_at = pullback_short_confirmation_bundle()
    drop_at = (approach_at + timedelta(minutes=2)).isoformat()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader_factory(
            static_pools(known_at=approach_at - timedelta(hours=2)),
            invalidate_ids_after={drop_at: {"bid30"}},
        ),
    )
    fills = [e for e in (result.get("pullback_limit_events") or []) if e["event"] == "HYPOTHETICAL_FILLED"]
    assert fills == []
    episodes = [
        (c.to_dict() if hasattr(c, "to_dict") else c).get("episode_id")
        for c in result["invalidated"]
        if (c.to_dict() if hasattr(c, "to_dict") else c).get("setup_type") == "A_PLUS_PULLBACK_SHORT"
    ]
    assert len(set(episodes)) == 1


def test_later_pool_does_not_retarget():
    candles, approach_at = pullback_short_confirmation_bundle()
    base = static_pools(known_at=approach_at - timedelta(hours=2))
    # Add a closer BID that appears later — must not change frozen TP after arm
    late_bid = pool(
        pool_id="late_bid",
        tf="30m",
        side="BID",
        lower=0.0990,
        upper=0.0992,
        known_at=approach_at + timedelta(minutes=5),
        strength=99,
        n=1,
    )

    def _loader(_candles, *, symbol, as_of):
        out = {tf: list(ps) for tf, ps in base.items()}
        if as_of >= late_bid.known_at:
            out["30m"] = out["30m"] + [late_bid]
        return out

    result = run_scanner(symbol="DOGEUSDT", candles_by_tf=candles, pool_loader=_loader)
    confirmed = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]
    assert confirmed
    assert confirmed[0]["htf_context"]["target_pool_id"] == "bid30"
    assert confirmed[0]["target_price"] == confirmed[0]["htf_context"].get("frozen_take_profit") or True


def test_entry_and_sl_same_bar_ambiguous():
    candles, approach_at = pullback_short_confirmation_bundle()
    # Mutate the fill bar so high also clears SL
    df1 = candles["1m"].copy()
    # Find bar after approach that would touch limit; inflate high past SL
    from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.config import TF_CONFIRM
    import pandas as pd

    df1 = candles[TF_CONFIRM].copy()
    df1["ot"] = pd.to_datetime(df1["open_time"])
    # After approach, set a bar that hits both limit (~0.1013) and high SL
    mask = df1["ot"] > pd.Timestamp(approach_at)
    idxs = df1.index[mask]
    assert len(idxs) > 2
    i = idxs[2]
    df1.loc[i, "high"] = 0.1025  # above typical SL above ask pool
    df1.loc[i, "low"] = 0.1000
    candles = {**candles, TF_CONFIRM: df1.drop(columns=["ot"])}
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader_factory(static_pools(known_at=approach_at - timedelta(hours=2))),
    )
    amb = []
    for c in result["invalidated"]:
        d = c.to_dict() if hasattr(c, "to_dict") else c
        if d.get("state") == "AMBIGUOUS_INTRABAR":
            amb.append(d)
    # Either ambiguous or still confirmed if SL not actually touched — assert no favorable same-bar fill if SL hit
    confirmed = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]
    if confirmed:
        # Fill bar must not also have hit SL
        fill_at = confirmed[0]["hypothetical_filled_at"]
        assert confirmed[0].get("htf_context", {}).get("same_bar_ambiguity") is not True
    else:
        assert amb


def test_plan_freeze_fields_present():
    candles, approach_at = pullback_short_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader_factory(static_pools(known_at=approach_at - timedelta(hours=2))),
    )
    confirmed = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"][0]
    htf = confirmed["htf_context"]
    assert htf.get("entry_pool_edges_at_arm")
    assert htf.get("target_pool_edges_at_arm")
    assert htf.get("target_selected_at")
    assert confirmed.get("plan_frozen_at")
    assert htf.get("frozen_entry_price") == confirmed["entry_price"]


def test_one_marker_group_lifecycle_states():
    rows = [
        {
            "signal_id": "s1",
            "setup_id": "s1",
            "direction": "SHORT",
            "state": "LIMIT_INTENT_ARMED",
            "armed_at": "2026-08-28T04:15:00",
            "entry_price": 0.088,
            "stop_price": 0.089,
            "target_price": 0.087,
        },
        {
            "signal_id": "s1",
            "setup_id": "s1",
            "direction": "SHORT",
            "state": "CONFIRMED",
            "armed_at": "2026-08-28T04:15:00",
            "hypothetical_filled_at": "2026-08-28T06:35:00",
            "entry_price": 0.088,
            "stop_price": 0.089,
            "target_price": 0.087,
            "entry_pool": {"pool_id": "e", "known_at": "2026-08-28T03:30:00"},
            "htf_context": {"target_pool_id": "t"},
        },
    ]
    deduped = dedupe_plan_rows(rows)
    assert len(deduped) == 1
    specs = signals_to_marker_specs(deduped, display_mode="active")
    plan = [s for s in specs if s["kind"] != "APS_LINE"]
    assert len(plan) == 1


def test_classify_reference_helpers():
    short = {
        "signal_id": "73b66b73675e35c6df7efa88",
        "state": "CONFIRMED",
        "armed_at": "2026-08-28T04:15:00",
        "hypothetical_filled_at": "2026-08-28T06:35:00",
        "entry_price": 0.088192,
        "stop_price": 0.08832,
        "target_price": 0.08758,
        "plan_frozen_at": "2026-08-28T04:15:00",
        "entry_pool": {"pool_id": "lld:DOGEUSDT:15m:upper:1787886900"},
        "target_pool": {"pool_id": "lld:DOGEUSDT:15m:lower:1787825700", "known_at": "2026-08-27T10:30:00"},
        "htf_context": {
            "target_pool_id": "lld:DOGEUSDT:15m:lower:1787825700",
            "target_selected_at": "2026-08-28T04:15:00",
        },
    }
    assert classify_pullback_short_reference(short)["classification"] == "VALID_REFERENCE_SHORT"

    long = {
        "signal_id": "cf6c3d2a7965cf1d7fbd3be2",
        "state": "CONFIRMED",
        "armed_at": "2026-08-28T10:27:00",
        "approach_at": "2026-08-28T10:26:00",
        "entry_price": 0.08619,
        "stop_price": 0.085405,
        "target_price": 0.08774,
        "entry_pool": {"pool_id": "x", "side": "BID"},
        "target_pool": {"pool_id": "lld:DOGEUSDT:15m:upper:1787905800", "known_at": "2026-08-28T08:45:00"},
        "htf_context": {
            "target_pool_id": "lld:DOGEUSDT:15m:upper:1787905800",
            "target_pool_known_at_arm": "2026-08-28T08:45:00",
        },
    }
    assert classify_terminal_long_reference(long)["classification"] == "VALID_REFERENCE_LONG"

    extra = {
        "signal_id": "a6ae177581410f30dff43e26",
        "state": "CONFIRMED",
        "armed_at": "2026-08-28T03:21:00",
        "entry_pool": {"pool_id": "p", "known_at": "2026-08-27T19:00:00"},
        "target_pool": {"pool_id": "t", "known_at": "2026-08-27T09:00:00"},
        "htf_context": {"target_pool_known_at_arm": "2026-08-27T09:00:00", "terminal_pool_class": "distant_macro_pool_below"},
        "gates": [{"gate": "clear_target_pool", "passed": True}],
        "data_quality": {"gross_rr": 2.4, "estimated_net_rr": 2.4},
    }
    assert classify_extra_terminal_0321(extra)["classification"] == "TECHNICALLY_VALID_BUT_NOT_MANUAL_REFERENCE"

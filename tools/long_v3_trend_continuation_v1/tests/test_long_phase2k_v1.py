"""Phase 2K — Variant A (frozen) vs B (dynamic upper) buy limit tests."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from long_research_config_v2d_v1 import LongResearchConfigV2DV1  # noqa: E402


def test_variant_a_default_buy_limit_semantics():
    cfg = LongResearchConfigV2DV1()
    assert cfg.buy_limit_semantics == "FROZEN_AT_PLACE_V2J"


def test_variant_b_label_includes_buy_semantics():
    cfg = LongResearchConfigV2DV1(
        support_mode="V2E_QUALIFIED",
        sl_mode="DEEPER_Q_UPPER",
        pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
        formation_semantics="CAUSAL_BELOW_PRICE_SEGMENT",
        touch_lifecycle="LINEAGE_AWARE_V2J",
        buy_limit_semantics="DYNAMIC_UPPER_GROWTH_V2K",
    )
    assert "buy=DYNAMIC_UPPER_GROWTH_V2K" in cfg.label()


def test_dynamic_module_noop_when_frozen():
    from long_dynamic_buy_limit_v2k_v1 import try_dynamic_upper_growth_reprice
    from long_entry_chronology_v2d_v1 import LongEntryStateV2DV1
    from long_pending_v1 import LongPendingOrderV1

    st = LongEntryStateV2DV1()
    st.pending = LongPendingOrderV1(
        order_id="1",
        decision_close_utc="t0",
        earliest_eligible_execution_utc="t1",
        pool_lower=0.079758,
        pool_upper=0.07997,
        pool_key="0.079758|0.07997",
        origin_ids="a|b",
        planned_entry=0.07997,
        planned_sl=0.078,
        lineage_id="lin_x",
    )
    cfg = LongResearchConfigV2DV1(buy_limit_semantics="FROZEN_AT_PLACE_V2J")
    n_ev = len(st.events)
    try_dynamic_upper_growth_reprice(
        st,
        bar="t2",
        close=0.081,
        snap={},
        config=cfg,
        session=None,
        lineage_tracker=None,
        streak_cache=None,
        bar_index=1,
        closes=["t0", "t1", "t2"],
        prices={"t2": {"c": 0.081}},
        exit_variant="LONG_A",
        ev_fn=lambda *a, **k: None,
        risk_blocked_fn=lambda *a, **k: (False, 1.0),
        origin_ids_from_support=lambda s: "a|b",
    )
    assert len(st.events) == n_ev
    assert st.pending.planned_entry == 0.07997

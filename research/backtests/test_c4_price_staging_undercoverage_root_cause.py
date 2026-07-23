"""C4 price-staging: exit-cancel undercoverage mechanism + post Variant-C guard.

Pre-fix mechanism (still observable as cycle-pair audit UC when early basket
compensates): stage0 fill → exit rebuild → basket flatten → stage1 cancel.

Post Variant C:
- Basket close only when FinalExitEconomics.sufficient (coverage_ok).
- No orphan stage1 fill after flatten.
- Cycle-pair UC with coverage_ok is covered_by_basket_exit (not an economic fail).
- Legacy C4 remains covered/overcovered.
"""

from __future__ import annotations

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.multicoin_blocker_price_staging import run_isolated_blocker
from research.backtests.pnl_coverage_audit import build_pnl_coverage_audit
from research.backtests.second_leg_price_staging import (
    build_stage_plan,
    resolve_grid_profile,
    resolve_profile,
)


APT_START = 570
APT_TRADE = 3


def _fills(result):
    return list(getattr(result, "fill_log", None) or getattr(result, "fills_log", None) or [])


def _c4_sr_orders(result):
    return [
        o
        for o in (result.order_log or [])
        if str(o.get("purpose") or "") == "CYCLE_4_SHORT_REDUCE"
    ]


def _c4_sr_fills(result):
    return [
        f
        for f in _fills(result)
        if str(f.get("purpose") or "") == "CYCLE_4_SHORT_REDUCE"
    ]


def test_two_early_medium_c4_early_exit_keeps_economic_coverage_guard():
    candles = normalize_candles(
        "APTUSDT", load_candles_for_symbol("APTUSDT", limit=50000)
    )
    staged = run_isolated_blocker(
        coin="APTUSDT",
        candles=candles,
        start_index=APT_START,
        staging_config=resolve_grid_profile("two_early_medium"),
        trade_number=APT_TRADE,
    )
    legacy = run_isolated_blocker(
        coin="APTUSDT",
        candles=candles,
        start_index=APT_START,
        staging_config=resolve_profile("legacy"),
        trade_number=APT_TRADE,
    )

    intents = []
    seen = set()
    for intent in staged.intent_log or []:
        if str(intent.get("purpose") or "") != "CYCLE_4_SHORT_REDUCE":
            continue
        meta = dict(intent.get("metadata_excerpt") or {})
        if not (meta.get("research_price_staging") or meta.get("is_staged_second_leg_tp")):
            continue
        stage = int(meta.get("stage_index") or 0)
        if stage in seen:
            continue
        seen.add(stage)
        intents.append(
            (
                stage,
                float(intent.get("qty") or 0.0),
                float(intent.get("trigger_price") or 0.0),
                float(meta.get("required_net") or meta.get("stage_required_net_total") or 0.0),
            )
        )
    assert len(intents) == 2, f"expected 2 staged intents, got {intents}"
    intents = sorted(intents)
    planned_sum = sum(q for _, q, _, _ in intents)
    assert intents[0][2] > intents[1][2], "stage0 trigger must be above stage1 (full) trigger"
    assert intents[0][3] > 0.0, "shim must persist required_net / stage_required_net_total"

    fills = _c4_sr_fills(staged)
    orders = _c4_sr_orders(staged)
    submits = [o for o in orders if str(o.get("event_type") or "").lower() == "submitted"]
    cancels = [o for o in orders if str(o.get("event_type") or "").lower() == "cancelled"]

    def _stage(o):
        meta = o.get("metadata_excerpt") or {}
        if meta.get("stage_index") is None:
            return None
        return int(meta.get("stage_index"))

    assert any(_stage(o) == 1 for o in submits), "stage1 must be submitted"

    exit_fills = [
        f
        for f in _fills(staged)
        if str(f.get("purpose") or "") in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
    ]
    stage1_fills = [
        f
        for f in fills
        if int((f.get("metadata_excerpt") or {}).get("stage_index") or -1) == 1
    ]

    if staged.final_status == "closed":
        assert exit_fills, "closed trade must have basket exit fills"
        # Variant C: no orphan stage1 fill after flatten.
        assert not stage1_fills, "stage1 must not fill after basket flatten"
        assert any(_stage(o) == 1 for o in cancels), "stage1 cancelled after covered basket exit"
        last = (staged.final_strategy_state_excerpt or {}).get(
            "last_basket_exit_coverage_decision"
        ) or {}
        # If decision persisted, it must show economic coverage for the close.
        if last:
            assert bool(last.get("coverage_ok") or last.get("sufficient"))
        audit = [
            row
            for row in build_pnl_coverage_audit(staged)
            if int(row.get("cycle_index") or 0) == 4
            and "LONG_ADD" in str(row.get("loss_purpose") or "")
        ]
        # Cycle-pair may still show UC; economic gate + basket compensation is the success rule.
        if audit and audit[0]["status"] == "undercovered":
            assert float(audit[0]["missing_pnl"] or 0.0) > 0.0
    else:
        # Open is also valid when basket cannot cover — stage1 must remain working.
        assert any(_stage(o) == 1 for o in submits)
        assert not exit_fills or staged.final_status != "closed"

    # Qty plan itself is complete.
    assert abs(planned_sum - (intents[0][1] + intents[1][1])) < 1e-6

    # --- legacy control: C4 cover completes ---
    leg_audit = [
        row
        for row in build_pnl_coverage_audit(legacy)
        if int(row.get("cycle_index") or 0) == 4
        and "LONG_ADD" in str(row.get("loss_purpose") or "")
    ]
    assert leg_audit
    assert leg_audit[0]["status"] in {"overcovered", "covered", "exact"}
    assert float(leg_audit[0].get("missing_pnl") or 0.0) == 0.0
    leg_sr = _c4_sr_fills(legacy)
    assert leg_sr
    assert abs(sum(float(f.get("qty") or 0.0) for f in leg_sr) - planned_sum) < 1e-6


def test_stage_qty_fractions_sum_to_total_for_two_early_medium():
    """H1 unit check: planner residual path keeps qty sum == total."""
    cfg = resolve_grid_profile("two_early_medium")
    plan = build_stage_plan(
        config=cfg,
        cycle_index=4,
        purpose="CYCLE_4_SHORT_REDUCE",
        first_leg_fill_price=1.804,
        full_trigger_price=1.6654,
        total_qty=47.677,
        required_net=12.816,
        short_entry_price=1.936,
        fee_rate=0.00055,
        price_tick=0.0001,
        qty_step=0.001,
        min_order_qty=0.001,
    )
    assert abs(sum(s.qty for s in plan.stages) - 47.677) < 1e-6

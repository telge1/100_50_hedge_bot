"""Chronological LONG research runner with Phase 2D entry config."""

from __future__ import annotations

from typing import Any, Callable

from long_entry_chronology_v2d_v1 import LongEntryStateV2DV1, process_long_entry_bar_v2d
from long_research_config_v2d_v1 import LongResearchConfigV2DV1
from pool_consecutive_streak_v2e_v1 import update_consecutive_lower_streaks  # noqa: E402
from causal_region_lineage_v2h_v1 import CausalLineageTrackerV2H  # noqa: E402
from research_runner_v1 import LongResearchRunResultV1


def run_long_research_v2d(
    *,
    bars: list[str],
    prices: dict[str, dict[str, float]],
    ema9: dict[str, float],
    ema20: dict[str, float],
    ema59: dict[str, float],
    ema200: dict[str, float],
    session,
    entry_config: LongResearchConfigV2DV1,
    exit_variant: str = "LONG_A",
    eval_start_index: int | None = None,
    eval_end_index: int | None = None,
    transport: str = "validated_frozen_bundle",
    fill_model: str = "causal_buy_limit_v2",
    record_state_trace: bool = True,
    on_bar_processed: Callable[[int, str, dict[str, Any]], None] | None = None,
) -> LongResearchRunResultV1:
    """Entry path uses v2d; exits reuse v1 runner by delegating after swapping entry processor."""

    from coverage_preflight_v1 import run_coverage_preflight
    from long_atomic_tp_v1 import select_long_a_tp_at_fill
    from long_exit_lifecycle_v1 import LongExitAuditEventV1, LongExitStateV1, process_long_exit_bar
    from long_intrabar_exit_v1 import gross_long_return_pct
    from long_runner_state_v1 import derive_runner_state
    from research_runner_v1 import LongTradeRecordV1

    closes = bars
    pre = run_coverage_preflight(
        decision_closes=closes,
        transport=transport,
        bundle_first_close=closes[0] if closes else None,
    )
    start_i = eval_start_index if eval_start_index is not None else pre.required_bars
    if start_i >= len(bars):
        return LongResearchRunResultV1(
            preflight=pre,
            exit_variant=exit_variant,  # type: ignore[arg-type]
            trades=[],
            entry_events=[],
            order_event_ledger=[],
            state_trace=[],
            ambiguous_cases=[],
            summary={},
            blocked=True,
            block_reason=pre.reason_code,
        )

    end_i = min(eval_end_index if eval_end_index is not None else len(bars) - 1, len(bars) - 1)

    entry_st = LongEntryStateV2DV1()
    exit_st: LongExitStateV1 | None = None
    exit_audit: list[LongExitAuditEventV1] = []
    trades: list[LongTradeRecordV1] = []
    open_trade: LongTradeRecordV1 | None = None
    state_trace: list[dict[str, Any]] = []
    order_ledger: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    cancels = replaces = 0
    live_streak: dict[str, int] = {}
    lineage: CausalLineageTrackerV2H | None = None
    yaml_sup = entry_config.v2e_yaml
    if entry_config.pool_age_semantics == "CAUSAL_REGION_LINEAGE_5" and yaml_sup is not None:
        lineage = CausalLineageTrackerV2H(
            min_unique_origins=yaml_sup.min_unique_origins,
            min_age_bars=yaml_sup.min_pool_age_bars,
        )
    elif entry_config.touch_lifecycle == "LINEAGE_AWARE_V2J" and yaml_sup is not None:
        lineage = CausalLineageTrackerV2H(
            min_unique_origins=yaml_sup.min_unique_origins,
            min_age_bars=yaml_sup.min_pool_age_bars,
        )

    def _advance_lineage(i: int) -> None:
        if lineage is None:
            return
        b = bars[i]
        snap_w = session.collect_at_close(b)
        mc = float(prices[b]["c"])
        lineage.advance(
            bar_utc=b,
            regions=lineage.regions_from_snapshot(snap_w),
            market_close=mc,
            atomic_streak=live_streak,
        )

    for i in range(0, start_i):
        snap_w = session.collect_at_close(bars[i])
        update_consecutive_lower_streaks(
            snap_w, live_streak, min_unique_origins=yaml_sup.min_unique_origins if yaml_sup else 2
        )
        _advance_lineage(i)

    for i in range(start_i, end_i + 1):
        bar = bars[i]
        prev = bars[i - 1] if i > 0 else None
        ohlc = prices[bar]
        snap = session.collect_at_close(bar)
        snap_prev = session.collect_at_close(prev) if prev else None
        close_prev = float(prices[prev]["c"]) if prev else None
        e200_p6 = ema200.get(bars[i - 6]) if i >= 6 else None

        update_consecutive_lower_streaks(
            snap,
            live_streak,
            min_unique_origins=yaml_sup.min_unique_origins if yaml_sup else 2,
        )
        _advance_lineage(i)

        prev_entry_n = len(entry_st.events)
        prev_exit_n = len(exit_audit)

        if exit_st is not None and not exit_st.closed and entry_st.position is not None:
            process_long_exit_bar(
                exit_st,
                prev_bar_utc=prev or bar,
                bar_utc=bar,
                next_bar_utc=bars[i + 1] if i + 1 < len(bars) else None,
                ohlc=ohlc,
                snapshot_at_close=snap,
                prev_ema59=float(ema59[prev]) if prev and prev in ema59 else float(ema59[bar]),
                prev_ema200=float(ema200[prev]) if prev and prev in ema200 else float(ema200[bar]),
                ema59=float(ema59[bar]),
                ema200=float(ema200[bar]),
                audit=exit_audit,
            )
            if exit_st.closed and open_trade is not None:
                open_trade.exit_bar_utc = exit_st.exit_bar_utc
                open_trade.exit_price = exit_st.exit_price
                open_trade.exit_reason = exit_st.exit_reason
                if exit_st.exit_reason == "AMBIGUOUS_INTRABAR" or exit_st.ambiguous_intrabar:
                    ambiguous.append(
                        {
                            "bar_utc": bar,
                            "variant": exit_variant,
                            "entry": open_trade.entry_price,
                            "sl": open_trade.sl_price,
                            "tp": exit_st.active_tp_limit,
                            "ohlc": dict(ohlc),
                            "exit_reason": exit_st.exit_reason,
                            "position_after": "closed_ambiguous",
                        }
                    )
                if exit_st.exit_price is not None:
                    open_trade.gross_return_pct = round(
                        gross_long_return_pct(open_trade.entry_price, exit_st.exit_price), 6
                    )
                open_trade.exit_audit = [
                    {"bar_utc": e.bar_utc, "action": e.action, "detail": e.detail} for e in exit_audit
                ]
                trades.append(open_trade)
                open_trade = None
                entry_st.position = None
                exit_st = None

        if entry_st.position is None:
            process_long_entry_bar_v2d(
                entry_st,
                bar=bar,
                bar_index=i,
                prev_bar=prev,
                ohlc=ohlc,
                close_prev=close_prev,
                snap_prev=snap_prev,
                snap=snap,
                ema9=ema9.get(bar),
                ema20=ema20.get(bar),
                ema59=ema59.get(bar),
                ema200=ema200.get(bar),
                ema200_prev6=e200_p6,
                config=entry_config,
                streak_cache=live_streak,
                closes=bars,
                session=session,
                prices=prices,
                fill_model=fill_model,
                lineage_tracker=lineage,
                exit_variant=exit_variant,
            )
            if entry_st.position is not None and exit_st is None:
                pos = entry_st.position
                tp_limit = None
                pending_tp = None
                pending_audit = None
                if exit_variant == "LONG_A":
                    tp_limit, tp_meta, _ = select_long_a_tp_at_fill(snap, entry_fill=pos.entry_price)
                    pending_tp = tp_limit
                    pending_audit = tp_meta
                exit_st = LongExitStateV1(
                    entry_price=pos.entry_price,
                    sl_price=pos.sl_price,
                    exit_mode=exit_variant,  # type: ignore[arg-type]
                    active_tp_limit=None,
                    pending_tp_limit=pending_tp,
                    pending_tp_audit=pending_audit,
                )
                open_trade = LongTradeRecordV1(
                    position_id=pos.position_id,
                    exit_variant=exit_variant,  # type: ignore[arg-type]
                    signal_close_utc=pos.signal_close_utc,
                    fill_bar_utc=pos.fill_bar_utc,
                    entry_price=pos.entry_price,
                    sl_price=pos.sl_price,
                    risk_pct=pos.risk_pct,
                    pool_key=pos.pool_key,
                    pool_lower=0.0,
                    pool_upper=0.0,
                    origin_ids="",
                    qualified_gap_pct=None,
                    actual_gap_pct=None,
                    tp_limit=tp_limit,
                    exit_bar_utc=None,
                    exit_price=None,
                    exit_reason=None,
                    gross_return_pct=None,
                    pending_events=list(entry_st.events),
                )
                process_long_exit_bar(
                    exit_st,
                    prev_bar_utc=prev or bar,
                    bar_utc=bar,
                    next_bar_utc=bars[i + 1] if i + 1 < len(bars) else None,
                    ohlc=ohlc,
                    snapshot_at_close=snap,
                    prev_ema59=float(ema59[prev]) if prev and prev in ema59 else float(ema59[bar]),
                    prev_ema200=float(ema200[prev]) if prev and prev in ema200 else float(ema200[bar]),
                    ema59=float(ema59[bar]),
                    ema200=float(ema200[bar]),
                    audit=exit_audit,
                )
                if exit_st.closed and open_trade is not None:
                    open_trade.exit_bar_utc = exit_st.exit_bar_utc
                    open_trade.exit_price = exit_st.exit_price
                    open_trade.exit_reason = exit_st.exit_reason
                    if exit_st.exit_price is not None:
                        open_trade.gross_return_pct = round(
                            gross_long_return_pct(open_trade.entry_price, exit_st.exit_price), 6
                        )
                    trades.append(open_trade)
                    open_trade = None
                    entry_st.position = None
                    exit_st = None

        ema_ok = (
            ema9.get(bar, 0) > ema200.get(bar, 0)
            and ema20.get(bar, 0) > ema200.get(bar, 0)
            and ema59.get(bar, 0) > ema200.get(bar, 0)
        )
        hist_ok = i >= pre.required_bars
        rs = derive_runner_state(entry=entry_st, exit_st=exit_st, pool_history_ok=hist_ok, ema_authorized=ema_ok)
        if record_state_trace:
            state_trace.append({"bar": bar, "runner_state": rs})
        if on_bar_processed is not None:
            on_bar_processed(
                i,
                bar,
                {
                    "runner_state": rs,
                    "trade_count": len(trades),
                    "open_position": open_trade is not None,
                },
            )
        for ev in entry_st.events[prev_entry_n:]:
            order_ledger.append({"phase": "entry", **ev})
            if ev.get("action") == "CANCEL":
                cancels += 1
            if ev.get("action") == "REPLACE":
                replaces += 1
        for ex in exit_audit[prev_exit_n:]:
            order_ledger.append(
                {"phase": "exit", "bar_utc": ex.bar_utc, "action": ex.action, "detail": ex.detail}
            )

    resolved = [t for t in trades if t.gross_return_pct is not None]
    wins = [t.gross_return_pct for t in resolved if (t.gross_return_pct or 0) > 0]
    losses = [t.gross_return_pct for t in resolved if (t.gross_return_pct or 0) < 0]
    touch_counts: dict[str, int] = {}
    for a in entry_st.touch_tracker.audit:
        touch_counts[a.get("event", "?")] = touch_counts.get(a.get("event", "?"), 0) + 1

    summary = {
        "fills": len(trades),
        "places": sum(1 for e in entry_st.events if e.get("action") == "PLACE"),
        "tp_exits": sum(1 for t in trades if t.exit_reason in ("FIXED_UPPER_TP", "UPPER_POOL_GAP_TP")),
        "sl_exits": sum(1 for t in trades if t.exit_reason == "STRUCTURAL_SL"),
        "ema_exits": sum(1 for t in trades if t.exit_reason == "EMA59_200_CROSS"),
        "ambiguous_exits": sum(1 for t in trades if t.exit_reason == "AMBIGUOUS_INTRABAR"),
        "cancels": cancels,
        "replaces": replaces,
        "sum_gross_return_pct": round(sum(t.gross_return_pct for t in resolved), 6) if resolved else 0.0,
        "sum_wins_pct": round(sum(wins), 6) if wins else 0.0,
        "sum_losses_pct": round(sum(losses), 6) if losses else 0.0,
        "fees_slippage": "not_modeled",
        "entry_config": entry_config.label(),
        "touch_event_counts": touch_counts,
    }
    open_at_end = None
    if open_trade is not None and entry_st.position is not None:
        open_at_end = {
            "fill_bar_utc": open_trade.fill_bar_utc,
            "entry_price": open_trade.entry_price,
            "sl_price": open_trade.sl_price,
            "tp_limit": open_trade.tp_limit,
        }

    return LongResearchRunResultV1(
        preflight=pre,
        exit_variant=exit_variant,  # type: ignore[arg-type]
        trades=trades,
        entry_events=entry_st.events,
        order_event_ledger=order_ledger,
        state_trace=state_trace,
        ambiguous_cases=ambiguous,
        summary=summary,
        blocked=False,
        block_reason=None,
        open_position_at_end=open_at_end,
        touch_forensics=list(entry_st.touch_tracker.audit),
        structure_audits=list(getattr(entry_st, "structure_audits", []) or []),
        repricing_audits=list(getattr(entry_st, "repricing_audits", []) or []),
    )

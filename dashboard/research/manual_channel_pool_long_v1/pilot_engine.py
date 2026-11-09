"""L1 pilot execution: shared candidate build + variant simulation."""

from __future__ import annotations

import copy
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from channel_cycle import ChannelCycleState
from config import (
    MIN_CHANNEL_ROOM_PCT,
    MIN_TRADE_PROFIT_PCT,
    PilotVariant,
    UPPER_MAIN_CHANNEL_LINE_ID,
)


def effective_tp_price(
    entry: float,
    pool_tp: float | None,
    main_upper: float | None,
) -> float | None:
    candidates: list[float] = []
    if pool_tp is not None and pool_tp > entry:
        candidates.append(float(pool_tp))
    if main_upper is not None and main_upper > entry:
        candidates.append(float(main_upper))
    if not candidates:
        return None
    return min(candidates)


def apply_variant_entry_filters(
    variant: PilotVariant,
    entry: float,
    main_upper: float | None,
    effective_tp: float | None,
    cycle: ChannelCycleState,
    row: dict[str, Any] | None = None,
) -> str | None:
    if variant.channel_room:
        indie = bool(row.get("_independent_support_mode")) if row is not None else False
        if main_upper is None:
            if not indie:
                return "UNRESOLVED_UPPER_CHANNEL_AT_ENTRY"
        else:
            room = (float(main_upper) - entry) / entry * 100.0
            if room < MIN_CHANNEL_ROOM_PCT:
                return "REJECTED_INSUFFICIENT_CHANNEL_ROOM"
    if variant.min_profit:
        if effective_tp is None:
            return "UNRESOLVED_EFFECTIVE_TP"
        pot = (effective_tp - entry) / entry * 100.0
        if pot < MIN_TRADE_PROFIT_PCT:
            return "REJECTED_MIN_PROFIT_BELOW_THRESHOLD"
    if variant.target_reached:
        if cycle.upper_reached:
            return "TARGET_ALREADY_REACHED_BEFORE_ENTRY"
    if variant.d2_canonical:
        if row is None:
            return "UNRESOLVED_D2_LANDSCAPE"
        if row.get("_pool_members") is None:
            return "UNRESOLVED_POOL_SNAPSHOT"
        if not row.get("_d2_gate_ok"):
            return row.get("d2_gate_reason") or "D2_GATE_NO_QUALIFIED_UPPER_BAND"
        if variant.d2_research_filter and not row.get("_d2_research_pass"):
            return row.get("d2_research_reason") or "D2_RESEARCH_FILTER_NOT_CALIBRATED"
        if variant.d2_nearness_filter and not row.get("_d2_nearness_ok"):
            return row.get("d2_nearness_reason") or "REJECTED_FIRST_D2_TOO_FAR"
        if variant.d2_early_staggering and not row.get("_d2_stagger_ok"):
            return row.get("d2_stagger_reason") or "REJECTED_NO_EARLY_D2_STAGGERING"
    elif variant.pool_landscape:
        if row is None:
            return "UNRESOLVED_POOL_LANDSCAPE"
        if row.get("_pool_members") is None:
            return "UNRESOLVED_POOL_SNAPSHOT"
        if not row.get("_pool_landscape_pass"):
            reason = row.get("_pool_landscape_reason") or "REJECTED_POOL_LANDSCAPE"
            return reason
    return None


def summarize_variant(trades: list[dict[str, Any]], touch_stats: dict[str, Any]) -> dict[str, Any]:
    executed = [t for t in trades if t.get("exit_kind") not in ("NO_TRADE", "")]
    return {
        "touch_events": touch_stats["touch_events_total"],
        "long_triggers_allowed": touch_stats["long_triggers_allowed"],
        "rows": len(trades),
        "executed": len(executed),
        "sl": sum(1 for t in executed if t.get("exit_kind") == "SL"),
        "tp_pool": sum(1 for t in executed if t.get("exit_kind") == "TP"),
        "tp_channel": sum(1 for t in executed if t.get("exit_kind") == "CHANNEL_UPPER"),
        "open_end": sum(1 for t in executed if t.get("exit_kind") == "OPEN_AT_WINDOW_END"),
        "ambiguous": sum(1 for t in executed if t.get("exit_kind") == "AMBIGUOUS"),
        "pnl_sum": sum(float(t["gross_pnl_pct"]) for t in executed if t.get("gross_pnl_pct")),
        "rejects": dict(Counter(t.get("reject_reason", "") for t in trades if t.get("reject_reason"))),
    }


def execute_variant(
    variant: PilotVariant,
    pending: list[dict[str, Any]],
    candles: list[dict],
    *,
    upper_line,
    l1_module: Any,
) -> list[dict[str, Any]]:
    channel_line_at_bar = l1_module.channel_line_at_bar

    def upper_px(candle: dict) -> float | None:
        return channel_line_at_bar(upper_line, candle)

    cycle = ChannelCycleState()
    pending_sorted = sorted(
        pending,
        key=lambda r: (
            int(r["_entry_idx"]) if r.get("_entry_idx") not in (None, "") else 10**9,
            r["touch_event_id"],
        ),
    )
    position_free_from_idx = 0
    traded_events: set[str] = set()
    trades: list[dict[str, Any]] = []

    for row in pending_sorted:
        out = {k: v for k, v in row.items() if not k.startswith("_")}
        eid = row["touch_event_id"]
        tsi = row.get("_touch_start_idx")
        if tsi is not None and row.get("long_trigger_allowed") == "yes":
            cycle.maybe_reset_at_touch_start(candles, int(tsi), upper_px)

        if eid in traded_events:
            out["reject_reason"] = "EVENT_TRADE_ALREADY_TAKEN"
            out["variant"] = variant.code
            trades.append(out)
            continue

        if row.get("reject_reason") == "NO_C1_CONFIRM_AFTER_FIRST_TOUCH":
            out["variant"] = variant.code
            trades.append(out)
            continue

        if row.get("long_trigger_allowed") != "yes" or not row.get("_entry_idx"):
            if not out.get("reject_reason"):
                out["reject_reason"] = row.get("reject_reason", "")
            out["variant"] = variant.code
            trades.append(out)
            continue

        entry_idx = int(row["_entry_idx"])
        cycle.advance_to(candles, entry_idx - 1, upper_px)

        entry = float(row["entry_price"])
        main_upper = row.get("_main_upper_at_entry")
        eff_tp = row.get("_effective_tp")

        vreject = apply_variant_entry_filters(variant, entry, main_upper, eff_tp, cycle, row=row)
        if vreject:
            out["reject_reason"] = vreject
            out["exit_kind"] = "NO_TRADE"
            out["variant"] = variant.code
            trades.append(out)
            continue

        if not row.get("_pick_ok"):
            out["exit_kind"] = "NO_TRADE"
            out["variant"] = variant.code
            trades.append(out)
            continue

        if entry_idx < position_free_from_idx:
            out["reject_reason"] = "GLOBAL_MAX_ONE_OPEN_LONG"
            out["exit_kind"] = "NO_TRADE"
            out["variant"] = variant.code
            trades.append(out)
            continue

        exit_kind, exit_ts, exit_price, gross_pnl_pct, exit_idx = l1_module.simulate_exit(
            candles,
            entry_idx,
            entry,
            row["_sl"],
            row["_pool_tp"],
            row.get("_channel_line"),
        )
        traded_events.add(eid)
        reject = row.get("reject_reason") or ""
        if exit_kind == "AMBIGUOUS":
            reject = "EXIT_AMBIGUOUS_SAME_BAR"
        out["exit_kind"] = exit_kind
        out["exit_utc"] = l1_module.fmt_utc(exit_ts) if exit_ts else ""
        out["exit_price"] = f"{exit_price:.10f}" if exit_price is not None else ""
        out["gross_pnl_pct"] = f"{gross_pnl_pct:.4f}" if gross_pnl_pct is not None else ""
        out["reject_reason"] = reject
        out["variant"] = variant.code

        if exit_idx is not None:
            cycle.advance_to(candles, exit_idx, upper_px)
            if exit_kind == "OPEN_AT_WINDOW_END":
                position_free_from_idx = len(candles)
            else:
                position_free_from_idx = exit_idx + 1
        trades.append(out)

    trades.sort(key=lambda r: (r["touch_utc"], r["touch_event_id"]))
    return trades


def enrich_pending_metrics(
    pending: list[dict[str, Any]],
    upper_line,
    l1_module: Any,
    *,
    independent: bool = False,
) -> None:
    for row in pending:
        if not row.get("_entry_idx"):
            continue
        entry = float(row["entry_price"])
        entry_ts = int(row["_entry_ts"])
        if independent:
            deckel = row.get("_channel_deckel_at_entry")
            main_up = float(deckel) if deckel is not None else None
        else:
            main_up = l1_module.channel_line_at_bar(upper_line, {"time": float(entry_ts)})
        row["_main_upper_at_entry"] = main_up
        row["_entry_ts"] = entry_ts
        pool_tp = row.get("_pool_tp")
        eff = effective_tp_price(entry, pool_tp, main_up)
        row["_effective_tp"] = eff
        row["channel_price_at_entry"] = f"{main_up:.10f}" if main_up is not None else ""
        if main_up is not None:
            row["channel_room_pct"] = f"{(main_up - entry) / entry * 100.0:.4f}"
        else:
            row["channel_room_pct"] = ""
        if eff is not None:
            row["effective_tp_at_entry"] = f"{eff:.10f}"
            row["potential_profit_pct"] = f"{(eff - entry) / entry * 100.0:.4f}"
        else:
            row["effective_tp_at_entry"] = ""
            row["potential_profit_pct"] = ""


def enrich_pool_landscape(pending: list[dict[str, Any]]) -> None:
    from pool_landscape import analyze_upper_landscape, metrics_to_row_fields

    for row in pending:
        if not row.get("_entry_idx"):
            continue
        entry = float(row["entry_price"])
        main_up = row.get("_main_upper_at_entry")
        members = row.get("_pool_members")
        m = analyze_upper_landscape(members, entry, channel_upper=main_up)
        row.update(metrics_to_row_fields(m, str(row.get("_pool_decision_close", ""))))
        row["_pool_landscape_pass"] = m.filter_pass
        row["_pool_landscape_reason"] = m.filter_reason

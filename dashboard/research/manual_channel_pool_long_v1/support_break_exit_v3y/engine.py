"""Chronological one-position execution with V3Y exits."""

from __future__ import annotations

import copy
from typing import Any

import pilot_engine as pe
import run_l1_pilot as l1

from support_sim import simulate_exit_v3y


def _support_line(row: dict, lines_by_id: dict) -> Any:
    sid = row.get("touch_drawing_id") or ""
    return lines_by_id.get(sid)


def execute_variant_v3y(
    variant,
    pending: list[dict],
    candles: list[dict],
    *,
    upper_line,
    lines_by_id: dict,
    l1_module: Any,
) -> list[dict]:
    channel_line_at_bar = l1_module.channel_line_at_bar

    def upper_px(candle: dict) -> float | None:
        return channel_line_at_bar(upper_line, candle)

    cycle = pe.ChannelCycleState()
    pending_sorted = sorted(
        pending,
        key=lambda r: (
            int(r["_entry_idx"]) if r.get("_entry_idx") not in (None, "") else 10**9,
            r["touch_event_id"],
        ),
    )
    position_free_from_idx = 0
    traded_events: set[str] = set()
    trades: list[dict] = []

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

        vreject = pe.apply_variant_entry_filters(variant, entry, main_upper, eff_tp, cycle, row=row)
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

        support = _support_line(row, lines_by_id)
        exit_kind, exit_ts, exit_price, gross_pnl_pct, exit_idx = simulate_exit_v3y(
            candles,
            entry_idx,
            entry,
            float(row["_sl"]),
            row.get("_pool_tp"),
            row.get("_channel_line"),
            support,
        )
        traded_events.add(eid)
        reject = row.get("reject_reason") or ""
        if exit_kind == "AMBIGUOUS":
            reject = "EXIT_AMBIGUOUS_SAME_BAR"
        out["exit_kind"] = exit_kind
        out["exit_utc"] = l1.fmt_utc(exit_ts) if exit_ts else ""
        out["exit_price"] = f"{exit_price:.10f}" if exit_price is not None else ""
        out["gross_pnl_pct"] = f"{gross_pnl_pct:.4f}" if gross_pnl_pct is not None else ""
        out["reject_reason"] = reject
        out["variant"] = variant.code
        out["support_drawing_id"] = row.get("touch_drawing_id", "")

        if exit_idx is not None:
            cycle.advance_to(candles, exit_idx, upper_px)
            if exit_kind == "OPEN_AT_WINDOW_END":
                position_free_from_idx = len(candles)
            else:
                position_free_from_idx = exit_idx + 1
        trades.append(out)

    trades.sort(key=lambda r: (r["touch_utc"], r["touch_event_id"]))
    return trades


def resimulate_row_v3u_baseline(row: dict, candles: list[dict], l1_module: Any) -> tuple:
    return l1_module.simulate_exit(
        candles,
        int(row["_entry_idx"]),
        float(row["entry_price"]),
        float(row["_sl"]),
        row.get("_pool_tp"),
        row.get("_channel_line"),
    )


def resimulate_row_v3y(
    row: dict,
    candles: list[dict],
    lines_by_id: dict,
    l1_module: Any,
) -> tuple:
    support = _support_line(row, lines_by_id)
    return simulate_exit_v3y(
        candles,
        int(row["_entry_idx"]),
        float(row["entry_price"]),
        float(row["_sl"]),
        row.get("_pool_tp"),
        row.get("_channel_line"),
        support,
    )

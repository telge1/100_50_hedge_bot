"""Build L1 pending rows from watcher entry indices — reuses run_l1_pilot SSOT (no duplicated gates)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from line_geometry import LineDef, classify_candle_interval

from watcher_fsm import EntryCandidate


def materialize_pending_row(
    *,
    l1,
    line: LineDef,
    candles: list[dict],
    lines: list[LineDef],
    lines_by_id: dict[str, LineDef],
    upper_line: LineDef | None,
    touch_idx: int,
    confirm_idx: int,
    entry_idx: int,
    touch_event_id: str,
    line_selection_mode: str,
    confirm_variant: str,
    struct_class: str,
    struct_reject: str | None,
) -> dict[str, Any]:
    """Mirror ``build_pilot_context`` row fields for one entry candidate."""
    from support_line_selection import line_causality_at_touch

    independent = line_selection_mode == l1.LINE_SELECTION_INDEPENDENT if hasattr(l1, "LINE_SELECTION_INDEPENDENT") else True
    from support_line_selection import LINE_SELECTION_INDEPENDENT

    independent = line_selection_mode == LINE_SELECTION_INDEPENDENT
    i0 = touch_idx
    touch_utc = l1.fmt_utc(int(candles[i0]["time"]))
    touch_ts = float(candles[i0]["time"])
    causality = line_causality_at_touch(line, touch_ts)
    _, lp_touch, _, _, _ = classify_candle_interval(
        line, touch_ts, float(candles[i0]["high"]), float(candles[i0]["low"])
    )

    row_base: dict[str, Any] = {
        "pilot_mode": l1.PILOT_LABEL,
        "line_selection_mode": line_selection_mode,
        "drawing_causality": causality.label,
        "drawing_geometry_active": "yes" if causality.geometry_active else "no",
        "drawing_anchor_status": causality.anchor_status,
        "symbol": l1.SYMBOL,
        "touch_event_id": touch_event_id,
        "touch_utc": touch_utc,
        "contact_phase_end_utc": touch_utc,
        "touch_drawing_id": line.drawing_id,
        "touch_line_price": f"{float(lp_touch or 0):.10f}",
        "structure_class": struct_class,
        "long_trigger_allowed": "yes" if struct_reject is None else "no",
        "confirm_variant": confirm_variant,
        "confirm_utc": "",
        "entry_utc": "",
        "entry_idx": "",
        "entry_price": "",
        "sl_price": "",
        "sl_pct": f"{l1.SL_PCT:.2f}",
        "tp_mode": "",
        "tp_price": "",
        "tp_pool_id": "",
        "tp_channel_line_id": "",
        "tp_target_dist_pct": "",
        "pool_gate_qualified_count": "",
        "pool_ssot": f"{l1.POOL_TIMEFRAME}_PD{l1.POOL_DETAIL}",
        "exit_kind": "NO_TRADE",
        "exit_utc": "",
        "exit_price": "",
        "gross_pnl_pct": "",
        "dynamic_channel_exit": "",
        "reject_reason": "",
        "channel_price_at_entry": "",
        "channel_room_pct": "",
        "effective_tp_at_entry": "",
        "potential_profit_pct": "",
        "_v3ao_entry_source": "watcher",
    }

    if struct_reject is not None:
        row = dict(row_base)
        row["reject_reason"] = struct_reject
        return row

    if entry_idx >= len(candles) or confirm_idx < 0:
        row = dict(row_base)
        row["reject_reason"] = "WATCHER_NO_ENTRY"
        return row

    confirm = candles[confirm_idx]
    row_base["confirm_utc"] = l1.fmt_utc(int(confirm["time"]))
    entry_ts = int(candles[entry_idx]["time"])
    entry = float(candles[entry_idx]["open"])
    sl = entry * (1.0 - l1.SL_PCT / 100.0)
    row_base["entry_utc"] = l1.fmt_utc(entry_ts)
    row_base["entry_idx"] = str(entry_idx)
    row_base["entry_price"] = f"{entry:.10f}"
    row_base["sl_price"] = f"{sl:.10f}"

    from pool_scan.clock import bar_close

    confirm_open_dt = datetime.fromtimestamp(int(confirm["time"]), tz=timezone.utc)
    pool_decision_close = bar_close(confirm_open_dt, l1.TIMEFRAME)
    members, pool_err = l1.pools_at_confirm_open(int(confirm["time"]))
    channel_upper, channel_id = l1.channel_upper_at(
        lines,
        entry_ts,
        entry,
        exclude_drawing_ids=frozenset({line.drawing_id}),
    )

    if members is None:
        pick = l1.PoolPick(
            None,
            "NONE",
            None,
            channel_id,
            None,
            0,
            False,
            pool_err or "POOL_SSOT_FAIL",
        )
    else:
        qualified = l1.qualify_upper_pools(members, entry)
        pick = l1.pick_tp(
            entry,
            qualified,
            channel_upper,
            channel_id,
            independent_support=independent and channel_upper is None,
        )

    row = dict(row_base)
    row["tp_mode"] = pick.tp_mode
    row["tp_price"] = f"{pick.tp_price:.10f}" if pick.tp_price is not None else ""
    row["tp_pool_id"] = pick.tp_pool_id or ""
    row["tp_channel_line_id"] = pick.tp_channel_line_id or ""
    row["tp_target_dist_pct"] = f"{pick.tp_dist_pct:.4f}" if pick.tp_dist_pct is not None else ""
    row["pool_gate_qualified_count"] = str(pick.gate_count)
    row["reject_reason"] = pick.block_reason or ""
    row["_entry_idx"] = entry_idx
    row["_sl"] = sl
    row["_pool_tp"] = l1.pool_tp_for_exit(pick)
    ch_id = pick.tp_channel_line_id
    row["_channel_line"] = lines_by_id.get(ch_id) if ch_id else None
    row["dynamic_channel_exit"] = "yes" if row["_channel_line"] is not None else "no"
    row["_pick_ok"] = (
        pick.gate_ok
        and not pick.block_reason
        and (row["_pool_tp"] is not None or row["_channel_line"] is not None)
    )
    row["_touch_start_idx"] = touch_idx
    row["_entry_ts"] = entry_ts
    row["_pool_members"] = members
    row["_pool_decision_close"] = l1.fmt_utc(int(pool_decision_close.timestamp()))
    row["_independent_support_mode"] = independent
    row["_channel_deckel_at_entry"] = channel_upper
    row["_channel_deckel_line_id"] = channel_id
    return row

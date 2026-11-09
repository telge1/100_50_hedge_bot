"""Shared timeline + slot competition analysis (V3AR, READ-ONLY)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pilot_engine as pe
import run_l1_pilot as l1
from config import L1_V3E_VARIANT_B
from support_break_exit_v3y.support_sim import simulate_exit_v3y

VARIANT_B = L1_V3E_VARIANT_B
BAR_SEC = 300


def _support_line(row: dict, lines_by_id: dict):
    return lines_by_id.get(row.get("touch_drawing_id") or "")


def _parse_utc(s: str) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def _minutes_between(a: str, b: str) -> float | None:
    da, db = _parse_utc(a), _parse_utc(b)
    if da is None or db is None:
        return None
    return (db - da).total_seconds() / 60.0


def hypothetical_exit(row: dict, candles: list[dict], lines_by_id: dict) -> dict:
    support = _support_line(row, lines_by_id)
    exit_kind, exit_ts, exit_price, gross_pnl_pct, exit_idx = simulate_exit_v3y(
        candles,
        int(row["_entry_idx"]),
        float(row["entry_price"]),
        float(row["_sl"]),
        row.get("_pool_tp"),
        row.get("_channel_line"),
        support,
    )
    return {
        "hypothetical_exit_utc": l1.fmt_utc(exit_ts) if exit_ts else "",
        "hypothetical_exit_kind": exit_kind,
        "hypothetical_exit_price": f"{exit_price:.10f}" if exit_price is not None else "",
        "hypothetical_gross_pnl_pct": f"{gross_pnl_pct:.4f}" if gross_pnl_pct is not None else "",
        "hypothetical_note": "DIAGNOSTIC_ONLY_NOT_IN_BACKTEST_SUM",
    }


@dataclass
class SimulationResult:
    label: str
    timeline: list[dict]
    executed: list[dict]
    slot_blocks: list[dict]
    hypotheticals: list[dict]
    stats: dict[str, Any]


def run_simulation(
    *,
    label: str,
    pending_ready: list[dict],
    candles: list[dict],
    lines_by_id: dict,
    upper_line,
    mod,
) -> SimulationResult:
    channel_line_at_bar = mod.channel_line_at_bar

    def upper_px(candle: dict) -> float | None:
        return channel_line_at_bar(upper_line, candle)

    row_by_eid = {r["touch_event_id"]: r for r in pending_ready if r.get("touch_event_id")}
    pending_sorted = sorted(
        pending_ready,
        key=lambda r: (
            int(r["_entry_idx"]) if r.get("_entry_idx") not in (None, "") else 10**9,
            r["touch_event_id"],
        ),
    )
    cycle = pe.ChannelCycleState()
    position_free_from_idx = 0
    traded_events: set[str] = set()
    timeline: list[dict] = []
    executed: list[dict] = []
    slot_blocks: list[dict] = []
    hypotheticals: list[dict] = []
    last_executed: dict[str, Any] | None = None

    for row in pending_sorted:
        eid = row["touch_event_id"]
        entry_utc = row.get("entry_utc", "")
        entry_idx = int(row["_entry_idx"]) if row.get("_entry_idx") not in (None, "") else None

        base_ev = {
            "variant": label,
            "touch_event_id": eid,
            "blocked_line_id": row.get("touch_drawing_id", ""),
            "touch_utc": row.get("touch_utc", ""),
            "entry_utc": entry_utc,
            "entry_price": row.get("entry_price", ""),
            "entry_idx": entry_idx if entry_idx is not None else "",
        }

        if row.get("long_trigger_allowed") != "yes" or not row.get("_entry_idx"):
            timeline.append({**base_ev, "result": "SKIP", "reject_reason": row.get("reject_reason", "")})
            continue

        entry_idx = int(row["_entry_idx"])
        tsi = row.get("_touch_start_idx")
        if tsi is not None:
            cycle.maybe_reset_at_touch_start(candles, int(tsi), upper_px)
        cycle.advance_to(candles, entry_idx - 1, upper_px)

        if eid in traded_events:
            timeline.append({**base_ev, "result": "SKIP", "reject_reason": "EVENT_TRADE_ALREADY_TAKEN"})
            continue

        if not row.get("_pick_ok"):
            timeline.append({**base_ev, "result": "BLOCKED_GATE", "reject_reason": row.get("reject_reason", "")})
            continue

        vreject = pe.apply_variant_entry_filters(
            VARIANT_B,
            float(row["entry_price"]),
            row.get("_main_upper_at_entry"),
            row.get("_effective_tp"),
            cycle,
            row=row,
        )
        if vreject:
            timeline.append({**base_ev, "result": "BLOCKED_V3E", "reject_reason": vreject})
            continue

        if entry_idx < position_free_from_idx:
            blk = {
                **base_ev,
                "result": "GLOBAL_MAX_ONE_OPEN_LONG",
                "reject_reason": "GLOBAL_MAX_ONE_OPEN_LONG",
                "gate_conform": "yes",
                "blocker_trade_id": last_executed.get("touch_event_id") if last_executed else "",
                "blocker_line_id": last_executed.get("touch_drawing_id") if last_executed else "",
                "blocker_entry_utc": last_executed.get("entry_utc") if last_executed else "",
                "blocker_entry_price": last_executed.get("entry_price") if last_executed else "",
                "blocker_exit_utc": last_executed.get("exit_utc") if last_executed else "",
                "blocker_exit_kind": last_executed.get("exit_kind") if last_executed else "",
                "blocker_gross_pnl_pct": last_executed.get("gross_pnl_pct") if last_executed else "",
                "blocker_sl": last_executed.get("sl_price") if last_executed else "",
                "blocker_tp": last_executed.get("tp_price") if last_executed else "",
                "minutes_blocked_until_exit": _minutes_between(entry_utc, last_executed.get("exit_utc", ""))
                if last_executed
                else None,
                "position_free_from_idx_before": position_free_from_idx,
            }
            if last_executed and last_executed.get("_entry_idx") is not None and last_executed.get("_exit_idx") is not None:
                blk["blocker_position_bars"] = int(last_executed["_exit_idx"]) - int(last_executed["_entry_idx"])
                blk["blocker_position_minutes"] = blk["blocker_position_bars"] * 5
            else:
                blk["blocker_position_bars"] = ""
                blk["blocker_position_minutes"] = ""
            hyp = hypothetical_exit(row, candles, lines_by_id)
            rec = {**blk, **hyp}
            slot_blocks.append(blk)
            hypotheticals.append(rec)
            timeline.append(blk)
            continue

        entry = float(row["entry_price"])
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
        exit_utc = l1.fmt_utc(exit_ts) if exit_ts else ""
        ex = {
            "variant": label,
            "touch_event_id": eid,
            "touch_drawing_id": row.get("touch_drawing_id", ""),
            "entry_utc": entry_utc,
            "entry_price": row.get("entry_price", ""),
            "sl_price": row.get("sl_price", ""),
            "tp_price": row.get("tp_price", ""),
            "exit_utc": exit_utc,
            "exit_kind": exit_kind,
            "gross_pnl_pct": f"{gross_pnl_pct:.4f}",
            "_entry_idx": entry_idx,
            "_exit_idx": exit_idx,
        }
        if exit_idx is not None and entry_idx is not None:
            ex["position_bars"] = exit_idx - entry_idx
            ex["position_minutes"] = ex["position_bars"] * 5
        executed.append(ex)
        timeline.append({**base_ev, "result": f"EXECUTED:{exit_kind}", "exit_utc": exit_utc, "gross_pnl_pct": ex["gross_pnl_pct"]})

        if exit_idx is not None:
            cycle.advance_to(candles, exit_idx, upper_px)
            position_free_from_idx = len(candles) if exit_kind == "OPEN_AT_WINDOW_END" else exit_idx + 1
            last_executed = ex

    stats = aggregate_stats(label, executed, slot_blocks, hypotheticals)
    return SimulationResult(label, timeline, executed, slot_blocks, hypotheticals, stats)


def aggregate_stats(
    label: str,
    executed: list[dict],
    slot_blocks: list[dict],
    hypotheticals: list[dict],
) -> dict[str, Any]:
    n_exec = len(executed)
    n_blk = len(slot_blocks)
    n_gate_candidates = n_exec + n_blk
    blk_pct = round(100.0 * n_blk / n_gate_candidates, 2) if n_gate_candidates else 0.0

    pos_mins = [float(e["position_minutes"]) for e in executed if e.get("position_minutes") is not None]
    slot_mins = [float(b["blocker_position_minutes"]) for b in slot_blocks if b.get("blocker_position_minutes") not in ("", None)]
    gross_sum = round(sum(float(e["gross_pnl_pct"]) for e in executed if e.get("gross_pnl_pct")), 4)

    blocker_loss = sum(
        1 for b in slot_blocks if float(b.get("blocker_gross_pnl_pct") or 0) < 0
    )
    blocker_win = sum(
        1 for b in slot_blocks if float(b.get("blocker_gross_pnl_pct") or 0) > 0
    )
    unique_hypo_periods = len(
        {
            (h.get("entry_utc", ""), h.get("blocker_trade_id", ""))
            for h in hypotheticals
        }
    )

    hyp_rows = [h for h in hypotheticals if h.get("hypothetical_gross_pnl_pct")]
    hyp_sum_if_added_naively = round(
        sum(float(h["hypothetical_gross_pnl_pct"]) for h in hyp_rows),
        4,
    )

    return {
        "variant": label,
        "executed_trades": n_exec,
        "gate_conform_blocked": n_blk,
        "gate_conform_total": n_gate_candidates,
        "blocked_pct_of_gate_conform": blk_pct,
        "avg_position_minutes": round(sum(pos_mins) / len(pos_mins), 1) if pos_mins else "",
        "avg_blocker_position_minutes": round(sum(slot_mins) / len(slot_mins), 1) if slot_mins else "",
        "realized_gross_pnl_pct_sum": gross_sum,
        "blocker_was_loss_count": blocker_loss,
        "blocker_was_win_count": blocker_win,
        "hypothetical_rows": len(hyp_rows),
        "hypothetical_sum_naive_overlap_UNSAFE": hyp_sum_if_added_naively,
        "hypothetical_overlap_warning": (
            "Blocked candidates overlap in time; sum is NOT a concurrent PnL."
        ),
        "unique_block_episodes": unique_hypo_periods,
    }

"""V3AP funnel metrics — per-stage blocks (D2, CAP6, E3, executor)."""

from __future__ import annotations

import copy
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pilot_engine as pe
import run_l1_pilot as l1

_PROBE = Path(__file__).resolve().parents[1]
for _sub in (
    "substantial_entry_gate_v3ak",
    "realistic_pool_tp_v3ah",
    "tp_research_v3w",
    "support_break_exit_v3ab",
    "support_break_exit_v3y",
):
    _p = _PROBE / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from substantial_entry_gate import ENTRY_GATE_SPECS, apply_substantial_entry_gate
from v3ah_tp import pending_with_realistic_tp

from watcher_integration_v3ao.v3ao_pipeline import CAP6_TP, execute_trades, executed_trades, pending_candidates


def _block_rows(rows: list[dict], pred) -> list[dict]:
    return [r for r in rows if pred(r)]


def analyze_v3ak_touch_episodes(ctx: dict) -> int:
    return len(ctx.get("all_events") or ctx.get("ctx", {}).get("all_events") or [])


def v3ak_entry_signals(pending: list[dict]) -> int:
    return sum(1 for p in pending if p.get("_entry_idx"))


def run_full_funnel(
    pending_post_d2: list[dict],
    *,
    lines: list,
    candles: list[dict],
    upper_line,
    lines_by_id: dict,
    entry_gate_code: str = "E3_SUBSTANTIAL_TP2c",
    entry_stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    mod = sys.modules[l1.__name__]
    es = entry_stats or {}
    candidates_pre = pending_candidates(pending_post_d2)

    d2_fail = _block_rows(
        candidates_pre,
        lambda r: not r.get("_d2_gate_ok"),
    )
    d2_fail_reasons = Counter(r.get("d2_gate_reason") or "D2_FAIL" for r in d2_fail)

    after_d2_ok = [r for r in candidates_pre if r.get("_d2_gate_ok")]

    pre_cap = copy.deepcopy(pending_post_d2)
    post_cap = pending_with_realistic_tp(copy.deepcopy(pending_post_d2), lines, l1_module=mod, params=CAP6_TP)
    cap_by_id = {r["touch_event_id"]: r for r in post_cap if r.get("touch_event_id")}

    cap_fail: list[dict] = []
    cap_reasons: Counter[str] = Counter()
    for r in after_d2_ok:
        a = cap_by_id.get(r["touch_event_id"], {})
        if not a.get("_pick_ok"):
            cap_fail.append(a or r)
            cap_reasons[a.get("reject_reason") or "CAP6_BLOCK"] += 1

    spec = next(s for s in ENTRY_GATE_SPECS if s.code == entry_gate_code)
    post_e3 = copy.deepcopy(post_cap)
    apply_substantial_entry_gate(post_e3, spec)
    pe.enrich_pending_metrics(post_e3, upper_line, mod, independent=True)
    e3_by_id = {r["touch_event_id"]: r for r in post_e3 if r.get("touch_event_id")}

    e3_fail: list[dict] = []
    e3_reasons: Counter[str] = Counter()
    for r in after_d2_ok:
        a = e3_by_id.get(r["touch_event_id"], {})
        if a.get("entry_gate_pass") == "no":
            e3_fail.append(a)
            e3_reasons[a.get("entry_gate_reason") or "E3_FAIL"] += 1

    trades = execute_trades(post_e3, candles, upper_line, lines_by_id)
    executed = executed_trades(trades)
    no_trade = [t for t in trades if t.get("exit_kind") in ("NO_TRADE", "", None)]
    slot_reasons = Counter(t.get("reject_reason") or "NO_TRADE" for t in no_trade)

    exit_counts = Counter(str(t.get("exit_kind") or "") for t in executed)
    gross = round(
        sum(float(t["gross_pnl_pct"]) for t in executed if t.get("gross_pnl_pct")),
        4,
    )

    return {
        "touch_episodes": es.get("touch_episodes", 0),
        "entry_signals": es.get("entry_signals", 0),
        "structure_rejected": es.get("structure_rejected", 0),
        "pending_candidates": len(candidates_pre),
        "d2_rejected": len(d2_fail),
        "d2_reject_reasons": dict(d2_fail_reasons),
        "cap6_rejected": len(cap_fail),
        "cap6_reject_reasons": dict(cap_reasons),
        "e3_rejected": len(e3_fail),
        "e3_reject_reasons": dict(e3_reasons),
        "executor_no_trade": len(no_trade),
        "executor_block_reasons": dict(slot_reasons),
        "trades_opened": len(executed),
        "exits_tp": exit_counts.get("TP", 0),
        "exits_sl": exit_counts.get("SL", 0),
        "exits_sbe": exit_counts.get("SUPPORT_BREAK_EXIT", 0),
        "exits_channel": exit_counts.get("CHANNEL_UPPER", 0),
        "exits_other": sum(
            v for k, v in exit_counts.items() if k not in ("TP", "SL", "SUPPORT_BREAK_EXIT", "CHANNEL_UPPER")
        ),
        "gross_pnl_pct_sum": gross,
        "net_pnl_pct_sum": "",
        "net_pnl_note": "Model exposes gross_pnl_pct only (no fee/slippage net in execute_variant_v3y)",
        "exit_counts": dict(exit_counts),
        "trades": executed,
        "pending_ready": post_e3,
    }

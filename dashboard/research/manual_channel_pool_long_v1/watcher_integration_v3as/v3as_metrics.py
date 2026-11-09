"""V3AS summary metrics from funnel + slot simulation."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


def _pct_dist(entry: float, target: float | None) -> float | None:
    if target is None or not entry:
        return None
    return (float(target) - float(entry)) / float(entry) * 100.0


def enrich_trade_row(t: dict, pending_row: dict | None) -> dict:
    entry = float(t.get("entry_price") or pending_row.get("entry_price") or 0)
    sl = float(t.get("sl_price") or pending_row.get("sl_price") or 0) if (t.get("sl_price") or pending_row) else None
    tp = float(t.get("tp_price") or pending_row.get("tp_price") or 0) if (t.get("tp_price") or pending_row) else None
    out = {**t}
    if entry and sl:
        out["sl_distance_pct"] = round((entry - sl) / entry * 100.0, 4)
    if entry and tp:
        out["tp_distance_pct"] = round((tp - entry) / entry * 100.0, 4)
    if pending_row:
        out.setdefault("touch_utc", pending_row.get("touch_utc", ""))
    return out


def variant_summary(
    *,
    variant: str,
    funnel: dict,
    slot_blocks: list[dict],
    executed: list[dict],
    n_valid_trend_lines: int,
) -> dict[str, Any]:
    ex = executed
    wins = [e for e in ex if float(e.get("gross_pnl_pct") or 0) > 0]
    losses = [e for e in ex if float(e.get("gross_pnl_pct") or 0) < 0]
    n = len(ex)
    gross = round(sum(float(e["gross_pnl_pct"]) for e in ex if e.get("gross_pnl_pct")), 4)
    pos_mins = [float(e["position_minutes"]) for e in ex if e.get("position_minutes") not in ("", None)]
    sl_dists = [float(e["sl_distance_pct"]) for e in ex if e.get("sl_distance_pct") not in ("", None)]
    tp_dists = [float(e["tp_distance_pct"]) for e in ex if e.get("tp_distance_pct") not in ("", None)]

    slot_reasons = funnel.get("executor_block_reasons") or {}
    slot_n = int(slot_reasons.get("GLOBAL_MAX_ONE_OPEN_LONG", 0))
    if not slot_n:
        slot_n = len(slot_blocks)

    return {
        "variant": variant,
        "valid_trend_lines": n_valid_trend_lines,
        "touch_episodes": funnel.get("touch_episodes", 0),
        "entry_signals": funnel.get("entry_signals", 0),
        "pending_candidates": funnel.get("pending_candidates", 0),
        "d2_rejected": funnel.get("d2_rejected", 0),
        "cap6_rejected": funnel.get("cap6_rejected", 0),
        "e3_rejected": funnel.get("e3_rejected", 0),
        "slot_blocked_gate_conform": slot_n,
        "trades_executed": n,
        "exits_tp": funnel.get("exits_tp", 0),
        "exits_sl": funnel.get("exits_sl", 0),
        "exits_sbe": funnel.get("exits_sbe", 0),
        "hit_rate_pct": round(100.0 * len(wins) / n, 2) if n else "",
        "avg_win_pct": round(sum(float(e["gross_pnl_pct"]) for e in wins) / len(wins), 4) if wins else "",
        "avg_loss_pct": round(sum(float(e["gross_pnl_pct"]) for e in losses) / len(losses), 4) if losses else "",
        "gross_pnl_pct_sum": gross,
        "avg_position_minutes": round(sum(pos_mins) / len(pos_mins), 1) if pos_mins else "",
        "avg_sl_distance_pct": round(sum(sl_dists) / len(sl_dists), 4) if sl_dists else "",
        "avg_tp_distance_pct": round(sum(tp_dists) / len(tp_dists), 4) if tp_dists else "",
    }


def rollup_by_month(trades: list[dict]) -> list[dict]:
    c: dict[str, list[float]] = defaultdict(list)
    n: Counter = Counter()
    for t in trades:
        eu = t.get("entry_utc", "")
        if len(eu) < 7:
            continue
        ym = eu[:7]
        n[ym] += 1
        if t.get("gross_pnl_pct"):
            c[ym].append(float(t["gross_pnl_pct"]))
    return [
        {
            "year_month": ym,
            "trades": n[ym],
            "gross_pnl_pct_sum": round(sum(c[ym]), 4),
        }
        for ym in sorted(n)
    ]


def rollup_by_line(trades: list[dict]) -> list[dict]:
    g: dict[str, dict] = defaultdict(lambda: {"trades": 0, "gross": 0.0})
    for t in trades:
        lid = t.get("touch_drawing_id", "")
        g[lid]["trades"] += 1
        if t.get("gross_pnl_pct"):
            g[lid]["gross"] += float(t["gross_pnl_pct"])
    return [
        {"touch_drawing_id": k, "trades": v["trades"], "gross_pnl_pct_sum": round(v["gross"], 4)}
        for k, v in sorted(g.items())
    ]

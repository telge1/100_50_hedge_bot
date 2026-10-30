"""Phase 2L — extract full trade evidence from research runs (A vs B)."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from typing import Any

from long_atomic_tp_v1 import intervals_from_snapshot, select_long_a_tp_at_fill
from long_risk_audit_v1 import risk_pct


def fmt_price(x: float | None) -> str | None:
    if x is None:
        return None
    return repr(float(x))


def _pct(num: float, den: float) -> float | None:
    if den <= 0:
        return None
    return (num - den) / den * 100.0


def _find_fill_event(ledger: list[dict], trade) -> dict | None:
    for ev in ledger:
        if ev.get("phase") != "entry" or ev.get("action") != "FILL":
            continue
        if ev.get("bar_utc") != trade.fill_bar_utc:
            continue
        if abs(float(ev.get("entry", 0)) - float(trade.entry_price)) > 1e-9:
            continue
        return ev
    return None


def _place_for_order(
    ledger: list[dict], order_id: str | None, fill_bar: str, entry: float, sl: float
) -> dict | None:
    if order_id:
        for ev in reversed(ledger):
            if ev.get("phase") != "entry" or ev.get("action") != "PLACE":
                continue
            if ev.get("bar_utc", "") > fill_bar:
                continue
            if ev.get("order_id") == order_id:
                return ev
    return _last_place_before_fill(ledger, fill_bar, sl, entry)


def _events_for_order_chain(ledger: list[dict], fill_ev: dict, repricing_audits: list[dict]) -> list[dict]:
    oid = fill_ev.get("order_id")
    out: list[dict] = []
    fill_bar = fill_ev.get("bar_utc")
    for ev in ledger:
        if ev.get("phase") != "entry":
            continue
        bar = ev.get("bar_utc")
        if bar and fill_bar and bar > fill_bar:
            continue
        if ev.get("order_id") == oid:
            out.append(ev)
        if ev.get("action") == "REPLACE" and ev.get("new_order_id") == oid:
            out.append(ev)
    for ra in repricing_audits:
        if ra.get("new_order_id") == oid or ra.get("old_order_id") == oid:
            out.append({"action": "REPRICING_AUDIT", **ra})
    return sorted(out, key=lambda e: e.get("bar_utc") or e.get("bar_utc", ""))


def _last_place_before_fill(
    ledger: list[dict], fill_bar: str, planned_sl: float, entry: float
) -> dict | None:
    candidates = []
    for ev in ledger:
        if ev.get("phase") != "entry" or ev.get("action") != "PLACE":
            continue
        if ev.get("bar_utc", "") > fill_bar:
            continue
        if planned_sl and ev.get("sl") is not None:
            if abs(float(ev["sl"]) - float(planned_sl)) > 1e-6:
                continue
        if ev.get("limit") is not None and abs(float(ev["limit"]) - entry) > 1e-6:
            continue
        candidates.append(ev)
    return candidates[-1] if candidates else None


def _region_upper_at_bar(session, bar: str, entry_price: float, pool_key: str | None) -> float | None:
    snap = session.collect_at_close(bar)
    from long_support_selection_v2e_v1 import _stacked

    yaml_min = 2
    stacked = _stacked(snap, yaml_min)
    if pool_key:
        lo_s, hi_s = pool_key.split("|")
        lo, hi = float(lo_s), float(hi_s)
        for r in stacked:
            if abs(r.lower_edge - lo) < 1e-9 and abs(r.upper_edge - hi) < 1e-9:
                return float(r.upper_edge)
    best = None
    best_d = 1e9
    for r in stacked:
        if abs(r.upper_edge - entry_price) < best_d:
            best_d = abs(r.upper_edge - entry_price)
            best = float(r.upper_edge)
    if best_d < 1e-4:
        return best
    return None


def _tp_pool_bounds(tp_meta: dict | None) -> tuple[float | None, float | None]:
    if not tp_meta:
        return None, None
    return float(tp_meta.get("bottom_price", tp_meta.get("bottom", 0))), float(
        tp_meta.get("top_price", tp_meta.get("top", 0))
    )


def build_trade_dossier(
    *,
    variant: str,
    window: str,
    trade,
    ledger: list[dict],
    repricing_audits: list[dict],
    session,
    open_at_end: dict | None,
) -> dict[str, Any]:
    fill_ev = _find_fill_event(ledger, trade)
    entry_f = float(trade.entry_price)
    sl_f = float(trade.sl_price)
    place_ev = None
    if fill_ev:
        place_ev = _place_for_order(
            ledger, fill_ev.get("order_id"), trade.fill_bar_utc, entry_f, sl_f
        )
    if place_ev is None:
        place_ev = _last_place_before_fill(ledger, trade.fill_bar_utc, sl_f, entry_f)

    place_utc = place_ev.get("bar_utc") if place_ev else None
    planned_limit = float(place_ev["limit"]) if place_ev and place_ev.get("limit") is not None else None
    p_lo = p_hi = q_lo = q_hi = None
    if place_ev:
        es = place_ev.get("entry_support") or {}
        p_lo = es.get("lower_edge")
        p_hi = es.get("upper_edge")
        if p_hi is None and planned_limit is not None:
            p_hi = planned_limit
        sq = place_ev.get("sl_pool_audit") or {}
        q_lo = sq.get("lower_edge")
        q_hi = sq.get("upper_edge")

    snap_fill = session.collect_at_close(trade.fill_bar_utc)
    tp_limit, tp_meta, tp_reason = select_long_a_tp_at_fill(snap_fill, entry_fill=float(trade.entry_price))
    tp_bot, tp_top = _tp_pool_bounds(tp_meta)
    if trade.tp_limit is not None:
        tp_limit = float(trade.tp_limit)

    p_upper_fill = _region_upper_at_bar(session, trade.fill_bar_utc, float(trade.entry_price), trade.pool_key)
    entry = float(trade.entry_price)
    sl = float(trade.sl_price)
    sl_dist = _pct(sl, entry)
    if sl_dist is not None:
        sl_dist = -sl_dist  # distance below entry as positive risk
        sl_dist = (entry - sl) / entry * 100.0

    tp_dist = _pct(tp_limit, entry) if tp_limit is not None else None

    chain = _events_for_order_chain(ledger, fill_ev or {}, repricing_audits) if fill_ev else []
    repricing_events = [e for e in chain if e.get("action") in ("REPLACE", "CANCEL", "REPRICING_AUDIT")]

    tp_ok = tp_dist is not None and tp_dist + 1e-12 >= 0.5
    tp_violation = None if tp_ok or tp_limit is None else "TP_BELOW_MIN_0_5_PCT_AT_FILL"

    dossier = {
        "variant": variant,
        "window": window,
        "position_id": trade.position_id,
        "signal_close_utc": trade.signal_close_utc,
        "place_utc": place_utc,
        "fill_utc": trade.fill_bar_utc,
        "entry_price_usdt": entry,
        "entry_price_usdt_repr": fmt_price(entry),
        "planned_limit_at_last_place": planned_limit,
        "p_lower_at_place": p_lo,
        "p_upper_at_place": p_hi,
        "p_upper_at_fill": p_upper_fill,
        "pool_key": trade.pool_key,
        "q_lower": q_lo,
        "q_upper": q_hi,
        "structural_sl_usdt": sl,
        "risk_pct_entry_to_sl": trade.risk_pct if trade.risk_pct is not None else round(sl_dist, 8) if sl_dist else None,
        "sl_distance_pct": round(sl_dist, 8) if sl_dist is not None else None,
        "tp_price_usdt": tp_limit,
        "tp_upper_pool_lower": tp_bot,
        "tp_upper_pool_upper": tp_top,
        "tp_distance_pct": round(tp_dist, 8) if tp_dist is not None else None,
        "tp_min_0_5_ok_at_fill": tp_ok,
        "tp_rule_violation": tp_violation,
        "exit_utc": trade.exit_bar_utc,
        "exit_price_usdt": trade.exit_price,
        "exit_reason": trade.exit_reason,
        "gross_return_pct": trade.gross_return_pct,
        "repricing_events": repricing_events,
        "chart_markers": {
            "ENTRY": fmt_price(entry),
            "STOP_LOSS": fmt_price(sl),
            "TAKE_PROFIT": fmt_price(tp_limit) if tp_limit is not None else None,
        },
        "event_timeline_utc": {
            "PLACE": place_utc,
            "FILL": trade.fill_bar_utc,
            "EXIT": trade.exit_bar_utc,
            "CANCEL_REPLACE": [
                {
                    "bar": e.get("bar_utc") or e.get("bar_utc"),
                    "action": e.get("action"),
                    "detail": {k: v for k, v in e.items() if k not in ("phase",)},
                }
                for e in repricing_events
            ],
        },
        "open_at_window_end": False,
    }
    if open_at_end and open_at_end.get("fill_bar_utc") == trade.fill_bar_utc:
        dossier["open_at_window_end"] = True
    return dossier


def audit_b_place_replace_tp(ledger: list[dict], session, min_tp_pct: float = 0.5) -> list[dict]:
    violations = []
    for ev in ledger:
        if ev.get("phase") != "entry" or ev.get("action") not in ("PLACE", "REPLACE"):
            continue
        if ev.get("replace_kind") == "DYNAMIC_UPPER_GROWTH_V2K" or (
            ev.get("action") == "PLACE" and ev.get("selection_reason") == "DYNAMIC_UPPER_GROWTH_REPRICE"
        ):
            lim = ev.get("new_limit") or ev.get("limit")
            if lim is None:
                continue
            bar = ev.get("bar_utc")
            snap = session.collect_at_close(bar)
            tp, _meta, reason = select_long_a_tp_at_fill(snap, entry_fill=float(lim))
            if tp is None:
                violations.append({"bar_utc": bar, "action": ev.get("action"), "issue": reason or "NO_TP"})
                continue
            gap = (float(tp) - float(lim)) / float(lim) * 100.0
            if gap + 1e-12 < min_tp_pct:
                violations.append(
                    {
                        "bar_utc": bar,
                        "action": ev.get("action"),
                        "issue": "TP_GAP_BELOW_0_5",
                        "planned_entry": float(lim),
                        "planned_tp": float(tp),
                        "gap_pct": gap,
                    }
                )
            elif ev.get("tp_gap_pct") is not None and float(ev["tp_gap_pct"]) + 1e-12 < min_tp_pct:
                violations.append(
                    {"bar_utc": bar, "action": ev.get("action"), "issue": "EVENT_TP_GAP_BELOW_0_5", **ev}
                )
    return violations


def window_comparison_table(a_dossiers: list[dict], b_dossiers: list[dict], a_sum: dict, b_sum: dict) -> dict:
    def key(d):
        return f"{d['fill_utc']}|{d['entry_price_usdt_repr']}"

    ka = {key(d): d for d in a_dossiers}
    kb = {key(d): d for d in b_dossiers}
    sa, sb = set(ka), set(kb)

    def avg_risk(ds):
        rs = [d["risk_pct_entry_to_sl"] for d in ds if d.get("risk_pct_entry_to_sl") is not None]
        return round(sum(rs) / len(rs), 8) if rs else None

    def sum_pos(ds):
        return round(sum(d["gross_return_pct"] for d in ds if (d.get("gross_return_pct") or 0) > 0), 6)

    def sum_neg(ds):
        return round(sum(d["gross_return_pct"] for d in ds if (d.get("gross_return_pct") or 0) < 0), 6)

    def sum_all(ds):
        return round(
            sum(d["gross_return_pct"] for d in ds if d.get("gross_return_pct") is not None),
            6,
        )

    return {
        "A_trades": len(a_dossiers),
        "B_trades": len(b_dossiers),
        "shared_fills": sorted(sa & sb),
        "only_A": sorted(sa - sb),
        "only_B": sorted(sb - sa),
        "A_pending_places": a_sum.get("places", 0) - a_sum.get("fills", 0),
        "B_pending_places": b_sum.get("places", 0) - b_sum.get("fills", 0),
        "A_cancels": a_sum.get("cancels"),
        "B_cancels": b_sum.get("cancels"),
        "A_replaces": a_sum.get("replaces"),
        "B_replaces": b_sum.get("replaces"),
        "A_tp_exits": a_sum.get("tp_exits"),
        "B_tp_exits": b_sum.get("tp_exits"),
        "A_sl_exits": a_sum.get("sl_exits"),
        "B_sl_exits": b_sum.get("sl_exits"),
        "A_avg_risk_pct": avg_risk(a_dossiers),
        "B_avg_risk_pct": avg_risk(b_dossiers),
        "A_sum_positive_gross_pct": sum_pos(a_dossiers),
        "B_sum_positive_gross_pct": sum_pos(b_dossiers),
        "A_sum_negative_gross_pct": sum_neg(a_dossiers),
        "B_sum_negative_gross_pct": sum_neg(b_dossiers),
        "A_sum_gross_pct": sum_all(a_dossiers),
        "B_sum_gross_pct": sum_all(b_dossiers),
    }


def write_trade_chart_csv(path, dossiers: list[dict]) -> None:
    fields = [
        "variant",
        "window",
        "fill_utc",
        "entry_price_usdt_repr",
        "structural_sl_usdt_repr",
        "tp_price_usdt_repr",
        "tp_distance_pct",
        "sl_distance_pct",
        "exit_utc",
        "exit_price_usdt_repr",
        "exit_reason",
        "gross_return_pct",
        "p_lower_at_place",
        "p_upper_at_place",
        "q_lower",
        "q_upper",
        "tp_upper_pool_lower",
        "tp_upper_pool_upper",
        "place_utc",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for d in dossiers:
            w.writerow(
                {
                    "variant": d["variant"],
                    "window": d["window"],
                    "fill_utc": d["fill_utc"],
                    "entry_price_usdt_repr": d["entry_price_usdt_repr"],
                    "structural_sl_usdt_repr": fmt_price(d["structural_sl_usdt"]),
                    "tp_price_usdt_repr": fmt_price(d["tp_price_usdt"]),
                    "tp_distance_pct": d.get("tp_distance_pct"),
                    "sl_distance_pct": d.get("sl_distance_pct"),
                    "exit_utc": d.get("exit_utc"),
                    "exit_price_usdt_repr": fmt_price(d.get("exit_price_usdt")),
                    "exit_reason": d.get("exit_reason"),
                    "gross_return_pct": d.get("gross_return_pct"),
                    "p_lower_at_place": d.get("p_lower_at_place"),
                    "p_upper_at_place": d.get("p_upper_at_place"),
                    "q_lower": d.get("q_lower"),
                    "q_upper": d.get("q_upper"),
                    "tp_upper_pool_lower": d.get("tp_upper_pool_lower"),
                    "tp_upper_pool_upper": d.get("tp_upper_pool_upper"),
                    "place_utc": d.get("place_utc"),
                }
            )


def dossier_to_chart_row(d: dict) -> dict:
    exit_p = d.get("exit_price_usdt")
    return {
        "datetime_utc": d["fill_utc"],
        "variant": d["variant"],
        "ENTRY_USDT": d["entry_price_usdt_repr"],
        "SL_USDT": fmt_price(d["structural_sl_usdt"]),
        "TP_USDT": fmt_price(d["tp_price_usdt"]),
        "EXIT_USDT": fmt_price(exit_p) if exit_p is not None else "NULL",
        "result_pct": d.get("gross_return_pct") if d.get("exit_utc") else "NULL",
    }

"""HTF structural break definitions D1–D5 vs current bearish_ll (analysis only)."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OUT_JSON = ROOT / "analyze_xrp_htf_structural_break_v1.json"
OUT_MD = ROOT / "analyze_xrp_htf_structural_break_v1.md"
OUT_CSV = ROOT / "analyze_xrp_htf_structural_break_compare_v1.csv"

SYMBOL = "XRPUSDT"

BAD_ENTRIES = {
    "2026-06-11T19:15:00+00:00",
    "2026-06-14T21:45:00+00:00",
    "2026-06-14T22:30:00+00:00",
    "2026-06-14T23:30:00+00:00",
}
GOOD_SAMPLE_SIZE = 5
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)

D1_SOURCE = "analyze_xrp_recovered_support_regime_v1.py::swing_points (1-bar pivot low/high)"


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _ensure_paths() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def last_closed_idx(bars: list[dict], moment: datetime) -> int | None:
    idx = None
    for i, b in enumerate(bars):
        if b["close_time"] <= moment:
            idx = i
    return idx


def confirmed_pivot_lows(bars: list[dict], end_idx: int, left: int, right: int) -> list[dict]:
    """Pivot low at i confirmed causally at bar close i+right (needs i+right <= end_idx)."""
    out = []
    if end_idx is None:
        return out
    for i in range(left, end_idx - right + 1):
        lv = bars[i]["low"]
        ok = True
        for j in range(1, left + 1):
            if bars[i - j]["low"] <= lv:
                ok = False
        for j in range(1, right + 1):
            if bars[i + j]["low"] <= lv:
                ok = False
        if not ok:
            continue
        known_i = i + right
        out.append(
            {
                "bar_index": i,
                "price": lv,
                "bar_open": bars[i]["open_time"].isoformat(),
                "known_at_close": bars[known_i]["close_time"].isoformat(),
                "uses_wick_low": True,
                "left_bars": left,
                "right_bars": right,
            }
        )
    return out


def confirmed_pivot_highs(bars: list[dict], end_idx: int, left: int, right: int) -> list[dict]:
    out = []
    if end_idx is None:
        return out
    for i in range(left, end_idx - right + 1):
        hv = bars[i]["high"]
        ok = True
        for j in range(1, left + 1):
            if bars[i - j]["high"] >= hv:
                ok = False
        for j in range(1, right + 1):
            if bars[i + j]["high"] >= hv:
                ok = False
        if not ok:
            continue
        known_i = i + right
        out.append(
            {
                "bar_index": i,
                "price": hv,
                "known_at_close": bars[known_i]["close_time"].isoformat(),
                "left_bars": left,
                "right_bars": right,
            }
        )
    return out


def d1_current_feature(bars: list[dict], end_idx: int) -> dict:
    """Mirrors recovered_support_regime swing_points bearish_ll."""
    from analyze_xrp_recovered_support_regime_v1 import swing_points

    sp = swing_points(bars, end_idx, 5)
    lows = sp.get("swing_lows") or []
    break_active = sp.get("last_structure_break_direction") == "bearish_ll"
    trigger = None
    if len(lows) >= 2 and lows[-1]["px"] < lows[-2]["px"]:
        bi = lows[-1]["i"]
        causal_known = (
            bars[bi + 1]["close_time"].isoformat()
            if bi + 1 <= end_idx
            else "NOT_YET_CONFIRMED_AT_END_IDX"
        )
        trigger = {
            "type": "pivot_low_lower_than_prior_pivot_low",
            "prior_pivot": lows[-2],
            "break_pivot": lows[-1],
            "pivot_bar_close_time_in_regime_code": lows[-1]["t"],
            "causal_known_after_right_bar_close": causal_known,
            "wick_based_pivot_low": lows[-1]["px"],
            "close_not_required": True,
        }
    return {
        "definition": "D1",
        "source": D1_SOURCE,
        "pivot_left_right": "1 / 1 (implicit: i-1, i+1)",
        "lookahead_risk": "Pivot at i only evaluable at end_idx>=i+1; no future beyond end_idx",
        "swing_lows_used": lows,
        "swing_highs_used": sp.get("swing_highs") or [],
        "bearish_break_at_entry": break_active,
        "trigger_detail": trigger,
        "last_structure_break_time": sp.get("last_structure_break_time"),
    }


def d2_close_below_prior_pivot_low(bars: list[dict], end_idx: int, left: int = 1, right: int = 1) -> dict:
    """Close below the pivot-low immediately before the latest confirmed pivot-low (scoped after prev known)."""
    pivots = confirmed_pivot_lows(bars, end_idx, left, right)
    break_ev = None
    if len(pivots) >= 2:
        prev = pivots[-2]
        last = pivots[-1]
        t0 = _utc(datetime.fromisoformat(prev["known_at_close"]))
        for i in range(prev["bar_index"] + 1, end_idx + 1):
            ct = bars[i]["close_time"]
            if ct < t0:
                continue
            if bars[i]["close"] < prev["price"]:
                break_ev = {
                    "time": ct.isoformat(),
                    "close": bars[i]["close"],
                    "level": prev["price"],
                    "prior_pivot_known_at": prev["known_at_close"],
                    "prior_pivot_price": prev["price"],
                    "latest_pivot_low": last,
                }
                break
    return {
        "definition": "D2",
        "pivot_lr": f"{left}/{right}",
        "confirmed_pivot_lows": pivots[-6:],
        "bearish_break_at_entry": break_ev is not None,
        "first_close_break_after_prev_pivot": break_ev,
        "uses_close": True,
    }


def last_higher_low_pivot(pivots: list[dict]) -> dict | None:
    if len(pivots) < 2:
        return None
    for k in range(len(pivots) - 1, 0, -1):
        if pivots[k]["price"] > pivots[k - 1]["price"]:
            return pivots[k]
    return None


def hl_before_current_ll_leg(pivots: list[dict]) -> dict | None:
    """Last HL pivot strictly before the latest LL pair (analytic, recovery context)."""
    if len(pivots) < 2 or pivots[-1]["price"] >= pivots[-2]["price"]:
        return None
    for k in range(len(pivots) - 2, 0, -1):
        if pivots[k]["price"] > pivots[k - 1]["price"]:
            return pivots[k]
    return None


def recovery_forensics(
    bars: list[dict],
    end_idx: int,
    reclaim_t: datetime | None,
    left: int = 1,
    right: int = 1,
) -> dict:
    pivots = confirmed_pivot_lows(bars, end_idx, left, right)
    if reclaim_t:
        pivots = [p for p in pivots if _utc(datetime.fromisoformat(p["known_at_close"])) >= reclaim_t]
    highs = confirmed_pivot_highs(bars, end_idx, left, right)
    if reclaim_t:
        highs = [p for p in highs if _utc(datetime.fromisoformat(p["known_at_close"])) >= reclaim_t]
    leg_hl = hl_before_current_ll_leg(pivots)
    close_break_leg_hl = None
    if leg_hl:
        t0 = _utc(datetime.fromisoformat(leg_hl["known_at_close"]))
        for i in range(end_idx + 1):
            if bars[i]["close_time"] < t0:
                continue
            if bars[i]["close"] < leg_hl["price"]:
                close_break_leg_hl = {
                    "time": bars[i]["close_time"].isoformat(),
                    "close": bars[i]["close"],
                    "level": leg_hl["price"],
                }
                break
    d1_ll = len(pivots) >= 2 and pivots[-1]["price"] < pivots[-2]["price"]
    return {
        "pivots_since_reclaim": pivots[-8:],
        "highs_since_reclaim": highs[-8:],
        "hl_before_current_ll_leg": leg_hl,
        "close_below_leg_hl": close_break_leg_hl is not None,
        "first_close_below_leg_hl": close_break_leg_hl,
        "d1_style_ll_on_1bar_pivots": d1_ll,
        "local_ll_but_leg_hl_close_intact": d1_ll and not close_break_leg_hl,
    }


def d3_close_below_last_hl(bars: list[dict], end_idx: int, left: int = 1, right: int = 1) -> dict:
    pivots = confirmed_pivot_lows(bars, end_idx, left, right)
    hl = last_higher_low_pivot(pivots)
    break_ev = None
    if hl:
        hl_known = _utc(datetime.fromisoformat(hl["known_at_close"]))
        for i in range(end_idx + 1):
            if bars[i]["close_time"] < hl_known:
                continue
            if bars[i]["close"] < hl["price"]:
                break_ev = {
                    "time": bars[i]["close_time"].isoformat(),
                    "close": bars[i]["close"],
                    "last_hl_pivot": hl,
                }
                break
    return {
        "definition": "D3",
        "pivot_lr": f"{left}/{right}",
        "last_confirmed_higher_low": hl,
        "bearish_break_at_entry": break_ev is not None,
        "first_close_break_below_hl": break_ev,
        "uses_close": True,
    }


def d4_multi_pivot(bars: list[dict], end_idx: int) -> dict:
    return {
        "definition": "D4",
        "lr_2_bearish_ll_sequence": d1_style_ll(confirmed_pivot_lows(bars, end_idx, 2, 2)),
        "lr_3_bearish_ll_sequence": d1_style_ll(confirmed_pivot_lows(bars, end_idx, 3, 3)),
        "d2_lr2_close_break": d2_close_below_prior_pivot_low(bars, end_idx, 2, 2)["bearish_break_at_entry"],
        "d2_lr3_close_break": d2_close_below_prior_pivot_low(bars, end_idx, 3, 3)["bearish_break_at_entry"],
        "d3_lr2_hl_break": d3_close_below_last_hl(bars, end_idx, 2, 2)["bearish_break_at_entry"],
        "d3_lr3_hl_break": d3_close_below_last_hl(bars, end_idx, 3, 3)["bearish_break_at_entry"],
        "bearish_break_at_entry": d3_close_below_last_hl(bars, end_idx, 3, 3)["bearish_break_at_entry"],
    }


def d1_style_ll(pivots: list[dict]) -> bool:
    if len(pivots) < 2:
        return False
    return pivots[-1]["price"] < pivots[-2]["price"]


def m30_after_4h_break(
    bars_30m: list[dict],
    end_30m: int,
    t_break: datetime,
    entry_t: datetime,
) -> dict:
    """30m LH/LL after a 4h structural event (causal pivots on 30m)."""
    piv_h = confirmed_pivot_highs(bars_30m, end_30m, 1, 1)
    piv_l = confirmed_pivot_lows(bars_30m, end_30m, 1, 1)
    m30: dict[str, Any] = {
        "30m_lh_confirmed": False,
        "30m_ll_confirmed": False,
        "minutes_since_4h_break": round((entry_t - t_break).total_seconds() / 60.0, 1),
    }
    post_h = [p for p in piv_h if _utc(datetime.fromisoformat(p["known_at_close"])) >= t_break]
    if len(post_h) >= 2 and post_h[-1]["price"] < post_h[-2]["price"]:
        m30["30m_lh_confirmed"] = True
        m30["30m_lh_at"] = post_h[-1]["known_at_close"]
    post_l = [p for p in piv_l if _utc(datetime.fromisoformat(p["known_at_close"])) >= t_break]
    if len(post_l) >= 2 and post_l[-1]["price"] < post_l[-2]["price"]:
        m30["30m_ll_confirmed"] = True
        m30["30m_ll_at"] = post_l[-1]["known_at_close"]
    if m30.get("30m_ll_at"):
        m30["minutes_since_30m_confirmation"] = round(
            (entry_t - _utc(datetime.fromisoformat(m30["30m_ll_at"]))).total_seconds() / 60.0,
            1,
        )
    return m30


def d5_4h_plus_30m(bars_4h: list[dict], bars_30m: list[dict], end_4h: int, end_30m: int) -> dict:
    d3 = d3_close_below_last_hl(bars_4h, end_4h, 1, 1)
    break_4h = d3.get("first_close_break_below_hl")
    m30 = {"30m_lh_confirmed": False, "30m_ll_confirmed": False}
    if not break_4h:
        return {
            "definition": "D5",
            "bearish_break_at_entry": False,
            "d3_4h": d3,
            "after_4h_break": m30,
        }
    t_break = _utc(datetime.fromisoformat(break_4h["time"]))
    entry_t = bars_30m[end_30m]["close_time"]
    m30 = m30_after_4h_break(bars_30m, end_30m, t_break, entry_t)
    ok = d3["bearish_break_at_entry"] and m30["30m_lh_confirmed"] and m30["30m_ll_confirmed"]
    return {
        "definition": "D5",
        "bearish_break_at_entry": ok,
        "d3_4h": d3,
        "after_4h_break": m30,
    }


def build_4h_timeline(
    bars: list[dict],
    start_moment: datetime,
    entry_moment: datetime,
) -> list[dict]:
    end = last_closed_idx(bars, entry_moment)
    start = last_closed_idx(bars, start_moment) or 0
    if end is None:
        return []
    rows = []
    for i in range(start, end + 1):
        b = bars[i]
        pl = confirmed_pivot_lows(bars, i, 1, 1)
        ph = confirmed_pivot_highs(bars, i, 1, 1)
        pivot_low = pl[-1]["price"] if pl else ""
        pivot_high = ph[-1]["price"] if ph else ""
        state = "range"
        if pl and len(pl) >= 2 and pl[-1]["price"] < pl[-2]["price"]:
            state = "bearish_ll_pivot"
        rows.append(
            {
                "timestamp": b["close_time"].isoformat(),
                "close": b["close"],
                "high": b["high"],
                "low": b["low"],
                "pivot_high": pivot_high,
                "pivot_low": pivot_low,
                "structural_state": state,
            }
        )
    return rows


def _pool_pack(pool: dict, price: float, side: str, dist_anchor: str) -> dict:
    px = float(pool[dist_anchor])
    dist = (price - px) / price * 100.0 if side == "lower" else (px - price) / price * 100.0
    known = pool.get("known")
    if hasattr(known, "isoformat"):
        known = known.isoformat()
    return {
        "pool_id": pool.get("pool_id"),
        "bottom": pool.get("bottom"),
        "top": pool.get("top"),
        "status": pool.get("status", "active"),
        "known": known,
        "dist_pct": round(dist, 4),
        "width_pct": round((float(pool["top"]) - float(pool["bottom"])) / float(pool["bottom"]) * 100.0, 4),
        "age_hours": pool.get("age_hours"),
    }


def pool_context_at_entry(
    symbol: str,
    moment: datetime,
    price: float,
    candles_15m,
    idx15: int,
    candles_30m,
    idx30: int,
) -> dict:
    from analyze_4h_lower_short_block_v1 import h4_snapshot
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index, ui_lld_config

    snap = h4_snapshot(symbol, moment)
    lowers4 = [
        r
        for r in (snap.lowers or [])
        if r.get("status") == "active" and float(r["top"]) < price
    ]
    lowers4.sort(key=lambda r: -float(r["top"]))

    cfg15 = ui_lld_config("15m")
    pools15, _, _ = scanner_pools_for_index(candles_15m, "15m", idx15, cfg15)
    lo15 = [p for p in pools15 if p["side"] == "lower" and float(p["top"]) < price and p["known"] <= moment]
    up15 = [p for p in pools15 if p["side"] == "upper" and float(p["bottom"]) > price and p["known"] <= moment]
    lo15.sort(key=lambda p: -float(p["top"]))
    up15.sort(key=lambda p: float(p["bottom"]))

    cfg30 = ui_lld_config("30m")
    pools30, _, _ = scanner_pools_for_index(candles_30m, "30m", idx30, cfg30)
    lo30 = [p for p in pools30 if p["side"] == "lower" and float(p["top"]) < price and p["known"] <= moment]
    up30 = [p for p in pools30 if p["side"] == "upper" and float(p["bottom"]) > price and p["known"] <= moment]
    lo30.sort(key=lambda p: -float(p["top"]))
    up30.sort(key=lambda p: float(p["bottom"]))

    # 4h uppers via same scanner path on 4h prefix (h4_snapshot only exposes lowers)
    up4 = "NOT_PROVABLE_PARTIAL"

    return {
        "below_4h_support_nearest": _pool_pack(lowers4[0], price, "lower", "top") if lowers4 else None,
        "below_30m_support_nearest": _pool_pack(lo30[0], price, "lower", "top") if lo30 else None,
        "below_15m_support_nearest": _pool_pack(lo15[0], price, "lower", "top") if lo15 else None,
        "above_30m_upper_nearest": _pool_pack(up30[0], price, "upper", "bottom") if up30 else None,
        "above_15m_upper_nearest": _pool_pack(up15[0], price, "upper", "bottom") if up15 else None,
        "above_4h_upper_nearest": up4,
    }


def analyze_trade(
    trade: dict,
    group: str,
    market: dict,
    bars15: list[dict],
    candles15,
    candles30,
    bars30_idx: dict[datetime, int],
    include_timeline: bool,
    include_pools: bool,
) -> dict:
    from analyze_4h_lower_short_block_v1 import h4_snapshot
    from analyze_xrp_recovered_support_regime_v1 import reclaim_on_4h_pool

    entry_open = _utc(datetime.fromisoformat(trade["entry_open"]))
    bar15 = next(b for b in bars15 if b["open_time"] == entry_open)
    moment = bar15["close_time"]
    b4 = market["4h"]["bars"]
    reclaim_t = None
    snap = h4_snapshot(SYMBOL, moment)
    if snap.deepest_active:
        pool_row = {
            "top": snap.deepest_active["top"],
            "bottom": snap.deepest_active["bottom"],
        }
        pr = reclaim_on_4h_pool(b4, pool_row, moment)
        fr = pr.get("first_confirmed_close_above_top")
        if isinstance(fr, dict) and fr.get("time"):
            reclaim_t = _utc(datetime.fromisoformat(fr["time"]))
    e4 = last_closed_idx(b4, moment)
    e30 = last_closed_idx(market["30m"]["bars"], moment)
    b4 = market["4h"]["bars"]
    b30 = market["30m"]["bars"]

    defs = {
        "D1": d1_current_feature(b4, e4),
        "D2": d2_close_below_prior_pivot_low(b4, e4, 1, 1),
        "D3": d3_close_below_last_hl(b4, e4, 1, 1),
        "D4": d4_multi_pivot(b4, e4),
        "D5": d5_4h_plus_30m(b4, b30, e4, e30),
    }

    d3 = defs["D3"]
    hl_close_break = d3.get("first_close_break_below_hl")
    recovery = recovery_forensics(b4, e4, reclaim_t) if e4 is not None else {}
    d4 = defs["D4"]

    forensic = {
        "A_local_pivot_only": bool(
            defs["D1"]["bearish_break_at_entry"]
            and not recovery.get("close_below_leg_hl")
            and not d4.get("lr_3_bearish_ll_sequence")
        ),
        "B_structural_hl_broken": bool(recovery.get("close_below_leg_hl")),
        "C_close_below_structural_hl": recovery.get("close_below_leg_hl"),
        "D1_bearish_ll": defs["D1"]["bearish_break_at_entry"],
        "D3_global_last_hl_close_break": hl_close_break is not None,
        "D4_lr3_pivot_ll": d4.get("lr_3_bearish_ll_sequence"),
        "verdict": (
            "LOCAL_LL_BUT_NO_CONFIRMED_STRUCTURAL_BREAK"
            if defs["D1"]["bearish_break_at_entry"]
            and not recovery.get("close_below_leg_hl")
            and not d4.get("lr_3_bearish_ll_sequence")
            else (
                "CONFIRMED_STRUCTURAL_BREAK"
                if recovery.get("close_below_leg_hl") or defs["D3"]["bearish_break_at_entry"]
                else "NO_BEARISH_BREAK"
            )
        ),
    }

    out: dict[str, Any] = {
        "group": group,
        "entry_open": trade["entry_open"],
        "entry_decision_close": moment.isoformat(),
        "entry_price": trade["entry_price"],
        "definitions": defs,
        "recovery_swing_forensics": recovery,
        "reclaim_time": reclaim_t.isoformat() if reclaim_t else None,
        "forensic_abc": forensic,
    }

    if include_timeline:
        start = datetime(2026, 6, 6, 0, tzinfo=timezone.utc)
        out["timeline_4h"] = build_4h_timeline(b4, start, moment)

    d1 = defs["D1"]
    t_ref = None
    if d1.get("trigger_detail"):
        ck = d1["trigger_detail"].get("causal_known_after_right_bar_close")
        if ck and ck != "NOT_YET_CONFIRMED_AT_END_IDX":
            t_ref = _utc(datetime.fromisoformat(ck))
    elif d3.get("first_close_break_below_hl"):
        t_ref = _utc(datetime.fromisoformat(d3["first_close_break_below_hl"]["time"]))
    if t_ref and e30 is not None:
        out["m30_after_4h_reference"] = m30_after_4h_break(
            b30, e30, t_ref, b30[e30]["close_time"]
        )

    if include_pools:
        i30 = bars30_idx.get(moment)
        out["pool_context"] = pool_context_at_entry(
            SYMBOL,
            moment,
            float(trade["entry_price"]),
            candles15,
            bar15["candle_index"],
            candles30,
            i30 if i30 is not None else 0,
        )

    return out


def comparison_row(def_name: str, cases: list[dict]) -> dict:
    bad = [c for c in cases if c["group"] == "BAD_4_SL"]
    good = [c for c in cases if c["group"] == "GOOD_SHORTS"]
    def_key = "D4" if def_name == "D4" else def_name

    def has_break(c: dict) -> bool:
        d = c["definitions"][def_key]
        if def_name == "D4":
            return bool(d.get("lr_3_bearish_ll_sequence"))
        return bool(d.get("bearish_break_at_entry"))

    bb = sum(1 for c in bad if has_break(c))
    bw = len(bad) - bb
    gb = sum(1 for c in good if has_break(c))
    gw = len(good) - gb
    return {
        "definition": def_name,
        "BAD_with_break": bb,
        "BAD_without_break": bw,
        "GOOD_with_break": gb,
        "GOOD_without_break": gw,
        "BAD_without_break_pct": round(100 * bw / len(bad), 1) if bad else 0,
        "GOOD_without_break_pct": round(100 * gw / len(good), 1) if good else 0,
    }


def write_md(report: dict) -> None:
    lines = [
        "# XRP HTF Structural Break — Forensik v1",
        "",
        "Nur Analyse. Keine Produktionsänderung.",
        "",
        report["executive_summary"],
        "",
        "## D1 — Technische Dokumentation (aktuell)",
        "",
        report["meta"]["d1_documentation_text"],
        "",
        "## D1–D5 comparison",
        "",
        "| Definition | BAD+ | BAD− | GOOD+ | GOOD− | BAD−% | GOOD−% |",
        "|------------|------|------|-------|-------|-------|--------|",
    ]
    for r in report["comparison_table"]:
        lines.append(
            f"| {r['definition']} | {r['BAD_with_break']} | {r['BAD_without_break']} | "
            f"{r['GOOD_with_break']} | {r['GOOD_without_break']} | {r['BAD_without_break_pct']} | "
            f"{r['GOOD_without_break_pct']} |"
        )
    lines += ["", "### Definitionen (nur Analyse)", "", report["definition_legend"], ""]
    for c in report["cases"]:
        if c["group"] != "BAD_4_SL":
            continue
        lines += [
            f"## BAD Timeline — {c['entry_open']}",
            "",
            f"Forensic: **{c['forensic_abc']['verdict']}**",
            "",
            "| timestamp | close | high | low | pivot_high | pivot_low | structural_state |",
            "|-----------|-------|------|-----|------------|-----------|------------------|",
        ]
        for row in c.get("timeline_4h") or []:
            lines.append(
                f"| {row['timestamp']} | {row['close']} | {row['high']} | {row['low']} | "
                f"{row['pivot_high']} | {row['pivot_low']} | {row['structural_state']} |"
            )
        lines.append("")
    j11 = report.get("june_11_forensic", {})
    lines += ["## 11 June forensic (detail)", "", j11.get("text", ""), ""]
    OUT_MD.write_text("\n".join(lines) + "\n")


def executive_summary(report: dict) -> str:
    cmp = {r["definition"]: r for r in report["comparison_table"]}
    j11 = report["june_11_forensic"]
    bad_cases = [c for c in report["cases"] if c["group"] == "BAD_4_SL"]
    bad_local = sum(
        1
        for c in bad_cases
        if c["forensic_abc"]["verdict"] == "LOCAL_LL_BUT_NO_CONFIRMED_STRUCTURAL_BREAK"
    )
    return f"""### Current LL feature
{D1_SOURCE}: Pivot-Low wenn `low[i] < low[i±1]` (1 Bar links/rechts). **LL** wenn letztes Pivot-Low < vorheriges Pivot-Low. **Nur Wick**, kein Close. Kausal bekannt ab Close von Bar `i+1`. Feature-Flag in Regime: `no_bearish_4h_ll_break` = (direction != bearish_ll).

### BAD 4
D1 LL: {cmp['D1']['BAD_with_break']}/4 | D4 (3-Bar-Pivot-LL): {cmp['D4']['BAD_with_break']}/4 | D5 (4h+30m): {cmp['D5']['BAD_with_break']}/4 | Recovery-Leg-HL ohne Close-Break (Forensik): {bad_local}/4

### GOOD 20
D1 LL: {cmp['D1']['GOOD_with_break']}/20 | D3 global HL-Close: {cmp['D3']['GOOD_with_break']}/20 | D4: {cmp['D4']['GOOD_with_break']}/20 | D5: {cmp['D5']['GOOD_with_break']}/20

### D1–D5 comparison
Siehe Tabelle im Report-Body und CSV.

### 11 June forensic verdict
{j11.get('verdict', '')}

### Pool asymmetry
{report.get('pool_asymmetry_note', '')}

### Next step
Recovery-scoped HL + D4/D5 als Sensitivität; Chart-Abgleich der Leg-HL; kein Threshold-Tuning."""


def main() -> None:
    _ensure_paths()
    from analyze_xrp_recovered_support_regime_v1 import select_good
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import load_pane_candles

    data = json.loads((ROOT / "floor_guard_signal_list_v1.json").read_text())
    trades = data["symbols"]["XRPUSDT"]["with_guard"]
    bad = [t for t in trades if t["entry_open"] in BAD_ENTRIES]
    good = select_good(trades)
    good_sample = good[:GOOD_SAMPLE_SIZE]

    market = load_market(SYMBOL)
    _packed, candles15 = load_pane_candles(
        SYMBOL,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    _p30, candles30 = load_pane_candles(
        SYMBOL,
        "30m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    bars15 = build_15m_bars(candles15)
    bars30_idx: dict[datetime, int] = {}
    for i, b in enumerate(market["30m"]["bars"]):
        bars30_idx[b["close_time"]] = i

    cases = []
    for t in bad:
        cases.append(
            analyze_trade(t, "BAD_4_SL", market, bars15, candles15, candles30, bars30_idx, True, True)
        )
    for t in good:
        cases.append(
            analyze_trade(
                t,
                "GOOD_SHORTS",
                market,
                bars15,
                candles15,
                candles30,
                bars30_idx,
                False,
                t in good_sample,
            )
        )

    comparison = [comparison_row(d, cases) for d in ("D1", "D2", "D3", "D4", "D5")]
    j11_case = next(c for c in cases if c["entry_open"] == "2026-06-11T19:15:00+00:00")
    d1 = j11_case["definitions"]["D1"]
    d3 = j11_case["definitions"]["D3"]
    rec = j11_case.get("recovery_swing_forensics") or {}
    j11_verdict = j11_case["forensic_abc"]["verdict"]
    if j11_case["definitions"]["D1"]["bearish_break_at_entry"] and not j11_case["definitions"]["D4"].get(
        "lr_3_bearish_ll_sequence"
    ):
        j11_verdict = (
            "LOCAL_LL_ON_1BAR_PIVOT; NO_3BAR_PIVOT_LL; "
            + ("LEG_HL_CLOSE_BROKEN" if rec.get("close_below_leg_hl") else "LEG_HL_CLOSE_NOT_BROKEN")
        )
    j11_text = json.dumps(
        {
            "d1_trigger": d1.get("trigger_detail"),
            "d3_hl": d3.get("last_confirmed_higher_low"),
            "d3_close_break": d3.get("first_close_break_below_hl"),
            "recovery_leg_hl": rec.get("hl_before_current_ll_leg"),
            "recovery_close_below_leg_hl": rec.get("first_close_below_leg_hl"),
            "d4_lr3_ll": j11_case["definitions"]["D4"].get("lr_3_bearish_ll_sequence"),
            "m30": j11_case.get("m30_after_4h_reference"),
            "abc": j11_case["forensic_abc"],
            "timeline_tail": (j11_case.get("timeline_4h") or [])[-8:],
        },
        indent=2,
    )
    d1_doc = (
        "Quelle: `analyze_xrp_recovered_support_regime_v1.py::swing_points`. "
        "Schleife `i in range(1, end_idx)` — Pivot-Low an Bar i wenn `low[i] < low[i-1]` und `low[i] < low[i+1]` "
        "(nur Wick). `bearish_ll` wenn letztes Pivot-Low < vorheriges Pivot-Low; Zeitstempel = **Close-Zeit der Pivot-Bar i** "
        "(nicht i+1; kausal erst nach Schluss von Bar i+1, da rechte Nachbarbar nötig). "
        "Kein Close für LL. Kein Lookahead über `end_idx` hinaus. "
        "Feature `no_bearish_4h_ll_break` = (`last_structure_break_direction` != `bearish_ll`)."
    )
    legend = (
        "- **D1**: aktuelles Regime-Feature (1-Bar-Pivot-LL, Wick).\n"
        "- **D2**: erstes 4h-**Close** unter dem zuletzt **kausal bestätigten** Pivot-Low (1/1).\n"
        "- **D3**: HH/HL über Pivot-Lows; Break wenn **Close** < letztes bestätigtes **Higher Low**.\n"
        "- **D4**: 3-Bar-Pivot (L/R=3) — sequentielles LL wie D1; zusätzlich D2/D3 mit 2/3 in JSON.\n"
        "- **D5**: D3-4h-Close-Break **und** 30m LH **und** 30m LL nach Break-Zeit."
    )
    bad_pools = [c.get("pool_context") for c in cases if c["group"] == "BAD_4_SL" and c.get("pool_context")]
    good_pools = [c.get("pool_context") for c in cases if c["group"] == "GOOD_SHORTS" and c.get("pool_context")]

    def avg_dist(rows: list[dict], key: str) -> float | None:
        vals = [r[key]["dist_pct"] for r in rows if r.get(key)]
        return round(sum(vals) / len(vals), 3) if vals else None

    pool_note = (
        f"BAD n={len(bad_pools)} avg dist below 4h support {avg_dist(bad_pools, 'below_4h_support_nearest')}% "
        f"vs GOOD sample n={len(good_pools)} {avg_dist(good_pools, 'below_4h_support_nearest')}%; "
        f"above 30m upper BAD {avg_dist(bad_pools, 'above_30m_upper_nearest')}% vs GOOD "
        f"{avg_dist(good_pools, 'above_30m_upper_nearest')}% (pool_context in JSON)."
    )
    report = {
        "meta": {
            "analysis_only": True,
            "d1_documentation": {
                "file": "analyze_xrp_recovered_support_regime_v1.py",
                "function": "swing_points",
                "pivot": "1 bar left and right on wick low/high",
                "bearish_ll": "last pivot low < previous pivot low",
                "close_required": False,
                "causal_known": "pivot i needs bar i+1; timestamp in code uses pivot bar close",
            },
            "d1_documentation_text": d1_doc,
        },
        "definition_legend": legend,
        "cases": cases,
        "comparison_table": comparison,
        "june_11_forensic": {
            "verdict": j11_verdict,
            "text": j11_text,
        },
        "pool_asymmetry_note": pool_note,
    }
    report["executive_summary"] = executive_summary(report)
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(comparison[0].keys()))
        w.writeheader()
        w.writerows(comparison)
    write_md(report)
    print("wrote", OUT_JSON, OUT_MD, OUT_CSV)


if __name__ == "__main__":
    main()

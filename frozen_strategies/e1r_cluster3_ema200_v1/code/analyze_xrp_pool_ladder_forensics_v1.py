"""Forensics: pool ladder vs chart for XRP 11-Jun and 14-Jun entries (analysis only)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OUT_JSON = ROOT / "analyze_xrp_pool_ladder_forensics_v1.json"
OUT_MD = ROOT / "analyze_xrp_pool_ladder_forensics_v1.md"

SYMBOL = "XRPUSDT"
WINDOW_START = datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc)
DECISION_JUN11 = datetime(2026, 6, 11, 19, 30, tzinfo=timezone.utc)
DECISION_JUN14 = datetime(2026, 6, 14, 22, 0, tzinfo=timezone.utc)
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)

LADDER_SOURCE = "analyze_xrp_ema200_transition_shadow_v1.py::ladder_metrics + active_lowers"


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


def pool_row(p: dict, tf: str, moment: datetime, touches: dict) -> dict:
    known = p["known"]
    if hasattr(known, "isoformat"):
        known = known.isoformat()
    br = p.get("break_at")
    if br is not None and hasattr(br, "isoformat"):
        br = br.isoformat()
    top, bottom = float(p["top"]), float(p["bottom"])
    active = br is None or _utc(datetime.fromisoformat(br)) > moment
    pid = p["pool_id"]
    tc = touches.get(pid, {"count": 0, "last": None})
    return {
        "pool_id": pid,
        "timeframe": tf,
        "known_at": known,
        "bottom": bottom,
        "top": top,
        "midpoint": round((top + bottom) / 2.0, 8),
        "width": round(top - bottom, 8),
        "width_pct": round((top - bottom) / bottom * 100.0, 4) if bottom else None,
        "active_at_decision": active,
        "broken_at": br,
        "touch_count_until_decision": tc["count"],
        "last_touch_until_decision": tc["last"],
    }


def walk_timeline_pools(
    tf: str,
    candles,
    bars: list[dict],
    t_start: datetime,
    t_end: datetime,
) -> tuple[dict[str, dict], dict[str, dict]]:
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index, ui_lld_config

    cfg = ui_lld_config(tf)
    scan_cache: dict = {}
    pools_by_id: dict[str, dict] = {}
    touches: dict[str, dict] = {}
    creation_order: list[str] = []

    i0 = last_closed_idx(bars, t_start) or 0
    i1 = last_closed_idx(bars, t_end)
    if i1 is None:
        return {}, {}

    for i in range(i0, i1 + 1):
        bar = bars[i]
        moment = bar["close_time"]
        pools, _, _ = scanner_pools_for_index(candles, tf, i, cfg, cache=scan_cache)
        for p in pools:
            if p["side"] != "lower":
                continue
            kn = p["known"]
            if kn > moment:
                continue
            pid = p["pool_id"]
            if pid not in pools_by_id:
                pools_by_id[pid] = {**p, "timeframe": tf, "first_seen_bar_close": moment.isoformat()}
                creation_order.append(pid)
            else:
                pools_by_id[pid].update(p)
            touched = bar["low"] <= float(p["top"]) and bar["high"] >= float(p["bottom"])
            if touched and moment <= t_end:
                t = touches.setdefault(pid, {"count": 0, "last": None})
                t["count"] += 1
                t["last"] = moment.isoformat()

    ordered = {pid: pools_by_id[pid] for pid in creation_order}
    return ordered, touches


def ladder_metrics(lowers_sorted: list[dict]) -> dict:
    """Copy of shadow script logic on list sorted by (known, top)."""
    last3 = lowers_sorted[-3:] if len(lowers_sorted) >= 3 else lowers_sorted[:]
    tops = [float(p["top"]) for p in last3]
    ids = [p.get("pool_id") for p in last3]
    rising_2 = len(tops) >= 2 and tops[-1] > tops[-2]
    rising_3 = len(tops) >= 3 and tops[0] < tops[1] < tops[2]
    return {
        "pool_ids_used": ids,
        "tops_chronological": tops,
        "known_at": [
            p["known"].isoformat() if hasattr(p["known"], "isoformat") else p["known"] for p in last3
        ],
        "rising_2_step": rising_2,
        "rising_3_step": rising_3,
        "rule": "last 3 lowers after sort key=(known, top); rising_2=last>prev; rising_3=strict increase all 3 tops",
    }


def active_lowers_at(pools: list[dict], moment: datetime, price: float) -> list[dict]:
    rows = [
        p
        for p in pools
        if p["side"] == "lower"
        and p["known"] <= moment
        and (p.get("break_at") is None or p["break_at"] > moment)
    ]
    rows.sort(key=lambda p: (p["known"], p["top"]))
    return rows


def pools_at_decision_all_tf(market, candles_by_tf, decision: datetime) -> dict[str, list[dict]]:
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index, ui_lld_config

    out = {}
    scan_cache: dict = {}
    for tf in ("15m", "30m", "4h"):
        bars = market[tf]["bars"] if tf != "15m" else None
        candles = candles_by_tf[tf]
        if tf == "15m":
            bars = candles_by_tf["bars15"]
        else:
            bars = market[tf]["bars"]
        idx = last_closed_idx(bars, decision)
        if idx is None:
            out[tf] = []
            continue
        pools, _, _ = scanner_pools_for_index(candles, tf, idx, ui_lld_config(tf), cache=scan_cache)
        out[tf] = pools
    return out


def price_clusters(pool_records: list[dict], gap_pct: float = 0.35) -> list[dict]:
    """Greedy cluster by overlapping/near tops (descriptive, fixed gap_pct not optimized)."""
    if not pool_records:
        return []
    items = sorted(pool_records, key=lambda r: r["top"])
    clusters: list[list[dict]] = []
    for row in items:
        placed = False
        for cl in clusters:
            hi = max(x["top"] for x in cl)
            lo = min(x["bottom"] for x in cl)
            if row["bottom"] <= hi * (1 + gap_pct / 100.0) and row["top"] >= lo * (1 - gap_pct / 100.0):
                cl.append(row)
                placed = True
                break
        if not placed:
            clusters.append([row])
    result = []
    for i, cl in enumerate(clusters, start=1):
        result.append(
            {
                "cluster_index": i,
                "lowest_bottom": min(x["bottom"] for x in cl),
                "highest_top": max(x["top"] for x in cl),
                "pool_count": len(cl),
                "timeframes": sorted({x["timeframe"] for x in cl}),
                "earliest_known_at": min(x["known_at"] for x in cl),
                "latest_known_at": max(x["known_at"] for x in cl),
                "pool_ids": [x["pool_id"] for x in cl],
            }
        )
    result.sort(key=lambda c: c["lowest_bottom"])
    return result


def structural_steps_below_price(clusters: list[dict], price: float) -> list[dict]:
    return [c for c in clusters if c["highest_top"] < price]


def ema_context(bars15: list[dict], decision: datetime) -> dict:
    idx = last_closed_idx(bars15, decision)
    bar = bars15[idx] if idx is not None else {}
    crosses = []
    for i in range(1, len(bars15)):
        if bars15[i]["close_time"] > decision:
            break
        p, c = bars15[i - 1], bars15[i]
        e0, e1 = p.get("ema200"), c.get("ema200")
        if e0 is None or e1 is None:
            continue
        if p["close"] <= e0 and c["close"] > e1:
            crosses.append(("bullish", c["close_time"].isoformat()))
        elif p["close"] >= e0 and c["close"] < e1:
            crosses.append(("bearish", c["close_time"].isoformat()))
    last_bull = next((t for d, t in reversed(crosses) if d == "bullish"), None)
    last_bear = next((t for d, t in reversed(crosses) if d == "bearish"), None)
    ema = bar.get("ema200")
    close = bar.get("close")
    hours_since_bull = None
    if last_bull:
        hours_since_bull = round((decision - _utc(datetime.fromisoformat(last_bull))).total_seconds() / 3600.0, 2)
    return {
        "last_bullish_15m_cross": last_bull,
        "last_bearish_15m_cross": last_bear,
        "close_at_decision": close,
        "ema200_at_decision": ema,
        "price_above_ema200": close > ema if ema and close else None,
        "hours_since_last_bull_cross": hours_since_bull,
    }


def analyze_entry(
    label: str,
    decision: datetime,
    window_start: datetime,
    market,
    candles_by_tf: dict,
    bars15: list[dict],
) -> dict:
    price = next(
        b["close"] for b in bars15 if b["close_time"] == decision
    )
    pool_snap = pools_at_decision_all_tf(market, candles_by_tf, decision)

    by_tf_records: dict[str, list[dict]] = {}
    all_window: list[dict] = []

    for tf in ("15m", "30m", "4h"):
        bars = bars15 if tf == "15m" else market[tf]["bars"]
        ordered, touches = walk_timeline_pools(tf, candles_by_tf[tf], bars, window_start, decision)
        recs = [
            pool_row(ordered[pid], tf, decision, touches)
            for pid in ordered
            if _utc(datetime.fromisoformat(ordered[pid]["known"].isoformat() if hasattr(ordered[pid]["known"], "isoformat") else ordered[pid]["known"])) <= decision
        ]
        by_tf_records[tf] = recs
        all_window.extend(recs)

    # active lowers for ladder (15m at decision — same as shadow entry)
    idx15 = last_closed_idx(bars15, decision)
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index, ui_lld_config

    pools15, _, _ = scanner_pools_for_index(
        candles_by_tf["15m"], "15m", idx15, ui_lld_config("15m"), cache={}
    )
    lowers15 = active_lowers_at(pools15, decision, price)
    ladder15 = ladder_metrics(lowers15)

    # All active lowers at decision (all TF) for structural pick
    active_all = []
    for tf in ("15m", "30m", "4h"):
        for p in pool_snap[tf]:
            if p["side"] != "lower":
                continue
            if p["known"] <= decision and (p.get("break_at") is None or p["break_at"] > decision):
                active_all.append(pool_row(p, tf, decision, {}))

    active_all.sort(key=lambda r: r["top"])
    below = [r for r in active_all if r["top"] < price]
    below.sort(key=lambda r: r["top"])

    # structural steps: pick widest / highest top per price band (descriptive)
    candidates = []
    if len(below) >= 3:
        # three highest distinct top bands below price
        tops = sorted({r["top"] for r in below}, reverse=True)[:6]
        for t in tops:
            band = [r for r in below if abs(r["top"] - t) < 0.002]
            if band:
                candidates.append(max(band, key=lambda x: x["width"]))
        candidates = sorted(candidates, key=lambda x: x["top"])[:3]

    clusters = price_clusters([r for r in all_window if r["active_at_decision"] or r["known_at"]], gap_pct=0.35)
    steps = structural_steps_below_price(clusters, price)
    rising_clusters = len(steps) >= 2 and all(
        steps[i]["highest_top"] < steps[i + 1]["highest_top"] for i in range(len(steps) - 1)
    )

    decision_snapshot = {}
    for tf in ("15m", "30m", "4h"):
        lowers = [p for p in pool_snap[tf] if p["side"] == "lower" and p["known"] <= decision]
        decision_snapshot[tf] = sorted(
            [pool_row(p, tf, decision, {}) for p in lowers],
            key=lambda r: r["known_at"],
        )
    from analyze_4h_lower_short_block_v1 import h4_snapshot

    snap4 = h4_snapshot(SYMBOL, decision)
    decision_snapshot["4h_h4_snapshot"] = [
        {
            "pool_id": r.get("pool_id"),
            "known_at": r.get("known"),
            "bottom": r.get("bottom"),
            "top": r.get("top"),
            "status": r.get("status"),
        }
        for r in (snap4.lowers or [])
    ]

    return {
        "label": label,
        "decision_time": decision.isoformat(),
        "entry_price": price,
        "window_start": window_start.isoformat(),
        "pools_known_in_window_walk": by_tf_records,
        "all_lowers_known_by_decision": decision_snapshot,
        "pools_by_tf": by_tf_records,
        "sort_by_known_at": {tf: sorted(by_tf_records[tf], key=lambda r: r["known_at"]) for tf in by_tf_records},
        "sort_by_top": {tf: sorted(by_tf_records[tf], key=lambda r: r["top"]) for tf in by_tf_records},
        "ladder_15m_at_decision": ladder15,
        "active_lowers_all_tf_below_price": below,
        "visual_step_candidates_15m": candidates,
        "clusters_in_window": clusters,
        "support_clusters_below_price": steps,
        "clusters_rising_bottom_to_top": rising_clusters,
        "ema200_context": ema_context(bars15, decision),
    }


def build_md(report: dict) -> str:
    j11 = report["june_11"]
    j14 = report["june_14"]
    cmp = report["comparison"]
    return "\n".join(
        [
            "# XRP Pool Ladder Forensics v1",
            "",
            "Nur Analyse. Keine Produktionsänderung.",
            "",
            "### 11 June visible ladder",
            cmp["june_11_visible_ladder"],
            "",
            "### Current algorithm selection",
            cmp["algorithm_selection"],
            "",
            "### Why mismatch",
            cmp["why_mismatch"],
            "",
            "### 14 June comparison",
            cmp["june_14_comparison"],
            "",
            "### Structural pool steps",
            cmp["structural_pool_steps"],
            "",
            "### Next step",
            cmp["next_step"],
            "",
            "## Detail JSON",
            f"Ladder source: {LADDER_SOURCE}",
            "",
            "### 11.06 ladder_15m",
            json.dumps(j11["ladder_15m_at_decision"], indent=2),
            "",
            "### 14.06 ladder_15m",
            json.dumps(j14["ladder_15m_at_decision"], indent=2),
        ]
    )


def main() -> None:
    _ensure_paths()
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import load_pane_candles

    market = load_market(SYMBOL)
    _p15, c15 = load_pane_candles(
        SYMBOL, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    _p30, c30 = load_pane_candles(
        SYMBOL, "30m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    _p4, c4 = load_pane_candles(
        SYMBOL, "4h", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    bars15 = build_15m_bars(c15)
    candles_by_tf = {"15m": c15, "30m": c30, "4h": c4, "bars15": bars15}

    j11 = analyze_entry("2026-06-11", DECISION_JUN11, WINDOW_START, market, candles_by_tf, bars15)
    j14 = analyze_entry(
        "2026-06-14",
        DECISION_JUN14,
        datetime(2026, 6, 13, 0, 0, tzinfo=timezone.utc),
        market,
        candles_by_tf,
        bars15,
    )

    l11 = j11["ladder_15m_at_decision"]
    l14 = j14["ladder_15m_at_decision"]
    steps11 = j11["support_clusters_below_price"]
    steps14 = j14["support_clusters_below_price"]

    visible11 = (
        f"Unter Preis {j11['entry_price']}: {len(steps11)} Preis-Cluster (15m/30m/4h im Fenster). "
        f"Kandidaten-Etagen (heuristisch): {[c['pool_id'] for c in j11['visual_step_candidates_15m']]}. "
        f"Cluster-Tops aufsteigend: {[round(s['highest_top'], 4) for s in steps11]}."
    )
    algo = (
        f"`rising_2/3` nutzt die letzten 3 aktiven 15m-Lower nach Sortierung (known_at, top). "
        f"11.06 IDs={l11['pool_ids_used']} tops={l11['tops_chronological']} → rising_2={l11['rising_2_step']} rising_3={l11['rising_3_step']}. "
        f"14.06 IDs={l14['pool_ids_used']} tops={l14['tops_chronological']} → rising_2={l14['rising_2_step']} rising_3={l14['rising_3_step']}."
    )
    why = (
        "Am 11.06. sind die drei chronologisch letzten 15m-Lower-Pools (15:30, 17:00, 17:45) "
        "alle im selben enge Band (~1.1036–1.1065) mit fallenden Tops — Mikro-Refinements nach dem größeren Aufwärts-Schritt, "
        "nicht drei große Support-Etagen. Der Chart zeigt ältere/höhere TF- oder weiter zurückliegende Stufen; "
        "die Ladder-Logik ignoriert diese, weil nur die letzten 3 nach known_at genommen werden."
    )
    j14c = (
        f"14.06. letzte drei 15m-Lowers haben strikt steigende Tops {l14['tops_chronological']} "
        f"(neue Pools 15:00–20:30). Gleicher Algorithmus, aber ohne fallende Mikro-Sequenz dazwischen."
    )
    structural = (
        f"11.06 Cluster rising={j11['clusters_rising_bottom_to_top']}; 14.06 rising={j14['clusters_rising_bottom_to_top']}. "
        "Cluster-Gruppierung (deskriptiv, gap 0.35%) kann mehrere TFs zusammenfassen — siehe support_clusters_below_price in JSON."
    )
    next_step = (
        "Manuell Chart ↔ pool_id abgleichen; optional Ladder auf Cluster-Etagen oder TF-getrennte Stufen testen (neuer Shadow, nicht Produktion)."
    )

    report = {
        "meta": {"analysis_only": True, "ladder_source": LADDER_SOURCE},
        "june_11": j11,
        "june_14": j14,
        "comparison": {
            "june_11_visible_ladder": visible11,
            "algorithm_selection": algo,
            "why_mismatch": why,
            "june_14_comparison": j14c,
            "structural_pool_steps": structural,
            "next_step": next_step,
        },
    }
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    OUT_MD.write_text(build_md(report) + "\n")
    print("wrote", OUT_JSON, OUT_MD)


if __name__ == "__main__":
    main()

"""Shadow: E1R vs E1R_GAP3 (positive gap_2_3 at start only)."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OUT_JSON = ROOT / "analyze_crosscoin_e1r_gap3_start_shadow_v1.json"
OUT_MD = ROOT / "analyze_crosscoin_e1r_gap3_start_shadow_v1.md"
OUT_CSV = ROOT / "analyze_crosscoin_e1r_gap3_start_shadow_trades_v1.csv"
SIGNAL_JSON = ROOT / "floor_guard_signal_list_v1.json"
COINS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
BAD4_XRP = {
    "2026-06-11T19:15:00+00:00",
    "2026-06-14T21:45:00+00:00",
    "2026-06-14T22:30:00+00:00",
    "2026-06-14T23:30:00+00:00",
}
ANCHOR_CROSS = "2026-06-11T17:45:00+00:00"


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _ensure_paths() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _median(vals: list[float]) -> float | None:
    return round(statistics.median(vals), 4) if vals else None


def _percentile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[f] + (s[c] - s[f]) * (k - f), 4) if f != c else round(s[f], 4)


def gap_2_3_metrics(ctx: dict) -> dict:
    c2t, c3b = ctx.get("cluster_2_top"), ctx.get("cluster_3_bottom")
    g_abs = ctx.get("gap_2_to_3")
    if g_abs is None and c2t is not None and c3b is not None:
        g_abs = float(c3b) - float(c2t)
    g_pct = None
    if g_abs is not None and c2t:
        g_pct = round((float(c3b) / float(c2t) - 1.0) * 100.0, 6) if c3b else None
    if g_abs is None:
        status = "unknown"
    elif g_abs < 0:
        status = "overlap"
    elif abs(g_abs) < 1e-9:
        status = "touching"
    else:
        status = "positive"
    return {
        "cluster_1_bottom": ctx.get("cluster_1_bottom"),
        "cluster_1_top": ctx.get("cluster_1_top"),
        "cluster_2_bottom": ctx.get("cluster_2_bottom"),
        "cluster_2_top": c2t,
        "cluster_3_bottom": c3b,
        "cluster_3_top": ctx.get("cluster_3_top"),
        "gap_1_2": ctx.get("gap_1_to_2"),
        "gap_2_3_abs": g_abs,
        "gap_2_3_pct": g_pct,
        "gap_2_3_status": status,
        "positive_gap_2_3": g_abs is not None and g_abs > 0,
    }


def start_payload(ctx: dict, cross: dict, bar: dict) -> dict:
    from analyze_xrp_bull_regime_exit_shadow_v1 import episode_start_payload

    base = episode_start_payload(ctx, cross, bar)
    return {**base, **gap_2_3_metrics(ctx), "cluster_3_pool_count": ctx.get("cluster_3_pool_count")}


def run_e1r_state_machine(
    bars15,
    candles15,
    cfg,
    cache,
    report_from,
    report_to,
    require_positive_gap: bool,
    variant_name: str,
) -> dict:
    from analyze_xrp_bull_regime_exit_shadow_v1 import close_episode
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import detect_crosses, ladder_at_index
    from analyze_crosscoin_e1_onebar_reclaim_shadow_v1 import structure_payload
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index

    cross_by_idx = {
        c["bar_index"]: c for c in detect_crosses(bars15, report_to) if c["cross_direction"] == "bullish"
    }
    machine: dict[datetime, str] = {}
    episodes: list[dict] = []
    starts: list[dict] = []
    state = "INACTIVE"
    ep: dict | None = None
    pending = None

    for i, bar in enumerate(bars15):
        ct = bar["close_time"]
        if ct < report_from or ct > report_to:
            continue
        ctx = ladder_at_index(candles15, bars15, i, cfg, cache)
        pools, _, _ = scanner_pools_for_index(candles15, "15m", i, cfg, cache=cache)
        rising_3 = ctx["lower_cluster_rising_3"]
        close = float(bar["close"])
        ema = bar.get("ema200")
        above = close > ema if ema else False
        gm = gap_2_3_metrics(ctx)
        bull_cross = i in cross_by_idx and rising_3
        can_start = bull_cross and (not require_positive_gap or gm["positive_gap_2_3"])

        if bull_cross:
            starts.append(
                {
                    "time": ct.isoformat(),
                    "variant": variant_name,
                    "rising_3": rising_3,
                    **gm,
                    "E1R_would_start": rising_3,
                    "E1R_GAP3_would_start": rising_3 and gm["positive_gap_2_3"],
                }
            )

        anchor = ep or {}
        if state == "INACTIVE":
            if can_start:
                state = "ACTIVE"
                ep = start_payload(ctx, cross_by_idx[i], bar)
                ep["variant"] = variant_name
        elif state == "ACTIVE":
            if not rising_3:
                state = "INACTIVE"
                if ep:
                    close_episode(ep, ct, "rising_3_false", ctx, bar)
                    episodes.append(ep)
                    ep = None
            elif above:
                pass
            else:
                sp = structure_payload(ctx, pools, ct, close, anchor)
                if sp["rising_3"] and sp["structural_gap_intact"]:
                    state = "PENDING"
                    pending = {"dip_time": ct.isoformat()}
                else:
                    state = "INACTIVE"
                    if ep:
                        close_episode(ep, ct, "ema_dip_structure_broken", ctx, bar)
                        episodes.append(ep)
                        ep = None
        elif state == "PENDING" and pending:
            sp_next = structure_payload(ctx, pools, ct, close, anchor)
            success = above and sp_next["rising_3"] and sp_next["structural_gap_intact"]
            if success:
                state = "ACTIVE"
            else:
                state = "INACTIVE"
                if ep:
                    close_episode(ep, ct, "ema_reclaim_failed", ctx, bar)
                    episodes.append(ep)
                    ep = None
            pending = None
        machine[ct] = state

    if state in ("ACTIVE", "PENDING") and ep:
        ep["end"] = None
        ep["end_reason"] = "still_active_at_report_end"
        episodes.append(ep)

    return {"machine": machine, "episodes": episodes, "start_events": starts}


def signal_e1r_ignore(decision, entry_price, machine, bars15, candles15, cfg, cache) -> dict:
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import ladder_at_index, last_closed_idx

    idx = last_closed_idx(bars15, decision)
    if idx is None:
        return {"ignore": False, "status": "NO_BAR"}
    bar = bars15[idx]
    ct = bar["close_time"]
    ctx = ladder_at_index(candles15, bars15, idx, cfg, cache, price=entry_price)
    st = machine.get(ct, "INACTIVE")
    if st == "PENDING":
        return {"ignore": False, "status": "PENDING_NOT_RESOLVED", "active": False}
    if st == "ACTIVE":
        ok = ctx["price_above_ema200"] and ctx["lower_cluster_rising_3"]
        return {"ignore": ok, "status": "ACTIVE" if ok else "ACTIVE_NO_CONT", "active": True}
    return {"ignore": False, "status": "INACTIVE", "active": False}


def find_episode_for_time(episodes: list[dict], moment: datetime) -> dict | None:
    for ep in episodes:
        st = _utc(datetime.fromisoformat(ep["start_time"]))
        en = _utc(datetime.fromisoformat(ep["end"])) if ep.get("end") else datetime.max.replace(tzinfo=timezone.utc)
        if st <= moment <= en:
            return ep
    return None


def outcome_class(trade, bars15, by_open):
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import outcome_class as oc

    return oc(trade, bars15, by_open)


def cohort(wg, key, bad4):
    trades = [t for t in wg if not (key and t.get(key))]
    ign = [t for t in wg if key and t.get(key)]
    oc = [t["final_outcome"] for t in trades]
    maes = [float(t["mae_pct"]) for t in trades if t.get("mae_pct") is not None]
    mfes = [float(t["mfe_pct"]) for t in trades if t.get("mfe_pct") is not None]
    streak = best = 0
    for t in trades:
        if t.get("first_hit") == "SL":
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    removed_loss = [t for t in ign if t["final_outcome"] in ("SL_FIRST", "MOVE_041_THEN_SL")]
    return {
        "allowed_trades": len(trades),
        "ignored_trades": len(ign),
        "sl": sum(1 for t in trades if t.get("first_hit") == "SL"),
        "tp": sum(1 for t in trades if t.get("first_hit") == "TP"),
        "TP_FIRST": sum(1 for t in trades if t["final_outcome"] == "TP_FIRST"),
        "tp_first_lost": sum(1 for t in ign if t["final_outcome"] == "TP_FIRST"),
        "MOVE_041_THEN_SL": sum(1 for o in oc if o == "MOVE_041_THEN_SL"),
        "DIRECT_SL": sum(1 for o in oc if o == "SL_FIRST"),
        "mfe_ge_041_before_sl": sum(1 for o in oc if o == "MOVE_041_THEN_SL"),
        "median_mae": _median(maes),
        "median_mfe": _median(mfes),
        "max_sl_streak": best,
        "removed_losses": len(removed_loss),
        "bad4_removed": sum(1 for t in ign if t.get("entry_open") in bad4),
    }


def episode_stats(eps):
    d = [e["duration_hours"] for e in eps if e.get("duration_hours")]
    return {
        "count": len(eps),
        "median_hours": _median(d),
        "p90_hours": _percentile(d, 90),
        "max_hours": max(d) if d else None,
        "with_ignored_trades": sum(1 for e in eps if e.get("ignored_count", 0) > 0),
    }


def analyze_coin(symbol: str, sym_data: dict, bad4: set) -> dict:
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, ui_lld_config
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import PANE_FROM, PANE_TO, REPORT_FROM, REPORT_TO

    wg_opens = {t["entry_open"] for t in sym_data["with_guard"]}
    load_market(symbol)
    _p, candles15 = load_pane_candles(
        symbol, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    bars15 = build_15m_bars(candles15)
    by_open = {b["open_time"]: b for b in bars15}
    cfg = ui_lld_config("15m")
    cache: dict = {}

    sim_e1r = run_e1r_state_machine(bars15, candles15, cfg, cache, REPORT_FROM, REPORT_TO, False, "E1R")
    sim_gap = run_e1r_state_machine(bars15, candles15, cfg, cache, REPORT_FROM, REPORT_TO, True, "E1R_GAP3")

    signals = []
    for t in sym_data["without_guard"]:
        ot = _utc(datetime.fromisoformat(t["entry_open"]))
        if ot not in by_open:
            continue
        dec = by_open[ot]["close_time"]
        if dec < REPORT_FROM or dec > REPORT_TO:
            continue
        oc = outcome_class(t, bars15, by_open)
        e1r = signal_e1r_ignore(dec, float(t["entry_price"]), sim_e1r["machine"], bars15, candles15, cfg, cache)
        gap = signal_e1r_ignore(dec, float(t["entry_price"]), sim_gap["machine"], bars15, candles15, cfg, cache)
        ep_e1r = find_episode_for_time(sim_e1r["episodes"], dec)
        ep_gap = find_episode_for_time(sim_gap["episodes"], dec)
        signals.append(
            {
                **t,
                "symbol": symbol,
                "signal_time": ot.isoformat(),
                "decision_time": dec.isoformat(),
                "decision_dt": dec,
                "final_outcome": oc,
                "current_guard_allows": t["entry_open"] in wg_opens,
                "ignored_E1R": e1r["ignore"] and t["entry_open"] in wg_opens,
                "ignored_E1R_GAP3": gap["ignore"] and t["entry_open"] in wg_opens,
                "E1R_active": e1r.get("active"),
                "E1R_GAP3_active": gap.get("active"),
                "regime_start_E1R": ep_e1r.get("start_time") if ep_e1r else None,
                "regime_start_GAP3": ep_gap.get("start_time") if ep_gap else None,
                "start_gap_2_3_E1R": ep_e1r.get("gap_2_3_abs") if ep_e1r else None,
                "start_gap_2_3_GAP3": ep_gap.get("gap_2_3_abs") if ep_gap else None,
            }
        )

    wg = [s for s in signals if s["current_guard_allows"]]
    for ep_list, key, sim in (
        (sim_e1r["episodes"], "ignored_E1R", sim_e1r),
        (sim_gap["episodes"], "ignored_E1R_GAP3", sim_gap),
    ):
        for ep in ep_list:
            st = _utc(datetime.fromisoformat(ep["start_time"]))
            en = _utc(datetime.fromisoformat(ep["end"])) if ep.get("end") else datetime.max.replace(tzinfo=timezone.utc)
            ep["ignored_count"] = sum(
                1 for s in wg if st <= s["decision_dt"] <= en and s.get(key)
            )

    comparison = {
        "CURRENT": cohort(wg, None, bad4),
        "E1R": cohort(wg, "ignored_E1R", bad4),
        "E1R_GAP3": cohort(wg, "ignored_E1R_GAP3", bad4),
    }

    lost_tp = [s for s in wg if s["ignored_E1R"] and s["final_outcome"] == "TP_FIRST"]
    for s in lost_tp:
        st = find_episode_for_time(sim_e1r["episodes"], s["decision_dt"]) or {}
        s["start_geometry"] = {k: st.get(k) for k in (
            "cluster_1_bottom", "cluster_1_top", "cluster_2_bottom", "cluster_2_top",
            "cluster_3_bottom", "cluster_3_top", "gap_1_2", "gap_2_3_abs", "gap_2_3_pct", "gap_2_3_status",
            "positive_gap_2_3", "cluster_3_pool_count",
        )}

    removed = [s for s in wg if s["ignored_E1R"] and s["final_outcome"] in ("SL_FIRST", "MOVE_041_THEN_SL")]
    for s in removed:
        st = find_episode_for_time(sim_e1r["episodes"], s["decision_dt"]) or {}
        s["start_geometry"] = {k: st.get(k) for k in ("gap_2_3_abs", "gap_2_3_status", "positive_gap_2_3")}

    def gap_counts(rows):
        n = len(rows)
        pos = sum(1 for r in rows if (r.get("start_geometry") or {}).get("positive_gap_2_3"))
        return {"n": n, "positive_gap_2_3": pos, "pct": round(100.0 * pos / n, 2) if n else None}

    recovered = [s for s in wg if s["ignored_E1R"] and not s["ignored_E1R_GAP3"]]
    reallowed = [s for s in wg if not s["ignored_E1R"] and s["ignored_E1R_GAP3"]]

    return {
        "comparison": comparison,
        "lost_tp_first_e1r": lost_tp,
        "removed_losses_e1r": removed,
        "gap_geometry": {
            "removed_loss": gap_counts(removed),
            "lost_tp_first": gap_counts(lost_tp),
        },
        "recovered_by_gap3": recovered,
        "extra_ignored_gap3": reallowed,
        "e1r_episode_stats": episode_stats(sim_e1r["episodes"]),
        "gap3_episode_stats": episode_stats(sim_gap["episodes"]),
        "start_events": sim_e1r["start_events"],
        "wg": wg,
    }


def main() -> None:
    _ensure_paths()
    data = json.loads(SIGNAL_JSON.read_text())
    report = {
        "E1R_GAP3_definition": (
            "Identisch zu E1R nach Start. Start nur wenn bull cross + rising_3 + gap_2_3>0 "
            "(cluster_3.bottom > cluster_2.top). Keine permanente Gap-Pflicht nach Start."
        ),
        "gap_note": "Gap nur Regime-Start-Bestätigung; spätere Cluster-Verfeinerung erlaubt.",
        "coins": {},
    }

    for sym in COINS:
        bad4 = BAD4_XRP if sym == "XRPUSDT" else set()
        report["coins"][sym] = analyze_coin(sym, data["symbols"][sym], bad4)

    # XRP 11 June cross
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import PANE_FROM, PANE_TO, REPORT_FROM, REPORT_TO
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, ui_lld_config
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import ladder_at_index, last_closed_idx

    load_market("XRPUSDT")
    _p, c15 = load_pane_candles("XRPUSDT", "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS)
    b15 = build_15m_bars(c15)
    cfg = ui_lld_config("15m")
    cache = {}
    idx = last_closed_idx(b15, _utc(datetime.fromisoformat(ANCHOR_CROSS)))
    ctx = ladder_at_index(c15, b15, idx, cfg, cache)
    gm = gap_2_3_metrics(ctx)
    report["xrp_11_june_cross"] = {**gm, "rising_3": ctx["lower_cluster_rising_3"]}

    bad4_tbl = []
    wg = report["coins"]["XRPUSDT"]["wg"]
    for eo in BAD4_XRP:
        s = next(x for x in wg if x["entry_open"] == eo)
        bad4_tbl.append(
            {
                "entry_open": eo,
                "ignored_E1R": s["ignored_E1R"],
                "ignored_E1R_GAP3": s["ignored_E1R_GAP3"],
                "mae_pct": s.get("mae_pct"),
                "mfe_pct": s.get("mfe_pct"),
                "outcome": s["final_outcome"],
            }
        )
    report["bad4_impact"] = bad4_tbl

    all_lost = []
    for sym in COINS:
        all_lost.extend(report["coins"][sym]["lost_tp_first_e1r"])

    recovered_tp = sum(
        1
        for sym in COINS
        for s in report["coins"][sym]["recovered_by_gap3"]
        if s["final_outcome"] == "TP_FIRST"
    )
    reallowed_sl = sum(
        1
        for sym in COINS
        for s in report["coins"][sym]["recovered_by_gap3"]
        if s["final_outcome"] in ("SL_FIRST", "MOVE_041_THEN_SL")
    )
    report["verdict"] = {
        "q1_bad_positive_gap": f"11.06. gap_2_3={report['xrp_11_june_cross']['gap_2_3_abs']} positive; BAD-4 alle weiterhin ignoriert.",
        "q2_lost_tp_gap_rate": "100% der 7 LOST_TP_FIRST und 100% REMOVED_LOSS hatten positive_gap_2_3 am Regime-Start (kein Trennsignal).",
        "q3_recovered_tp": recovered_tp,
        "q4_sl_reallowed": reallowed_sl,
        "q5_cross_coin": "E1R und E1R_GAP3 identisch auf Trade-Ebene (Jun–Jul): jeder E1R-Start hatte bereits gap_2_3>0.",
    }

    md = build_md(report, all_lost)
    report["executive_summary"] = md
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    OUT_MD.write_text(md + "\n")

    fields = [
        "symbol", "signal_time", "entry_open", "ignored_E1R", "ignored_E1R_GAP3",
        "final_outcome", "mae_pct", "mfe_pct", "start_gap_2_3_E1R",
    ]
    rows = []
    for sym in COINS:
        rows.extend(report["coins"][sym]["wg"])
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    for sym in COINS:
        p = ROOT / f"analyze_crosscoin_e1r_gap3_start_shadow_{sym}_v1.csv"
        with p.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in report["coins"][sym]["wg"]:
                w.writerow(r)

    print("wrote", OUT_JSON, OUT_MD, OUT_CSV)


def build_md(r: dict, all_lost: list) -> str:
    lines = [
        "# E1R vs E1R_GAP3 Start Shadow v1",
        "",
        "### E1R_GAP3 definition",
        r["E1R_GAP3_definition"],
        "",
        "### XRP 11 June",
        json.dumps(r["xrp_11_june_cross"], indent=2),
        "",
        "### XRP 14 June",
        json.dumps([x for x in r["bad4_impact"] if "2026-06-14" in x["entry_open"]], indent=2),
        "",
        "### Lost TP_FIRST geometry",
        json.dumps(all_lost, indent=2, default=str)[:12000],
        "",
        "### Removed loss geometry",
        json.dumps({s: r["coins"][s]["gap_geometry"] for s in COINS}, indent=2),
    ]
    for sym in COINS:
        lines += [f"### {sym.split('USDT')[0]} current vs E1R vs GAP3", tbl(r["coins"][sym]["comparison"]), ""]
    lines += [
        "### Trades recovered by GAP3",
        json.dumps({s: len(r["coins"][s]["recovered_by_gap3"]) for s in COINS}),
        "",
        "### BAD-4 impact",
        json.dumps(r["bad4_impact"], indent=2),
        "",
        "### Episode impact",
        json.dumps({s: {"E1R": r["coins"][s]["e1r_episode_stats"], "GAP3": r["coins"][s]["gap3_episode_stats"]} for s in COINS}, indent=2),
        "",
        "### Verdict",
    ]
    for k, v in r["verdict"].items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def tbl(c: dict) -> str:
    lines = ["| Metrik | CURRENT | E1R | E1R_GAP3 |", "|---|--:|--:|--:|"]
    for k in (
        "allowed_trades", "ignored_trades", "sl", "tp", "tp_first_lost", "MOVE_041_THEN_SL",
        "DIRECT_SL", "mfe_ge_041_before_sl", "median_mae", "median_mfe", "max_sl_streak", "removed_losses",
    ):
        lines.append(f"| {k} | {c['CURRENT'].get(k)} | {c['E1R'].get(k)} | {c['E1R_GAP3'].get(k)} |")
    return "\n".join(lines)


if __name__ == "__main__":
    main()

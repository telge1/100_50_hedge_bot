"""Shadow: bull regime exit variants E1/E2/E3 (analysis only)."""

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
OUT_JSON = ROOT / "analyze_xrp_bull_regime_exit_shadow_v1.json"
OUT_MD = ROOT / "analyze_xrp_bull_regime_exit_shadow_v1.md"
OUT_CSV = ROOT / "analyze_xrp_bull_regime_exit_shadow_trades_v1.csv"
SIGNAL_JSON = ROOT / "floor_guard_signal_list_v1.json"
ANCHOR_BREAK = "2026-06-11T17:45:00+00:00"
JUN_REF_END = datetime(2026, 6, 17, 23, 59, 59, tzinfo=timezone.utc)
BAD4 = {
    "2026-06-11T19:15:00+00:00",
    "2026-06-14T21:45:00+00:00",
    "2026-06-14T22:30:00+00:00",
    "2026-06-14T23:30:00+00:00",
}
SYMBOL = "XRPUSDT"
MFE_THRESH = 0.41


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
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return round(s[f] + (s[c] - s[f]) * (k - f), 4) if f != c else round(s[f], 4)


def pool_broken(pool: dict, moment: datetime) -> bool:
    ba = pool.get("break_at")
    return ba is not None and ba <= moment


def structural_support_failure(
    ctx: dict,
    pools: list[dict],
    moment: datetime,
    close: float,
    anchor: dict,
) -> tuple[bool, str]:
    """E3 exit: causal pool break / close below higher support stages."""
    c2b, c3b = ctx.get("cluster_2_bottom"), ctx.get("cluster_3_bottom")
    if c3b is not None and close < float(c3b):
        return True, "close_below_cluster_3_bottom"
    if c2b is not None and close < float(c2b):
        return True, "close_below_cluster_2_bottom"

    pool_by_id = {p["pool_id"]: p for p in pools}
    for label in ("cluster_2_pool_ids", "cluster_3_pool_ids"):
        for pid in anchor.get(label) or []:
            p = pool_by_id.get(pid)
            if p is None:
                continue
            if pool_broken(p, moment):
                return True, f"pool_broken:{pid}"
            if p.get("break_at") and p["break_at"] <= moment:
                return True, f"pool_broken:{pid}"

    if ctx.get("lower_cluster_rising_3"):
        return False, "rising_3_intact"

    t1, t2, t3 = ctx.get("cluster_1_top"), ctx.get("cluster_2_top"), ctx.get("cluster_3_top")
    if t1 and t2 and t3 and float(t1) < float(t2) < float(t3):
        return False, "rising_3_false_micro_reorder_tops_still_ascending"

    return True, "rising_3_false_structure_not_reconstructable"


def episode_start_payload(ctx: dict, cross: dict, bar: dict) -> dict:
    return {
        "start_time": bar["close_time"].isoformat(),
        "close": bar["close"],
        "ema200": bar.get("ema200"),
        "cluster_1_top": ctx.get("cluster_1_top"),
        "cluster_2_top": ctx.get("cluster_2_top"),
        "cluster_3_top": ctx.get("cluster_3_top"),
        "gap_1_to_2": ctx.get("gap_1_to_2"),
        "gap_2_to_3": ctx.get("gap_2_to_3"),
        "rising_3": ctx.get("lower_cluster_rising_3"),
        "cluster_2_pool_ids": ctx.get("cluster_2_pool_ids") or [],
        "cluster_3_pool_ids": ctx.get("cluster_3_pool_ids") or [],
        "start_cross": cross,
    }


def close_episode(ep: dict, ct: datetime, reason: str, ctx: dict, bar: dict) -> None:
    ep["end"] = ct.isoformat()
    ep["end_reason"] = reason
    ep["duration_hours"] = round((_utc(ct) - _utc(datetime.fromisoformat(ep["start_time"]))).total_seconds() / 3600.0, 2)
    ep["end_close"] = bar["close"]
    ep["end_ema200"] = bar.get("ema200")
    ep["end_rising_3"] = ctx.get("lower_cluster_rising_3")


def simulate_variants(
    bars15: list[dict],
    candles15,
    cfg,
    cache: dict,
    report_from: datetime,
    report_to: datetime,
) -> dict[str, Any]:
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import detect_crosses, ladder_at_index
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index

    cross_by_idx = {
        c["bar_index"]: c for c in detect_crosses(bars15, report_to) if c["cross_direction"] == "bullish"
    }

    state = {v: False for v in ("E1", "E2", "E3")}
    ep_cur: dict[str, dict | None] = {v: None for v in ("E1", "E2", "E3")}
    episodes: dict[str, list[dict]] = {v: [] for v in ("E1", "E2", "E3")}
    active_at: dict[str, dict[datetime, bool]] = {v: {} for v in ("E1", "E2", "E3")}
    retest_log: dict[str, list] = {v: [] for v in ("E1", "E2", "E3")}

    for i, bar in enumerate(bars15):
        ct = bar["close_time"]
        if ct < report_from or ct > report_to:
            continue
        ctx = ladder_at_index(candles15, bars15, i, cfg, cache)
        pools, _, _ = scanner_pools_for_index(candles15, "15m", i, cfg, cache=cache)
        rising_3 = ctx["lower_cluster_rising_3"]
        above = ctx["price_above_ema200"] is True
        close = float(bar["close"])
        ema = bar.get("ema200")
        is_start = i in cross_by_idx and rising_3

        for v in ("E1", "E2", "E3"):
            if not state[v]:
                if is_start:
                    state[v] = True
                    ep_cur[v] = episode_start_payload(ctx, cross_by_idx[i], bar)
                    ep_cur[v]["variant"] = v
                    ep_cur[v]["ema_retest"] = {
                        "first_touch_ema_from_above": None,
                        "first_close_below_ema": None,
                        "first_reclaim_after_below": None,
                    }
            else:
                ep = ep_cur[v]
                anchor = ep or {}
                # EMA retest doc while active
                rt = ep["ema_retest"]
                if ep.get("start_time") and above and bar["low"] <= ema and rt["first_touch_ema_from_above"] is None:
                    if _utc(ct) > _utc(datetime.fromisoformat(ep["start_time"])):
                        rt["first_touch_ema_from_above"] = ct.isoformat()
                if not above and rt["first_close_below_ema"] is None:
                    rt["first_close_below_ema"] = ct.isoformat()
                if rt["first_close_below_ema"] and above and rt["first_reclaim_after_below"] is None:
                    if ct > _utc(datetime.fromisoformat(rt["first_close_below_ema"])):
                        rt["first_reclaim_after_below"] = ct.isoformat()

                end_reason = None
                if v == "E1" and (not above or not rising_3):
                    end_reason = "price_below_or_equal_ema200" if not above else "rising_3_false"
                elif v == "E2" and not rising_3:
                    end_reason = "rising_3_false"
                elif v == "E3":
                    fail, why = structural_support_failure(ctx, pools, ct, close, anchor)
                    if fail:
                        end_reason = why

                if end_reason:
                    state[v] = False
                    if ep:
                        close_episode(ep, ct, end_reason, ctx, bar)
                        episodes[v].append(ep)
                        ep_cur[v] = None

            active_at[v][ct] = state[v]

    for v in ("E1", "E2", "E3"):
        if state[v] and ep_cur[v]:
            ep = ep_cur[v]
            ep["end"] = None
            ep["end_reason"] = "still_active_at_report_end"
            ep["duration_hours"] = round((report_to - _utc(datetime.fromisoformat(ep["start_time"]))).total_seconds() / 3600.0, 2)
            episodes[v].append(ep)

    return {"episodes": episodes, "active_at": active_at}


def variant_active_at_signal(
    decision: datetime,
    bars15: list[dict],
    active_at: dict[datetime, bool],
    candles15,
    cfg,
    cache: dict,
    entry_price: float,
    variant: str,
    anchor: dict | None = None,
) -> bool:
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import ladder_at_index, last_closed_idx
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index

    idx = last_closed_idx(bars15, decision)
    if idx is None:
        return False
    bar = bars15[idx]
    flag = active_at.get(bar["close_time"], False)
    if not flag:
        return False
    ctx = ladder_at_index(candles15, bars15, idx, cfg, cache, price=entry_price)
    rising_3 = ctx["lower_cluster_rising_3"]
    above = ctx["price_above_ema200"] is True
    if variant == "E1":
        return above and rising_3
    if variant == "E2":
        return rising_3
    if variant == "E3":
        return flag
    return False


def outcome_class(trade: dict, bars15, by_open) -> str:
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import outcome_class as oc

    return oc(trade, bars15, by_open)


def cohort(wg: list[dict], key: str | None) -> dict:
    trades = [t for t in wg if not (key and t.get(key))]
    ign = sum(1 for t in wg if key and t.get(key))
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
    return {
        "allowed_trades": len(trades),
        "ignored_trades": ign,
        "sl": sum(1 for t in trades if t.get("first_hit") == "SL"),
        "tp": sum(1 for t in trades if t.get("first_hit") == "TP"),
        "MOVE_041_THEN_SL": sum(1 for o in oc if o == "MOVE_041_THEN_SL"),
        "DIRECT_SL": sum(1 for o in oc if o == "SL_FIRST"),
        "mfe_ge_041_before_sl": sum(1 for o in oc if o == "MOVE_041_THEN_SL"),
        "median_mae": _median(maes),
        "median_mfe": _median(mfes),
        "max_sl_streak": best,
        "bad4_removed": sum(1 for t in wg if t.get("known_bad_4") and key and t.get(key)),
        "tp_first_lost": sum(1 for t in wg if key and t.get(key) and t.get("final_outcome") == "TP_FIRST"),
    }


def episode_stats(eps: list[dict]) -> dict:
    durs = [e["duration_hours"] for e in eps if e.get("duration_hours")]
    return {
        "count": len(eps),
        "median_hours": _median(durs),
        "p75_hours": _percentile(durs, 75),
        "p90_hours": _percentile(durs, 90),
        "max_hours": max(durs) if durs else None,
    }


def enrich_episodes(eps: list[dict], wg: list[dict], ignore_key: str) -> None:
    for ep in eps:
        st = _utc(datetime.fromisoformat(ep["start_time"]))
        en = _utc(datetime.fromisoformat(ep["end"])) if ep.get("end") else datetime.max.replace(tzinfo=timezone.utc)
        ign = [
            s
            for s in wg
            if st <= s["decision_dt"] <= en and s.get(ignore_key)
        ]
        ep["ignored_shorts"] = len(ign)
        ep["ignored_sl"] = sum(1 for s in ign if s.get("first_hit") == "SL")
        ep["ignored_tp"] = sum(1 for s in ign if s.get("final_outcome") == "TP_FIRST")


def june_episode_summary(eps: list[dict], label: str) -> dict | None:
    anchor = _utc(datetime.fromisoformat(ANCHOR_BREAK))
    for ep in eps:
        st = _utc(datetime.fromisoformat(ep["start_time"]))
        if abs((st - anchor).total_seconds()) < 60:
            return {
                "variant": label,
                "start": ep["start_time"],
                "end": ep.get("end"),
                "duration_hours": ep.get("duration_hours"),
                "end_reason": ep.get("end_reason"),
            }
    for ep in eps:
        st = _utc(datetime.fromisoformat(ep["start_time"]))
        en = _utc(datetime.fromisoformat(ep["end"])) if ep.get("end") else JUN_REF_END
        if st <= anchor <= en:
            return {
                "variant": label,
                "start": ep["start_time"],
                "end": ep.get("end"),
                "duration_hours": ep.get("duration_hours"),
                "end_reason": ep.get("end_reason"),
                "note": "anchor_inside_episode",
            }
    return None


def main() -> None:
    _ensure_paths()
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, ui_lld_config
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import PANE_FROM, PANE_TO, REPORT_FROM, REPORT_TO

    data = json.loads(SIGNAL_JSON.read_text())
    sym = data["symbols"]["XRPUSDT"]
    wg_opens = {t["entry_open"] for t in sym["with_guard"]}

    load_market(SYMBOL)
    _p, candles15 = load_pane_candles(
        SYMBOL, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    bars15 = build_15m_bars(candles15)
    by_open = {b["open_time"]: b for b in bars15}
    cfg = ui_lld_config("15m")
    cache: dict = {}

    sim = simulate_variants(bars15, candles15, cfg, cache, REPORT_FROM, REPORT_TO)

    signals = []
    for t in sym["without_guard"]:
        ot = _utc(datetime.fromisoformat(t["entry_open"]))
        if ot not in by_open:
            continue
        dec = by_open[ot]["close_time"]
        if dec < REPORT_FROM or dec > REPORT_TO:
            continue
        oc = outcome_class(t, bars15, by_open)
        rec = {
            **t,
            "signal_time": ot.isoformat(),
            "decision_time": dec.isoformat(),
            "decision_dt": dec,
            "known_bad_4": t["entry_open"] in BAD4,
            "current_guard_allows": t["entry_open"] in wg_opens,
            "final_outcome": oc,
            "entry": t["entry_price"],
        }
        for v, key in [("E1", "ignored_E1"), ("E2", "ignored_E2"), ("E3", "ignored_E3")]:
            act = variant_active_at_signal(
                dec, bars15, sim["active_at"][v], candles15, cfg, cache, float(t["entry_price"]), v
            )
            rec[f"{v}_active"] = act
            rec[key] = act and rec["current_guard_allows"]
        signals.append(rec)

    wg = [s for s in signals if s["current_guard_allows"]]
    for v, ik in [("E1", "ignored_E1"), ("E2", "ignored_E2"), ("E3", "ignored_E3")]:
        enrich_episodes(sim["episodes"][v], wg, ik)

    comparison = {
        "CURRENT": cohort(wg, None),
        "E1": cohort(wg, "ignored_E1"),
        "E2": cohort(wg, "ignored_E2"),
        "E3": cohort(wg, "ignored_E3"),
    }

    bad4 = []
    for eo in BAD4:
        s = next(x for x in wg if x["entry_open"] == eo)
        bad4.append(
            {
                "entry_open": eo,
                "E1_active": s["E1_active"],
                "E2_active": s["E2_active"],
                "E3_active": s["E3_active"],
                "ignored_E1": s["ignored_E1"],
                "ignored_E2": s["ignored_E2"],
                "ignored_E3": s["ignored_E3"],
                "mae_pct": s.get("mae_pct"),
                "mfe_pct": s.get("mfe_pct"),
                "final_outcome": s["final_outcome"],
            }
        )

    def ep_at_start(eps: list[dict], start: str) -> dict | None:
        return next((e for e in eps if e.get("start_time") == start), None)

    june_cmp = {
        "E1_at_1745_break": ep_at_start(sim["episodes"]["E1"], ANCHOR_BREAK),
        "E2_episode_containing_1745": june_episode_summary(sim["episodes"]["E2"], "E2"),
        "E3_episode_containing_1745": june_episode_summary(sim["episodes"]["E3"], "E3"),
        "note": "E2/E3 können vor 17:45 starten, wenn früherer Bull-Cross+rising_3 nicht per E2/E3-Reset endete.",
    }

    ep_duration = {v: episode_stats(sim["episodes"][v]) for v in ("E1", "E2", "E3")}
    top10 = {
        v: sorted(sim["episodes"][v], key=lambda e: e.get("duration_hours") or 0, reverse=True)[:10]
        for v in ("E1", "E2", "E3")
    }

    verdict = {
        "q1_e1_too_sensitive": "Ja — E1 endet 12.06. 06:45 bei erstem Close≤EMA trotz rising_3 und schnellem Reclaim.",
        "q2_e2_rising3_anchor": f"E2 median episode {ep_duration['E2']['median_hours']}h max {ep_duration['E2']['max_hours']}h; hält über 12.06.-Dip.",
        "q3_e3_vs_e2": (
            "E3 entfernt "
            f"{comparison['E3']['ignored_trades'] - comparison['E2']['ignored_trades']} andere Ignores vs E2; "
            f"BAD-4 E2={comparison['E2']['bad4_removed']} E3={comparison['E3']['bad4_removed']}."
        ),
        "q4_best_bad4_tp_tradeoff": (
            f"BAD-4: E1={comparison['E1']['bad4_removed']} E2={comparison['E2']['bad4_removed']} E3={comparison['E3']['bad4_removed']}; "
            f"TP_FIRST: E1={comparison['E1']['tp_first_lost']} E2={comparison['E2']['tp_first_lost']} E3={comparison['E3']['tp_first_lost']}."
        ),
        "q5_ada_doge_oos": "E2/E3 nur nach Review langer Episoden (stickiness) — siehe episode_stats.",
    }

    report = {
        "meta": {"analysis_only": True, "symbol": SYMBOL},
        "regime_start_definition": "Bull 15m EMA200 cross + rising_3 structural lowers at cross bar close.",
        "E1": "Reset: close<=EMA200 OR rising_3 false (legacy persistent).",
        "E2": "Reset: rising_3 false only; EMA dips ignored.",
        "E3": "Reset: structural_support_failure (pool break / close below c2|c3 bottom / non-reconstructable tops).",
        "E2_vs_E3": "E2 ends on rising_3 false; E3 may continue if tops still ascending or only micro-reorder.",
        "june_11_17_comparison": june_cmp,
        "bad4": bad4,
        "comparison": comparison,
        "episode_duration": ep_duration,
        "top10_long_episodes": top10,
        "all_episodes": sim["episodes"],
        "verdict": verdict,
        "signals": signals,
    }

    md = build_md(report)
    report["executive_summary"] = md
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    OUT_MD.write_text(md + "\n")

    fields = [
        "signal_time",
        "decision_time",
        "entry_open",
        "entry",
        "E1_active",
        "E2_active",
        "E3_active",
        "ignored_E1",
        "ignored_E2",
        "ignored_E3",
        "final_outcome",
        "mae_pct",
        "mfe_pct",
        "known_bad_4",
    ]
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for s in wg:
            w.writerow(s)

    print("wrote", OUT_JSON, OUT_MD, OUT_CSV)


def build_md(r: dict) -> str:
    c = r["comparison"]
    lines = [
        "# XRP Bull Regime Exit Shadow v1",
        "",
        "### Regime start definition",
        r["regime_start_definition"],
        "",
        "### E1 old EMA reset",
        r["E1"],
        "",
        "### E2 rising3-only reset",
        r["E2"],
        "",
        "### E3 structural support failure reset",
        r["E3"],
        "",
        "### 11–17 June comparison",
        json.dumps(r["june_11_17_comparison"], indent=2),
        "",
        "### BAD-4 impact",
        json.dumps(r["bad4"], indent=2),
        "",
        "### Current vs E1 vs E2 vs E3",
        "| Metrik | CURRENT | E1 | E2 | E3 |",
        "|---|--:|--:|--:|--:|",
    ]
    for k in (
        "allowed_trades",
        "ignored_trades",
        "sl",
        "tp",
        "MOVE_041_THEN_SL",
        "DIRECT_SL",
        "mfe_ge_041_before_sl",
        "median_mae",
        "median_mfe",
        "max_sl_streak",
        "bad4_removed",
        "tp_first_lost",
    ):
        lines.append(f"| {k} | {c['CURRENT'][k]} | {c['E1'][k]} | {c['E2'][k]} | {c['E3'][k]} |")
    lines += [
        "",
        "### Lost TP_FIRST",
        f"E1={c['E1']['tp_first_lost']} E2={c['E2']['tp_first_lost']} E3={c['E3']['tp_first_lost']}",
        "",
        "### SL streak impact",
        f"max: CURRENT={c['CURRENT']['max_sl_streak']} E1={c['E1']['max_sl_streak']} "
        f"E2={c['E2']['max_sl_streak']} E3={c['E3']['max_sl_streak']}",
        "",
        "### MAE / MFE",
        f"CURRENT {c['CURRENT']['median_mae']}/{c['CURRENT']['median_mfe']}; "
        f"E1 {c['E1']['median_mae']}/{c['E1']['median_mfe']}; "
        f"E2 {c['E2']['median_mae']}/{c['E2']['median_mfe']}; "
        f"E3 {c['E3']['median_mae']}/{c['E3']['median_mfe']}.",
        "",
        "### Episode duration",
        json.dumps(r["episode_duration"], indent=2),
        "",
        "### State stickiness",
        "Siehe top10_long_episodes in JSON (ignored SL/TP, end_reason, ema_retest).",
        "",
        "### EMA200 retest observations",
        "Pro Episode ema_retest in all_episodes.",
        "",
        "### Verdict",
    ]
    for i, v in enumerate(r["verdict"].values(), 1):
        lines.append(f"{i}. {v}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()

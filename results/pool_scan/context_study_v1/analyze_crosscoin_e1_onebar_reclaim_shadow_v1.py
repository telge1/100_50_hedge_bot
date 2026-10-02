"""Shadow: E1 vs E1R (one-bar EMA reclaim), XRP/ADA/DOGE."""

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
OUT_JSON = ROOT / "analyze_crosscoin_e1_onebar_reclaim_shadow_v1.json"
OUT_MD = ROOT / "analyze_crosscoin_e1_onebar_reclaim_shadow_v1.md"
OUT_CSV = ROOT / "analyze_crosscoin_e1_onebar_reclaim_shadow_trades_v1.csv"
SIGNAL_JSON = ROOT / "floor_guard_signal_list_v1.json"
COINS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
BAD4_XRP = {
    "2026-06-11T19:15:00+00:00",
    "2026-06-14T21:45:00+00:00",
    "2026-06-14T22:30:00+00:00",
    "2026-06-14T23:30:00+00:00",
}
DIP_XRP = "2026-06-12T06:45:00+00:00"
RECLAIM_XRP = "2026-06-12T07:00:00+00:00"


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


def gap_label(gap: float | None) -> str:
    if gap is None:
        return "unknown"
    if gap < 0:
        return "overlap"
    if abs(gap) < 1e-9:
        return "touching"
    return "positive"


def structure_payload(ctx: dict, pools: list[dict], moment: datetime, close: float, anchor: dict) -> dict:
    from analyze_xrp_bull_regime_exit_shadow_v1 import structural_support_failure

    fail, why = structural_support_failure(ctx, pools, moment, close, anchor)
    return {
        "rising_3": ctx.get("lower_cluster_rising_3"),
        "cluster_1_top": ctx.get("cluster_1_top"),
        "cluster_2_top": ctx.get("cluster_2_top"),
        "cluster_3_top": ctx.get("cluster_3_top"),
        "gap_1_2": ctx.get("gap_1_to_2"),
        "gap_2_3": ctx.get("gap_2_to_3"),
        "gap_1_2_status": gap_label(ctx.get("gap_1_to_2")),
        "gap_2_3_status": gap_label(ctx.get("gap_2_to_3")),
        "overlap_1_2": ctx.get("overlap_1_2"),
        "overlap_2_3": ctx.get("overlap_2_3"),
        "structural_gap_intact": not fail,
        "structure_fail_reason": why if fail else None,
        "cluster_2_pool_ids": ctx.get("cluster_2_pool_ids"),
        "cluster_3_pool_ids": ctx.get("cluster_3_pool_ids"),
    }


def simulate_e1_e1r(
    bars15: list[dict],
    candles15,
    cfg,
    cache: dict,
    report_from: datetime,
    report_to: datetime,
) -> dict[str, Any]:
    from analyze_xrp_bull_regime_exit_shadow_v1 import close_episode, episode_start_payload
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import detect_crosses, ladder_at_index
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index

    cross_by_idx = {
        c["bar_index"]: c for c in detect_crosses(bars15, report_to) if c["cross_direction"] == "bullish"
    }

    e1_active: dict[datetime, bool] = {}
    e1r_machine: dict[datetime, str] = {}
    e1_episodes: list[dict] = []
    e1r_episodes: list[dict] = []
    reclaim_events: list[dict] = []

    e1_on = False
    e1_ep: dict | None = None
    e1r_state = "INACTIVE"  # INACTIVE | ACTIVE | PENDING
    e1r_ep: dict | None = None
    pending: dict | None = None

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
        is_start = i in cross_by_idx and rising_3

        # --- E1 ---
        if not e1_on:
            if is_start:
                e1_on = True
                e1_ep = episode_start_payload(ctx, cross_by_idx[i], bar)
                e1_ep["variant"] = "E1"
        else:
            if not above or not rising_3:
                e1_on = False
                if e1_ep:
                    close_episode(
                        e1_ep,
                        ct,
                        "price_below_or_equal_ema200" if not above else "rising_3_false",
                        ctx,
                        bar,
                    )
                    e1_episodes.append(e1_ep)
                    e1_ep = None
        e1_active[ct] = e1_on

        # --- E1R ---
        anchor = e1r_ep or {}
        if e1r_state == "INACTIVE":
            if is_start:
                e1r_state = "ACTIVE"
                e1r_ep = episode_start_payload(ctx, cross_by_idx[i], bar)
                e1r_ep["variant"] = "E1R"
        elif e1r_state == "ACTIVE":
            if not rising_3:
                e1r_state = "INACTIVE"
                if e1r_ep:
                    close_episode(e1r_ep, ct, "rising_3_false", ctx, bar)
                    e1r_episodes.append(e1r_ep)
                    e1r_ep = None
            elif above:
                pass
            else:
                sp = structure_payload(ctx, pools, ct, close, anchor)
                if sp["rising_3"] and sp["structural_gap_intact"]:
                    e1r_state = "PENDING"
                    pending = {
                        "dip_time": ct.isoformat(),
                        "dip_close": close,
                        "ema200": ema,
                        "distance_below_ema_pct": round((close - ema) / ema * 100.0, 4) if ema else None,
                        **sp,
                    }
                else:
                    e1r_state = "INACTIVE"
                    if e1r_ep:
                        close_episode(e1r_ep, ct, "ema_dip_structure_broken", ctx, bar)
                        e1r_episodes.append(e1r_ep)
                        e1r_ep = None
        elif e1r_state == "PENDING" and pending:
            sp_next = structure_payload(ctx, pools, ct, close, anchor)
            success = above and sp_next["rising_3"] and sp_next["structural_gap_intact"]
            ev = {
                **pending,
                "next_bar_time": ct.isoformat(),
                "next_close": close,
                "next_ema200": ema,
                "next_distance_close_to_ema_pct": round((close - ema) / ema * 100.0, 4) if ema else None,
                "rising_3_next_bar": sp_next["rising_3"],
                "structure_next_bar": sp_next,
                "result": "EMA_RECLAIM_SUCCESS" if success else "EMA_RECLAIM_FAILED",
            }
            reclaim_events.append(ev)
            if success:
                e1r_state = "ACTIVE"
                if e1r_ep:
                    e1r_ep.setdefault("reclaims", []).append(ev)
            else:
                e1r_state = "INACTIVE"
                if e1r_ep:
                    close_episode(e1r_ep, ct, "ema_reclaim_failed", ctx, bar)
                    e1r_episodes.append(e1r_ep)
                    e1r_ep = None
            pending = None

        e1r_machine[ct] = e1r_state

    if e1_on and e1_ep:
        e1_ep["end"] = None
        e1_ep["end_reason"] = "still_active_at_report_end"
        e1_episodes.append(e1_ep)
    if e1r_state in ("ACTIVE", "PENDING") and e1r_ep:
        e1r_ep["end"] = None
        e1r_ep["end_reason"] = "still_active_at_report_end"
        e1r_episodes.append(e1r_ep)

    return {
        "e1_active": e1_active,
        "e1r_machine": e1r_machine,
        "e1_episodes": e1_episodes,
        "e1r_episodes": e1r_episodes,
        "reclaim_events": reclaim_events,
    }


def signal_ignore(
    decision: datetime,
    entry_price: float,
    sim: dict,
    bars15,
    candles15,
    cfg,
    cache,
    variant: str,
) -> dict:
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import ladder_at_index, last_closed_idx

    idx = last_closed_idx(bars15, decision)
    if idx is None:
        return {"active": False, "ignore": False, "status": "NO_BAR"}
    bar = bars15[idx]
    ct = bar["close_time"]
    ctx = ladder_at_index(candles15, bars15, idx, cfg, cache, price=entry_price)
    above = ctx["price_above_ema200"] is True
    rising_3 = ctx["lower_cluster_rising_3"]

    if variant == "E1":
        in_ep = sim["e1_active"].get(ct, False)
        ok = in_ep and above and rising_3
        return {"active": in_ep, "ignore": ok, "status": "ACTIVE" if ok else "INACTIVE_OR_NO_CONTINUATION"}

    st = sim["e1r_machine"].get(ct, "INACTIVE")
    if st == "PENDING":
        return {"active": False, "ignore": False, "status": "PENDING_NOT_RESOLVED"}
    if st == "ACTIVE":
        ok = above and rising_3
        return {"active": True, "ignore": ok, "status": "ACTIVE" if ok else "ACTIVE_NO_CONTINUATION"}
    return {"active": False, "ignore": False, "status": "INACTIVE"}


def outcome_class(trade, bars15, by_open):
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import outcome_class as oc

    return oc(trade, bars15, by_open)


def cohort(wg: list[dict], key: str | None, bad4: set) -> dict:
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
    return {
        "allowed_trades": len(trades),
        "ignored_trades": len(ign),
        "sl": sum(1 for t in trades if t.get("first_hit") == "SL"),
        "tp": sum(1 for t in trades if t.get("first_hit") == "TP"),
        "TP_FIRST": sum(1 for t in trades if t["final_outcome"] == "TP_FIRST"),
        "MOVE_041_THEN_SL": sum(1 for o in oc if o == "MOVE_041_THEN_SL"),
        "DIRECT_SL": sum(1 for o in oc if o == "SL_FIRST"),
        "mfe_ge_041_before_sl": sum(1 for o in oc if o == "MOVE_041_THEN_SL"),
        "median_mae": _median(maes),
        "median_mfe": _median(mfes),
        "max_sl_streak": best,
        "bad4_removed": sum(1 for t in ign if t.get("entry_open") in bad4),
        "tp_first_lost": sum(1 for t in ign if t["final_outcome"] == "TP_FIRST"),
        "sl_removed": sum(1 for t in ign if t.get("first_hit") == "SL"),
    }


def episode_duration_stats(eps: list[dict]) -> dict:
    d = [e["duration_hours"] for e in eps if e.get("duration_hours")]
    return {
        "count": len(eps),
        "median_hours": _median(d),
        "p75_hours": _percentile(d, 75),
        "p90_hours": _percentile(d, 90),
        "max_hours": max(d) if d else None,
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

    sim = simulate_e1_e1r(bars15, candles15, cfg, cache, REPORT_FROM, REPORT_TO)

    signals = []
    for t in sym_data["without_guard"]:
        ot = _utc(datetime.fromisoformat(t["entry_open"]))
        if ot not in by_open:
            continue
        dec = by_open[ot]["close_time"]
        if dec < REPORT_FROM or dec > REPORT_TO:
            continue
        oc = outcome_class(t, bars15, by_open)
        e1s = signal_ignore(dec, float(t["entry_price"]), sim, bars15, candles15, cfg, cache, "E1")
        e1rs = signal_ignore(dec, float(t["entry_price"]), sim, bars15, candles15, cfg, cache, "E1R")
        rec = {
            **t,
            "symbol": symbol,
            "signal_time": ot.isoformat(),
            "decision_time": dec.isoformat(),
            "decision_dt": dec,
            "final_outcome": oc,
            "current_guard_allows": t["entry_open"] in wg_opens,
            "E1_active": e1s["active"],
            "E1R_active": e1rs["active"],
            "E1_status": e1s["status"],
            "E1R_status": e1rs["status"],
            "ignored_E1": e1s["ignore"] and t["entry_open"] in wg_opens,
            "ignored_E1R": e1rs["ignore"] and t["entry_open"] in wg_opens,
        }
        signals.append(rec)

    wg = [s for s in signals if s["current_guard_allows"]]
    cmp_tbl = {
        "CURRENT": cohort(wg, None, bad4),
        "E1": cohort(wg, "ignored_E1", bad4),
        "E1R": cohort(wg, "ignored_E1R", bad4),
    }

    extra = [s for s in wg if not s["ignored_E1"] and s["ignored_E1R"]]
    for s in extra:
        s["delta_reason"] = "E1R_reclaim_extension"

    reclaim_ok = sum(1 for e in sim["reclaim_events"] if e["result"] == "EMA_RECLAIM_SUCCESS")
    reclaim_fail = len(sim["reclaim_events"]) - reclaim_ok

    return {
        "comparison": cmp_tbl,
        "reclaim_events": sim["reclaim_events"],
        "reclaim_success_count": reclaim_ok,
        "reclaim_failure_count": reclaim_fail,
        "e1_episodes": sim["e1_episodes"],
        "e1r_episodes": sim["e1r_episodes"],
        "e1_episode_stats": episode_duration_stats(sim["e1_episodes"]),
        "e1r_episode_stats": episode_duration_stats(sim["e1r_episodes"]),
        "additional_ignores_vs_e1": extra,
        "signals": signals,
        "wg_trades": wg,
    }


def xrp_12_june(sim_events: list[dict], wg: list[dict]) -> dict:
    dip = next((e for e in sim_events if e.get("dip_time") == DIP_XRP), None)
    bad4_rows = [s for s in wg if s["entry_open"] in BAD4_XRP]
    for s in bad4_rows:
        s["would_ignore_E1"] = s["ignored_E1"]
        s["would_ignore_E1R"] = s["ignored_E1R"]
    return {"dip_reclaim_event": dip, "bad4": bad4_rows}


def main() -> None:
    _ensure_paths()
    data = json.loads(SIGNAL_JSON.read_text())
    coins_out = {}
    for sym in COINS:
        bad4 = BAD4_XRP if sym == "XRPUSDT" else set()
        print("analyze", sym)
        coins_out[sym] = analyze_coin(sym, data["symbols"][sym], bad4)

    xrp_sim = coins_out["XRPUSDT"]["reclaim_events"]
    xrp_ref = xrp_12_june(xrp_sim, coins_out["XRPUSDT"]["wg_trades"])

    cross = {}
    for sym in COINS:
        c = coins_out[sym]["comparison"]
        e1, e1r = coins_out[sym]["e1_episode_stats"], coins_out[sym]["e1r_episode_stats"]
        cross[sym] = {
            "trades": c["CURRENT"]["allowed_trades"],
            "E1_trades": c["E1"]["allowed_trades"],
            "E1R_trades": c["E1R"]["allowed_trades"],
            "sl_current": c["CURRENT"]["sl"],
            "sl_E1": c["E1"]["sl"],
            "sl_E1R": c["E1R"]["sl"],
            "tp_first_current": c["CURRENT"]["TP_FIRST"],
            "tp_first_E1": c["E1"]["TP_FIRST"],
            "tp_first_E1R": c["E1R"]["TP_FIRST"],
            "tp_first_lost_E1": c["E1"]["tp_first_lost"],
            "tp_first_lost_E1R": c["E1R"]["tp_first_lost"],
            "sl_removed_E1": c["CURRENT"]["sl"] - c["E1"]["sl"],
            "sl_removed_E1R": c["CURRENT"]["sl"] - c["E1R"]["sl"],
            "max_sl_current": c["CURRENT"]["max_sl_streak"],
            "max_sl_E1": c["E1"]["max_sl_streak"],
            "max_sl_E1R": c["E1R"]["max_sl_streak"],
            "median_mae_current": c["CURRENT"]["median_mae"],
            "median_mae_E1": c["E1"]["median_mae"],
            "median_mae_E1R": c["E1R"]["median_mae"],
            "median_mfe_current": c["CURRENT"]["median_mfe"],
            "median_mfe_E1": c["E1"]["median_mfe"],
            "median_mfe_E1R": c["E1R"]["median_mfe"],
            "median_episode_h_E1": e1["median_hours"],
            "median_episode_h_E1R": e1r["median_hours"],
            "max_episode_h_E1": e1["max_hours"],
            "max_episode_h_E1R": e1r["max_hours"],
        }

    verdict = {
        "q1_xrp_1206": f"Reclaim event: {xrp_ref['dip_reclaim_event'].get('result') if xrp_ref.get('dip_reclaim_event') else 'NOT_FOUND'}",
        "q2_extra_sl": "Siehe additional_ignores_vs_e1 pro Coin.",
        "q3_extra_tp": {sym: coins_out[sym]["comparison"]["E1R"]["tp_first_lost"] - coins_out[sym]["comparison"]["E1"]["tp_first_lost"] for sym in COINS},
        "q4_stickiness": {sym: (coins_out[sym]["e1r_episode_stats"]["max_hours"], coins_out[sym]["e1_episode_stats"]["max_hours"]) for sym in COINS},
        "q5_cross_coin": "Vergleich cross-Tabelle.",
    }

    report = {
        "E1R_definition": (
            "Start wie E1. Bei close<=EMA: wenn rising_3+Struktur intakt → PENDING eine Bar; "
            "Reclaim wenn nächste Bar close>EMA+rising_3+Struktur; sonst Reset. Pending-Signale: PENDING_NOT_RESOLVED."
        ),
        "xrp_june_12": xrp_ref,
        "coins": coins_out,
        "cross_coin_table": cross,
        "verdict": verdict,
    }

    md = build_md(report)
    report["executive_summary"] = md
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    OUT_MD.write_text(md + "\n")

    fields = [
        "symbol",
        "signal_time",
        "decision_time",
        "entry_open",
        "entry_price",
        "ignored_E1",
        "ignored_E1R",
        "E1_status",
        "E1R_status",
        "final_outcome",
        "mae_pct",
        "mfe_pct",
    ]
    rows = []
    for sym in COINS:
        rows.extend(coins_out[sym]["wg_trades"])
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    for sym in COINS:
        p = ROOT / f"analyze_crosscoin_e1_onebar_reclaim_shadow_{sym}_v1.csv"
        with p.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in coins_out[sym]["wg_trades"]:
                w.writerow(r)

    print("wrote", OUT_JSON, OUT_MD, OUT_CSV)


def build_md(r: dict) -> str:
    x = r["coins"]["XRPUSDT"]["comparison"]
    lines = [
        "# Cross-Coin E1 vs E1R One-Bar Reclaim Shadow v1",
        "",
        "### E1R definition",
        r["E1R_definition"],
        "",
        "### XRP 12 June reclaim",
        json.dumps(r["xrp_june_12"].get("dip_reclaim_event"), indent=2, default=str),
        "",
        "### XRP BAD-4",
        json.dumps(r["xrp_june_12"].get("bad4"), indent=2, default=str),
        "",
        "### XRP current vs E1 vs E1R",
        table(r["coins"]["XRPUSDT"]["comparison"]),
        "",
        "### XRP additional ignores caused by reclaim",
        f"count={len(r['coins']['XRPUSDT']['additional_ignores_vs_e1'])}",
        "",
        "### ADA current vs E1 vs E1R",
        table(r["coins"]["ADAUSDT"]["comparison"]),
        "",
        "### DOGE current vs E1 vs E1R",
        table(r["coins"]["DOGEUSDT"]["comparison"]),
        "",
        "### Reclaim success/failure counts",
        json.dumps(
            {
                s: (r["coins"][s]["reclaim_success_count"], r["coins"][s]["reclaim_failure_count"])
                for s in ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
            },
            indent=2,
        ),
        "",
        "### Lost TP_FIRST",
        json.dumps(
            {
                s: (r["coins"][s]["comparison"]["E1"]["tp_first_lost"], r["coins"][s]["comparison"]["E1R"]["tp_first_lost"])
                for s in ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
            }
        ),
        "",
        "### SL reduction",
        json.dumps(
            {
                s: (r["cross_coin_table"][s]["sl_removed_E1"], r["cross_coin_table"][s]["sl_removed_E1R"])
                for s in ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
            }
        ),
        "",
        "### SL streaks",
        json.dumps(
            {
                s: (r["cross_coin_table"][s]["max_sl_E1"], r["cross_coin_table"][s]["max_sl_E1R"])
                for s in ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
            }
        ),
        "",
        "### MAE / MFE",
        f"XRP CURRENT {x['CURRENT']['median_mae']}/{x['CURRENT']['median_mfe']}; "
        f"E1 {x['E1']['median_mae']}/{x['E1']['median_mfe']}; E1R {x['E1R']['median_mae']}/{x['E1R']['median_mfe']}",
        "",
        "### Episode duration",
        json.dumps(
            {s: {"E1": r["coins"][s]["e1_episode_stats"], "E1R": r["coins"][s]["e1r_episode_stats"]} for s in ("XRPUSDT", "ADAUSDT", "DOGEUSDT")},
            indent=2,
        ),
        "",
        "### Gap / cluster structure during successful reclaims",
        "Siehe reclaim_events[].gap_* in JSON.",
        "",
        "### Verdict",
    ]
    for i, (k, v) in enumerate(r["verdict"].items(), 1):
        lines.append(f"{i}. {k}: {v}")
    return "\n".join(lines)


def table(c: dict) -> str:
    lines = ["| Metrik | CURRENT | E1 | E1R |", "|---|--:|--:|--:|"]
    for k in (
        "allowed_trades",
        "ignored_trades",
        "sl",
        "TP_FIRST",
        "tp_first_lost",
        "MOVE_041_THEN_SL",
        "DIRECT_SL",
        "mfe_ge_041_before_sl",
        "median_mae",
        "median_mfe",
        "max_sl_streak",
        "bad4_removed",
    ):
        lines.append(f"| {k} | {c['CURRENT'].get(k)} | {c['E1'].get(k)} | {c['E1R'].get(k)} |")
    return "\n".join(lines)


if __name__ == "__main__":
    main()

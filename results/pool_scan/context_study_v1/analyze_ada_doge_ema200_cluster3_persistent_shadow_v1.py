"""Out-of-sample ADA/DOGE shadow: persistent CLUSTER_3 + EMA200 (same as XRP)."""

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
SIGNAL_JSON = ROOT / "floor_guard_signal_list_v1.json"
XRP_JSON = ROOT / "analyze_xrp_ema200_cluster3_persistent_shadow_v1.json"
OUT_JSON = ROOT / "analyze_ada_doge_ema200_cluster3_persistent_shadow_v1.json"
OUT_MD = ROOT / "analyze_ada_doge_ema200_cluster3_persistent_shadow_v1.md"
OUT_CSV = ROOT / "analyze_ada_doge_ema200_cluster3_persistent_shadow_trades_v1.csv"
OUT_CSV_ADA = ROOT / "analyze_ada_doge_ema200_cluster3_persistent_shadow_ADAUSDT_v1.csv"
OUT_CSV_DOGE = ROOT / "analyze_ada_doge_ema200_cluster3_persistent_shadow_DOGEUSDT_v1.csv"

COINS = ("ADAUSDT", "DOGEUSDT")
MFE_THRESH = 0.41

from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import (  # noqa: E402
    CLUSTER_GAP_PCT,
    PANE_FROM,
    PANE_TO,
    REPORT_FROM,
    REPORT_TO,
    _ensure_paths,
    _median,
    _utc,
    bull_active_at_decision,
    detect_crosses,
    ladder_at_index,
    last_closed_idx,
    outcome_class,
    simulate_bull_episodes,
    strip_internal,
)


def _mean(vals: list[float]) -> float | None:
    return round(statistics.mean(vals), 4) if vals else None


def _percentile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return round(s[f], 4)
    return round(s[f] + (s[c] - s[f]) * (k - f), 4)


def active_upper_records(pools: list[dict], moment: datetime, price: float) -> list[dict]:
    rows = []
    for p in pools:
        if p["side"] != "upper":
            continue
        if p["known"] > moment:
            continue
        if p.get("break_at") and p["break_at"] <= moment:
            continue
        if float(p["bottom"]) <= price:
            continue
        known = p["known"].isoformat() if hasattr(p["known"], "isoformat") else p["known"]
        rows.append(
            {
                "pool_id": p["pool_id"],
                "timeframe": "15m",
                "known_at": known,
                "bottom": float(p["bottom"]),
                "top": float(p["top"]),
            }
        )
    return rows


def upper_ladder_at_index(candles15, bars15, idx, cfg, cache, price: float | None = None) -> dict:
    from analyze_xrp_ema200_cluster_transition_shadow_v1 import structural_upper_ladder
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index

    bar = bars15[idx]
    decision = bar["close_time"]
    px = price if price is not None else float(bar["close"])
    pools, _, _ = scanner_pools_for_index(candles15, "15m", idx, cfg, cache=cache)
    uppers_raw = [
        p
        for p in pools
        if p["side"] == "upper"
        and p["known"] <= decision
        and (p.get("break_at") is None or p["break_at"] > decision)
        and float(p["bottom"]) > px
    ]
    lad = structural_upper_ladder(px, uppers_raw)
    ema = bar.get("ema200")
    close = bar.get("close")
    return {
        **lad,
        "close_at_decision": close,
        "ema200_at_decision": ema,
        "price_below_ema200": close < ema if ema and close else None,
    }


def simulate_bear_episodes(bars15, candles15, cfg, cache) -> tuple[list[dict], dict]:
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index

    active_at: dict[datetime, bool] = {}
    episodes: list[dict] = []
    bear_active = False
    ep: dict | None = None
    cross_by_idx = {c["bar_index"]: c for c in detect_crosses(bars15, REPORT_TO) if c["cross_direction"] == "bearish"}

    for i, bar in enumerate(bars15):
        ct = bar["close_time"]
        if ct < REPORT_FROM or ct > REPORT_TO:
            continue
        ctx = upper_ladder_at_index(candles15, bars15, i, cfg, cache)
        falling_3 = ctx["upper_cluster_falling_3"]
        below = ctx["price_below_ema200"] is True
        close = ctx["close_at_decision"]
        ema = ctx["ema200_at_decision"]
        is_bear_cross = i in cross_by_idx

        if not bear_active:
            if is_bear_cross and falling_3:
                bear_active = True
                ep = {"start": ct.isoformat(), "start_cross": cross_by_idx[i]}
        else:
            if not below or not falling_3:
                bear_active = False
                if ep:
                    ep["end"] = ct.isoformat()
                    ep["duration_hours"] = round(
                        (_utc(ct) - _utc(datetime.fromisoformat(ep["start"]))).total_seconds() / 3600.0,
                        2,
                    )
                    if not below:
                        ep["end_reason"] = "price_above_or_equal_ema200"
                    else:
                        ep["end_reason"] = "falling_3_false"
                    episodes.append(ep)
                    ep = None
        active_at[ct] = bear_active

    if bear_active and ep:
        ep["end"] = None
        ep["end_reason"] = "still_active_at_report_end"
        ep["duration_hours"] = round(
            (REPORT_TO - _utc(datetime.fromisoformat(ep["start"]))).total_seconds() / 3600.0,
            2,
        )
        episodes.append(ep)
    return episodes, active_at


def cohort_row(all_wg: list[dict], ignore_key: str | None) -> dict:
    if ignore_key:
        ignored_n = sum(1 for t in all_wg if t.get(ignore_key))
        trades = [t for t in all_wg if not t.get(ignore_key)]
    else:
        ignored_n = 0
        trades = all_wg
    oc = [t.get("final_outcome") or t.get("outcome_class") for t in trades]
    maes = [float(t["mae_pct"]) for t in trades if t.get("mae_pct") is not None]
    mfes = [float(t["mfe_pct"]) for t in trades if t.get("mfe_pct") is not None]
    streak = best = 0
    for t in trades:
        if t.get("first_hit") == "SL":
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    all_ignored = [t for t in all_wg if ignore_key and t.get(ignore_key)]
    return {
        "allowed_trades": len(trades),
        "ignored_trades": ignored_n,
        "sl": sum(1 for t in trades if t.get("first_hit") == "SL"),
        "tp": sum(1 for t in trades if t.get("first_hit") == "TP"),
        "MOVE_041_THEN_SL": sum(1 for o in oc if o == "MOVE_041_THEN_SL"),
        "DIRECT_SL": sum(1 for o in oc if o == "SL_FIRST"),
        "mfe_ge_041_before_sl": sum(1 for o in oc if o == "MOVE_041_THEN_SL"),
        "median_mae": _median(maes),
        "median_mfe": _median(mfes),
        "mean_mae": _mean(maes),
        "mean_mfe": _mean(mfes),
        "max_sl_streak": best,
        "tp_first_lost": sum(1 for t in all_ignored if t.get("final_outcome") == "TP_FIRST"),
        "sl_removed": sum(1 for t in all_ignored if t.get("first_hit") == "SL"),
    }


def sl_streak_analysis(wg: list[dict], ignore_key: str) -> dict:
    ordered = sorted(wg, key=lambda s: s["decision_dt"])

    def streaks(trades: list[dict]) -> list[list[dict]]:
        out: list[list[dict]] = []
        cur: list[dict] = []
        for t in trades:
            if t.get("first_hit") == "SL":
                cur.append(t)
            else:
                if cur:
                    out.append(cur)
                cur = []
        if cur:
            out.append(cur)
        return out

    cur_exec = [t for t in ordered if not t.get(ignore_key)]
    cur_streaks = streaks(ordered)
    pers_streaks = streaks(cur_exec)
    removed_from_streaks = []
    for st in cur_streaks:
        removed = [t for t in st if t.get(ignore_key)]
        if removed:
            removed_from_streaks.append(
                {
                    "streak_len_current": len(st),
                    "removed_count": len(removed),
                    "remaining_len_persistent": len([t for t in st if not t.get(ignore_key)]),
                    "removed_entry_opens": [t["entry_open"] for t in removed],
                }
            )
    return {
        "max_sl_streak_current": max((len(s) for s in cur_streaks), default=0),
        "max_sl_streak_persistent": max((len(s) for s in pers_streaks), default=0),
        "streak_count_current": len(cur_streaks),
        "streak_detail_removed": removed_from_streaks,
    }


def enrich_episodes(episodes: list[dict], signals: list[dict], bars15, candles15, cfg, cache) -> list[dict]:
    for ep in episodes:
        start = _utc(datetime.fromisoformat(ep["start"]))
        end = _utc(datetime.fromisoformat(ep["end"])) if ep.get("end") else REPORT_TO
        in_ep = [s for s in signals if start <= s["decision_dt"] <= end]
        ep["shorts_in_episode"] = len(in_ep)
        ep["shorts_guard_allowed"] = sum(1 for s in in_ep if s["current_guard_allows"])
        ignored = [s for s in in_ep if s.get("persistent_shadow_ignore") and s["current_guard_allows"]]
        ep["ignored_guard"] = len(ignored)
        ep["ignored_sl"] = sum(1 for s in ignored if s.get("first_hit") == "SL")
        ep["ignored_tp"] = sum(1 for s in ignored if s.get("final_outcome") == "TP_FIRST")
        # rising_3 held on every 15m bar in episode?
        rising_ok = True
        ema_closes = []
        for i, bar in enumerate(bars15):
            ct = bar["close_time"]
            if ct < start or ct > end:
                continue
            ctx = ladder_at_index(candles15, bars15, i, cfg, cache)
            ema_closes.append({"t": ct.isoformat(), "close": ctx["close_at_decision"], "ema": ctx["ema200_at_decision"]})
            if not ctx["lower_cluster_rising_3"]:
                rising_ok = False
        ep["rising_3_every_bar"] = rising_ok
        ep["ema_close_min"] = min((x["close"] for x in ema_closes), default=None)
        ep["ema_close_max"] = max((x["close"] for x in ema_closes), default=None)
    return episodes


def episode_stats(episodes: list[dict]) -> dict:
    durs = [e["duration_hours"] for e in episodes if e.get("duration_hours") is not None]
    return {
        "count": len(episodes),
        "median_duration_hours": _median(durs),
        "p75_duration_hours": _percentile(durs, 75),
        "p90_duration_hours": _percentile(durs, 90),
        "max_duration_hours": max(durs) if durs else None,
    }


def top_long_episodes(episodes: list[dict], n: int = 10) -> list[dict]:
    ranked = sorted(episodes, key=lambda e: e.get("duration_hours") or 0, reverse=True)[:n]
    return ranked


def find_episode_for_time(episodes: list[dict], moment: datetime) -> dict | None:
    for ep in episodes:
        st = _utc(datetime.fromisoformat(ep["start"]))
        en = _utc(datetime.fromisoformat(ep["end"])) if ep.get("end") else REPORT_TO
        if st <= moment <= en:
            return ep
    return None


def analyze_symbol(symbol: str, sym_data: dict, window: list[str]) -> dict:
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, ui_lld_config

    without = sym_data["without_guard"]
    wg_opens = {t["entry_open"] for t in sym_data["with_guard"]}
    shorts_wg = len(sym_data["with_guard"])
    shorts_blocked = len(without) - shorts_wg

    load_market(symbol)
    _p, candles15 = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    bars15 = build_15m_bars(candles15)
    by_open = {b["open_time"]: b for b in bars15}
    cfg = ui_lld_config("15m")
    cache: dict = {}

    crosses = detect_crosses(bars15, REPORT_TO)
    crosses_win = [c for c in crosses if REPORT_FROM <= _utc(datetime.fromisoformat(c["cross_time"])) <= REPORT_TO]
    bull_cross_n = sum(1 for c in crosses_win if c["cross_direction"] == "bullish")
    bear_cross_n = sum(1 for c in crosses_win if c["cross_direction"] == "bearish")

    signals: list[dict] = []
    long_count = 0
    for t in without:
        direction = t.get("direction", "SHORT")
        if direction != "SHORT":
            long_count += 1
            continue
        ot = _utc(datetime.fromisoformat(t["entry_open"]))
        if ot not in by_open:
            continue
        bar = by_open[ot]
        decision = bar["close_time"]
        if decision < REPORT_FROM or decision > REPORT_TO:
            continue
        idx = bar["candle_index"]
        price = float(t["entry_price"])
        ctx = ladder_at_index(candles15, bars15, idx, cfg, cache, price=price)
        oc = outcome_class(t, bars15, by_open)
        last_bull = None
        for c in crosses_win:
            if c["cross_direction"] != "bullish":
                continue
            if _utc(datetime.fromisoformat(c["cross_time"])) <= decision:
                last_bull = c
        hours_since = None
        if last_bull:
            hours_since = round(
                (decision - _utc(datetime.fromisoformat(last_bull["cross_time"]))).total_seconds() / 3600.0,
                2,
            )
        signals.append(
            {
                **t,
                "symbol": symbol,
                "signal_time": ot.isoformat(),
                "decision_time": decision.isoformat(),
                "decision_dt": decision,
                "direction": "SHORT",
                "current_guard_allows": t["entry_open"] in wg_opens,
                "latest_bull_cross_time": last_bull["cross_time"] if last_bull else None,
                "hours_since_bull_cross": hours_since,
                "rising_3": ctx["lower_cluster_rising_3"],
                "cluster_1_top": ctx.get("cluster_1_top"),
                "cluster_2_top": ctx.get("cluster_2_top"),
                "cluster_3_top": ctx.get("cluster_3_top"),
                "price_above_ema200": ctx["price_above_ema200"],
                "final_outcome": oc,
                "outcome_class": oc,
                "TP_FIRST": oc == "TP_FIRST",
                "SL_FIRST": oc == "SL_FIRST",
                "MOVE_041_THEN_SL": oc == "MOVE_041_THEN_SL",
                "DIRECT_SL": oc == "SL_FIRST",
                "mfe_ge_041_before_sl": oc == "MOVE_041_THEN_SL",
                "_bars15": bars15,
                "_by_open": by_open,
            }
        )

    bull_eps, active_at = simulate_bull_episodes(bars15, candles15, cfg, cache)
    bear_eps, _ = simulate_bear_episodes(bars15, candles15, cfg, cache)
    bull_eps = enrich_episodes(bull_eps, signals, bars15, candles15, cfg, cache)

    for s in signals:
        st = bull_active_at_decision(s["decision_dt"], bars15, active_at, candles15, cfg, cache, float(s["entry_price"]))
        s["bull_transition_active"] = st["bull_transition_active"]
        s["persistent_shadow_ignore"] = st["bull_transition_active"]
        ep = find_episode_for_time(bull_eps, s["decision_dt"])
        s["transition_start"] = ep["start"] if ep else None
        if ep and s.get("persistent_shadow_ignore"):
            s["hours_since_transition_start"] = round(
                (s["decision_dt"] - _utc(datetime.fromisoformat(ep["start"]))).total_seconds() / 3600.0,
                2,
            )
        else:
            s["hours_since_transition_start"] = None

    wg = [s for s in signals if s["current_guard_allows"]]
    comparison = {
        "CURRENT": cohort_row(wg, None),
        "PERSISTENT_CLUSTER3": cohort_row(wg, "persistent_shadow_ignore"),
    }

    ignored_wg = [s for s in wg if s.get("persistent_shadow_ignore")]
    blocked = {
        "SL_FIRST": sum(1 for s in ignored_wg if s["final_outcome"] == "SL_FIRST"),
        "MOVE_041_THEN_SL": sum(1 for s in ignored_wg if s["final_outcome"] == "MOVE_041_THEN_SL"),
        "TP_FIRST": sum(1 for s in ignored_wg if s["final_outcome"] == "TP_FIRST"),
        "OTHER": sum(1 for s in ignored_wg if s["final_outcome"] == "OTHER"),
    }
    tp_first_detail = [
        strip_internal(
            {
                **s,
                "tp_time": s.get("exit_time"),
                "cluster_tops": [s.get("cluster_1_top"), s.get("cluster_2_top"), s.get("cluster_3_top")],
            }
        )
        for s in ignored_wg
        if s["final_outcome"] == "TP_FIRST"
    ]

    ignored_loss_context = {
        "ignored_with_price_above_ema": sum(1 for s in ignored_wg if s.get("price_above_ema200")),
        "ignored_with_rising_3": sum(1 for s in ignored_wg if s.get("rising_3")),
        "total_ignored": len(ignored_wg),
    }

    return {
        "signal_source": str(SIGNAL_JSON),
        "window": window,
        "coverage": {
            "shorts_without_guard": len(signals),
            "shorts_with_guard": shorts_wg,
            "shorts_guard_blocked": shorts_blocked,
            "long_signals": long_count,
            "bull_crosses_15m": bull_cross_n,
            "bear_crosses_15m": bear_cross_n,
            "bull_transition_episodes": len(bull_eps),
            "bear_transition_episodes": len(bear_eps),
        },
        "comparison": comparison,
        "blocked_breakdown": blocked,
        "ignored_tp_first_detail": tp_first_detail,
        "ignored_loss_context": ignored_loss_context,
        "sl_streak": sl_streak_analysis(wg, "persistent_shadow_ignore"),
        "bull_episode_stats": episode_stats(bull_eps),
        "bear_episode_stats": episode_stats(bear_eps),
        "top_10_long_bull_episodes": top_long_episodes(bull_eps, 10),
        "bull_transition_episodes": bull_eps,
        "bear_transition_episodes": bear_eps,
        "trades_guard": [strip_internal(s) for s in wg],
        "all_signals": [strip_internal(s) for s in signals],
    }


def load_xrp_reference() -> dict:
    x = json.loads(XRP_JSON.read_text())
    c = x["comparison"]
    es = x["episode_summary"]
    cur = c["CURRENT"]
    per = c["CLUSTER_3_PERSISTENT"]
    return {
        "current_trades": cur["allowed_trades"],
        "persistent_trades": per["allowed_trades"],
        "current_sl": cur["sl"],
        "persistent_sl": per["sl"],
        "current_tp": cur["tp"],
        "persistent_tp": per["tp"],
        "tp_lost": per["tp_first_lost"],
        "sl_removed": cur["sl"] - per["sl"],
        "current_max_sl_streak": cur["max_sl_streak"],
        "persistent_max_sl_streak": per["max_sl_streak"],
        "median_mae_current": cur["median_mae"],
        "median_mae_persistent": per["median_mae"],
        "median_mfe_current": cur["median_mfe"],
        "median_mfe_persistent": per["median_mfe"],
        "transition_episodes": es["total_episodes"],
        "median_episode_duration": es["median_duration_hours"],
        "max_episode_duration": es["max_duration_hours"],
        "source": str(XRP_JSON),
    }


def coin_cross_row(coin_result: dict) -> dict:
    c = coin_result["comparison"]
    cur, per = c["CURRENT"], c["PERSISTENT_CLUSTER3"]
    es = coin_result["bull_episode_stats"]
    return {
        "current_trades": cur["allowed_trades"],
        "persistent_trades": per["allowed_trades"],
        "current_sl": cur["sl"],
        "persistent_sl": per["sl"],
        "current_tp": cur["tp"],
        "persistent_tp": per["tp"],
        "tp_lost": per["tp_first_lost"],
        "sl_removed": cur["sl"] - per["sl"],
        "current_max_sl_streak": cur["max_sl_streak"],
        "persistent_max_sl_streak": per["max_sl_streak"],
        "median_mae_current": cur["median_mae"],
        "median_mae_persistent": per["median_mae"],
        "median_mfe_current": cur["median_mfe"],
        "median_mfe_persistent": per["median_mfe"],
        "transition_episodes": es["count"],
        "median_episode_duration": es["median_duration_hours"],
        "max_episode_duration": es["max_duration_hours"],
    }


def build_md(report: dict) -> str:
    fixed = report["fixed_rule"]
    lines = [
        "# ADA / DOGE Persistent CLUSTER_3 Shadow v1 (OOS)",
        "",
        "Nur Shadow-Analyse. Keine Produktionsänderung.",
        "",
        "### Fixed rule",
        fixed,
        "",
    ]

    def coin_section(title: str, key: str) -> None:
        d = report["coins"][key]
        cov = d["coverage"]
        c = d["comparison"]
        lines.append(f"### {title} coverage")
        lines.append(json.dumps({**cov, "signal_source": d["signal_source"], "window": d["window"]}, indent=2))
        lines.append("")
        lines.append(f"### {title} current vs persistent")
        lines.append("| Metrik | CURRENT | PERSISTENT_CLUSTER3 |")
        lines.append("|---|---:|---:|")
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
            "mean_mae",
            "mean_mfe",
            "max_sl_streak",
        ):
            lines.append(f"| {k} | {c['CURRENT'][k]} | {c['PERSISTENT_CLUSTER3'][k]} |")
        lines.append("")
        lines.append(f"### {title} blocked winners / removed losses")
        lines.append(json.dumps(d["blocked_breakdown"], indent=2))
        lines.append(f"ignored context: {json.dumps(d['ignored_loss_context'])}")
        lines.append("")
        lines.append(f"### {title} SL streak impact")
        lines.append(json.dumps(d["sl_streak"], indent=2))
        lines.append("")
        lines.append(f"### {title} transition episodes")
        lines.append(json.dumps(d["bull_episode_stats"], indent=2))

    coin_section("ADA", "ADAUSDT")
    coin_section("DOGE", "DOGEUSDT")

    lines += [
        "",
        "### Long episodes",
        "Top-10 längste Bull-Episoden pro Coin in JSON (`top_10_long_bull_episodes`). Keine Long-Signale in Pipeline.",
        "",
        "### Cross-coin comparison",
        "| Metrik | XRP | ADA | DOGE |",
        "|---|--:|--:|--:|",
    ]
    cc = report["cross_coin"]
    for metric in (
        "current_trades",
        "persistent_trades",
        "current_sl",
        "persistent_sl",
        "current_tp",
        "persistent_tp",
        "tp_lost",
        "sl_removed",
        "current_max_sl_streak",
        "persistent_max_sl_streak",
        "median_mae_current",
        "median_mae_persistent",
        "median_mfe_current",
        "median_mfe_persistent",
        "transition_episodes",
        "median_episode_duration",
        "max_episode_duration",
    ):
        lines.append(
            f"| {metric} | {cc['XRP'][metric]} | {cc['ADAUSDT'][metric]} | {cc['DOGEUSDT'][metric]} |"
        )

    lines += [
        "",
        "### Robustness observations",
        report["robustness"],
        "",
        "### Verdict",
    ]
    for i, v in enumerate(report["verdict"].values(), 1):
        lines.append(f"{i}. {v}")
    return "\n".join(lines)


def main() -> None:
    _ensure_paths()
    data = json.loads(SIGNAL_JSON.read_text())
    window = data["window"]
    results = {}
    for sym in COINS:
        print("analyzing", sym)
        results[sym] = analyze_symbol(sym, data["symbols"][sym], window)

    xrp_ref = load_xrp_reference()
    cross = {"XRP": xrp_ref, "ADAUSDT": coin_cross_row(results["ADAUSDT"]), "DOGEUSDT": coin_cross_row(results["DOGEUSDT"])}

    ada_sl_down = cross["ADAUSDT"]["persistent_sl"] < cross["ADAUSDT"]["current_sl"]
    doge_sl_down = cross["DOGEUSDT"]["persistent_sl"] < cross["DOGEUSDT"]["current_sl"]

    robustness = (
        f"ADA SL {cross['ADAUSDT']['current_sl']}→{cross['ADAUSDT']['persistent_sl']} "
        f"({'reduziert' if ada_sl_down else 'nicht reduziert'}). "
        f"DOGE SL {cross['DOGEUSDT']['current_sl']}→{cross['DOGEUSDT']['persistent_sl']} "
        f"({'reduziert' if doge_sl_down else 'nicht reduziert'}). "
        f"SL-Streaks: ADA {cross['ADAUSDT']['current_max_sl_streak']}→{cross['ADAUSDT']['persistent_max_sl_streak']}, "
        f"DOGE {cross['DOGEUSDT']['current_max_sl_streak']}→{cross['DOGEUSDT']['persistent_max_sl_streak']}, "
        f"XRP {cross['XRP']['current_max_sl_streak']}→{cross['XRP']['persistent_max_sl_streak']}. "
        f"Max-Episodenlänge: XRP {cross['XRP']['max_episode_duration']}h, "
        f"ADA {cross['ADAUSDT']['max_episode_duration']}h, DOGE {cross['DOGEUSDT']['max_episode_duration']}h."
    )

    verdict = {
        "q1_reduces_losses_ada_doge": (
            f"ADA: {'ja' if ada_sl_down else 'nein'}; DOGE: {'ja' if doge_sl_down else 'nein'} "
            f"(unveränderte XRP-Regel, Jun–Jul 2026)."
        ),
        "q2_tp_first_cost": (
            f"TP_FIRST verloren — XRP: {cross['XRP']['tp_lost']}, "
            f"ADA: {cross['ADAUSDT']['tp_lost']}, DOGE: {cross['DOGEUSDT']['tp_lost']}."
        ),
        "q3_shorter_streaks": (
            f"Max SL-Serie kürzer — ADA: {cross['ADAUSDT']['current_max_sl_streak']}→{cross['ADAUSDT']['persistent_max_sl_streak']}, "
            f"DOGE: {cross['DOGEUSDT']['current_max_sl_streak']}→{cross['DOGEUSDT']['persistent_max_sl_streak']}."
        ),
        "q4_long_episodes": (
            f"Längste Bull-Episoden (h): XRP {cross['XRP']['max_episode_duration']}, "
            f"ADA {cross['ADAUSDT']['max_episode_duration']}, DOGE {cross['DOGEUSDT']['max_episode_duration']} — Details in top_10 JSON."
        ),
        "q5_more_testing": (
            "Evidenz ist gemischt über Coins; längerer Zeitraum/weitere Coins nur nach Review der OOS-Episoden und TP-Kosten."
        ),
    }

    fixed_rule = (
        f"1:1 aus analyze_xrp_ema200_cluster3_persistent_shadow_v1.py: 15m EMA200, cluster gap {CLUSTER_GAP_PCT}%, "
        "rising_3 lower clusters, persistent bull transition until price<=EMA200 or rising_3 false; "
        "bear mirror prepared for longs."
    )

    report = {
        "meta": {"analysis_only": True, "oos_coins": list(COINS), "xrp_reference_file": str(XRP_JSON)},
        "fixed_rule": fixed_rule,
        "coins": results,
        "cross_coin": cross,
        "robustness": robustness,
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
        "current_guard_allows",
        "latest_bull_cross_time",
        "hours_since_bull_cross",
        "price_above_ema200",
        "cluster_1_top",
        "cluster_2_top",
        "cluster_3_top",
        "rising_3",
        "bull_transition_active",
        "persistent_shadow_ignore",
        "mae_pct",
        "mfe_pct",
        "TP_FIRST",
        "SL_FIRST",
        "MOVE_041_THEN_SL",
        "final_outcome",
    ]
    for path, rows in (
        (OUT_CSV, results["ADAUSDT"]["trades_guard"] + results["DOGEUSDT"]["trades_guard"]),
        (OUT_CSV_ADA, results["ADAUSDT"]["trades_guard"]),
        (OUT_CSV_DOGE, results["DOGEUSDT"]["trades_guard"]),
    ):
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for s in rows:
                w.writerow(s)

    print("wrote", OUT_JSON, OUT_MD, OUT_CSV, OUT_CSV_ADA, OUT_CSV_DOGE)


if __name__ == "__main__":
    main()

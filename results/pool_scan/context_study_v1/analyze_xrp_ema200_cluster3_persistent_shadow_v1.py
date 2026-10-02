"""Shadow: persistent CLUSTER_3 + EMA200 bull transition (analysis only)."""

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
OUT_JSON = ROOT / "analyze_xrp_ema200_cluster3_persistent_shadow_v1.json"
OUT_MD = ROOT / "analyze_xrp_ema200_cluster3_persistent_shadow_v1.md"
OUT_CSV = ROOT / "analyze_xrp_ema200_cluster3_persistent_shadow_trades_v1.csv"
PRIOR_JSON = ROOT / "analyze_xrp_ema200_cluster_transition_shadow_v1.json"

SYMBOL = "XRPUSDT"
CLUSTER_GAP_PCT = 0.35
MFE_THRESH = 0.41
BAD_ENTRIES = {
    "2026-06-11T19:15:00+00:00",
    "2026-06-14T21:45:00+00:00",
    "2026-06-14T22:30:00+00:00",
    "2026-06-14T23:30:00+00:00",
}
JUNE14_BAD = [
    "2026-06-14T21:45:00+00:00",
    "2026-06-14T22:30:00+00:00",
    "2026-06-14T23:30:00+00:00",
]
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
REPORT_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
REPORT_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)


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


def last_closed_idx(bars: list[dict], moment: datetime) -> int | None:
    idx = None
    for i, b in enumerate(bars):
        if b["close_time"] <= moment:
            idx = i
    return idx


def detect_crosses(bars15: list[dict], t_end: datetime) -> list[dict]:
    out = []
    for i in range(1, len(bars15)):
        if bars15[i]["close_time"] > t_end:
            break
        p, c = bars15[i - 1], bars15[i]
        e0, e1 = p.get("ema200"), c.get("ema200")
        if e0 is None or e1 is None:
            continue
        direction = None
        if p["close"] <= e0 and c["close"] > e1:
            direction = "bullish"
        elif p["close"] >= e0 and c["close"] < e1:
            direction = "bearish"
        if direction:
            out.append(
                {
                    "cross_time": c["close_time"].isoformat(),
                    "cross_direction": direction,
                    "close_before": p["close"],
                    "ema_before": e0,
                    "close_after": c["close"],
                    "ema_after": e1,
                    "known_at": c["close_time"].isoformat(),
                    "bar_index": i,
                }
            )
    return out


def active_lower_records(pools: list[dict], moment: datetime, price: float) -> list[dict]:
    rows = []
    for p in pools:
        if p["side"] != "lower":
            continue
        if p["known"] > moment:
            continue
        if p.get("break_at") and p["break_at"] <= moment:
            continue
        if float(p["top"]) >= price:
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


def ladder_at_index(
    candles15,
    bars15: list[dict],
    idx: int,
    cfg,
    cache: dict,
    price: float | None = None,
) -> dict:
    from analyze_xrp_ema200_cluster_transition_shadow_v1 import structural_lower_ladder

    bar = bars15[idx]
    decision = bar["close_time"]
    px = price if price is not None else float(bar["close"])
    pools, _, _ = __import__(
        "dashboard.research_charts.lld_research_kernel", fromlist=["scanner_pools_for_index"]
    ).scanner_pools_for_index(candles15, "15m", idx, cfg, cache=cache)
    lowers = active_lower_records(pools, decision, px)
    lad = structural_lower_ladder(px, lowers)
    ema = bar.get("ema200")
    close = bar.get("close")
    return {
        **lad,
        "close_at_decision": close,
        "ema200_at_decision": ema,
        "price_above_ema200": close > ema if ema and close else None,
        "decision_time": decision.isoformat(),
    }


def outcome_class(trade: dict, bars15: list[dict], by_open: dict) -> str:
    fh = trade.get("first_hit")
    if fh == "TP":
        return "TP_FIRST"
    if fh != "SL":
        return "OTHER"
    ot = _utc(datetime.fromisoformat(trade["entry_open"]))
    bar = by_open[ot]
    entry = float(trade["entry_price"])
    stop = float(trade["stop"])
    target = entry * (1.0 - MFE_THRESH / 100.0)
    from analyze_4h_lower_short_block_v1 import HORIZON_BARS

    end = min(len(bars15), bar["candle_index"] + 1 + HORIZON_BARS)
    for i in range(bar["candle_index"] + 1, end):
        b = bars15[i]
        if b["high"] >= stop:
            return "SL_FIRST"
        if b["low"] <= target:
            return "MOVE_041_THEN_SL"
    return "SL_FIRST"


def simulate_bull_episodes(
    bars15: list[dict],
    candles15,
    cfg,
    cache: dict,
) -> tuple[list[dict], dict[datetime, bool]]:
    """Bar-by-bar BULL_TRANSITION_ACTIVE; returns episodes + active flag at each bar close_time."""
    active_at: dict[datetime, bool] = {}
    episodes: list[dict] = []
    bull_active = False
    ep: dict | None = None

    cross_by_idx = {c["bar_index"]: c for c in detect_crosses(bars15, REPORT_TO) if c["cross_direction"] == "bullish"}

    for i, bar in enumerate(bars15):
        ct = bar["close_time"]
        if ct < REPORT_FROM or ct > REPORT_TO:
            continue
        ctx = ladder_at_index(candles15, bars15, i, cfg, cache)
        rising_3 = ctx["lower_cluster_rising_3"]
        above = ctx["price_above_ema200"] is True
        close = ctx["close_at_decision"]
        ema = ctx["ema200_at_decision"]

        is_bull_cross = i in cross_by_idx

        if not bull_active:
            if is_bull_cross and rising_3:
                bull_active = True
                ep = {
                    "start": ct.isoformat(),
                    "start_cross": cross_by_idx[i],
                    "ignored_shorts": [],
                }
        else:
            if not above or not rising_3:
                bull_active = False
                if ep:
                    ep["end"] = ct.isoformat()
                    ep["duration_hours"] = round(
                        (_utc(ct) - _utc(datetime.fromisoformat(ep["start"]))).total_seconds() / 3600.0,
                        2,
                    )
                    if not above:
                        ep["end_reason"] = "price_below_or_equal_ema200"
                    else:
                        ep["end_reason"] = "rising_3_false"
                    ep["end_close"] = close
                    ep["end_ema200"] = ema
                    ep["end_rising_3"] = rising_3
                    episodes.append(ep)
                    ep = None

        active_at[ct] = bull_active

    if bull_active and ep:
        ep["end"] = None
        ep["end_reason"] = "still_active_at_report_end"
        ep["duration_hours"] = round(
            (REPORT_TO - _utc(datetime.fromisoformat(ep["start"]))).total_seconds() / 3600.0,
            2,
        )
        episodes.append(ep)

    return episodes, active_at


def bull_active_at_decision(
    decision: datetime,
    bars15: list[dict],
    active_at: dict[datetime, bool],
    candles15,
    cfg,
    cache: dict,
    entry_price: float,
) -> dict:
    idx = last_closed_idx(bars15, decision)
    if idx is None:
        return {"bull_transition_active": False}
    bar = bars15[idx]
    ctx = ladder_at_index(candles15, bars15, idx, cfg, cache, price=entry_price)
    ep_flag = active_at.get(bar["close_time"], False)
    # At signal: must still satisfy continuation conditions
    cont = ctx["price_above_ema200"] is True and ctx["lower_cluster_rising_3"] is True
    active = ep_flag and cont
    return {
        "bull_transition_active": active,
        "episode_flag_at_bar": ep_flag,
        "continuation_ok": cont,
        **ctx,
    }


def apply_one_skip_cluster3(signals: list[dict], all_crosses: list[dict]) -> None:
    from analyze_xrp_ema200_cluster_transition_shadow_v1 import apply_cluster_shadow_flags

    apply_cluster_shadow_flags(signals, all_crosses)


def cohort_row(all_wg: list[dict], ignore_key: str | None) -> dict:
    if ignore_key:
        ignored_n = sum(1 for t in all_wg if t.get(ignore_key))
        trades = [t for t in all_wg if not t.get(ignore_key)]
    else:
        ignored_n = 0
        trades = all_wg
    oc = [t.get("outcome_class") or outcome_class(t, t["_bars15"], t["_by_open"]) for t in trades]
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
        "ignored_trades": ignored_n,
        "sl": sum(1 for t in trades if t.get("first_hit") == "SL"),
        "tp": sum(1 for t in trades if t.get("first_hit") == "TP"),
        "MOVE_041_THEN_SL": sum(1 for o in oc if o == "MOVE_041_THEN_SL"),
        "DIRECT_SL": sum(1 for o in oc if o == "SL_FIRST"),
        "median_mae": _median(maes),
        "median_mfe": _median(mfes),
        "max_sl_streak": best,
        "bad4_removed": sum(1 for t in all_wg if t.get("known_bad_4") and t.get(ignore_key)) if ignore_key else 0,
        "tp_first_lost": sum(
            1 for t in all_wg if t.get(ignore_key) and (t.get("outcome_class") == "TP_FIRST")
        )
        if ignore_key
        else 0,
    }


def strip_internal(s: dict) -> dict:
    return {k: v for k, v in s.items() if not k.startswith("_")}


def main() -> None:
    _ensure_paths()
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import (
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )
    from analyze_xrp_ema200_cluster_transition_shadow_v1 import detect_crosses as dc

    data = json.loads((ROOT / "floor_guard_signal_list_v1.json").read_text())
    sym = data["symbols"]["XRPUSDT"]
    without = sym["without_guard"]
    wg_opens = {t["entry_open"] for t in sym["with_guard"]}

    load_market(SYMBOL)
    _p, candles15 = load_pane_candles(
        SYMBOL, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    bars15 = build_15m_bars(candles15)
    by_open = {b["open_time"]: b for b in bars15}
    cfg = ui_lld_config("15m")
    scan_cache: dict = {}
    all_crosses = [
        c
        for c in dc(bars15, REPORT_TO)
        if REPORT_FROM <= _utc(datetime.fromisoformat(c["cross_time"])) <= REPORT_TO
    ]

    signals: list[dict] = []
    for t in without:
        ot = _utc(datetime.fromisoformat(t["entry_open"]))
        if ot not in by_open:
            continue
        bar = by_open[ot]
        decision = bar["close_time"]
        if decision < REPORT_FROM or decision > REPORT_TO:
            continue
        idx = bar["candle_index"]
        price = float(t["entry_price"])
        ctx = ladder_at_index(candles15, bars15, idx, cfg, scan_cache, price=price)
        signals.append(
            {
                **t,
                "signal_time": ot.isoformat(),
                "decision_time": decision.isoformat(),
                "decision_dt": decision,
                "bar_index": idx,
                "direction": "SHORT",
                "current_guard_allows": t["entry_open"] in wg_opens,
                "known_bad_4": t["entry_open"] in BAD_ENTRIES,
                "outcome_class": outcome_class(t, bars15, by_open),
                **{k: ctx[k] for k in ctx if k not in ("decision_time",)},
                "_bars15": bars15,
                "_by_open": by_open,
            }
        )

    apply_one_skip_cluster3(signals, all_crosses)

    episodes, active_at = simulate_bull_episodes(bars15, candles15, cfg, scan_cache)

    for s in signals:
        st = bull_active_at_decision(
            s["decision_dt"], bars15, active_at, candles15, cfg, scan_cache, float(s["entry_price"])
        )
        s["bull_transition_active"] = st["bull_transition_active"]
        s["shadow_cluster_3_persistent_ignore"] = st["bull_transition_active"]
        s["would_ignore_persistent"] = st["bull_transition_active"]
        s["price_at_decision"] = st["close_at_decision"]
        s["ema200_at_decision"] = st["ema200_at_decision"]
        s["price_above_ema200"] = st["price_above_ema200"]
        s["rising_3_at_decision"] = st["lower_cluster_rising_3"]

    # attach ignored shorts to episodes (with_guard only for counts)
    sig_by_open = {s["entry_open"]: s for s in signals}
    for ep in episodes:
        start = _utc(datetime.fromisoformat(ep["start"]))
        end = _utc(datetime.fromisoformat(ep["end"])) if ep.get("end") else REPORT_TO
        ignored = []
        for s in signals:
            if not s["current_guard_allows"]:
                continue
            dt = s["decision_dt"]
            if dt < start or dt > end:
                continue
            if s.get("shadow_cluster_3_persistent_ignore"):
                ignored.append(s)
        ep["ignored_shorts_guard"] = [strip_internal(x) for x in ignored]
        ep["ignored_count"] = len(ignored)
        ep["ignored_sl"] = sum(1 for x in ignored if x.get("first_hit") == "SL")
        ep["ignored_tp"] = sum(1 for x in ignored if x.get("outcome_class") == "TP_FIRST")

    wg = [s for s in signals if s["current_guard_allows"]]
    comparison = {
        "CURRENT": cohort_row(wg, None),
        "CLUSTER_3_ONE_SKIP": cohort_row(wg, "shadow_cluster_3_ignore"),
        "CLUSTER_3_PERSISTENT": cohort_row(wg, "shadow_cluster_3_persistent_ignore"),
    }

    june14_rows = []
    for eo in JUNE14_BAD:
        s = sig_by_open[eo]
        june14_rows.append(
            {
                "entry_open": eo,
                "decision_time": s["decision_time"],
                "price": s["price_at_decision"],
                "ema200": s["ema200_at_decision"],
                "price_above_ema200": s["price_above_ema200"],
                "rising_3": s["rising_3_at_decision"],
                "BULL_TRANSITION_ACTIVE": s["bull_transition_active"],
                "would_ignore": s["would_ignore_persistent"],
                "mae_pct": s.get("mae_pct"),
                "mfe_pct": s.get("mfe_pct"),
                "outcome_class": s.get("outcome_class"),
            }
        )

    j11 = sig_by_open["2026-06-11T19:15:00+00:00"]
    j11_ep = next((e for e in episodes if _utc(datetime.fromisoformat(e["start"])) <= j11["decision_dt"]), None)
    for e in episodes:
        st = _utc(datetime.fromisoformat(e["start"]))
        en = _utc(datetime.fromisoformat(e["end"])) if e.get("end") else REPORT_TO
        if st <= j11["decision_dt"] <= en:
            j11_ep = e
            break

    shorts_in_j11_ep = []
    if j11_ep:
        st = _utc(datetime.fromisoformat(j11_ep["start"]))
        en = _utc(datetime.fromisoformat(j11_ep["end"])) if j11_ep.get("end") else REPORT_TO
        shorts_in_j11_ep = [
            strip_internal(s)
            for s in signals
            if st <= s["decision_dt"] <= en and s["direction"] == "SHORT"
        ]

    long_during_bull = [s for s in signals if s.get("direction") == "LONG"]  # none expected

    ep_durations = [e["duration_hours"] for e in episodes if e.get("duration_hours") is not None]
    ep_with_ignores = [e for e in episodes if e.get("ignored_count", 0) > 0]
    episode_summary = {
        "total_episodes": len(episodes),
        "episodes_with_ignored_guard_shorts": len(ep_with_ignores),
        "median_duration_hours": _median(ep_durations),
        "max_duration_hours": max(ep_durations) if ep_durations else None,
        "total_ignored_shorts_in_episodes": sum(e.get("ignored_count", 0) for e in episodes),
    }

    verdict = {
        "q1_one_skip_too_early": (
            "Ja für die 14.06.-Streak: ONE_SKIP blockiert nur 21:45; 22:30/23:30 laufen weiter, "
            "obwohl BULL_TRANSITION zwischen den drei Decision-Zeitpunkten durchgehend aktiv bleibt."
        ),
        "q2_persistent_bad4": f"Persistent entfernt {comparison['CLUSTER_3_PERSISTENT']['bad4_removed']}/4 BAD-4 vs ONE_SKIP {comparison['CLUSTER_3_ONE_SKIP']['bad4_removed']}/4.",
        "q3_extra_tp_cost": (
            f"Zusätzliche TP_FIRST-Kosten vs ONE_SKIP: "
            f"{comparison['CLUSTER_3_PERSISTENT']['tp_first_lost'] - comparison['CLUSTER_3_ONE_SKIP']['tp_first_lost']} "
            f"(persistent {comparison['CLUSTER_3_PERSISTENT']['tp_first_lost']} vs one-skip {comparison['CLUSTER_3_ONE_SKIP']['tp_first_lost']})."
        ),
        "q4_cross_coin": "Reset-Regel (EMA oder rising_3) ist einfach; Episodenlängen vor Cross-Coin prüfen.",
    }

    report = {
        "meta": {
            "analysis_only": True,
            "variant": "CLUSTER_3_PERSISTENT",
            "cluster_gap_pct": CLUSTER_GAP_PCT,
            "reset": "price<=EMA200 OR rising_3 false at closed 15m bar",
        },
        "coverage": f"{len(signals)} shorts; {len(wg)} with_guard.",
        "comparison": comparison,
        "june_11": {
            "short": strip_internal(j11),
            "episode": j11_ep,
            "shorts_in_episode": shorts_in_j11_ep,
        },
        "june_14_bad_streak": june14_rows,
        "transition_episodes": episodes,
        "episode_summary": episode_summary,
        "long_signals_during_bull": long_during_bull,
        "long_signals_note": "Keine Long-Signale im XRP floor_guard_signal_list Universum.",
        "bear_mirror": "BEAR_TRANSITION_ACTIVE code path mirrored in module doc; no long data.",
        "verdict": verdict,
        "signals": [strip_internal(s) for s in signals],
    }

    md = build_md(report)
    report["executive_summary"] = md
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    OUT_MD.write_text(md + "\n")

    fields = [
        "signal_time",
        "decision_time",
        "entry_open",
        "entry_price",
        "current_guard_allows",
        "known_bad_4",
        "price_above_ema200",
        "rising_3_at_decision",
        "bull_transition_active",
        "shadow_cluster_3_ignore",
        "shadow_cluster_3_persistent_ignore",
        "outcome_class",
        "mae_pct",
        "mfe_pct",
        "first_hit",
    ]
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for s in report["signals"]:
            w.writerow(s)

    print("wrote", OUT_JSON, OUT_MD, OUT_CSV)


def build_md(report: dict) -> str:
    c = report["comparison"]
    j11 = report["june_11"]
    ep = j11.get("episode") or {}
    lines = [
        "# XRP CLUSTER_3 Persistent EMA200 Shadow v1",
        "",
        "Nur Shadow-Analyse. Keine Produktionsänderung.",
        "",
        "### 11 June",
        f"Episode start: {ep.get('start')}; end: {ep.get('end')}; reason: {ep.get('end_reason')}; "
        f"duration_h: {ep.get('duration_hours')}; shorts in episode: {len(j11.get('shorts_in_episode', []))}.",
        "",
        "### 14 June BAD streak",
    ]
    for row in report["june_14_bad_streak"]:
        lines.append(
            f"- {row['entry_open']}: above_ema={row['price_above_ema200']} rising_3={row['rising_3']} "
            f"active={row['BULL_TRANSITION_ACTIVE']} ignore={row['would_ignore']} → {row['outcome_class']}"
        )
    all3 = all(r["would_ignore"] for r in report["june_14_bad_streak"])
    lines.append(f"Alle drei blockiert (persistent): **{all3}**")
    lines += [
        "",
        "### BAD-4 impact",
        f"ONE_SKIP: {c['CLUSTER_3_ONE_SKIP']['bad4_removed']}/4; PERSISTENT: {c['CLUSTER_3_PERSISTENT']['bad4_removed']}/4.",
        "",
        "### Lost TP trades",
        f"ONE_SKIP TP_FIRST lost: {c['CLUSTER_3_ONE_SKIP']['tp_first_lost']}; "
        f"PERSISTENT: {c['CLUSTER_3_PERSISTENT']['tp_first_lost']}.",
        "",
        "### Current vs One-Skip vs Persistent",
        "| | CURRENT | ONE_SKIP | PERSISTENT |",
        "|---|--:|--:|--:|",
    ]
    for k in (
        "allowed_trades",
        "ignored_trades",
        "sl",
        "tp",
        "MOVE_041_THEN_SL",
        "DIRECT_SL",
        "median_mae",
        "median_mfe",
        "max_sl_streak",
    ):
        lines.append(
            f"| {k} | {c['CURRENT'][k]} | {c['CLUSTER_3_ONE_SKIP'][k]} | {c['CLUSTER_3_PERSISTENT'][k]} |"
        )
    lines += [
        "",
        "### Transition episodes",
        json.dumps(report.get("episode_summary", {}), indent=2),
        "",
        "### MAE / MFE",
        f"CURRENT MAE {c['CURRENT']['median_mae']} MFE {c['CURRENT']['median_mfe']}; "
        f"ONE_SKIP MAE {c['CLUSTER_3_ONE_SKIP']['median_mae']} MFE {c['CLUSTER_3_ONE_SKIP']['median_mfe']}; "
        f"PERSISTENT MAE {c['CLUSTER_3_PERSISTENT']['median_mae']} MFE {c['CLUSTER_3_PERSISTENT']['median_mfe']}.",
        "",
        "### Verdict",
        "1. " + report["verdict"]["q1_one_skip_too_early"],
        "2. " + report["verdict"]["q2_persistent_bad4"],
        "3. " + report["verdict"]["q3_extra_tp_cost"],
        "4. " + report["verdict"]["q4_cross_coin"],
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()

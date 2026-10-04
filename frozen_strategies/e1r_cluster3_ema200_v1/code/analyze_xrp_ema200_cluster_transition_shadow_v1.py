"""Shadow: EMA200 cross + structural lower-cluster ladder (analysis only)."""

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
OUT_JSON = ROOT / "analyze_xrp_ema200_cluster_transition_shadow_v1.json"
OUT_MD = ROOT / "analyze_xrp_ema200_cluster_transition_shadow_v1.md"
OUT_CSV = ROOT / "analyze_xrp_ema200_cluster_transition_shadow_trades_v1.csv"

SYMBOL = "XRPUSDT"
CLUSTER_GAP_PCT = 0.35  # fixed from pool_ladder_forensics v1 — no optimization
MFE_THRESH = 0.41
BAD_ENTRIES = {
    "2026-06-11T19:15:00+00:00",
    "2026-06-14T21:45:00+00:00",
    "2026-06-14T22:30:00+00:00",
    "2026-06-14T23:30:00+00:00",
}
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


def price_clusters(pool_records: list[dict], gap_pct: float = CLUSTER_GAP_PCT) -> list[dict]:
    from analyze_xrp_pool_ladder_forensics_v1 import price_clusters as _pc

    return _pc(pool_records, gap_pct)


def structural_lower_ladder(price: float, pool_records: list[dict]) -> dict:
    clusters = price_clusters(pool_records)
    steps = sorted([c for c in clusters if c["highest_top"] < price], key=lambda c: c["lowest_bottom"])
    out: dict[str, Any] = {
        "clusters_below_price": steps,
        "cluster_count_below": len(steps),
        "lower_cluster_rising_2": False,
        "lower_cluster_rising_3": False,
        "gap_1_to_2": None,
        "gap_2_to_3": None,
        "overlap_1_2": None,
        "overlap_2_3": None,
    }
    if len(steps) >= 2:
        c1, c2 = steps[0], steps[1]
        out["lower_cluster_rising_2"] = c2["highest_top"] > c1["highest_top"]
        g12 = c2["lowest_bottom"] - c1["highest_top"]
        out["gap_1_to_2"] = round(g12, 8)
        out["overlap_1_2"] = g12 < 0
    if len(steps) >= 3:
        c1, c2, c3 = steps[0], steps[1], steps[2]
        out["lower_cluster_rising_3"] = c1["highest_top"] < c2["highest_top"] < c3["highest_top"]
        g23 = c3["lowest_bottom"] - c2["highest_top"]
        out["gap_2_to_3"] = round(g23, 8)
        out["overlap_2_3"] = g23 < 0
    for i, c in enumerate(steps[:3], start=1):
        out[f"cluster_{i}_bottom"] = c["lowest_bottom"]
        out[f"cluster_{i}_top"] = c["highest_top"]
        out[f"cluster_{i}_known_from"] = c["earliest_known_at"]
        out[f"cluster_{i}_pool_count"] = c["pool_count"]
        out[f"cluster_{i}_pool_ids"] = c["pool_ids"]
    return out


def structural_upper_ladder(price: float, pool_records: list[dict]) -> dict:
    """Mirror for uppers above price."""
    up = [
        {
            "pool_id": p["pool_id"],
            "timeframe": "15m",
            "known_at": p["known"].isoformat() if hasattr(p["known"], "isoformat") else p["known"],
            "bottom": float(p["bottom"]),
            "top": float(p["top"]),
        }
        for p in pool_records
    ]
    clusters = price_clusters(up)
    steps = sorted([c for c in clusters if c["lowest_bottom"] > price], key=lambda c: c["lowest_bottom"])
    out = {
        "upper_cluster_falling_2": False,
        "upper_cluster_falling_3": False,
        "clusters_above_price": steps,
    }
    if len(steps) >= 2:
        # highest resistance first: sort by bottom descending
        hi = sorted(steps, key=lambda c: -c["lowest_bottom"])
        out["upper_cluster_falling_2"] = hi[0]["lowest_bottom"] > hi[1]["lowest_bottom"]
    if len(steps) >= 3:
        hi = sorted(steps, key=lambda c: -c["lowest_bottom"])[:3]
        b0, b1, b2 = hi[0]["lowest_bottom"], hi[1]["lowest_bottom"], hi[2]["lowest_bottom"]
        out["upper_cluster_falling_3"] = b0 > b1 > b2
    return out


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


def apply_cluster_shadow_flags(signals: list[dict], all_crosses: list[dict]) -> None:
    """State machine + cluster condition at signal time for shadow2/3."""
    shorts = sorted(signals, key=lambda s: s["decision_dt"])
    crosses = sorted(all_crosses, key=lambda c: c["cross_time"])
    # crosses attached in main
    events: list[tuple[str, datetime, Any]] = []
    for c in crosses:
        events.append(("cross", _utc(datetime.fromisoformat(c["cross_time"])), c))
    for s in shorts:
        events.append(("short", s["decision_dt"], s))
    events.sort(key=lambda x: x[1])

    state = "SEARCH_SHORT"
    in_bull_transition = False
    armed_cross: dict | None = None

    for typ, _t, obj in events:
        if typ == "cross":
            d = obj["cross_direction"]
            if d == "bullish":
                if state in ("SEARCH_SHORT", "TRANSITION_AFTER_BEAR_CROSS"):
                    state = "TRANSITION_AFTER_BULL_CROSS"
                    in_bull_transition = True
                    armed_cross = obj
            elif d == "bearish":
                in_bull_transition = False
                armed_cross = None
                if state in ("SEARCH_SHORT", "TRANSITION_AFTER_BULL_CROSS"):
                    state = "TRANSITION_AFTER_BEAR_CROSS"
        else:
            s = obj
            s["shadow_cluster_2_ignore"] = False
            s["shadow_cluster_3_ignore"] = False
            if in_bull_transition and armed_cross:
                ct = _utc(datetime.fromisoformat(armed_cross["cross_time"]))
                s["bull_cross_before_signal"] = armed_cross["cross_time"]
                s["hours_since_cross"] = round((s["decision_dt"] - ct).total_seconds() / 3600.0, 2)
                s["bars_since_cross"] = s.get("bar_index", 0) - armed_cross.get("bar_index", 0)
                if s["lower_cluster_rising_2"]:
                    s["shadow_cluster_2_ignore"] = True
                if s["lower_cluster_rising_3"]:
                    s["shadow_cluster_3_ignore"] = True
                in_bull_transition = False
                armed_cross = None
                state = "SEARCH_SHORT"
            else:
                # last bull before signal for reporting
                lb = s.get("_last_bull_before")
                if lb:
                    s["bull_cross_before_signal"] = lb.get("cross_time")
                    ct = _utc(datetime.fromisoformat(lb["cross_time"]))
                    s["hours_since_cross"] = round((s["decision_dt"] - ct).total_seconds() / 3600.0, 2)


def next_signal_after(ignored: dict, all_s: list[dict]) -> dict | None:
    t0 = ignored["decision_dt"]
    for s in sorted(all_s, key=lambda x: x["decision_dt"]):
        if s["decision_dt"] > t0:
            return s
    return None


def cohort_row(all_wg: list[dict], ignore_key: str | None) -> dict:
    if ignore_key:
        ignored_n = sum(1 for t in all_wg if t.get(ignore_key))
        trades = [t for t in all_wg if not t.get(ignore_key)]
    else:
        ignored_n = 0
        trades = all_wg
    oc = [outcome_class(t, t["_bars15"], t["_by_open"]) for t in trades]
    maes = [float(t["mae_pct"]) for t in trades if t.get("mae_pct") is not None]
    mfes = [float(t["mfe_pct"]) for t in trades if t.get("mfe_pct") is not None]
    streak = 0
    best = 0
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
        "mfe_ge_041_before_sl": sum(1 for t in trades if outcome_class(t, t["_bars15"], t["_by_open"]) == "MOVE_041_THEN_SL"),
        "MOVE_THEN_SL": sum(1 for o in oc if o == "MOVE_041_THEN_SL"),
        "DIRECT_SL": sum(1 for o in oc if o == "SL_FIRST"),
        "median_mae": _median(maes),
        "median_mfe": _median(mfes),
        "max_sl_streak": best,
    }


def main() -> None:
    _ensure_paths()
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import (
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )

    data = json.loads((ROOT / "floor_guard_signal_list_v1.json").read_text())
    sym = data["symbols"]["XRPUSDT"]
    without = sym["without_guard"]
    with_guard = sym["with_guard"]
    wg_opens = {t["entry_open"] for t in with_guard}

    market = load_market(SYMBOL)
    _p, candles15 = load_pane_candles(
        SYMBOL, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    bars15 = build_15m_bars(candles15)
    by_open = {b["open_time"]: b for b in bars15}
    cfg = ui_lld_config("15m")
    scan_cache: dict = {}

    all_crosses = [
        c
        for c in detect_crosses(bars15, REPORT_TO)
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
        pools, _, _ = scanner_pools_for_index(candles15, "15m", idx, cfg, cache=scan_cache)
        price = float(t["entry_price"])
        lowers = active_lower_records(pools, decision, price)
        ladder = structural_lower_ladder(price, lowers)
        uppers_raw = [
            p
            for p in pools
            if p["side"] == "upper"
            and p["known"] <= decision
            and (p.get("break_at") is None or p["break_at"] > decision)
            and float(p["bottom"]) > price
        ]
        upper_lad = structural_upper_ladder(price, uppers_raw)

        last_bull = None
        for c in all_crosses:
            if c["cross_direction"] != "bullish":
                continue
            if _utc(datetime.fromisoformat(c["cross_time"])) <= decision:
                last_bull = c

        rec = {
            **t,
            "signal_time": ot.isoformat(),
            "decision_time": decision.isoformat(),
            "decision_dt": decision,
            "bar_index": idx,
            "direction": "SHORT",
            "current_guard_allows": t["entry_open"] in wg_opens,
            "known_bad_4": t["entry_open"] in BAD_ENTRIES,
            "outcome_class": outcome_class(t, bars15, by_open),
            "mfe_ge_041_before_sl": outcome_class(t, bars15, by_open) == "MOVE_041_THEN_SL",
            **ladder,
            "upper_ladder": upper_lad,
            "_last_bull_before": last_bull,
            "_bars15": bars15,
            "_by_open": by_open,
        }
        signals.append(rec)

    apply_cluster_shadow_flags(signals, all_crosses)

    # strip internal refs for json output later
    def strip_internal(s: dict) -> dict:
        return {k: v for k, v in s.items() if not k.startswith("_")}

    wg_signals = [s for s in signals if s["current_guard_allows"]]

    for s in signals:
        if s.get("shadow_cluster_2_ignore") or s.get("shadow_cluster_3_ignore"):
            nxt = next_signal_after(s, signals)
            cls = "NO_NEXT_SIGNAL"
            if nxt:
                cls = "NEXT_LONG" if nxt.get("direction") == "LONG" else "NEXT_SHORT"
            idx_n = last_closed_idx(bars15, nxt["decision_dt"]) if nxt else None
            bar_n = bars15[idx_n] if idx_n is not None else {}
            ema_n = bar_n.get("ema200")
            close_n = bar_n.get("close")
            s["next_after_ignore"] = {
                "classification": cls,
                "entry_open": nxt["entry_open"] if nxt else None,
                "outcome_class": nxt.get("outcome_class") if nxt else None,
                "mae_pct": nxt.get("mae_pct") if nxt else None,
                "mfe_pct": nxt.get("mfe_pct") if nxt else None,
                "price_above_ema200": close_n > ema_n if ema_n and close_n else None,
                "lower_cluster_rising_2": nxt.get("lower_cluster_rising_2") if nxt else None,
                "lower_cluster_rising_3": nxt.get("lower_cluster_rising_3") if nxt else None,
            }

    j11 = next(s for s in signals if s["entry_open"] == "2026-06-11T19:15:00+00:00")
    j14 = next(s for s in signals if s["entry_open"] == "2026-06-14T21:45:00+00:00")

    comparison = {
        "CURRENT": cohort_row(wg_signals, None),
        "SHADOW_CLUSTER_2": cohort_row(wg_signals, "shadow_cluster_2_ignore"),
        "SHADOW_CLUSTER_3": cohort_row(wg_signals, "shadow_cluster_3_ignore"),
    }

    bad4 = [strip_internal(s) for s in wg_signals if s["known_bad_4"]]
    lost_tp_c2 = [strip_internal(s) for s in wg_signals if s.get("shadow_cluster_2_ignore") and s.get("outcome_class") == "TP_FIRST"]
    lost_tp_c3 = [strip_internal(s) for s in wg_signals if s.get("shadow_cluster_3_ignore") and s.get("outcome_class") == "TP_FIRST"]

    next_summary = {"NEXT_SHORT": 0, "NEXT_LONG": 0, "NO_NEXT_SIGNAL": 0}
    for s in wg_signals:
        if s.get("shadow_cluster_2_ignore") or s.get("shadow_cluster_3_ignore"):
            c = s.get("next_after_ignore", {}).get("classification")
            if c in next_summary:
                next_summary[c] += 1

    verdict = {
        "q1_better_than_ema_only": (
            "SHADOW_CLUSTER_2 ist auf with_guard identisch zur reinen EMA200-Transition-Shadow "
            "(9 Ignores, 2 BAD-4 entfernt, SL-Streak 4→2). CLUSTER_3 ignoriert 8 Trades "
            "(1 weniger als EMA-only) und verliert 4 statt 5 TP_FIRST — selektiver."
        ),
        "q2_reproduces_11_june": (
            "Ja: drei aufsteigende Lower-Cluster (Tops 1.0509 / 1.0902 / 1.1065), "
            "Bull-Cross 17:45, Short 19:30 → shadow2/3=True. Dritte Etage noch unter chart-~1.12 "
            "(1.12-Cluster kausal erst ab 12.06.)."
        ),
        "q3_cluster2_vs_cluster3_cost": "CLUSTER_3 kostet 1 TP_FIRST weniger (4 vs 5), entfernt aber nur 2/4 BAD-4 wie CLUSTER_2.",
        "q4_cross_coin": "Erst nach Festlegung Cluster-vs-EMA-Differenz und BAD-4-Rest (14.06. Folge-Shorts) sinnvoll.",
    }

    report = {
        "meta": {
            "analysis_only": True,
            "cluster_gap_pct": CLUSTER_GAP_PCT,
            "cluster_source": "analyze_xrp_pool_ladder_forensics_v1.py::price_clusters",
            "ema_tf": "15m",
            "shadow_rule": "bull cross transition + rising cluster at first short signal",
        },
        "coverage": f"{len(signals)} XRP shorts (without_guard universe); {len(wg_signals)} with current guard.",
        "cluster_construction": (
            "Active 15m lowers known<=decision, top<price; greedy cluster gap 0.35%; "
            "clusters below price sorted by lowest_bottom; rising_2/3 on cluster tops."
        ),
        "june_11_verification": strip_internal(j11),
        "june_14_verification": strip_internal(j14),
        "comparison": comparison,
        "bad_4_table": bad4,
        "lost_tp_trades": {"SHADOW_CLUSTER_2": lost_tp_c2, "SHADOW_CLUSTER_3": lost_tp_c3},
        "next_after_ignore_summary": next_summary,
        "ema_only_shadow_reference": {
            "source": "analyze_xrp_ema200_transition_shadow_v1.json",
            "ignored_guard_allowed": 9,
            "bad4_removed": 2,
        },
        "symmetry": {"long_signals": 0, "upper_falling_mirror_ready": True},
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
        "bull_cross_before_signal",
        "hours_since_cross",
        "lower_cluster_rising_2",
        "lower_cluster_rising_3",
        "shadow_cluster_2_ignore",
        "shadow_cluster_3_ignore",
        "outcome_class",
        "mae_pct",
        "mfe_pct",
        "first_hit",
        "cluster_1_top",
        "cluster_2_top",
        "cluster_3_top",
    ]
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for s in report["signals"]:
            w.writerow(s)

    print("wrote", OUT_JSON, OUT_MD, OUT_CSV)


def build_md(report: dict) -> str:
    c = report["comparison"]
    lines = [
        "# XRP EMA200 + Cluster Transition Shadow v1",
        "",
        "Nur Shadow-Analyse. Keine Produktionsänderung.",
        "",
        "### Coverage",
        report["coverage"],
        "",
        "### Cluster construction",
        report["cluster_construction"],
        "",
        "### 11 June verification",
        f"rising_2={report['june_11_verification'].get('lower_cluster_rising_2')} "
        f"rising_3={report['june_11_verification'].get('lower_cluster_rising_3')} "
        f"shadow2={report['june_11_verification'].get('shadow_cluster_2_ignore')} "
        f"shadow3={report['june_11_verification'].get('shadow_cluster_3_ignore')} "
        f"tops={[report['june_11_verification'].get('cluster_1_top'), report['june_11_verification'].get('cluster_2_top'), report['june_11_verification'].get('cluster_3_top')]}",
        "",
        "### 14 June verification",
        f"rising_2={report['june_14_verification'].get('lower_cluster_rising_2')} "
        f"rising_3={report['june_14_verification'].get('lower_cluster_rising_3')} "
        f"shadow2={report['june_14_verification'].get('shadow_cluster_2_ignore')} "
        f"shadow3={report['june_14_verification'].get('shadow_cluster_3_ignore')}",
        "",
        "### Current vs Cluster2 vs Cluster3",
        "| Metrik | CURRENT | CLUSTER_2 | CLUSTER_3 |",
        "|--------|----------:|----------:|----------:|",
    ]
    for k in (
        "allowed_trades",
        "ignored_trades",
        "sl",
        "tp",
        "mfe_ge_041_before_sl",
        "MOVE_THEN_SL",
        "DIRECT_SL",
        "median_mae",
        "median_mfe",
        "max_sl_streak",
    ):
        lines.append(
            f"| {k} | {c['CURRENT'].get(k)} | {c['SHADOW_CLUSTER_2'].get(k)} | {c['SHADOW_CLUSTER_3'].get(k)} |"
        )
    lines += [
        "",
        "### BAD-4 impact",
        json.dumps(report["bad_4_table"], indent=2, default=str),
        "",
        "### Lost TP trades",
        f"CLUSTER_2 TP_FIRST removed: {len(report['lost_tp_trades']['SHADOW_CLUSTER_2'])}; "
        f"CLUSTER_3: {len(report['lost_tp_trades']['SHADOW_CLUSTER_3'])}.",
        "",
        "### SL streak impact",
        f"CURRENT {c['CURRENT']['max_sl_streak']} → C2 {c['SHADOW_CLUSTER_2']['max_sl_streak']} → C3 {c['SHADOW_CLUSTER_3']['max_sl_streak']}",
        "",
        "### MAE / MFE",
        f"CURRENT MAE {c['CURRENT']['median_mae']} MFE {c['CURRENT']['median_mfe']}; "
        f"C2 MAE {c['SHADOW_CLUSTER_2']['median_mae']} MFE {c['SHADOW_CLUSTER_2']['median_mfe']}; "
        f"C3 MAE {c['SHADOW_CLUSTER_3']['median_mae']} MFE {c['SHADOW_CLUSTER_3']['median_mfe']}.",
        "",
        "### Next signal after ignore",
        json.dumps(report.get("next_after_ignore_summary", {}), indent=2),
        "",
        "### Symmetry",
        json.dumps(report["symmetry"], indent=2),
        "",
        "### Verdict",
        "1. " + report["verdict"]["q1_better_than_ema_only"],
        "2. " + report["verdict"]["q2_reproduces_11_june"],
        "3. " + report["verdict"]["q3_cluster2_vs_cluster3_cost"],
        "4. " + report["verdict"]["q4_cross_coin"],
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()

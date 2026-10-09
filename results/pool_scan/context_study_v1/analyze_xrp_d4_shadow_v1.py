"""Shadow analysis: D4 (3L/3R 4h pivot LL) vs current floor guard — XRP only, no production changes."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OUT_JSON = ROOT / "analyze_xrp_d4_shadow_v1.json"
OUT_MD = ROOT / "analyze_xrp_d4_shadow_v1.md"
OUT_CSV = ROOT / "analyze_xrp_d4_shadow_trades_v1.csv"

SYMBOL = "XRPUSDT"
BAD_ENTRIES = {
    "2026-06-11T19:15:00+00:00",
    "2026-06-14T21:45:00+00:00",
    "2026-06-14T22:30:00+00:00",
    "2026-06-14T23:30:00+00:00",
}
MFE_GOOD_THRESH = 0.41
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
D4_LEFT = 3
D4_RIGHT = 3
DELAY_HOURS = (4, 8, 12, 24)


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


def _mean(vals: list[float]) -> float | None:
    return round(statistics.mean(vals), 4) if vals else None


def good_clean(
    bars: list[dict],
    entry_idx: int,
    entry: float,
    stop: float,
    tp: float,
    first_hit: str,
    mfe_pct: float,
) -> bool:
    from analyze_4h_lower_short_block_v1 import HORIZON_BARS

    if first_hit == "TP":
        return True
    target = entry * (1.0 - MFE_GOOD_THRESH / 100.0)
    end = min(len(bars), entry_idx + 1 + HORIZON_BARS)
    for i in range(entry_idx + 1, end):
        bar = bars[i]
        if bar["high"] >= stop:
            return False
        if bar["low"] <= target:
            return True
        if bar["low"] <= tp:
            return True
    return first_hit != "SL" and mfe_pct >= MFE_GOOD_THRESH


def d4_detail(bars: list[dict], end_idx: int | None, decision_time: datetime) -> dict:
    from analyze_xrp_htf_structural_break_v1 import confirmed_pivot_lows, d1_style_ll

    if end_idx is None:
        return {
            "pivot_low_previous": None,
            "pivot_low_latest": None,
            "pivot_previous_time": None,
            "pivot_latest_time": None,
            "pivot_latest_confirmed_at": None,
            "D4_available_at_entry": False,
            "D4_bearish_ll": False,
        }
    pivots = confirmed_pivot_lows(bars, end_idx, D4_LEFT, D4_RIGHT)
    prev_p = pivots[-2] if len(pivots) >= 2 else None
    latest_p = pivots[-1] if len(pivots) >= 1 else None
    bearish = d1_style_ll(pivots)
    confirmed_at = None
    available = False
    if latest_p:
        confirmed_at = latest_p["known_at_close"]
        available = _utc(datetime.fromisoformat(confirmed_at)) <= decision_time
    return {
        "pivot_low_previous": prev_p["price"] if prev_p else None,
        "pivot_low_latest": latest_p["price"] if latest_p else None,
        "pivot_previous_time": prev_p["known_at_close"] if prev_p else None,
        "pivot_latest_time": latest_p["bar_open"] if latest_p else None,
        "pivot_latest_confirmed_at": confirmed_at,
        "D4_available_at_entry": available and len(pivots) >= 2,
        "D4_bearish_ll": bool(bearish and available),
        "pivot_low_previous_full": prev_p,
        "pivot_low_latest_full": latest_p,
    }


def scan_first_d4_after(
    bars_4h: list[dict],
    after_moment: datetime,
    max_moment: datetime | None = None,
) -> dict | None:
    """First causal D4 bearish LL whose latest pivot confirms after `after_moment`."""
    for end_idx, bar in enumerate(bars_4h):
        ct = bar["close_time"]
        if ct <= after_moment:
            continue
        if max_moment and ct > max_moment:
            break
        d4 = d4_detail(bars_4h, end_idx, ct)
        if d4["D4_bearish_ll"]:
            return {
                "confirmed_at": d4["pivot_latest_confirmed_at"],
                "decision_4h_close": ct.isoformat(),
                "pivot_low_previous": d4["pivot_low_previous"],
                "pivot_low_latest": d4["pivot_low_latest"],
                "detail": d4,
            }
    return None


def mfe_mae_from_idx(bars: list[dict], entry_idx: int, entry: float, stop: float, tp: float) -> dict:
    from analyze_xrp_floor_guard_pnl_v1 import short_trade_detail

    d = short_trade_detail(bars, entry_idx, entry, stop, tp)
    return {
        "mae_pct": d.get("mae_pct"),
        "mfe_pct": d.get("mfe_pct"),
        "first_hit": d.get("first_hit"),
        "outcome": d.get("outcome"),
    }


def pool_metrics(symbol: str, moment: datetime, price: float, candles15, idx15: int, candles30, idx30: int) -> dict:
    from analyze_xrp_htf_structural_break_v1 import pool_context_at_entry

    ctx = pool_context_at_entry(symbol, moment, price, candles15, idx15, candles30, idx30)
    lo = ctx.get("below_4h_support_nearest") or {}
    up = ctx.get("above_30m_upper_nearest") or {}
    lo_d = lo.get("dist_pct")
    up_d = up.get("dist_pct")
    ratio = round(up_d / lo_d, 4) if lo_d and up_d and lo_d > 0 else None
    return {
        "nearest_4h_support_dist_pct": lo_d,
        "nearest_30m_upper_dist_pct": up_d,
        "upper_over_lower_support_ratio": ratio,
    }


def cohort_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    maes = [float(t["mae_pct"]) for t in trades if t.get("mae_pct") is not None]
    mfes = [float(t["mfe_pct"]) for t in trades if t.get("mfe_pct") is not None]
    return {
        "n": len(trades),
        "sl": sum(1 for t in trades if t.get("first_hit") == "SL"),
        "tp": sum(1 for t in trades if t.get("first_hit") == "TP"),
        "mfe_ge_041": sum(1 for t in trades if float(t.get("mfe_pct") or 0) >= MFE_GOOD_THRESH),
        "mfe_lt_041": sum(1 for t in trades if float(t.get("mfe_pct") or 0) < MFE_GOOD_THRESH),
        "median_mae": _median(maes),
        "median_mfe": _median(mfes),
        "mean_mae": _mean(maes),
        "mean_mfe": _mean(mfes),
        "good_clean": sum(1 for t in trades if t.get("good_clean")),
    }


def analyze_trade(
    trade: dict,
    guard_blocked: bool,
    market: dict,
    bars15: list[dict],
    bars15_by_open: dict,
    candles15,
    candles30,
    bars30_idx: dict[datetime, int],
) -> dict:
    from analyze_xrp_htf_structural_break_v1 import (
        d1_current_feature,
        d2_close_below_prior_pivot_low,
        d3_close_below_last_hl,
        d4_multi_pivot,
        d5_4h_plus_30m,
        last_closed_idx,
    )

    entry_open = _utc(datetime.fromisoformat(trade["entry_open"]))
    bar15 = bars15_by_open[entry_open]
    decision_time = bar15["close_time"]
    entry_idx = bar15["candle_index"]
    entry_price = float(trade["entry_price"])
    stop = float(trade["stop"])
    tp = float(trade["tp"])
    b4 = market["4h"]["bars"]
    b30 = market["30m"]["bars"]
    e4 = last_closed_idx(b4, decision_time)
    e30 = last_closed_idx(b30, decision_time)

    d4 = d4_detail(b4, e4, decision_time)
    d1 = d1_current_feature(b4, e4)
    d2 = d2_close_below_prior_pivot_low(b4, e4, 1, 1)
    d3 = d3_close_below_last_hl(b4, e4, 1, 1)
    d4m = d4_multi_pivot(b4, e4)
    d5 = d5_4h_plus_30m(b4, b30, e4, e30)

    shadow_d4_allows = d4["D4_bearish_ll"]
    gc = good_clean(bars15, entry_idx, entry_price, stop, tp, trade.get("first_hit", ""), float(trade.get("mfe_pct") or 0))

    # Delay / first D4 after entry
    first_d4 = scan_first_d4_after(b4, decision_time, None)
    delay: dict[str, Any] = {}
    for h in DELAY_HOURS:
        win_end = decision_time + timedelta(hours=h)
        hit = scan_first_d4_after(b4, decision_time, win_end)
        delay[f"d4_within_{h}h"] = hit is not None
    delayed_entry: dict | None = None
    if first_d4 and first_d4.get("confirmed_at"):
        t_conf = _utc(datetime.fromisoformat(first_d4["confirmed_at"]))
        hours_after = round((t_conf - decision_time).total_seconds() / 3600.0, 2)
        d4_after_entry = t_conf > decision_time
        # 15m price at confirmation
        d_idx = last_closed_idx(bars15, t_conf)
        d_price = float(bars15[d_idx]["close"]) if d_idx is not None else None
        hypo = None
        if d_idx is not None and d_price is not None:
            hypo = mfe_mae_from_idx(bars15, d_idx, d_price, stop, tp)
            sl_avoided = hypo["first_hit"] != "SL"
        else:
            sl_avoided = None
        delayed_entry = {
            "first_d4_confirmed_at": first_d4["confirmed_at"],
            "hours_after_original_entry": hours_after,
            "d4_only_after_entry": d4_after_entry,
            "hypothetical_entry_price_15m_close": d_price,
            "mae_mfe_from_delayed_entry": hypo,
            "original_sl_avoided_at_delayed_entry": sl_avoided,
        }

    i30 = bars30_idx.get(decision_time, 0)
    pools = pool_metrics(SYMBOL, decision_time, entry_price, candles15, entry_idx, candles30, i30)

    if guard_blocked:
        abcd = "C" if shadow_d4_allows else "D"
    else:
        abcd = "A" if shadow_d4_allows else "B"

    return {
        "signal_time": entry_open.isoformat(),
        "decision_time": decision_time.isoformat(),
        "entry_open": trade["entry_open"],
        "entry_price": entry_price,
        "guard_blocked": guard_blocked,
        "shadow_d4_allows_short": shadow_d4_allows,
        "shadow_group": abcd,
        "stop": stop,
        "tp": tp,
        "mae_pct": trade.get("mae_pct"),
        "mfe_pct": trade.get("mfe_pct"),
        "first_hit": trade.get("first_hit"),
        "outcome": trade.get("outcome"),
        "pnl_pct": trade.get("pnl_pct"),
        "good_041": float(trade.get("mfe_pct") or 0) >= MFE_GOOD_THRESH,
        "good_tp": trade.get("first_hit") == "TP",
        "good_clean": gc,
        "known_bad_4": trade["entry_open"] in BAD_ENTRIES,
        "D1": d1["bearish_break_at_entry"],
        "D2": d2["bearish_break_at_entry"],
        "D3": d3["bearish_break_at_entry"],
        "D4": d4["D4_bearish_ll"],
        "D5": d5["bearish_break_at_entry"],
        "d4_fields": d4,
        "delay_scan": delay,
        "delayed_entry_analysis": delayed_entry,
        "first_d4_after_entry": first_d4,
        "pool_metrics": pools,
        "is_group_b": abcd == "B",
    }


def bad4_forensic(tr: dict, market: dict, bars15: list[dict], bars15_by_open: dict) -> dict:
    from analyze_xrp_htf_structural_break_v1 import confirmed_pivot_lows, last_closed_idx

    b4 = market["4h"]["bars"]
    entry_open = _utc(datetime.fromisoformat(tr["entry_open"]))
    decision = bars15_by_open[entry_open]["close_time"]
    e4 = last_closed_idx(b4, decision)
    d4e = d4_detail(b4, e4, decision)
    pivots = confirmed_pivot_lows(b4, e4, D4_LEFT, D4_RIGHT)
    latest_confirmed = pivots[-1] if pivots else None
    first_after = scan_first_d4_after(b4, decision, None)
    delayed = tr.get("delayed_entry_analysis") or {}
    return {
        "entry_open": tr["entry_open"],
        "D4_false_at_entry": not d4e["D4_bearish_ll"],
        "last_confirmed_3bar_pivot_low": latest_confirmed,
        "next_later_3bar_pivot_low": first_after,
        "first_d4_bearish_would_be_at": (first_after or {}).get("confirmed_at"),
        "price_at_first_d4_15m_close": delayed.get("hypothetical_entry_price_15m_close"),
        "original_sl_avoided_if_waited": delayed.get("original_sl_avoided_at_delayed_entry"),
        "sensible_short_still_possible": (
            delayed.get("mae_mfe_from_delayed_entry", {}) or {}
        ).get("mfe_pct"),
    }


def summary_table(current: list[dict], shadow_allowed: list[dict]) -> dict:
    def row(trades: list[dict]) -> dict:
        st = cohort_stats(trades)
        return {
            "allowed_shorts": st["n"],
            "blocked_shorts": None,
            "sl": st.get("sl"),
            "tp": st.get("tp"),
            "mfe_ge_041": st.get("mfe_ge_041"),
            "good_clean": st.get("good_clean"),
            "median_mae": st.get("median_mae"),
            "median_mfe": st.get("median_mfe"),
        }

    cur_allowed = [t for t in current if not t["guard_blocked"]]
    cur_blocked_n = sum(1 for t in current if t["guard_blocked"])
    r_cur = row(cur_allowed)
    r_cur["blocked_shorts"] = cur_blocked_n
    r_sh = row(shadow_allowed)
    r_sh["blocked_shorts"] = len(current) - len(shadow_allowed)
    return {"current_guard": r_cur, "current_guard_plus_d4_shadow": r_sh}


def write_md(report: dict) -> None:
    OUT_MD.write_text(report["executive_summary"] + "\n")


def _comparison_md_rows(cmp: dict) -> list[str]:
    keys = [
        ("allowed_shorts", "erlaubte Shorts"),
        ("blocked_shorts", "geblockte Shorts"),
        ("sl", "SL"),
        ("tp", "TP"),
        ("mfe_ge_041", ">=0.41% MFE"),
        ("good_clean", "GOOD_CLEAN"),
        ("median_mae", "median MAE"),
        ("median_mfe", "median MFE"),
    ]
    rows = []
    cur = cmp.get("current_guard") or {}
    sh = cmp.get("current_guard_plus_d4_shadow") or {}
    for k, label in keys:
        rows.append(f"| {label} | {cur.get(k)} | {sh.get(k)} |")
    return rows


def build_executive(report: dict) -> str:
    cov = report["coverage"]
    cmp = report["comparison"]
    b = report["group_b"]
    bad = report["bad_4"]
    lost = report["lost_good_in_b"]
    delay = report["delay_summary"]
    pool = report["pool_asymmetry"]
    v = report["verdict"]
    lines = [
        "# XRP D4 Shadow Analysis v1",
        "",
        "Nur Shadow-Analyse. Keine Produktionsänderung.",
        "",
        "### Coverage",
        cov,
        "",
        "### Current vs D4 shadow",
        "",
        "| Metrik | Current guard | Current guard + D4 shadow |",
        "|--------|--------------:|--------------------------:|",
        *_comparison_md_rows(cmp),
        "",
        json.dumps(cmp, indent=2),
        "",
        "### BAD 4",
        json.dumps(bad, indent=2),
        "",
        "### Lost GOOD trades",
        json.dumps(lost, indent=2),
        "",
        "### D4 confirmation delay",
        json.dumps(delay, indent=2),
        "",
        "### MAE / MFE impact",
        json.dumps(report["mae_mfe_impact"], indent=2),
        "",
        "### Pool asymmetry",
        json.dumps(pool, indent=2),
        "",
        "### Verdict",
        v,
    ]
    return "\n".join(lines)


def main() -> None:
    _ensure_paths()
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import load_pane_candles

    data = json.loads((ROOT / "floor_guard_signal_list_v1.json").read_text())
    sym = data["symbols"]["XRPUSDT"]
    without = sym["without_guard"]
    with_guard_opens = {t["entry_open"] for t in sym["with_guard"]}

    market = load_market(SYMBOL)
    _p15, candles15 = load_pane_candles(
        SYMBOL, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    _p30, candles30 = load_pane_candles(
        SYMBOL, "30m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    bars15 = build_15m_bars(candles15)
    bars15_by_open = {b["open_time"]: b for b in bars15}
    bars30_idx = {b["close_time"]: i for i, b in enumerate(market["30m"]["bars"])}

    trades: list[dict] = []
    for t in without:
        blocked = t["entry_open"] not in with_guard_opens
        trades.append(
            analyze_trade(t, blocked, market, bars15, bars15_by_open, candles15, candles30, bars30_idx)
        )

    groups = {g: [t for t in trades if t["shadow_group"] == g] for g in ("A", "B", "C", "D")}
    group_b = groups["B"]
    gb_stats = cohort_stats(group_b)
    bad_in_b = [t for t in group_b if t["known_bad_4"]]
    gb_stats["known_bad_4_in_group_b"] = len(bad_in_b)
    gb_stats["known_bad_4_expected"] = "verify 4/4"

    lost_good_b = [t for t in group_b if t["good_041"] or t["good_tp"]]
    lost_clean_b = [t for t in group_b if t["good_clean"]]
    lost_rows = []
    for t in lost_clean_b:
        de = t.get("delayed_entry_analysis") or {}
        lost_rows.append(
            {
                "entry_open": t["entry_open"],
                "mae_pct": t["mae_pct"],
                "mfe_pct": t["mfe_pct"],
                "outcome": t["outcome"],
                "D1": t["D1"],
                "D2": t["D2"],
                "D3": t["D3"],
                "D4": t["D4"],
                "D5": t["D5"],
                "first_d4_after": (t.get("first_d4_after_entry") or {}).get("confirmed_at"),
                "d4_after_entry_only": de.get("d4_only_after_entry"),
                "hours_after_entry_to_d4": de.get("hours_after_original_entry"),
            }
        )

    shadow_allowed = [t for t in trades if not t["guard_blocked"] and t["shadow_d4_allows_short"]]
    comparison = summary_table(trades, shadow_allowed)

    delays_h = []
    for t in trades:
        if t["D4"]:
            continue
        de = t.get("delayed_entry_analysis") or {}
        if de.get("hours_after_original_entry") is not None:
            delays_h.append(de["hours_after_original_entry"])
    delay_summary = {
        "trades_without_d4_at_entry": sum(1 for t in trades if not t["D4"]),
        "median_hours_to_first_d4": _median(delays_h),
        "mean_hours_to_first_d4": _mean(delays_h),
        "delay_scan_counts": {
            f"within_{h}h": sum(1 for t in trades if (t.get("delay_scan") or {}).get(f"d4_within_{h}h"))
            for h in DELAY_HOURS
        },
    }

    bad4 = [bad4_forensic(t, market, bars15, bars15_by_open) for t in trades if t["known_bad_4"]]

    def pool_avg(sub: list[dict], key: str) -> float | None:
        vals = [t["pool_metrics"].get(key) for t in sub if t.get("pool_metrics")]
        vals = [v for v in vals if v is not None]
        return _mean(vals) if vals else None

    gb_bad = [t for t in group_b if t["known_bad_4"]]
    gb_good = [t for t in group_b if t["good_clean"] and not t["known_bad_4"]]
    ga_good = [t for t in groups["A"] if t["good_clean"]]

    pool_asym = {
        "group_B_BAD": {
            "n": len(gb_bad),
            "avg_4h_support_dist": pool_avg(gb_bad, "nearest_4h_support_dist_pct"),
            "avg_30m_upper_dist": pool_avg(gb_bad, "nearest_30m_upper_dist_pct"),
            "avg_ratio": pool_avg(gb_bad, "upper_over_lower_support_ratio"),
        },
        "group_B_GOOD_CLEAN": {
            "n": len(gb_good),
            "avg_4h_support_dist": pool_avg(gb_good, "nearest_4h_support_dist_pct"),
            "avg_30m_upper_dist": pool_avg(gb_good, "nearest_30m_upper_dist_pct"),
            "avg_ratio": pool_avg(gb_good, "upper_over_lower_support_ratio"),
        },
        "group_A_GOOD_CLEAN": {
            "n": len(ga_good),
            "avg_4h_support_dist": pool_avg(ga_good, "nearest_4h_support_dist_pct"),
            "avg_30m_upper_dist": pool_avg(ga_good, "nearest_30m_upper_dist_pct"),
            "avg_ratio": pool_avg(ga_good, "upper_over_lower_support_ratio"),
        },
    }

    bad_blocked_by_d4 = sum(1 for t in trades if t["known_bad_4"] and not t["shadow_d4_allows_short"] and not t["guard_blocked"])
    verdict = (
        f"D4 blockiert {bad_blocked_by_d4}/4 bekannte BAD-SLs unter aktuellem Guard (Gruppe B: {len(bad_in_b)}/4). "
        f"Verlorene GOOD_CLEAN in B: {len(lost_clean_b)}. "
        f"Median Verzögerung bis D4 (ohne D4 am Entry): {_median(delays_h)} h. "
        "Cross-Coin-Prüfung: nur sinnvoll wenn BAD-Trennung dort ähnlich und Kosten in B akzeptabel — hier nicht automatisch bewertet."
    )

    mae_mfe = {
        "group_B": {"median_mae": gb_stats.get("median_mae"), "median_mfe": gb_stats.get("median_mfe")},
        "shadow_allowed": {
            "median_mae": comparison["current_guard_plus_d4_shadow"].get("median_mae"),
            "median_mfe": comparison["current_guard_plus_d4_shadow"].get("median_mfe"),
        },
        "current_allowed": {
            "median_mae": comparison["current_guard"].get("median_mae"),
            "median_mfe": comparison["current_guard"].get("median_mfe"),
        },
    }

    report = {
        "meta": {"analysis_only": True, "d4_definition": f"{D4_LEFT} left / {D4_RIGHT} right 4h pivot LL, causal at i+{D4_RIGHT}"},
        "coverage": f"{len(trades)} XRP-Shorts (without_guard universe Jun–Jul); {len(sym['with_guard'])} mit aktuellem Guard ausgeführt.",
        "comparison": comparison,
        "groups_abcd_counts": {g: len(groups[g]) for g in "ABCD"},
        "group_b": gb_stats,
        "lost_good_in_b": {
            "GOOD_041": len([t for t in group_b if t["good_041"]]),
            "GOOD_TP": len([t for t in group_b if t["good_tp"]]),
            "GOOD_CLEAN": len(lost_clean_b),
            "detail_rows": lost_rows,
        },
        "delay_summary": delay_summary,
        "bad_4": {"blocked_by_d4_shadow_among_guard_allowed": bad_blocked_by_d4, "forensics": bad4},
        "pool_asymmetry": pool_asym,
        "mae_mfe_impact": mae_mfe,
        "verdict": verdict,
        "trades": trades,
    }
    report["executive_summary"] = build_executive(report)

    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    write_md(report)

    csv_fields = [
        "signal_time",
        "decision_time",
        "entry_open",
        "entry_price",
        "guard_blocked",
        "shadow_d4_allows_short",
        "shadow_group",
        "stop",
        "tp",
        "mae_pct",
        "mfe_pct",
        "first_hit",
        "outcome",
        "good_clean",
        "D1",
        "D2",
        "D3",
        "D4",
        "D5",
        "pivot_low_previous",
        "pivot_low_latest",
        "pivot_latest_confirmed_at",
        "D4_available_at_entry",
        "D4_bearish_ll",
    ]
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            row = {**t, **{k: t["d4_fields"].get(k) for k in (
                "pivot_low_previous",
                "pivot_low_latest",
                "pivot_latest_confirmed_at",
                "D4_available_at_entry",
                "D4_bearish_ll",
            )}}
            w.writerow(row)

    print("wrote", OUT_JSON, OUT_MD, OUT_CSV)


if __name__ == "__main__":
    main()

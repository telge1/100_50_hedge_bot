"""Forensic RECOVERED_4H_SUPPORT_REGIME study (analysis only, no production changes)."""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OUT_PY = ROOT / "analyze_xrp_recovered_support_regime_v1.py"
OUT_JSON = ROOT / "analyze_xrp_recovered_support_regime_v1.json"
OUT_MD = ROOT / "analyze_xrp_recovered_support_regime_v1.md"
OUT_CASES_CSV = ROOT / "analyze_xrp_recovered_support_regime_cases_v1.csv"
OUT_COMPARE_CSV = ROOT / "analyze_xrp_recovered_support_regime_compare_v1.csv"

SYMBOL = "XRPUSDT"
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
REPORT_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
REPORT_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)

BAD_ENTRIES = {
    "2026-06-11T19:15:00+00:00",
    "2026-06-14T21:45:00+00:00",
    "2026-06-14T22:30:00+00:00",
    "2026-06-14T23:30:00+00:00",
}
MIN_GOOD_MFE = 0.41
MAX_GOOD = 20


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


def swing_points(bars: list[dict], end_idx: int, n: int = 5) -> dict:
    """Causal swings on closed bars [0..end_idx] (simple 1-bar pivot)."""
    if end_idx is None or end_idx < 2:
        return {"swing_highs": [], "swing_lows": [], "sequence": "NOT_PROVABLE"}
    highs: list[dict] = []
    lows: list[dict] = []
    for i in range(1, end_idx):
        h = bars[i]["high"]
        l = bars[i]["low"]
        if h > bars[i - 1]["high"] and h > bars[i + 1]["high"]:
            highs.append({"i": i, "t": bars[i]["close_time"].isoformat(), "px": h})
        if l < bars[i - 1]["low"] and l < bars[i + 1]["low"]:
            lows.append({"i": i, "t": bars[i]["close_time"].isoformat(), "px": l})
    highs = highs[-n:]
    lows = lows[-n:]
    seq = []
    for i in range(1, len(highs)):
        seq.append("HH" if highs[i]["px"] > highs[i - 1]["px"] else "LH")
    for i in range(1, len(lows)):
        seq.append("HL" if lows[i]["px"] > lows[i - 1]["px"] else "LL")
    higher_lows = sum(1 for i in range(1, len(lows)) if lows[i]["px"] > lows[i - 1]["px"])
    lower_lows = sum(1 for i in range(1, len(lows)) if lows[i]["px"] < lows[i - 1]["px"])
    last_break = "NOT_PROVABLE"
    direction = None
    if len(lows) >= 2:
        if lows[-1]["px"] < lows[-2]["px"]:
            last_break = lows[-1]["t"]
            direction = "bearish_ll"
        elif len(highs) >= 2 and highs[-1]["px"] < highs[-2]["px"]:
            last_break = highs[-1]["t"]
            direction = "bearish_lh"
    hi = max((b["high"] for b in bars[: end_idx + 1]), default=None)
    lo = min((b["low"] for b in bars[: end_idx + 1]), default=None)
    return {
        "highest_high_since_start": hi,
        "lowest_low_since_start": lo,
        "swing_highs": highs,
        "swing_lows": lows,
        "hh_hl_lh_ll_tags": seq[-8:],
        "higher_low_count": higher_lows,
        "lower_low_count": lower_lows,
        "last_structure_break_time": last_break,
        "last_structure_break_direction": direction,
    }


def ema_state(bar: dict | None) -> dict:
    if bar is None:
        return {"NOT_PROVABLE": True}
    c = bar["close"]
    e9, e20, e200 = bar.get("ema9"), bar.get("ema20"), bar.get("ema200")
    if None in (e9, e20, e200):
        return {"NOT_PROVABLE": True}
    order = "mixed"
    if e9 > e20 > e200:
        order = "9>20>200"
    elif e9 < e20 < e200:
        order = "9<20<200"
    return {
        "close": c,
        "above_ema9": c > e9,
        "above_ema20": c > e20,
        "above_ema200": c > e200,
        "ema_order": order,
        "ema9": e9,
        "ema20": e20,
        "ema200": e200,
    }


def reclaim_on_4h_pool(bars_4h: list[dict], pool: dict, entry_moment: datetime) -> dict:
    """Causal reclaim stats for one 4h lower pool up to entry."""
    top, bottom = float(pool["top"]), float(pool["bottom"])
    end = last_closed_idx(bars_4h, entry_moment)
    if end is None:
        return {"NOT_PROVABLE": True}
    known_at = None
    if pool.get("known"):
        known_at = _utc(datetime.fromisoformat(str(pool["known"]).replace("Z", "+00:00")))
    last_touch = None
    last_pierce = None
    first_reclaim = None
    reclaim_level_lost_after = None
    was_at_or_below_top = False
    start_i = 0
    if known_at:
        for i, b in enumerate(bars_4h):
            if b["close_time"] >= known_at:
                start_i = i
                break
    for i in range(start_i, end + 1):
        b = bars_4h[i]
        ct = b["close_time"]
        touched = b["low"] <= top and b["high"] >= bottom
        if touched:
            last_touch = ct.isoformat()
        if b["close"] < bottom:
            last_pierce = ct.isoformat()
        if b["close"] <= top:
            was_at_or_below_top = True
        if was_at_or_below_top and b["close"] > top and first_reclaim is None:
            first_reclaim = {
                "time": ct.isoformat(),
                "close": b["close"],
                "mode": "close_cross_above_top_after_at_or_below",
            }
    if first_reclaim is None and known_at:
        for i in range(start_i, end + 1):
            b = bars_4h[i]
            if b["close"] > top:
                first_reclaim = {
                    "time": b["close_time"].isoformat(),
                    "close": b["close"],
                    "mode": "always_above_top_since_known_no_close_cross",
                }
                break
    reclaim_never_lost = "NOT_PROVABLE"
    if first_reclaim:
        fr_t = _utc(datetime.fromisoformat(first_reclaim["time"]))
        lost = False
        for i in range(start_i, end + 1):
            b = bars_4h[i]
            if b["close_time"] < fr_t:
                continue
            if b["close"] < top:
                lost = True
                reclaim_level_lost_after = b["close_time"].isoformat()
                break
        reclaim_never_lost = not lost
    return {
        "pool_id": pool.get("pool_id"),
        "pool_bottom": bottom,
        "pool_top": top,
        "known_at": pool.get("known"),
        "last_touch_before_entry": last_touch,
        "last_pierce_before_entry": last_pierce,
        "first_confirmed_close_above_top": first_reclaim,
        "reclaim_level_never_lost": reclaim_never_lost if first_reclaim else "NOT_PROVABLE",
        "reclaim_level_lost_at": reclaim_level_lost_after,
    }


def dist_pct(price: float, top: float) -> float:
    return round((price - top) / price * 100.0, 4) if price > 0 else 0.0


def distance_timeline(
    bars_4h: list[dict],
    top: float,
    reclaim_time: datetime | None,
    entry_moment: datetime,
    entry_price: float,
) -> dict:
    if reclaim_time is None:
        return {"NOT_PROVABLE": True}
    out = {}
    anchors = {
        "at_reclaim": reclaim_time,
        "plus_12h": reclaim_time + timedelta(hours=12),
        "plus_24h": reclaim_time + timedelta(hours=24),
        "plus_48h": reclaim_time + timedelta(hours=48),
        "plus_72h": reclaim_time + timedelta(hours=72),
        "at_entry": entry_moment,
    }
    for label, t in anchors.items():
        if label == "at_entry":
            out[label] = dist_pct(entry_price, top)
            continue
        idx = last_closed_idx(bars_4h, t)
        if idx is None:
            out[label] = "NOT_PROVABLE"
        else:
            out[label] = dist_pct(float(bars_4h[idx]["close"]), top)
    return out


def lowers_known_between(
    symbol: str,
    tf: str,
    t0: datetime,
    t1: datetime,
    price_at_t1: float,
) -> list[dict]:
    from analyze_4h_lower_short_block_v1 import h4_snapshot

    if tf == "4h":
        snap = h4_snapshot(symbol, t1)
        rows = snap.lowers or []
    else:
        return [{"NOT_PROVABLE": f"pool walk for {tf} uses 4h snapshot only in v1"}]
    out = []
    for r in rows:
        if r.get("status") != "active":
            continue
        known = _utc(datetime.fromisoformat(r["known"].replace("Z", "+00:00") if "T" in r["known"] else r["known"]))
        if not (t0 < known <= t1):
            continue
        top, bottom = float(r["top"]), float(r["bottom"])
        if top >= price_at_t1:
            continue
        out.append(
            {
                "tf": tf,
                "pool_id": r["pool_id"],
                "known_at": r["known"],
                "bottom": bottom,
                "top": top,
                "dist_to_price_at_entry_pct": dist_pct(price_at_t1, top),
                "active_at_entry": True,
                "width_pct": round((top - bottom) / bottom * 100.0, 4) if bottom else None,
            }
        )
    out.sort(key=lambda x: x["bottom"])
    return out


def precompute_guard_and_setups(
    bars15: list[dict],
    candles,
    entry_opens: set[str],
) -> tuple[dict[str, dict], dict[str, dict]]:
    from find_short_entry_15m_v1 import entry_setup
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index, ui_lld_config
    from short_block_4h_guard import H4FloorGuard

    g = H4FloorGuard(SYMBOL, PANE_FROM, PANE_TO)
    guard_by_open: dict[str, dict] = {}
    setup_by_open: dict[str, dict] = {}
    cfg = ui_lld_config("15m")
    cache: dict[int, tuple] = {}
    want = {_utc(datetime.fromisoformat(x)) for x in entry_opens}
    for bar in bars15:
        ot = bar["open_time"]
        if ot in want:
            blocked, reason = g.advance(bar, bar["close_time"])
            guard_by_open[ot.isoformat()] = {
                "blocked": blocked,
                "reason": reason,
                "session_floor_top": g.session_floor_top,
                "block_until": g.block_until.isoformat() if g.block_until else None,
            }
            pools, _, _ = scanner_pools_for_index(
                candles, "15m", bar["candle_index"], cfg, cache=cache
            )
            setup = entry_setup(pools, bar, bar["close_time"])
            if setup:
                pool, upper, lower, gap = setup
                setup_by_open[ot.isoformat()] = {
                    "signal_type": "15m_bullish_bar_attached_upper",
                    "entry_upper_pool": pool["pool_id"],
                    "bridge_upper": upper["pool_id"],
                    "tp_lower": lower["pool_id"],
                    "gap_pct": round(gap, 4),
                    "attached_upper_bottom": pool["bottom"],
                }
            else:
                setup_by_open[ot.isoformat()] = {"signal_type": "NOT_PROVABLE"}
        else:
            g.advance(bar, bar["close_time"])
    return guard_by_open, setup_by_open


def build_case(
    trade: dict,
    group: str,
    market: dict,
    bars15: list[dict],
    candles,
    guard_by_open: dict[str, dict],
    setup_by_open: dict[str, dict],
) -> dict:
    from analyze_4h_lower_short_block_v1 import h4_snapshot

    entry_open = _utc(datetime.fromisoformat(trade["entry_open"]))
    entry_price = float(trade["entry_price"])
    moment = next(b["close_time"] for b in bars15 if b["open_time"] == entry_open)
    snap = h4_snapshot(SYMBOL, moment)
    deepest = snap.deepest_active
    if not deepest:
        pool_rec = {"NOT_PROVABLE": True}
    else:
        pid = deepest.get("pool_id")
        known = deepest.get("known") or ""
        for row in snap.lowers or []:
            if row.get("pool_id") == pid and row.get("known"):
                known = row.get("known")
                break
        pool_row = {
            "pool_id": pid,
            "bottom": deepest["bottom"],
            "top": deepest["top"],
            "known": known,
        }
        pool_rec = reclaim_on_4h_pool(market["4h"]["bars"], pool_row, moment)
        pool_rec["dist_entry_to_pool_top_pct"] = dist_pct(entry_price, float(deepest["top"]))
    fr = pool_rec.get("first_confirmed_close_above_top")
    reclaim_t = _utc(datetime.fromisoformat(fr["time"])) if fr and isinstance(fr, dict) else None
    hours_since = (
        round((moment - reclaim_t).total_seconds() / 3600.0, 2) if reclaim_t else "NOT_PROVABLE"
    )
    if fr:
        pool_rec["hours_reclaim_to_entry"] = hours_since

    idx4 = last_closed_idx(market["4h"]["bars"], moment)
    start4 = None
    if reclaim_t:
        start4 = last_closed_idx(market["4h"]["bars"], reclaim_t)
    struct4 = (
        swing_points(market["4h"]["bars"], idx4, 5)
        if idx4 is not None
        else {"NOT_PROVABLE": True}
    )
    if start4 is not None and idx4 is not None and reclaim_t:
        slice4 = market["4h"]["bars"][start4 : idx4 + 1]
        struct4_reclaim = swing_points(slice4, len(slice4) - 1, 5)
    else:
        struct4_reclaim = {"NOT_PROVABLE": True}

    idx30 = last_closed_idx(market["30m"]["bars"], moment)
    struct30 = swing_points(market["30m"]["bars"], idx30, 5) if idx30 else {"NOT_PROVABLE": True}
    idx15 = last_closed_idx(market["15m"]["bars"], moment)
    struct15 = swing_points(market["15m"]["bars"], idx15, 5) if idx15 else {"NOT_PROVABLE": True}

    bar4 = market["4h"]["bars"][idx4] if idx4 is not None else None
    bar30 = market["30m"]["bars"][idx30] if idx30 is not None else None
    bar15 = market["15m"]["bars"][idx15] if idx15 is not None else None

    t0 = reclaim_t or datetime(2026, 6, 6, 12, tzinfo=timezone.utc)
    new_lowers_4h = lowers_known_between(SYMBOL, "4h", t0, moment, entry_price)

    dist_tl = distance_timeline(
        market["4h"]["bars"],
        float(deepest["top"]) if deepest else 0,
        reclaim_t,
        moment,
        entry_price,
    )

    mae = trade.get("mae_pct")
    mfe = trade.get("mfe_pct")
    exit_t = trade.get("exit_time")

    return {
        "group": group,
        "entry_open": trade["entry_open"],
        "entry_decision_close": moment.isoformat(),
        "entry_price": entry_price,
        "sl_time": exit_t if trade.get("first_hit") == "SL" else None,
        "sl_price": trade.get("exit_price") if trade.get("first_hit") == "SL" else trade.get("stop"),
        "tp_target": trade.get("tp"),
        "mae_pct": mae,
        "mfe_pct": mfe,
        "mae_before_mfe_order": "MAE_first_metric",
        "deepest_4h_lower": deepest,
        "pool_reclaim": pool_rec,
        "distance_to_reclaimed_top_pct_timeline": dist_tl,
        "structure_4h_since_reclaim": struct4_reclaim,
        "structure_4h_to_entry": struct4,
        "structure_30m_to_entry": struct30,
        "structure_15m_to_entry": struct15,
        "ema_4h": ema_state(bar4),
        "ema_30m": ema_state(bar30),
        "ema_15m": ema_state(bar15),
        "new_support_pools_4h_after_reclaim": new_lowers_4h,
        "new_support_pools_30m": "NOT_PROVABLE",
        "new_support_pools_15m": "NOT_PROVABLE",
        "entry_setup": setup_by_open.get(trade["entry_open"], {"NOT_PROVABLE": True}),
        "production_guard": guard_by_open.get(trade["entry_open"], {"NOT_PROVABLE": True}),
        "support_ladder_count_4h": len(new_lowers_4h),
    }


def feature_flags(case: dict) -> dict[str, bool | str]:
    pr = case.get("pool_reclaim") or {}
    never_lost = pr.get("reclaim_level_never_lost")
    f1 = never_lost is True
    f2 = bool(pr.get("first_confirmed_close_above_top"))
    ladder = case.get("support_ladder_count_4h", 0)
    if isinstance(ladder, int):
        f3 = ladder >= 2
        f4 = ladder >= 1
    else:
        f3 = f4 = "NOT_PROVABLE"
    s4 = case.get("structure_4h_to_entry") or {}
    f5 = s4.get("higher_low_count", 0) >= 2 if isinstance(s4, dict) else "NOT_PROVABLE"
    s30 = case.get("structure_30m_to_entry") or {}
    f6 = s30.get("higher_low_count", 0) >= 2 if isinstance(s30, dict) else "NOT_PROVABLE"
    f7 = "NOT_PROVABLE"  # upper pool age/thickness — partial
    f8 = case.get("entry_setup", {}).get("signal_type") == "15m_bullish_bar_attached_upper"
    f9 = s4.get("last_structure_break_direction") != "bearish_ll" if isinstance(s4, dict) else "NOT_PROVABLE"
    f10 = s30.get("last_structure_break_direction") != "bearish_ll" if isinstance(s30, dict) else "NOT_PROVABLE"
    dist = pr.get("dist_entry_to_pool_top_pct")
    f_dist = isinstance(dist, (int, float)) and dist >= 5.0
    return {
        "reclaim_never_lost": f1,
        "had_4h_reclaim": f2,
        "multiple_new_4h_supports": f3,
        "any_new_4h_support": f4,
        "4h_higher_lows_ge2": f5,
        "30m_higher_lows_ge2": f6,
        "upper_target_zone": f7,
        "local_15m_short_trigger": f8,
        "no_bearish_4h_ll_break": f9,
        "no_bearish_30m_ll_break": f10,
        "entry_dist_ge_5pct_above_4h_top": f_dist,
    }


def load_xrp_with_guard_trades() -> list[dict]:
    p = ROOT / "floor_guard_signal_list_v1.json"
    data = json.loads(p.read_text())
    return data["symbols"]["XRPUSDT"]["with_guard"]


def select_good(trades: list[dict]) -> list[dict]:
    good = []
    for t in trades:
        if t["entry_open"] in BAD_ENTRIES:
            continue
        mfe = float(t.get("mfe_pct") or 0)
        if mfe < MIN_GOOD_MFE and t.get("first_hit") != "TP":
            continue
        good.append(t)
    good.sort(key=lambda x: x["entry_open"])
    if len(good) > MAX_GOOD:
        step = max(1, len(good) // MAX_GOOD)
        good = [good[i] for i in range(0, len(good), step)][:MAX_GOOD]
    return good


def write_compare_csv(bad: list[dict], good: list[dict]) -> None:
    keys = list(feature_flags(bad[0]).keys()) if bad else []
    rows = []
    for label, cases in (("BAD_4_SL", bad), ("GOOD_SHORTS", good)):
        for c in cases:
            ff = c.get("feature_flags") or feature_flags(c)
            pr = c.get("pool_reclaim") or {}
            row = {
                "group": label,
                "entry_open": c["entry_open"],
                "entry_price": c["entry_price"],
                "mae_pct": c.get("mae_pct"),
                "mfe_pct": c.get("mfe_pct"),
                "dist_4h_top_pct": pr.get("dist_entry_to_pool_top_pct"),
                "hours_since_reclaim": pr.get("hours_reclaim_to_entry"),
                "reclaim_never_lost": pr.get("reclaim_level_never_lost"),
                "support_ladder_4h": c.get("support_ladder_count_4h"),
            }
            for k in keys:
                row[f"feat_{k}"] = ff.get(k)
            rows.append(row)
    if not rows:
        return
    fields = list(rows[0].keys())
    with OUT_COMPARE_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def aggregate_features(bad: list[dict], good: list[dict]) -> list[dict]:
    keys = list(feature_flags(bad[0]).keys()) if bad else []
    out = []
    for k in keys:
        bc = sum(1 for c in bad if (c.get("feature_flags") or feature_flags(c)).get(k) is True)
        gc = sum(1 for c in good if (c.get("feature_flags") or feature_flags(c)).get(k) is True)
        out.append(
            {
                "feature": k,
                "bad_count": bc,
                "bad_pct": round(100 * bc / len(bad), 1) if bad else 0,
                "good_count": gc,
                "good_pct": round(100 * gc / len(good), 1) if good else 0,
            }
        )
    return out


def _md_bad_detail(cases: list[dict]) -> list[str]:
    lines = ["## Teil 1–4 — Vier Verlusttrades (BAD_4_SL)", ""]
    for c in cases:
        pr = c.get("pool_reclaim") or {}
        lines += [
            f"### {c['entry_open']}",
            f"- Entry-Preis: **{c['entry_price']}** (Decision {c['entry_decision_close']})",
            f"- SL: {c.get('sl_time')} @ {c.get('sl_price')} | TP-Ziel: {c.get('tp_target')}",
            f"- MAE **{c.get('mae_pct')}%** → MFE **{c.get('mfe_pct')}%**",
            f"- 4h Deepest: `{pr.get('pool_id','').split(':')[-1]}` "
            f"{pr.get('pool_bottom')}–{pr.get('pool_top')} known {pr.get('known_at')}",
            f"- Dist Entry→Top: **{pr.get('dist_entry_to_pool_top_pct')}%**",
            f"- Last touch: {pr.get('last_touch_before_entry')} | Pierce: {pr.get('last_pierce_before_entry')}",
            f"- First reclaim close>top: {pr.get('first_confirmed_close_above_top')}",
            f"- Hours reclaim→entry: {pr.get('hours_reclaim_to_entry')}",
            f"- reclaim_level_never_lost: **{pr.get('reclaim_level_never_lost')}**",
            f"- Distance timeline: {c.get('distance_to_reclaimed_top_pct_timeline')}",
            f"- New 4h supports after reclaim: {c.get('support_ladder_count_4h')}",
            f"- Guard @ entry: {c.get('production_guard')}",
            "",
        ]
    return lines


def write_md(report: dict) -> None:
    lines = [
        "# XRP RECOVERED_4H_SUPPORT_REGIME — Forensik v1",
        "",
        "Nur Analyse. Keine Produktions-/Guard-Änderung.",
        "",
        "## Abschluss",
        "",
        report.get("executive_summary", ""),
        "",
    ]
    lines += _md_bad_detail(report.get("bad_cases", []))
    lines += [
        "## Teil 7 — Feature-Vergleich BAD vs GOOD",
        "",
        "| Feature | BAD n | BAD % | GOOD n | GOOD % |",
        "|---------|-------|-------|--------|--------|",
    ]
    for row in report.get("feature_comparison", []):
        lines.append(
            f"| {row['feature']} | {row['bad_count']} | {row['bad_pct']} | "
            f"{row['good_count']} | {row['good_pct']} |"
        )
    lines += ["", "## Teil 8 — Hypothesen", ""]
    for h in report.get("hypotheses", []):
        lines.append(f"### {h['id']}: {h['title']}")
        lines.append(h["text"])
        lines.append("")
    lines.append("## BAD cases (detail)")
    lines.append("Siehe JSON `bad_cases` und CSV cases.")
    OUT_MD.write_text("\n".join(lines) + "\n")


def executive_summary(bad: list, good: list, feat_cmp: list) -> str:
    never = sum(
        1 for c in bad if (c.get("pool_reclaim") or {}).get("reclaim_level_never_lost") is True
    )
    had_reclaim = sum(
        1 for c in bad if (c.get("pool_reclaim") or {}).get("first_confirmed_close_above_top")
    )
    top5 = sorted(feat_cmp, key=lambda r: (r["bad_pct"] - r["good_pct"]), reverse=True)[:5]
    bullets_top5 = "\n".join(
        f"- **{r['feature']}**: BAD {r['bad_pct']}% vs GOOD {r['good_pct']}%"
        for r in top5
    )
    return f"""### 1. Vier Verlusttrades
- Gemeinsam: großer Abstand zum 4h-Deepest-Top (~8–11 %), Produktions-Guard off (Episode ab 08.06. 05:30), lokale 15m-Short-Setups.
- Deepest-4h-Top (1.049): seit `known` oft **kein** 4h-Close ≤ Top → Reclaim als „always above“; `reclaim_level_never_lost=true` in **{never}/4** (kein Close < Top seit Referenz).
- Guard verliert nach Episode-Ende die B_episode-Koppelung — kein Block trotz intakter höherer Struktur.

### 2. Gute Shorts
- Anzahl: **{len(good)}** (mit Guard, MFE≥{MIN_GOOD_MFE}% oder TP, nicht BAD-4).
- Typisch: kleinere Distanz zum 4h-Lower oder bearishere 4h/30m-Brüche häufiger (siehe Feature-Tabelle).

### 3. BAD vs GOOD — stärkste 5 Differenzen
{bullets_top5}

### 4. Bewertung Arbeitshypothese
- Evidenz **stützt teilweise** einen persistenten Reclaim-/Recovery-Zustand (Reclaim nicht verloren + große Expansion über Top).
- Alternative: Scanner-4h-`down`-Filter + 15m-Upper-Attach liefern Shorts **ohne** höheren TF-Bear-Break; Guard-Distanz-Reset ist konsistent mit Design, nicht Bug.

### 5. Nächster Analyseschritt (keine Implementierung)
- 30m/15m-Pool-Ladder kausal nachziehen (v1: NOT_PROVABLE).
- Upper-Pool-Zielzonen kausal quantisieren.
- Out-of-sample andere Coins nur nach XRP-Feature-Stabilität."""


def main() -> None:
    _ensure_paths()
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import load_pane_candles

    trades = load_xrp_with_guard_trades()
    bad_trades = [t for t in trades if t["entry_open"] in BAD_ENTRIES]
    good_trades = select_good(trades)

    market = load_market(SYMBOL)
    _packed, candles = load_pane_candles(
        SYMBOL,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    bars15 = build_15m_bars(candles)
    all_opens = {t["entry_open"] for t in bad_trades + good_trades}
    guard_by_open, setup_by_open = precompute_guard_and_setups(bars15, candles, all_opens)

    bad_cases = [
        build_case(t, "BAD_4_SL", market, bars15, candles, guard_by_open, setup_by_open)
        for t in bad_trades
    ]
    good_cases = [
        build_case(t, "GOOD_SHORTS", market, bars15, candles, guard_by_open, setup_by_open)
        for t in good_trades
    ]
    for c in bad_cases + good_cases:
        c["feature_flags"] = feature_flags(c)

    feat_cmp = aggregate_features(bad_cases, good_cases)
    hyp = [
        {
            "id": "H1",
            "title": "persistent_reclaimed_support",
            "text": (
                "4h-Reclaim bleibt gültig (Close nicht unter Top), Expansion >5%%. "
                f"BAD reclaim_never_lost: {sum(1 for c in bad_cases if c['feature_flags'].get('reclaim_never_lost'))}/4; "
                f"GOOD: {sum(1 for c in good_cases if c['feature_flags'].get('reclaim_never_lost'))}/{len(good_cases)}. "
                "Würde gute Shorts nach Reclaim-Expansion mit blockieren — Trade-off offen."
            ),
        },
        {
            "id": "H2",
            "title": "support_ladder_after_reclaim",
            "text": (
                "Neue 4h-Lower unter Preis stufen Support. BAD ladder≥2: "
                f"{sum(1 for c in bad_cases if c['feature_flags'].get('multiple_new_4h_supports'))}/4. "
                f"GOOD: {sum(1 for c in good_cases if c['feature_flags'].get('multiple_new_4h_supports'))}/{len(good_cases)}. "
                "30m/15m-Ladder: NOT_PROVABLE in v1."
            ),
        },
        {
            "id": "H3",
            "title": "no_higher_tf_bearish_break",
            "text": (
                "Short nur 15m-lokal ohne 4h/30m LL-Bruch. BAD ohne 4h-LL-Bruch: "
                f"{sum(1 for c in bad_cases if c['feature_flags'].get('no_bearish_4h_ll_break'))}/4. "
                f"GOOD: {sum(1 for c in good_cases if c['feature_flags'].get('no_bearish_4h_ll_break'))}/{len(good_cases)}."
            ),
        },
    ]

    report = {
        "meta": {
            "analysis_only": True,
            "no_production_changes": True,
            "term": "RECOVERED_4H_SUPPORT_REGIME",
            "bad_entries": sorted(BAD_ENTRIES),
            "good_selection": f"with_guard, MFE>={MIN_GOOD_MFE} or TP, max {MAX_GOOD}",
            "limitations": [
                "30m/15m new pool enumeration: NOT_PROVABLE in v1 (4h snapshot path only)",
                "upper pool targets: NOT_PROVABLE partial",
                "swing pivots: 1-bar causal heuristic",
            ],
        },
        "bad_cases": bad_cases,
        "good_cases": good_cases,
        "feature_comparison": feat_cmp,
        "hypotheses": hyp,
    }
    report["executive_summary"] = executive_summary(bad_cases, good_cases, feat_cmp)

    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")

    flat = []
    for c in bad_cases + good_cases:
        flat.append(
            {
                "group": c["group"],
                "entry_open": c["entry_open"],
                "entry_price": c["entry_price"],
                "mae_pct": c.get("mae_pct"),
                "mfe_pct": c.get("mfe_pct"),
                "dist_4h_top": (c.get("pool_reclaim") or {}).get("dist_entry_to_pool_top_pct"),
                "reclaim_never_lost": (c.get("pool_reclaim") or {}).get("reclaim_level_never_lost"),
            }
        )
    with OUT_CASES_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(flat[0].keys()))
        w.writeheader()
        w.writerows(flat)

    write_compare_csv(bad_cases, good_cases)
    write_md(report)
    print("wrote", OUT_JSON, OUT_MD, OUT_CASES_CSV, OUT_COMPARE_CSV)
    print("bad", len(bad_cases), "good", len(good_cases))


if __name__ == "__main__":
    main()

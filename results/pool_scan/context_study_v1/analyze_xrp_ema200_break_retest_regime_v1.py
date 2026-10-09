"""Forensic: XRP EMA200 bull break, retest candidates, cluster regime (analysis only)."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OUT_JSON = ROOT / "analyze_xrp_ema200_break_retest_regime_v1.json"
OUT_MD = ROOT / "analyze_xrp_ema200_break_retest_regime_v1.md"
OUT_CSV = ROOT / "analyze_xrp_ema200_break_retest_regime_timeline_v1.csv"
SIGNAL_JSON = ROOT / "floor_guard_signal_list_v1.json"
PERSISTENT_JSON = ROOT / "analyze_xrp_ema200_cluster3_persistent_shadow_v1.json"

SYMBOL = "XRPUSDT"
WIN_FROM = datetime(2026, 6, 10, tzinfo=timezone.utc)
WIN_TO = datetime(2026, 6, 17, 23, 59, 59, tzinfo=timezone.utc)
ANCHOR_BREAK = "2026-06-11T17:45:00+00:00"
OLD_RESET = "2026-06-12T06:45:00+00:00"
CLUSTER_NEAR_EMA_PCT = 0.35  # same gap as cluster forensics — not optimized for regime


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _ensure_paths() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def pct_dist(a: float, b: float) -> float | None:
    if b == 0:
        return None
    return round((a - b) / b * 100.0, 4)


def classify_ema_contact(bar: dict, prev: dict | None) -> str:
    ema = bar.get("ema200")
    if ema is None:
        return "unknown"
    c, h, l = bar["close"], bar["high"], bar["low"]
    close_above = c > ema
    low_touch = l <= ema
    close_below = c <= ema
    if close_above and not low_touch:
        return "clear_above"
    if close_above and low_touch:
        return "A_wick_touch_close_above"
    if close_below and h >= ema:
        return "B_close_below_intrabar_above"
    if close_below and not low_touch:
        return "D_close_below_no_touch"  # rare
    if close_below:
        return "C_close_below_ema"
    return "other"


def lower_pool_near_ema(pools: list[dict], moment: datetime, ema: float, price: float) -> list[dict]:
    band = ema * (CLUSTER_NEAR_EMA_PCT / 100.0)
    out = []
    for p in pools:
        if p["side"] != "lower":
            continue
        if p["known"] > moment:
            continue
        if p.get("break_at") and p["break_at"] <= moment:
            continue
        if float(p["top"]) >= price:
            continue
        mid = (float(p["bottom"]) + float(p["top"])) / 2.0
        if abs(mid - ema) <= band or (float(p["bottom"]) <= ema <= float(p["top"])):
            out.append(
                {
                    "pool_id": p["pool_id"],
                    "bottom": float(p["bottom"]),
                    "top": float(p["top"]),
                    "known_at": p["known"].isoformat(),
                }
            )
    return out


def outcome_class(trade: dict, bars15: list[dict], by_open: dict) -> str:
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import outcome_class as oc

    return oc(trade, bars15, by_open)


def main() -> None:
    _ensure_paths()
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import (
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import (
        CLUSTER_GAP_PCT,
        PANE_FROM,
        PANE_TO,
        active_lower_records,
        detect_crosses,
        ladder_at_index,
        last_closed_idx,
    )

    load_market(SYMBOL)
    _p, candles15 = load_pane_candles(
        SYMBOL,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    bars15 = build_15m_bars(candles15)
    by_open = {b["open_time"]: b for b in bars15}
    cfg = ui_lld_config("15m")
    cache: dict = {}

    crosses_all = detect_crosses(bars15, WIN_TO)
    crosses_win = [
        c
        for c in crosses_all
        if WIN_FROM <= _utc(datetime.fromisoformat(c["cross_time"])) <= WIN_TO
    ]

    anchor = _utc(datetime.fromisoformat(ANCHOR_BREAK))
    anchor_idx = last_closed_idx(bars15, anchor)
    anchor_ctx = ladder_at_index(candles15, bars15, anchor_idx, cfg, cache)
    regime_start = {
        "bull_break_time": ANCHOR_BREAK,
        "bull_regime_candidate_start": ANCHOR_BREAK,
        "rising_3_at_break": anchor_ctx["lower_cluster_rising_3"],
        "clusters_at_break": anchor_ctx.get("clusters_below_price", [])[:3],
        "cluster_1_top": anchor_ctx.get("cluster_1_top"),
        "cluster_2_top": anchor_ctx.get("cluster_2_top"),
        "cluster_3_top": anchor_ctx.get("cluster_3_top"),
        "gap_1_to_2": anchor_ctx.get("gap_1_to_2"),
        "gap_2_to_3": anchor_ctx.get("gap_2_to_3"),
        "overlap_1_2": anchor_ctx.get("overlap_1_2"),
        "overlap_2_3": anchor_ctx.get("overlap_2_3"),
    }

    # Also check bar after break if rising_3 false at exact cross close
    if anchor_idx is not None and anchor_idx + 1 < len(bars15):
        nxt = ladder_at_index(candles15, bars15, anchor_idx + 1, cfg, cache)
        regime_start["rising_3_next_bar"] = nxt["lower_cluster_rising_3"]

    bar_rows: list[dict] = []
    timeline: list[dict] = []
    prev_bar: dict | None = None
    prev_rising_3: bool | None = None
    cluster_events: list[dict] = []
    first_rising3_time = None
    last_continuous_rising3 = None

    for i, bar in enumerate(bars15):
        ct = bar["close_time"]
        if ct < WIN_FROM or ct > WIN_TO:
            prev_bar = bar
            continue
        ema = bar.get("ema200")
        if ema is None:
            prev_bar = bar
            continue
        ctx = ladder_at_index(candles15, bars15, i, cfg, cache)
        rising_3 = ctx["lower_cluster_rising_3"]
        if rising_3 and first_rising3_time is None and ct >= anchor:
            first_rising3_time = ct.isoformat()
        if rising_3:
            last_continuous_rising3 = ct.isoformat()
        if prev_rising_3 is True and not rising_3:
            cluster_events.append({"time": ct.isoformat(), "event": "rising_3_became_false"})
        if prev_rising_3 is False and rising_3:
            cluster_events.append({"time": ct.isoformat(), "event": "rising_3_became_true"})
        prev_rising_3 = rising_3

        pools, _, _ = scanner_pools_for_index(candles15, "15m", i, cfg, cache=cache)
        near_lower = lower_pool_near_ema(pools, ct, ema, float(bar["close"]))

        row = {
            "timestamp": ct.isoformat(),
            "close": bar["close"],
            "high": bar["high"],
            "low": bar["low"],
            "ema200": ema,
            "close_above_ema": bar["close"] > ema,
            "low_touch_ema": bar["low"] <= ema,
            "close_below_ema": bar["close"] <= ema,
            "distance_close_to_ema_pct": pct_dist(bar["close"], ema),
            "distance_low_to_ema_pct": pct_dist(bar["low"], ema),
            "contact_class": classify_ema_contact(bar, prev_bar),
            "rising_3": rising_3,
            "cluster_1_top": ctx.get("cluster_1_top"),
            "cluster_2_top": ctx.get("cluster_2_top"),
            "cluster_3_top": ctx.get("cluster_3_top"),
            "lower_pools_near_ema": near_lower,
        }
        bar_rows.append(row)

        if ct >= anchor:
            timeline.append(
                {
                    "timestamp": ct.isoformat(),
                    "event": "bar",
                    "price": bar["close"],
                    "ema200": ema,
                    "ema_state": "above" if bar["close"] > ema else "below",
                    "rising_3": rising_3,
                    "contact_class": row["contact_class"],
                    "notes": "",
                }
            )
        prev_bar = bar

    # R1–R4 after anchor break
    post = [r for r in bar_rows if _utc(datetime.fromisoformat(r["timestamp"])) >= anchor]
    r_markers: dict[str, Any] = {}

    clearly_above = lambda b: b["close_above_ema"] and (b["distance_close_to_ema_pct"] or 0) > 0.05

    for j, r in enumerate(post):
        ts = r["timestamp"]
        if "R1" not in r_markers and j > 0 and clearly_above(post[j - 1]) and r["low_touch_ema"]:
            r_markers["R1"] = {"time": ts, "bar": r}
        if "R2" not in r_markers and r["low_touch_ema"] and r["close_above_ema"]:
            r_markers["R2"] = {"time": ts, "bar": r}
        if "R3" not in r_markers and j > 0 and post[j - 1]["close_above_ema"] and r["low_touch_ema"]:
            r_markers["R3"] = {"time": ts, "bar": r}
        if "R4" not in r_markers and r["low_touch_ema"] and r["lower_pools_near_ema"]:
            r_markers["R4"] = {"time": ts, "bar": r, "pools": r["lower_pools_near_ema"]}

    # 12.06 old reset forensic
    reset_t = _utc(datetime.fromisoformat(OLD_RESET))
    reset_idx = last_closed_idx(bars15, reset_t)
    below_bars: list[dict] = []
    reclaim_bar = None
    max_below_pct = 0.0
    for r in post:
        t = _utc(datetime.fromisoformat(r["timestamp"]))
        if t < reset_t - timedelta(hours=6):
            continue
        if t > reset_t + timedelta(hours=24):
            break
        if r["close_below_ema"]:
            below_bars.append(r)
            d = r["distance_close_to_ema_pct"] or 0
            max_below_pct = min(max_below_pct, d)
        if reclaim_bar is None and below_bars and r["close_above_ema"] and t > reset_t:
            reclaim_bar = r

    reset_ctx = ladder_at_index(candles15, bars15, reset_idx, cfg, cache) if reset_idx else {}
    reset_bar = next((x for x in bar_rows if x["timestamp"] == OLD_RESET), None)
    june12_forensic = {
        "old_persistent_reset_time": OLD_RESET,
        "reset_bar": reset_bar,
        "wick_only": reset_bar and reset_bar["low_touch_ema"] and not reset_bar["close_below_ema"],
        "close_below": reset_bar and reset_bar["close_below_ema"],
        "closed_15m_bars_below_ema_12h_after": sum(
            1
            for r in post
            if reset_t <= _utc(datetime.fromisoformat(r["timestamp"])) <= reset_t + timedelta(hours=12)
            and r["close_below_ema"]
        ),
        "max_close_below_pct_near_reset": min(
            (r["distance_close_to_ema_pct"] or 0 for r in post if abs((_utc(datetime.fromisoformat(r["timestamp"])) - reset_t).total_seconds()) < 3600 * 6),
            default=0,
        ),
        "rising_3_at_reset": reset_ctx.get("lower_cluster_rising_3"),
        "reclaim_bars_after": [
            r["timestamp"]
            for r in post
            if _utc(datetime.fromisoformat(r["timestamp"])) > reset_t and r["close_above_ema"]
        ][:5],
    }
    # higher high after reset
    if reset_idx:
        hh = max(bars15[j]["high"] for j in range(reset_idx, min(len(bars15), reset_idx + 96)))
        june12_forensic["max_high_24h_after_reset"] = hh

    regime_end_candidates = {k: v["time"] for k, v in r_markers.items()}
    june16_touches = [r for r in bar_rows if r["timestamp"].startswith("2026-06-16") and r["low_touch_ema"]]
    first_june16_retest = june16_touches[0]["timestamp"] if june16_touches else "2026-06-16T23:45:00+00:00"

    # Shorts: bull start → first June-16 EMA retest from above (deskriptiv, nicht 12.06. Micro-Dip)
    end_bound = _utc(datetime.fromisoformat(first_june16_retest))

    sym = json.loads(SIGNAL_JSON.read_text())["symbols"]["XRPUSDT"]
    shorts_in_regime = []
    for t in sym["without_guard"]:
        ot = _utc(datetime.fromisoformat(t["entry_open"]))
        if ot not in by_open:
            continue
        dec = by_open[ot]["close_time"]
        if dec < anchor or dec > end_bound:
            continue
        idx = by_open[ot]["candle_index"]
        ctx = ladder_at_index(candles15, bars15, idx, cfg, cache, price=float(t["entry_price"]))
        oc = outcome_class(t, bars15, by_open)
        shorts_in_regime.append(
            {
                "signal_time": ot.isoformat(),
                "decision_time": dec.isoformat(),
                "entry": t["entry_price"],
                "mae_pct": t.get("mae_pct"),
                "mfe_pct": t.get("mfe_pct"),
                "first_hit": t.get("first_hit"),
                "final_outcome": oc,
                "price_above_ema200": ctx["price_above_ema200"],
                "rising_3": ctx["lower_cluster_rising_3"],
                "cluster_tops": [ctx.get("cluster_1_top"), ctx.get("cluster_2_top"), ctx.get("cluster_3_top")],
            }
        )

    # 16 June retest context — bars on 2026-06-16 with low_touch
    june16 = [r for r in bar_rows if r["timestamp"].startswith("2026-06-16") and r["low_touch_ema"]]
    june16_ctx = {
        "touch_bars": june16,
        "rising_3_on_touch_bars": [r for r in june16 if r["rising_3"]],
        "upper_targets": None,
    }
    if june16:
        idx16 = last_closed_idx(bars15, _utc(datetime.fromisoformat(june16[0]["timestamp"])))
        if idx16:
            pools, _, _ = scanner_pools_for_index(candles15, "15m", idx16, cfg, cache=cache)
            uppers = [
                {"pool_id": p["pool_id"], "bottom": float(p["bottom"]), "top": float(p["top"])}
                for p in pools
                if p["side"] == "upper"
                and p["known"] <= bars15[idx16]["close_time"]
                and float(p["bottom"]) > float(bars15[idx16]["close"])
            ]
            june16_ctx["upper_targets"] = sorted(uppers, key=lambda x: x["bottom"])[:5]

    # Timeline milestones
    milestones = [
        ("2026-06-11T17:45:00+00:00", "bull_ema200_break"),
        ("2026-06-11T19:30:00+00:00", "short_decision_11_06"),
        (OLD_RESET, "old_persistent_shadow_reset"),
        ("2026-06-06-15:00:00+00:00", "placeholder"),
    ]
    for ts, label in [
        (ANCHOR_BREAK, "bull_break"),
        ("2026-06-11T19:30:00+00:00", "short_11_06_decision"),
        (OLD_RESET, "old_persistent_reset"),
    ]:
        for tl in timeline:
            if tl["timestamp"] == ts:
                tl["event"] = label

    narrative_timeline = []
    for label, ts in [
        ("Bull EMA200 break", ANCHOR_BREAK),
        ("Short 11.06 decision", "2026-06-11T19:30:00+00:00"),
        ("Old persistent reset", OLD_RESET),
    ]:
        br = next((x for x in bar_rows if x["timestamp"] == ts), None)
        if br:
            narrative_timeline.append({**br, "milestone": label})

    # Pool ladder persistence summary
    ladder_persistence = {
        "first_rising3_time_after_anchor": first_rising3_time,
        "last_bar_with_rising3_in_window": last_continuous_rising3,
        "cluster_events": cluster_events,
        "first_rising3_false_after_anchor": next(
            (e for e in cluster_events if e["event"] == "rising_3_became_false" and e["time"] >= ANCHOR_BREAK),
            None,
        ),
    }

    old_ep = None
    if PERSISTENT_JSON.exists():
        pj = json.loads(PERSISTENT_JSON.read_text())
        old_ep = (pj.get("june_11") or {}).get("episode")
        if not old_ep:
            for ep in pj.get("transition_episodes", []):
                if isinstance(ep, dict) and ep.get("start") == ANCHOR_BREAK:
                    old_ep = ep
                    break

    report = {
        "meta": {
            "analysis_only": True,
            "symbol": SYMBOL,
            "window": [WIN_FROM.isoformat(), WIN_TO.isoformat()],
            "cluster_gap_pct": CLUSTER_GAP_PCT,
        },
        "bull_breaks_15m": crosses_win,
        "regime_start": regime_start,
        "old_persistent_episode_ref": old_ep,
        "ema_bars_after_break": [r for r in bar_rows if _utc(datetime.fromisoformat(r["timestamp"])) >= anchor],
        "retest_markers_R1_R4": r_markers,
        "regime_end_candidates": regime_end_candidates,
        "first_june16_ema_retest_from_above": first_june16_retest,
        "shorts_regime_window_end": first_june16_retest,
        "june_12_old_reset_forensic": june12_forensic,
        "narrative_timeline": narrative_timeline,
        "pool_ladder_persistence": ladder_persistence,
        "shorts_inside_candidate_regime": shorts_in_regime,
        "june_16_retest_context": june16_ctx,
        "long_hypothetical_context_only": {
            "note": "Keine Long-Signale in floor_guard_signal_list.",
            "at_june_16_touch": "Preis von oben, EMA-Retest-Kontakt siehe june_16_retest_context; Lower nahe EMA in touch bars.",
        },
        "bear_mirror_description": (
            "Bear: break below EMA200 + falling_3 upper clusters → regime until first retest from below."
        ),
        "candidate_regime_interpretation": (
            "Deskriptiv: 11.06. Break mit rising_3; 12.06. 06:45 Close unter EMA beendet alte Persistent-Logik, "
            "Reclaim folgt; strukturelle rising_3 und Expansion setzen fort bis ~16.06. EMA-Touch von oben (R-Marker)."
        ),
        "next_step": (
            "Shadow-Vergleich: Persistent-Reset vs. Retest-basiertes Regime-Ende auf gleichem Jun–Jul-Fenster, ohne Parameter-Tuning."
        ),
    }

    md = build_md(report)
    report["executive_summary"] = md
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    OUT_MD.write_text(md + "\n")

    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "close",
                "ema200",
                "close_above_ema",
                "contact_class",
                "rising_3",
                "cluster_1_top",
                "cluster_2_top",
                "cluster_3_top",
                "distance_close_to_ema_pct",
                "distance_low_to_ema_pct",
            ],
        )
        w.writeheader()
        for r in bar_rows:
            w.writerow({k: r.get(k) for k in w.fieldnames})

    print("wrote", OUT_JSON, OUT_MD, OUT_CSV)


def build_md(r: dict) -> str:
    rs = r["regime_start"]
    j12 = r["june_12_old_reset_forensic"]
    rm = r["retest_markers_R1_R4"]
    lines = [
        "# XRP EMA200 Break / Retest Regime Forensics v1",
        "",
        "Nur Analyse. Keine Produktionsänderung.",
        "",
        "### Bull break",
        f"Relevanter Break: **{ANCHOR_BREAK}** (15m). Alle Crosses im Fenster: {len(r['bull_breaks_15m'])}.",
        "",
        "### Pool structure at break",
        f"rising_3={rs['rising_3_at_break']}; tops {rs.get('cluster_1_top')}, {rs.get('cluster_2_top')}, {rs.get('cluster_3_top')}; "
        f"gaps {rs.get('gap_1_to_2')}, {rs.get('gap_2_to_3')}.",
        "",
        "### 12 June old reset",
        f"Zeit {OLD_RESET}; close_below={j12.get('close_below')}; wick_only={j12.get('wick_only')}; "
        f"bars_below_12h={j12.get('closed_15m_bars_below_ema_12h_after')}; rising_3={j12.get('rising_3_at_reset')}; "
        f"reclaim={j12.get('reclaim_bars_after')[:3] if j12.get('reclaim_bars_after') else []}.",
        "",
        "### EMA200 contacts after break",
        f"{len(r['ema_bars_after_break'])} bars dokumentiert (JSON/CSV).",
        "",
        "### First meaningful retest from above",
        json.dumps({k: v.get("time") for k, v in rm.items()}, indent=2),
        "",
        "### Pool ladder persistence",
        json.dumps(r["pool_ladder_persistence"], indent=2),
        "",
        "### Shorts inside regime",
        f"Fenster Ende (erster 16.06. EMA-Touch): {r.get('shorts_regime_window_end')}.",
        json.dumps(r["shorts_inside_candidate_regime"], indent=2),
        "",
        "### 16 June retest context",
        json.dumps(r["june_16_retest_context"], indent=2, default=str)[:4000],
        "",
        "### Candidate regime interpretation",
        r["candidate_regime_interpretation"],
        "",
        "### Next step",
        r["next_step"],
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()

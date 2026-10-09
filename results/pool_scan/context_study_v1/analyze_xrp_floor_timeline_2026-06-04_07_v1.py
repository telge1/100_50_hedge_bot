"""XRP floor timeline 2026-06-04..07 UTC — causal 4h/15m, no production changes."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OUT_JSON = ROOT / "xrp_floor_timeline_2026-06-04_07_v1.json"
OUT_MD = ROOT / "xrp_floor_timeline_2026-06-04_07_v1.md"
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
WINDOW_FROM = datetime(2026, 6, 4, 0, 0, tzinfo=timezone.utc)
WINDOW_TO = datetime(2026, 6, 8, 0, 0, tzinfo=timezone.utc)
LOW_15M = datetime(2026, 6, 6, 5, 0, tzinfo=timezone.utc)  # documented 15m low bar open
SIGNAL_ENTRIES = (
    datetime(2026, 6, 7, 1, 30, tzinfo=timezone.utc),
    datetime(2026, 6, 7, 1, 45, tzinfo=timezone.utc),
    datetime(2026, 6, 7, 2, 45, tzinfo=timezone.utc),
    datetime(2026, 6, 7, 3, 15, tzinfo=timezone.utc),
)
LAST_SHORT_OPEN = datetime(2026, 6, 4, 19, 15, tzinfo=timezone.utc)
LAST_SHORT_DECISION = datetime(2026, 6, 4, 19, 30, tzinfo=timezone.utc)
PROX_PCTS = (0.5, 0.7, 1.0)
CRASH_STAGE_MAX_BOTTOM = 1.15  # 15m lowers relevant to Jun crash leg


def _ensure_paths() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def load_15m() -> list[dict]:
    from backtester.short.dashboard.research_charts.lld_research_kernel import load_pane_candles
    from pool_scan.clock import bar_close

    _p, candles = load_pane_candles(
        "XRPUSDT",
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
    )
    bars = []
    for i, c in enumerate(candles):
        ot = _utc(c.timestamp)
        bars.append(
            {
                "index": i,
                "open_time": ot,
                "close_time": bar_close(ot, "15m"),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
            }
        )
    return bars


def active_4h_lowers(decision: datetime) -> list[dict]:
    from backtester.short.dashboard.research_charts.lld_research_kernel import (
        compute_lld_selected,
        filter_rows_at_decision,
        pool_row,
        resolve_tip,
        ui_lld_config,
    )
    from pool_scan.clock import bar_close

    from analyze_4h_lower_short_block_v1 import _load_4h_candles, h4_snapshot

    snap = h4_snapshot("XRPUSDT", decision)
    rows = []
    for item in snap.lowers:
        if item.get("status") != "active" or not item.get("known_at_decision"):
            continue
        rows.append(
            {
                "pool_id": item["pool_id"],
                "bottom": item["bottom"],
                "top": item["top"],
                "known": item["known"],
                "break_at": item.get("break_at"),
            }
        )
    rows.sort(key=lambda r: r["bottom"])
    return rows


def active_15m_lowers(candles_15m: list, index: int, decision: datetime) -> list[dict]:
    from backtester.short.dashboard.research_charts.lld_research_kernel import (
        compute_lld_selected,
        filter_rows_at_decision,
        pool_row,
        resolve_tip,
        ui_lld_config,
    )

    prefix = candles_15m[: index + 1]
    cfg = ui_lld_config("15m")
    selected, _, _, _ = compute_lld_selected(
        prefix, "15m", cfg, tip=resolve_tip(prefix, None)
    )
    rows = []
    for pool in selected:
        r = pool_row(pool)
        if not r or r["side"] != "lower":
            continue
        if r["known"] > decision:
            continue
        if r.get("break_at") and r["break_at"] <= decision:
            continue
        rows.append(
            {
                "pool_id": r["pool_id"],
                "bottom": r["bottom"],
                "top": r["top"],
                "known": r["known"].isoformat(),
            }
        )
    rows.sort(key=lambda r: r["bottom"])
    return rows


def dist_top_pct(close: float, top: float) -> float:
    return (close - top) / close * 100.0


def touches_pool(bar: dict, pool: dict) -> bool:
    return bar["low"] <= pool["top"] and bar["high"] >= pool["bottom"]


def ladder_15m(rows: list[dict], close: float) -> dict:
    """Measurable ladder: >=2 active lowers, ascending bottoms, close above deepest top."""
    if len(rows) < 2:
        return {"ladder_active": False, "rungs": len(rows)}
    bottoms = [r["bottom"] for r in rows]
    ascending = all(bottoms[i] <= bottoms[i + 1] for i in range(len(bottoms) - 1))
    deepest = rows[0]
    under_price = close > deepest["top"]
    gap_ok = all(rows[i + 1]["bottom"] - rows[i]["top"] >= -0.002 for i in range(len(rows) - 1))
    return {
        "ladder_active": ascending and under_price and gap_ok,
        "rungs": len(rows),
        "bottoms": bottoms[:4],
        "tops": [r["top"] for r in rows[:4]],
    }


def main() -> None:
    _ensure_paths()
    from backtester.short.dashboard.research_charts.lld_research_kernel import load_pane_candles

    bars = [b for b in load_15m() if WINDOW_FROM <= b["close_time"] < WINDOW_TO]
    _p15, candles_15m = load_pane_candles(
        "XRPUSDT",
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
    )

    first_touch = None
    series: list[dict] = []

    test_anchor: dict | None = None  # pool used at first touch
    first_test_close_time = None
    first_reclaim = None

    first_ladder = None
    first_ladder_pools = None
    pool_stage_events: list[dict] = []

    prev_15m_ids: set[str] = set()

    for bar in bars:
        decision = bar["close_time"]
        h4 = active_4h_lowers(decision)
        deepest = h4[0] if h4 else None
        idx = bar["index"]
        l15 = active_15m_lowers(candles_15m, idx, decision)
        lad = ladder_15m(l15, bar["close"])

        new_15m = [p for p in l15 if p["pool_id"] not in prev_15m_ids]
        for p in new_15m:
            pool_stage_events.append(
                {
                    "time": decision.isoformat(),
                    "event": "15m_lower_new_in_selection",
                    "pool_id": p["pool_id"],
                    "bottom": p["bottom"],
                    "top": p["top"],
                    "known": p["known"],
                    "close": bar["close"],
                }
            )
        prev_15m_ids = {p["pool_id"] for p in l15}

        if first_ladder is None and lad["ladder_active"]:
            first_ladder = decision.isoformat()
            first_ladder_pools = l15[:4]

        row = {
            "decision_close": decision.isoformat(),
            "close": bar["close"],
            "low": bar["low"],
            "high": bar["high"],
            "h4_active_count": len(h4),
            "h4_deepest": deepest,
            "h4_all_active": h4[:6],
            "l15_active_count": len(l15),
            "l15_ladder": lad,
        }
        series.append(row)

        if deepest is None:
            continue

        touched = touches_pool(bar, deepest)
        if first_touch is None and touched:
            first_touch = {
                "decision_close": decision.isoformat(),
                "open_time": bar["open_time"].isoformat(),
                "pool_id": deepest["pool_id"],
                "bottom": deepest["bottom"],
                "top": deepest["top"],
                "close": bar["close"],
                "low": bar["low"],
                "dist_to_top_pct": round(dist_top_pct(bar["close"], deepest["top"]), 4),
                "block_active_from": decision.isoformat(),
            }
            test_anchor = deepest
            first_test_close_time = decision

        if first_reclaim is None and test_anchor and first_test_close_time and decision > first_test_close_time:
            if bar["close"] > test_anchor["top"]:
                first_reclaim = {
                    "decision_close": decision.isoformat(),
                    "close": bar["close"],
                    "pool_id": test_anchor["pool_id"],
                    "pool_top": test_anchor["top"],
                    "after_first_test": first_test_close_time.isoformat(),
                }

    def scan_triggers(bar_list: list[dict], min_decision: datetime | None) -> dict:
        touch = None
        prox = {p: None for p in PROX_PCTS}
        anchor = None
        test_close = None
        reclaim = None
        for bar in bar_list:
            decision = bar["close_time"]
            if min_decision and decision <= min_decision:
                continue
            h4 = active_4h_lowers(decision)
            if not h4:
                continue
            deepest = h4[0]
            if touch is None and touches_pool(bar, deepest):
                touch = {
                    "decision_close": decision.isoformat(),
                    "open_time": bar["open_time"].isoformat(),
                    "pool_id": deepest["pool_id"],
                    "bottom": deepest["bottom"],
                    "top": deepest["top"],
                    "close": bar["close"],
                    "low": bar["low"],
                    "dist_to_top_pct": round(dist_top_pct(bar["close"], deepest["top"]), 4),
                    "block_active_from": decision.isoformat(),
                }
                anchor = deepest
                test_close = decision
            if bar["close"] > deepest["top"]:
                d = dist_top_pct(bar["close"], deepest["top"])
                for p in PROX_PCTS:
                    if prox[p] is None and d <= p:
                        prox[p] = {
                            "decision_close": decision.isoformat(),
                            "pool_id": deepest["pool_id"],
                            "bottom": deepest["bottom"],
                            "top": deepest["top"],
                            "close": bar["close"],
                            "dist_to_top_pct": round(d, 4),
                            "threshold_pct": p,
                            "block_active_from": decision.isoformat(),
                        }
            if reclaim is None and anchor and test_close and decision > test_close:
                if bar["close"] > anchor["top"]:
                    reclaim = {
                        "decision_close": decision.isoformat(),
                        "close": bar["close"],
                        "pool_id": anchor["pool_id"],
                        "pool_top": anchor["top"],
                        "after_first_test": test_close.isoformat(),
                    }
        return {
            "zone_touch_deepest_4h": touch,
            "close_within_pct_above_deepest_top": prox,
            "reclaim_after_test": reclaim,
        }

    first_prox_close_only = scan_triggers(bars, None)["close_within_pct_above_deepest_top"]
    if first_touch is None:
        first_touch = scan_triggers(bars, None)["zone_touch_deepest_4h"]
    after_short = scan_triggers(bars, LAST_SHORT_DECISION)
    first_touch = after_short["zone_touch_deepest_4h"] or first_touch
    first_prox_after_short = after_short["close_within_pct_above_deepest_top"]
    first_reclaim_after_short = after_short["reclaim_after_test"]

    # 15m low bar
    low_bar = next((b for b in bars if b["open_time"] == LOW_15M), None)
    sig_times = [t.isoformat() for t in SIGNAL_ENTRIES]
    crash_stages = [
        e
        for e in pool_stage_events
        if e["bottom"] < CRASH_STAGE_MAX_BOTTOM
        and _utc(datetime.fromisoformat(e["time"])) > LAST_SHORT_DECISION
    ]
    crash_by_known = sorted(crash_stages, key=lambda e: e["known"])
    first_15m_stage = crash_by_known[0] if len(crash_by_known) >= 1 else None
    second_15m_stage = crash_by_known[1] if len(crash_by_known) >= 2 else None

    ladder_after_short = None
    ladder_pools_after = None
    for bar in bars:
        if bar["close_time"] <= LAST_SHORT_DECISION:
            continue
        idx = bar["index"]
        l15 = active_15m_lowers(candles_15m, idx, bar["close_time"])
        lad = ladder_15m(l15, bar["close"])
        if lad["ladder_active"] and all(r["bottom"] < CRASH_STAGE_MAX_BOTTOM for r in l15[:2]):
            ladder_after_short = bar["close_time"].isoformat()
            ladder_pools_after = l15[:4]
            break

    def before(iso: str | None, ref: datetime) -> bool | None:
        if not iso:
            return None
        return _utc(datetime.fromisoformat(iso)) < ref

    trigger_iso = (
        after_short["zone_touch_deepest_4h"]["decision_close"]
        if after_short["zone_touch_deepest_4h"]
        else None
    )
    report = {
        "symbol": "XRPUSDT",
        "window_utc": [WINDOW_FROM.isoformat(), WINDOW_TO.isoformat()],
        "method": {
            "4h": "prefix pane; h4_snapshot / UI selection; active at 15m decision close",
            "15m": "prefix to bar; compute_lld_selected; known & not broken",
            "lookahead": False,
        },
        "first_trigger": {
            "full_window": {
                "zone_touch_deepest_4h": scan_triggers(bars, None)["zone_touch_deepest_4h"],
                "close_within_pct_above_deepest_top": first_prox_close_only,
            },
            "after_last_short_2026_06_04_1915": {
                "zone_touch_deepest_4h": after_short["zone_touch_deepest_4h"],
                "close_within_pct_above_deepest_top": first_prox_after_short,
                "note": "Primary leg for Jun 5-7 crash; decision after 2026-06-04T19:30Z",
            },
        },
        "confirmation": {
            "after_last_short": {
                "reclaim_15m_close_above_test_pool_top": first_reclaim_after_short,
                "first_15m_two_rung_ladder_crash_leg": {
                    "time": ladder_after_short,
                    "pools": ladder_pools_after,
                },
                "first_15m_lower_stage": first_15m_stage,
                "second_15m_lower_stage": second_15m_stage,
            },
            "full_window_legacy": {
                "reclaim": first_reclaim,
                "ladder": {"time": first_ladder, "pools": first_ladder_pools},
            },
            "15m_new_lower_in_selection_events": pool_stage_events,
        },
        "reference": {
            "june_6_15m_low_open": LOW_15M.isoformat(),
            "june_6_low_bar": low_bar,
            "short_signals_4_7_entry_open": sig_times,
        },
        "gates": {
            "trigger_before_june_6_low": before(trigger_iso, LOW_15M),
            "trigger_before_shorts_4_7": before(trigger_iso, SIGNAL_ENTRIES[0]),
            "ladder_crash_leg_before_june_6_low": before(ladder_after_short, LOW_15M),
            "ladder_crash_leg_before_shorts_4_7": before(ladder_after_short, SIGNAL_ENTRIES[0]),
        },
        "timeline_4h_snapshots": [
            {
                "decision": b["close_time"].isoformat(),
                "close": b["close"],
                "h4": active_4h_lowers(b["close_time"]),
            }
            for b in bars
            if b["close_time"]
            in {
                datetime(2026, 6, 4, 19, 30, tzinfo=timezone.utc),
                datetime(2026, 6, 5, 4, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 6, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 6, 4, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 6, 5, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 7, 1, 30, tzinfo=timezone.utc),
            }
        ],
        "timeline_series_sample": series[::16],
        "timeline_series_count": len(series),
    }

    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")

    t_touch = after_short["zone_touch_deepest_4h"]
    prox_as = first_prox_after_short
    lines = [
        "# XRP Boden-Timeline 04.–07.06.2026 UTC",
        "",
        "Kausal (Prefix-Pane, UI-LLD). Kein Lookahead.",
        "",
        "## 1. Erster Trigger — nach letztem Short (04.06. 19:15, Entscheidung > 19:30 UTC)",
        "",
    ]
    if t_touch:
        lines.append(
            f"- **Zonen-Touch (tiefster 4h-Lower):** `{t_touch['decision_close']}` | "
            f"`{t_touch['pool_id'].split(':')[-1]}` | {t_touch['bottom']:.5f}–{t_touch['top']:.5f} | "
            f"Close {t_touch['close']} | Block ab **{t_touch['block_active_from']}**"
        )
    else:
        lines.append("- **Zonen-Touch:** keiner nach 19:30 UTC.")
    lines.append("")
    lines.append("| Schwelle (Close über Top) | Erster 15m-Close | Pool | Abstand % | Block ab |")
    lines.append("|---------------------------|------------------|------|-----------|----------|")
    for p in PROX_PCTS:
        rec = prox_as.get(p)
        if rec:
            lines.append(
                f"| ≤{p}% | {rec['decision_close']} | `{rec['pool_id'].split(':')[-1]}` | "
                f"{rec['dist_to_top_pct']} | {rec['block_active_from']} |"
            )
        else:
            lines.append(f"| ≤{p}% | — | — | — | — |")
    lines.extend(["", "## 2. Bestätigung (Crash-Leg)", ""])
    if first_reclaim_after_short:
        r = first_reclaim_after_short
        lines.append(
            f"- **15m-Reclaim** nach Test: `{r['decision_close']}` Close {r['close']} > Top {r['pool_top']}"
        )
    else:
        lines.append("- **15m-Reclaim:** nicht gefunden nach erstem Post-Short-Test.")
    if first_15m_stage:
        lines.append(f"- **1. 15m-Stufe:** `{first_15m_stage['time']}` known `{first_15m_stage['known']}` "
                     f"{first_15m_stage['bottom']:.4f}–{first_15m_stage['top']:.4f}")
    if second_15m_stage:
        lines.append(f"- **2. 15m-Stufe:** `{second_15m_stage['time']}` known `{second_15m_stage['known']}` "
                     f"{second_15m_stage['bottom']:.4f}–{second_15m_stage['top']:.4f}")
    if ladder_after_short:
        lines.append(
            f"- **15m-Leiter (≥2 Rungen, bottom<{CRASH_STAGE_MAX_BOTTOM}):** `{ladder_after_short}`"
        )
    lines.extend(["", "## UTC-Zeitleiste (Kernpunkte)", ""])
    lines.append("| Phase | Zeit (15m decision close) | Detail |")
    lines.append("|-------|---------------------------|--------|")
    if t_touch:
        lines.append(f"| Erster 4h-Test (post-Short) | {t_touch['decision_close']} | Touch |")
    for p in PROX_PCTS:
        r = prox_as.get(p)
        if r:
            lines.append(f"| Prox ≤{p}% | {r['decision_close']} | Close über Top |")
    if first_reclaim_after_short:
        lines.append(f"| Reclaim | {first_reclaim_after_short['decision_close']} | 15m > Test-Top |")
    if ladder_after_short:
        lines.append(f"| 15m-Leiter | {ladder_after_short} | Crash-Leg |")
    if low_bar:
        lines.append(
            f"| Markt-Tief (~) | {low_bar['close_time']} | Low {low_bar['low']} Close {low_bar['close']} |"
        )
    lines.append(f"| Shorts 4–7 | {sig_times[0]} … | Entry opens |")
    lines.extend(["", "## Fazit", ""])
    g = report["gates"]
    lines.append(
        f"- Vorsorglicher **4h-Test-Block** vor 06.06.-Tief: **{g['trigger_before_june_6_low']}**; "
        f"vor Shorts 4–7: **{g['trigger_before_shorts_4_7']}**."
    )
    lines.append(
        f"- **15m-Leiter-Bestätigung** vor Tief: **{g['ladder_crash_leg_before_june_6_low']}**; "
        f"vor Shorts 4–7: **{g['ladder_crash_leg_before_shorts_4_7']}**."
    )
    lines.append(
        "- **Trennung:** Erster Trigger = 4h-Test/Nähe; Bestätigung = Reclaim + 15m-Pool-Leiter (später)."
    )
    OUT_MD.write_text("\n".join(lines) + "\n")
    print("wrote", OUT_JSON, OUT_MD)
    print(json.dumps(report["first_trigger"], indent=2)[:2000])


if __name__ == "__main__":
    main()

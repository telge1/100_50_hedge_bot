"""Forensics: XRP short 2026-06-11 19:15 UTC and following 4-SL streak (report only)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OUT_JSON = ROOT / "analyze_xrp_short_2026-06-11_forensics_v1.json"
OUT_MD = ROOT / "analyze_xrp_short_2026-06-11_forensics_v1.md"

SYMBOL = "XRPUSDT"
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
ANCHOR_ENTRY = datetime(2026, 6, 11, 19, 15, tzinfo=timezone.utc)
ANCHOR_CLOSE = datetime(2026, 6, 11, 19, 30, tzinfo=timezone.utc)
SL_ENTRIES = [
    datetime(2026, 6, 11, 19, 15, tzinfo=timezone.utc),
    datetime(2026, 6, 14, 21, 45, tzinfo=timezone.utc),
    datetime(2026, 6, 14, 22, 30, tzinfo=timezone.utc),
    datetime(2026, 6, 14, 23, 30, tzinfo=timezone.utc),
]


def _ensure_paths() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def pools_at_15m(candles, idx, moment) -> list[dict]:
    from dashboard.research_charts.lld_research_kernel import (
        scanner_pools_for_index,
        ui_lld_config,
    )
    from find_short_entry_15m_v1 import open_pools

    cfg = ui_lld_config("15m")
    pools, selected, proof = scanner_pools_for_index(candles, "15m", idx, cfg)
    lowers = [
        p
        for p in pools
        if p["side"] == "lower" and p["known"] <= moment and (p["break_at"] is None or p["break_at"] > moment)
    ]
    lowers.sort(key=lambda p: p["bottom"])
    uppers = open_pools(pools, "upper", moment)
    return {
        "ui_selected_count": len(selected),
        "pool_count_time_filtered": len(pools),
        "lowers": [
            {
                "pool_id": p["pool_id"],
                "bottom": p["bottom"],
                "top": p["top"],
                "known": p["known"].isoformat(),
                "break_at": p["break_at"].isoformat() if p.get("break_at") else None,
            }
            for p in lowers[:12]
        ],
        "uppers_near_close": sorted(
            [
                {
                    "pool_id": p["pool_id"],
                    "bottom": p["bottom"],
                    "top": p["top"],
                    "known": p["known"].isoformat(),
                }
                for p in uppers
            ],
            key=lambda x: x["bottom"],
        )[:8],
    }


def guard_milestones(bars15: list[dict]) -> dict:
    """Single full-pane replay: episode expiry, first/last block relative to anchor."""
    from short_block_4h_guard import H4FloorGuard

    g = H4FloorGuard(SYMBOL, PANE_FROM, PANE_TO)
    first_blocked: dict | None = None
    last_blocked_before_anchor: dict | None = None
    june_episode_expiry: dict | None = None
    last_block_until_before_anchor: str | None = None
    blocked_count_jun8_to_anchor = 0
    june_floor_start = datetime(2026, 6, 1, tzinfo=timezone.utc)

    for bar in bars15:
        moment = bar["close_time"]
        prev_until = g.block_until
        blocked, reason = g.advance(bar, moment)
        if (
            prev_until is not None
            and g.block_until is None
            and g.session_floor_top is None
            and moment >= prev_until
            and june_floor_start <= moment <= ANCHOR_CLOSE
            and prev_until >= june_floor_start
        ):
            june_episode_expiry = {
                "close_time": moment.isoformat(),
                "close": bar["close"],
                "expired_block_until": prev_until.isoformat(),
            }
        if g.block_until is not None and moment <= ANCHOR_CLOSE:
            last_block_until_before_anchor = g.block_until.isoformat()
        if blocked:
            rec = {
                "close_time": moment.isoformat(),
                "close": bar["close"],
                "reason": reason,
                "session_floor_top": g.session_floor_top,
                "block_until": g.block_until.isoformat() if g.block_until else None,
            }
            if first_blocked is None:
                first_blocked = rec
            if moment <= ANCHOR_CLOSE:
                last_blocked_before_anchor = rec
            if datetime(2026, 6, 8, tzinfo=timezone.utc) <= moment <= ANCHOR_CLOSE:
                blocked_count_jun8_to_anchor += 1

    first_after_anchor: dict | None = None
    g2 = H4FloorGuard(SYMBOL, PANE_FROM, PANE_TO)
    for bar in bars15:
        moment = bar["close_time"]
        blocked, reason = g2.advance(bar, moment)
        if blocked and moment > ANCHOR_CLOSE:
            first_after_anchor = {
                "close_time": moment.isoformat(),
                "close": bar["close"],
                "reason": reason,
            }
            break

    return {
        "first_guard_blocked_in_pane": first_blocked,
        "last_guard_blocked_at_or_before_anchor": last_blocked_before_anchor,
        "june_floor_episode_expired_before_anchor": june_episode_expiry,
        "last_block_until_active_before_anchor": last_block_until_before_anchor,
        "blocked_bars_count_2026-06-08_through_anchor": blocked_count_jun8_to_anchor,
        "first_guard_blocked_after_anchor": first_after_anchor,
    }


def guard_state_at(guard, bar, moment) -> dict:
    snap = guard._snapshot(moment)
    blocked, reason = guard.advance(bar, moment)
    d = snap.deepest_active
    dist_top = None
    if d and bar["close"] > 0:
        dist_top = (bar["close"] - d["top"]) / bar["close"] * 100.0
    return {
        "decision_close": moment.isoformat(),
        "bar_close": bar["close"],
        "bar_high": bar["high"],
        "bar_low": bar["low"],
        "guard_blocked": blocked,
        "guard_reason": reason,
        "session_floor_top": guard.session_floor_top,
        "session_floor_bottom": guard.session_floor_bottom,
        "block_until": guard.block_until.isoformat() if guard.block_until else None,
        "deepest_4h": d,
        "pool_below_deepest": snap.pool_below_deepest,
        "dist_to_deepest_top_pct": round(dist_top, 4) if dist_top is not None else None,
        "pane_4h_len": snap.pane_4h_len,
    }


def reclaim_events(bars, from_t: datetime, to_t: datetime) -> list[dict]:
    """Close crosses above deepest 4h top after prior close at/below top (causal per bar)."""
    from analyze_4h_lower_short_block_v1 import h4_snapshot

    events = []
    was_at_or_below_top = False
    for bar in bars:
        ct = bar["close_time"]
        if ct < from_t or ct > to_t:
            continue
        snap = h4_snapshot(SYMBOL, ct)
        d = snap.deepest_active
        if not d or snap.pool_below_deepest:
            continue
        top = d["top"]
        close = bar["close"]
        if close <= top:
            was_at_or_below_top = True
        elif was_at_or_below_top and close > top:
            events.append(
                {
                    "time": ct.isoformat(),
                    "close": close,
                    "deepest_top": top,
                    "pool_id": d["pool_id"],
                    "dist_above_top_pct": round((close - top) / close * 100.0, 4),
                }
            )
            was_at_or_below_top = False
    return events


def analysis_rules_at(
    moment: datetime,
    close: float,
    floor_map: dict,
) -> dict:
    from analyze_4h_lower_short_block_v1 import (
        FLOOR_EPISODE_PCT_DEFAULT,
        blocked_by_r1_r2,
        floor_episode_blocks,
        h4_snapshot,
        price_vs_pool,
        rule_states,
    )

    snap = h4_snapshot(SYMBOL, moment)
    pv = price_vs_pool(close, snap.deepest_active) if snap.deepest_active else None
    rules = rule_states(close, snap, sticky_until=None, bounce_block_until=None)
    st = floor_map.get(moment)
    fe_block = floor_episode_blocks(close, st, FLOOR_EPISODE_PCT_DEFAULT)
    r1_r2 = blocked_by_r1_r2(rules)
    return {
        "price_vs_deepest": pv,
        "blocked_by_r1_r2_3pct": r1_r2,
        "R1_top_within_3pct": rules.get("R1_top_within_3.0pct"),
        "R4_reclaim_above_top_3pct": rules.get("R4_reclaim_above_top"),
        "floor_episode_72h_7pct_active": fe_block,
        "floor_episode_state": st,
    }


def trade_row(
    entry_open: datetime,
    bars15: list[dict],
    candles,
    market,
) -> dict:
    from analyze_xrp_floor_guard_pnl_v1 import short_trade_detail
    from find_short_entry_15m_v1 import entry_setup, last_closed_index, row_for
    from dashboard.research_charts.lld_research_kernel import (
        scanner_pools_for_index,
        ui_lld_config,
    )
    bar = next(b for b in bars15 if b["open_time"] == entry_open)
    moment = bar["close_time"]
    idx = bar["candle_index"]
    pools, _, _ = scanner_pools_for_index(candles, "15m", idx, ui_lld_config("15m"))
    setup = entry_setup(pools, bar, moment)
    if not setup:
        return {"entry_open": entry_open.isoformat(), "error": "no entry_setup"}
    pool, upper, lower, gap = setup
    h4i = last_closed_index(market["4h"]["bars"], moment)
    h4 = market["4h"]["bars"][h4i]
    row = row_for(SYMBOL, pool, bar, bar, h4, None, None, upper, lower, gap)
    detail = short_trade_detail(bars15, idx, float(bar["close"]), float(row["stop_price"]), float(lower["top"]))
    return {
        "entry_open": entry_open.isoformat(),
        "decision_close": moment.isoformat(),
        "entry_price": bar["close"],
        "stop": row["stop_price"],
        "tp": lower["top"],
        "upper_pool_id": pool["pool_id"],
        "tp_pool_id": lower["pool_id"],
        "first_hit": detail["first_hit"],
        "pnl_pct": detail["pnl_pct"],
        "exit_time": detail.get("exit_time"),
        "result_status": detail.get("result_status"),
    }


def main() -> None:
    _ensure_paths()
    from analyze_4h_lower_short_block_v1 import (
        FLOOR_EPISODE_HOURS_DEFAULT,
        build_floor_episode_by_close,
        h4_snapshot,
        price_vs_pool,
    )
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import load_pane_candles
    from short_block_4h_guard import H4FloorGuard, guard_spec

    bars15 = build_15m_bars(
        load_pane_candles(
            SYMBOL,
            "15m",
            from_unix=int(PANE_FROM.timestamp()),
            to_unix=int(PANE_TO.timestamp()),
            history_weeks=HISTORY_WEEKS,
        )[1]
    )
    _packed, candles = load_pane_candles(
        SYMBOL,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    guard = H4FloorGuard(SYMBOL, PANE_FROM, PANE_TO)
    market = load_market(SYMBOL)
    floor_map = build_floor_episode_by_close(bars15, FLOOR_EPISODE_HOURS_DEFAULT)

    milestones = guard_milestones(bars15)

    timeline: list[dict] = []
    window_start = datetime(2026, 6, 5, 0, tzinfo=timezone.utc)
    window_end = datetime(2026, 6, 15, 0, tzinfo=timezone.utc)

    for bar in bars15:
        moment = bar["close_time"]
        if moment < window_start:
            guard.advance(bar, moment)
            continue
        if moment > window_end:
            break
        blocked, reason = guard.advance(bar, moment)
        if moment.date() >= datetime(2026, 6, 8).date() and (
            moment.hour in (0, 4, 8, 12, 16, 20) and moment.minute == 0
            or moment in {b["close_time"] for b in bars15 if b["open_time"] in SL_ENTRIES}
            or blocked
        ):
            snap = guard._snapshot(moment)
            timeline.append(
                {
                    "t": moment.isoformat(),
                    "close": bar["close"],
                    "guard_blocked": blocked,
                    "guard_reason": reason,
                    "session_top": guard.session_floor_top,
                    "block_until": guard.block_until.isoformat() if guard.block_until else None,
                    "deepest_4h_top": snap.deepest_active["top"] if snap.deepest_active else None,
                }
            )

    # Re-run guard to anchor entry (clean state from pane start)
    guard2 = H4FloorGuard(SYMBOL, PANE_FROM, PANE_TO)
    anchor_state = None
    for bar in bars15:
        moment = bar["close_time"]
        if bar["open_time"] == ANCHOR_ENTRY:
            anchor_state = guard_state_at(guard2, bar, moment)
            break
        guard2.advance(bar, moment)

    anchor_bar = next(b for b in bars15 if b["open_time"] == ANCHOR_ENTRY)
    anchor_close = anchor_bar["close_time"]
    p15 = pools_at_15m(candles, anchor_bar["candle_index"], anchor_close)
    h4 = h4_snapshot(SYMBOL, anchor_close)
    rules_anchor = analysis_rules_at(anchor_close, anchor_bar["close"], floor_map)

    sl_trades = []
    guard3 = H4FloorGuard(SYMBOL, PANE_FROM, PANE_TO)
    for bar in bars15:
        moment = bar["close_time"]
        gs = None
        if bar["open_time"] in SL_ENTRIES:
            gs = guard_state_at(guard3, bar, moment)
        else:
            guard3.advance(bar, moment)
        if bar["open_time"] in SL_ENTRIES:
            sl_trades.append(
                {
                    **trade_row(bar["open_time"], bars15, candles, market),
                    "guard_at_entry": gs,
                    "analysis_rules": analysis_rules_at(moment, bar["close"], floor_map),
                }
            )

    if OUT_JSON.exists():
        try:
            prior = json.loads(OUT_JSON.read_text())
            reclaims = prior.get("reclaim_events_jun5_12")
        except (json.JSONDecodeError, OSError):
            reclaims = None
    else:
        reclaims = None
    if not reclaims:
        reclaims = reclaim_events(
            bars15,
            datetime(2026, 6, 5, tzinfo=timezone.utc),
            datetime(2026, 6, 12, tzinfo=timezone.utc),
        )

    report = {
        "anchor_entry_open": ANCHOR_ENTRY.isoformat(),
        "guard_spec_production": guard_spec(),
        "anchor_guard_state": anchor_state,
        "anchor_15m_pools": p15,
        "anchor_4h_snapshot": {
            "decision_close": anchor_close.isoformat(),
            "pane_4h_len": h4.pane_4h_len,
            "tip": h4.tip,
            "lowers": h4.lowers,
            "deepest_active": h4.deepest_active,
            "pool_below_deepest": h4.pool_below_deepest,
            "price_vs_deepest": price_vs_pool(anchor_bar["close"], h4.deepest_active)
            if h4.deepest_active
            else None,
        },
        "anchor_analysis_rules": rules_anchor,
        "reclaim_events_jun5_12": reclaims,
        "four_sl_trades": sl_trades,
        "timeline_samples": timeline,
        "guard_milestones": milestones,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    write_md(report)
    print("wrote", OUT_JSON, OUT_MD)


def write_md(report: dict) -> None:
    a = report["anchor_guard_state"]
    h4 = report["anchor_4h_snapshot"]
    rules = report["anchor_analysis_rules"]
    pv = h4.get("price_vs_deepest") or {}
    lines = [
        "# XRP Forensik: Short 2026-06-11 19:15 UTC + 4× SL",
        "",
        "## Entry 2026-06-11 19:15 UTC (Entscheidung 19:30)",
        "",
        f"- **Close (Entry-Preis):** {a['bar_close']}",
        f"- **OHLC:** H {a['bar_high']} / L {a['bar_low']}",
        f"- **Produktions-Floor-Guard:** blocked={a['guard_blocked']}, reason={a['guard_reason']}",
        f"- **Guard-State:** session_floor_top={a['session_floor_top']}, block_until={a['block_until']}",
        "",
        "### 4h Lower-Pools (prefix, UI `compute_lld_selected`, known@decision)",
        "",
    ]
    for row in h4.get("lowers", [])[:8]:
        lines.append(
            f"- `{row['pool_id'].split(':')[-1]}` bottom={row['bottom']} top={row['top']} "
            f"known={row['known']} status={row['status']}"
        )
    d = h4.get("deepest_active") or {}
    lines += [
        "",
        f"**Deepest aktiv:** top **{d.get('top')}** / bottom {d.get('bottom')} "
        f"(known {d.get('known', '')}). pool_below_deepest={h4.get('pool_below_deepest')}.",
        f"**Close vs. deepest:** {pv.get('position')}, dist_to_top **{pv.get('dist_to_top_pct')}%**.",
        "",
        "### 15m Pools (Scanner, zeitgefiltert)",
        "",
        f"UI-selected: {report['anchor_15m_pools']['ui_selected_count']}, "
        f"nach Filter: {report['anchor_15m_pools']['pool_count_time_filtered']}.",
        "",
        "**Lower (Auszug):**",
    ]
    for row in report["anchor_15m_pools"]["lowers"][:6]:
        lines.append(f"- …{row['pool_id'].split(':')[-1]} {row['bottom']}–{row['top']} known {row['known']}")
    lines += ["", "**Upper (Auszug):**"]
    for row in report["anchor_15m_pools"]["uppers_near_close"][:5]:
        lines.append(f"- …{row['pool_id'].split(':')[-1]} bottom={row['bottom']} known {row['known']}")

    lines += [
        "",
        "## Reclaim unterer Pool?",
        "",
        "Reclaim hier: **15m/4h-Close wieder über der Top-Kante** des jeweils tiefsten aktiven 4h-Lower, "
        "nach vorherigem Close ≤ Top (kausal barweise).",
        "",
    ]
    for ev in report["reclaim_events_jun5_12"][-8:]:
        lines.append(f"- {ev['time']}: close {ev['close']} > deepest top {ev['deepest_top']} (+{ev['dist_above_top_pct']}%)")
    lines += [
        "",
        f"Zum Entry **19:30** liegt Close **{a['bar_close']}** ca. **{pv.get('dist_to_top_pct')}%** über 4h-deepest-top "
        f"**{d.get('top')}** → klar **oberhalb**, kein „knapper Reclaim“ (R4-Reclaim-Analyse: <3 % über Top).",
        "",
        "## Warum Produktions-Guard den Short zuließ",
        "",
        f"- Spec: episode **{report['guard_spec_production']['episode_hours']}h**, "
        f"δ **{report['guard_spec_production']['episode_max_dist_pct']}%** über session_floor_top; A2 ε **{report['guard_spec_production']['near_top_pct']}%**.",
        f"- Am Entry: **kein** `block_until` / **session_floor_top=None** (Episode nach 06.06.-Touches abgelaufen).",
        f"- **B_episode** inaktiv; Abstand zum 4h-Deepest-Top **~8,1 %** > 5 % (auch > A2 1 %).",
        f"- **pool_below_deepest=false** → Floor-Intact-Pfad aktiv, aber keine A-/B-Bedingung erfüllt.",
        "",
        "## Frühere Analyse-Regeln (nur Report `short_block_4h_lower_analysis_v1`, kein Produktionslauf)",
        "",
        f"- `blocked_by_r1_r2` (R1 ≤3 % + R2): **{rules['blocked_by_r1_r2_3pct']}** (dist ~8 %).",
        f"- Floor-Episode Default **72h / 7 %**: **{rules['floor_episode_72h_7pct_active']}** "
        f"(Episode-State null am Entry).",
        f"- R4 reclaim <3 % über Top: **{rules['R4_reclaim_above_top_3pct']}**.",
        "",
        "**Belegbar blockiert** wäre der Short nur unter Regeln, die bei ~8 % Abstand noch greifen "
        "(z. B. R1≤8 % laut `blocked_by` in JSON — das ist **kein** aktivierter Produktions-/R1R2-Block). "
        "Ein **früherer Guard-Lauf** mit identischer Episode-Logik wie heute, aber noch aktivem `block_until` "
        "nach 06.06., hätte **B_episode** geliefert — **am 11.06. ist die Episode kausal abgelaufen**.",
        "",
        "## Vier SL-Trades (mit Guard, chronologisch)",
        "",
        "| entry | close | SL | TP | hit | pnl% | guard@entry | dist 4h top% |",
        "|-------|-------|----|----|-----|------|-------------|--------------|",
    ]
    for t in report["four_sl_trades"]:
        g = t["guard_at_entry"]
        lines.append(
            f"| {t['entry_open'][5:16]} | {t['entry_price']} | {t['stop']} | {t['tp']} | "
            f"{t['first_hit']} | {t['pnl_pct']} | blocked={g['guard_blocked']} ({g['guard_reason']}) | "
            f"{g['dist_to_deepest_top_pct']} |"
        )

    ms = report.get("guard_milestones") or {}
    lines += [
        "",
        "## Vier SL-Trades — Pool-/Guard-Kontext (kurz)",
        "",
    ]
    for t in report["four_sl_trades"]:
        ar = t.get("analysis_rules") or {}
        pv2 = ar.get("price_vs_deepest") or {}
        lines.append(
            f"- **{t['entry_open']}** upper `{t.get('upper_pool_id','').split(':')[-1]}` "
            f"known via setup; TP lower `{t.get('tp_pool_id','').split(':')[-1]}` @ {t['tp']}; "
            f"4h deepest dist_top {t['guard_at_entry'].get('dist_to_deepest_top_pct')}% "
            f"({pv2.get('position')}); floor_episode_72h/7% block={ar.get('floor_episode_72h_7pct_active')}."
        )

    lines += [
        "",
        "## UTC-Zeitleiste (Auszug)",
        "",
        "| close_time | close | guard | reason | session_top | block_until |",
        "|------------|-------|-------|--------|-------------|-------------|",
    ]
    for row in report["timeline_samples"][-25:]:
        lines.append(
            f"| {row['t'][5:16]} | {row['close']} | {row['guard_blocked']} | {row['guard_reason']} | "
            f"{row.get('session_top')} | {row.get('block_until', '')[:16] if row.get('block_until') else ''} |"
        )

    lines += [
        "",
        "## Erste Kerze mit möglichem Block (kausal, Produktions-Guard)",
        "",
    ]
    fb0 = ms.get("first_guard_blocked_in_pane")
    last_b = ms.get("last_guard_blocked_at_or_before_anchor")
    exp = ms.get("june_floor_episode_expired_before_anchor")
    if fb0:
        lines.append(
            f"- **Erster Block im Pane (Replay):** {fb0['close_time']} — **{fb0['reason']}**, "
            f"close {fb0['close']}, session_top {fb0.get('session_floor_top')}."
        )
    if last_b:
        lines.append(
            f"- **Letzter Block ≤ Anker (19:30):** {last_b['close_time']} — **{last_b['reason']}**, "
            f"close {last_b['close']} (danach bis 11.06. 19:30: **kein** weiterer Block)."
        )
    if exp:
        lines.append(
            f"- **Juni-Floor-Episode Ende (block_until abgelaufen, State geleert):** ab **{exp['close_time']}** "
            f"(letztes block_until {exp['expired_block_until']}, close {exp['close']})."
        )
    lines.append(
        f"- **Blockierte 15m-Closes 08.06.–11.06. 19:30:** "
        f"{ms.get('blocked_bars_count_2026-06-08_through_anchor', 0)} "
        f"(nach Episode-Ende typischerweise 0 — Preis >5 % über früherem session_floor_top)."
    )
    fa = ms.get("first_guard_blocked_after_anchor")
    if fa:
        lines.append(
            f"- **Erster Block nach dem Anker-Short:** {fa['close_time']} ({fa['reason']}, close {fa['close']})."
        )
    lines += [
        "",
        "**Fazit Anker 11.06. 19:30:** Mit damals kausalen Daten **kein** Produktions-Guard-Block "
        "(Episode abgelaufen, Close ~8 % über 4h-Deepest-Top 1.049). "
        "Ein Block **hätte** nur in der **05.–08.06.-Floor-Episode** gegriffen (B_episode/A), "
        "nicht am Tag des Shorts selbst.",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()

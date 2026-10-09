"""Apr/May low-trade forensics for e1r_4month_oos (read-only, frozen E1R)."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).resolve().parent
V1 = OUT_DIR.parents[1] / "context_study_v1"
REPO = OUT_DIR.parents[3]
COINS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
FOUR_MO_FROM = datetime(2026, 4, 1, tzinfo=timezone.utc)
FOUR_MO_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)

MONTHS = ("2026-04", "2026-05", "2026-06", "2026-07")
DAYS_PER_MONTH = {"2026-04": 30, "2026-05": 31, "2026-06": 30, "2026-07": 31}


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _ensure_paths() -> None:
    for p in (str(REPO), str(V1)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def month_key_from_moment(moment: datetime) -> str | None:
    m = moment.strftime("%Y-%m")
    return m if m in DAYS_PER_MONTH else None


def month_key_entry(entry_open: str) -> str:
    return entry_open[:7]


def classify_e1r(t: dict) -> str:
    st = t.get("E1R_status", "")
    ign = bool(t.get("ignored_E1R"))
    if st == "PENDING_NOT_RESOLVED":
        return "pending_reclaim_allowed"
    if ign and st == "ACTIVE":
        return "e1r_active_bull_rising3_blocked"
    if not ign:
        return "e1r_not_active_allowed"
    return "other_not_e1r"


def collect_funnel(symbol: str, report_from: datetime, report_to: datetime) -> dict[str, Any]:
    from find_short_entry_15m_v1 import (
        HISTORY_WEEKS,
        build_15m_bars,
        down_4h,
        entry_setup,
        last_closed_index,
        load_market,
        row_for,
    )
    from dashboard.research_charts.lld_research_kernel import (
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )
    from short_block_4h_guard import H4FloorGuard

    import analyze_floor_guard_signal_list_v1 as fg

    market = load_market(symbol)
    _packed, candles = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    lld_cfg = ui_lld_config("15m")
    bars15 = build_15m_bars(candles)
    by_open = {bar["open_time"]: bar for bar in bars15}

    guard = H4FloorGuard(symbol, PANE_FROM, PANE_TO)
    cache: dict[int, tuple] = {}
    seen_no_guard: set[str] = set()
    seen_with_guard: set[str] = set()
    no_guard: list[dict] = []
    with_guard: list[dict] = []

    raw_by_m: dict[str, int] = defaultdict(int)
    setup_no_down4h_by_m: dict[str, int] = defaultdict(int)
    floor_block_events_by_m: dict[str, int] = defaultdict(int)
    guard_block_new_pool_by_m: dict[str, int] = defaultdict(int)

    for bar in bars15:
        moment = bar["close_time"]
        blocked, _reason = guard.advance(bar, moment)
        if moment < report_from or moment > report_to:
            continue
        mk = month_key_from_moment(moment)
        if not mk:
            continue
        idx = bar["candle_index"]
        pools, _ui, _proof = scanner_pools_for_index(candles, "15m", idx, lld_cfg, cache=cache)
        setup = entry_setup(pools, bar, moment)
        if setup is None:
            continue
        pool, upper, lower, gap = setup
        h4_index = last_closed_index(market["4h"]["bars"], moment)
        if not down_4h(market["4h"]["bars"], h4_index):
            setup_no_down4h_by_m[mk] += 1
            continue
        raw_by_m[mk] += 1
        h4 = market["4h"]["bars"][h4_index]
        source_t = pool.get("source")
        birth = by_open.get(source_t, bar) if source_t is not None else bar
        row = row_for(symbol, pool, birth, bar, h4, None, None, upper, lower, gap)
        if blocked:
            floor_block_events_by_m[mk] += 1
        would_new_no = pool["pool_id"] not in seen_no_guard
        would_new_with = pool["pool_id"] not in seen_with_guard and not blocked
        if would_new_no:
            seen_no_guard.add(pool["pool_id"])
            no_guard.append(row)
        if blocked and would_new_no:
            guard_block_new_pool_by_m[mk] += 1
        if would_new_with:
            seen_with_guard.add(pool["pool_id"])
            with_guard.append(row)

    from analyze_floor_guard_signal_list_v1 import enrich_and_drawdown

    wg = enrich_and_drawdown(symbol, "with_guard", with_guard, bars15)
    ng = enrich_and_drawdown(symbol, "without_guard", no_guard, bars15)
    return {
        "symbol": symbol,
        "bars15": bars15,
        "with_guard": wg,
        "without_guard": ng,
        "raw_by_month": dict(raw_by_m),
        "setup_no_down4h_by_month": dict(setup_no_down4h_by_m),
        "floor_block_events_by_month": dict(floor_block_events_by_m),
        "guard_block_new_pool_by_month": dict(guard_block_new_pool_by_m),
    }


def apply_e1r(trades: list[dict], symbol: str, report_from: datetime, report_to: datetime) -> tuple[list[dict], list[dict]]:
    from analyze_crosscoin_e1_onebar_reclaim_shadow_v1 import outcome_class, signal_ignore, simulate_e1_e1r
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, ui_lld_config
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars

    _p, candles15 = load_pane_candles(
        symbol, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    bars15 = build_15m_bars(candles15)
    by_open = {b["open_time"]: b for b in bars15}
    cfg = ui_lld_config("15m")
    cache: dict = {}
    sim = simulate_e1_e1r(bars15, candles15, cfg, cache, report_from, report_to)
    out = []
    wg_opens = {t["entry_open"] for t in trades}
    for t in trades:
        ot = _utc(datetime.fromisoformat(t["entry_open"]))
        if ot not in by_open:
            continue
        dec = by_open[ot]["close_time"]
        oc = outcome_class(t, bars15, by_open)
        e1r = signal_ignore(dec, float(t["entry_price"]), sim, bars15, candles15, cfg, cache, "E1R")
        out.append(
            {
                **t,
                "symbol": symbol,
                "decision_time": dec.isoformat(),
                "final_outcome": oc,
                "ignored_E1R": e1r["ignore"] and t["entry_open"] in wg_opens,
                "E1R_status": e1r["status"],
            }
        )
    return out, sim["e1r_episodes"]


def coverage_check(symbol: str) -> dict[str, Any]:
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars
    from dashboard.research_charts.lld_research_kernel import load_pane_candles

    _p, candles = load_pane_candles(
        symbol, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    bars15 = build_15m_bars(candles)
    in_window = [b for b in bars15 if FOUR_MO_FROM <= b["close_time"] <= FOUR_MO_TO]
    expected = int((FOUR_MO_TO - FOUR_MO_FROM).total_seconds() / 900) + 1
    gaps = []
    for i in range(1, len(in_window)):
        prev = in_window[i - 1]["close_time"]
        cur = in_window[i]["close_time"]
        if (cur - prev).total_seconds() > 900:
            gaps.append((prev.isoformat(), cur.isoformat()))
    warmup_bars = [b for b in bars15 if b["close_time"] < FOUR_MO_FROM]
    ema_ok = sum(1 for b in warmup_bars if b.get("ema200") is not None)
    by_month: dict[str, int] = defaultdict(int)
    for b in in_window:
        by_month[b["close_time"].strftime("%Y-%m")] += 1
    return {
        "symbol": symbol,
        "bars_in_4mo_window": len(in_window),
        "expected_15m_bars_approx": expected,
        "complete": len(in_window) >= expected - 2,
        "gap_count": len(gaps),
        "gaps_sample": gaps[:5],
        "warmup_bars_before_apr": len(warmup_bars),
        "warmup_bars_with_ema200": ema_ok,
        "bars_per_month": dict(by_month),
    }


def signals_in_episode(trades: list[dict], ep: dict, by_open: dict) -> tuple[int, int]:
    start = _utc(datetime.fromisoformat(ep["start_time"]))
    end_s = ep.get("end")
    end = _utc(datetime.fromisoformat(end_s)) if end_s else FOUR_MO_TO
    inside = []
    for t in trades:
        ot = _utc(datetime.fromisoformat(t["entry_open"]))
        if ot not in by_open:
            continue
        dec = by_open[ot]["close_time"]
        if start <= dec <= end:
            inside.append(t)
    blocked = sum(1 for t in inside if t.get("ignored_E1R"))
    return len(inside), blocked


def aggregate_funnel(tagged: list[dict], raw: dict, ng: list, wg: list) -> dict[str, dict[str, int]]:
    acc: dict[str, dict[str, int]] = {m: defaultdict(int) for m in MONTHS}
    for m, c in raw.items():
        if m in acc:
            acc[m]["raw"] = c
    for t in ng:
        m = month_key_entry(t["entry_open"])
        if m in acc:
            acc[m]["without_guard"] += 1
    for t in wg:
        m = month_key_entry(t["entry_open"])
        if m in acc:
            acc[m]["with_guard"] += 1
    for t in tagged:
        m = month_key_entry(t["entry_open"])
        if m not in acc:
            continue
        if t.get("ignored_E1R"):
            acc[m]["e1r_blocked"] += 1
        else:
            acc[m]["e1r_allowed"] += 1
    return {m: dict(acc[m]) for m in MONTHS}


def main() -> None:
    _ensure_paths()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_tagged: dict[str, list[dict]] = {}
    all_episodes: dict[str, list[dict]] = {}
    funnel_by_coin: dict[str, dict] = {}
    classify: dict[str, dict[str, dict[str, int]]] = {sym: {m: defaultdict(int) for m in MONTHS} for sym in COINS}
    floor_extra: dict[str, dict[str, dict[str, int]]] = {}

    totals = {m: defaultdict(int) for m in MONTHS}

    for sym in COINS:
        print("funnel", sym, flush=True)
        data = collect_funnel(sym, FOUR_MO_FROM, FOUR_MO_TO)
        tagged, episodes = apply_e1r(data["with_guard"], sym, FOUR_MO_FROM, FOUR_MO_TO)
        all_tagged[sym] = tagged
        all_episodes[sym] = episodes
        funnel_by_coin[sym] = aggregate_funnel(
            tagged, data["raw_by_month"], data["without_guard"], data["with_guard"]
        )
        floor_extra[sym] = {
            "setup_no_down4h": data["setup_no_down4h_by_month"],
            "floor_block_events": data["floor_block_events_by_month"],
            "guard_block_new_pool": data["guard_block_new_pool_by_month"],
        }
        for m in MONTHS:
            row = funnel_by_coin[sym].get(m, {})
            for k in ("raw", "without_guard", "with_guard", "e1r_blocked", "e1r_allowed"):
                totals[m][k] += row.get(k, 0)
        for t in tagged:
            m = month_key_entry(t["entry_open"])
            if m in ("2026-04", "2026-05"):
                classify[sym][m][classify_e1r(t)] += 1

    cov = [coverage_check(sym) for sym in COINS]

    # Episodes overlapping Apr or May
    episode_rows: list[dict] = []
    for sym in COINS:
        from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars
        from dashboard.research_charts.lld_research_kernel import load_pane_candles

        _p, candles15 = load_pane_candles(
            sym, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
        )
        bars15 = build_15m_bars(candles15)
        by_open = {b["open_time"]: b for b in bars15}
        apr_may_end = datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc)
        for ep in all_episodes[sym]:
            start = _utc(datetime.fromisoformat(ep["start_time"]))
            end_s = ep.get("end")
            end = _utc(datetime.fromisoformat(end_s)) if end_s else FOUR_MO_TO
            if start > apr_may_end or end < FOUR_MO_FROM:
                continue
            n_sig, n_blk = signals_in_episode(all_tagged[sym], ep, by_open)
            dur = ep.get("duration_hours")
            if dur is None and end_s:
                dur = round((end - start).total_seconds() / 3600.0, 2)
            episode_rows.append(
                {
                    "symbol": sym,
                    "start": ep["start_time"],
                    "end": end_s,
                    "duration_hours": dur,
                    "baseline_signals_inside": n_sig,
                    "blocked_inside": n_blk,
                    "end_reason": ep.get("end_reason"),
                }
            )

    def md_table_funnel(rows: list[tuple]) -> str:
        lines = [
            "| Monat | Raw Short Signals | without_guard | with_guard | E1R blocked | E1R allowed |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for r in rows:
            lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} |")
        return "\n".join(lines)

    total_rows = [
        (
            m,
            totals[m].get("raw", 0),
            totals[m].get("without_guard", 0),
            totals[m].get("with_guard", 0),
            totals[m].get("e1r_blocked", 0),
            totals[m].get("e1r_allowed", 0),
        )
        for m in MONTHS
    ]

    density_lines = ["| Monat | with_guard / Tag | E1R allowed / Tag |", "|---|---:|---:|"]
    for m in MONTHS:
        d = DAYS_PER_MONTH[m]
        wg_d = totals[m].get("with_guard", 0) / d
        al_d = totals[m].get("e1r_allowed", 0) / d
        density_lines.append(f"| {m} | {wg_d:.3f} | {al_d:.3f} |")

    apr_may_wg = totals["2026-04"].get("with_guard", 0) + totals["2026-05"].get("with_guard", 0)
    jun_jul_wg = totals["2026-06"].get("with_guard", 0) + totals["2026-07"].get("with_guard", 0)
    apr_may_raw = totals["2026-04"].get("raw", 0) + totals["2026-05"].get("raw", 0)
    jun_jul_raw = totals["2026-06"].get("raw", 0) + totals["2026-07"].get("raw", 0)

    floor_sum = {m: defaultdict(int) for m in MONTHS}
    for sym in COINS:
        for kind in floor_extra[sym].values():
            for m, v in kind.items():
                if m in floor_sum:
                    floor_sum[m]["_"] += v  # wrong - need per kind

    floor_agg = {m: {"setup_no_down4h": 0, "floor_block_events": 0, "guard_block_new_pool": 0} for m in MONTHS}
    for sym in COINS:
        for key in ("setup_no_down4h", "floor_block_events", "guard_block_new_pool"):
            for m, v in floor_extra[sym][key].items():
                if m in floor_agg:
                    floor_agg[m][key] += v

    # Conclusion logic
    oos_allowed = totals["2026-04"].get("e1r_allowed", 0) + totals["2026-05"].get("e1r_allowed", 0)
    oos_wg = apr_may_wg
    oos_blocked = totals["2026-04"].get("e1r_blocked", 0) + totals["2026-05"].get("e1r_blocked", 0)
    dev_allowed = totals["2026-06"].get("e1r_allowed", 0) + totals["2026-07"].get("e1r_allowed", 0)
    dev_wg = jun_jul_wg
    block_rate_oos = oos_blocked / oos_wg if oos_wg else 0
    block_rate_dev = (
        (totals["2026-06"].get("e1r_blocked", 0) + totals["2026-07"].get("e1r_blocked", 0)) / dev_wg
        if dev_wg
        else 0
    )
    cov_ok = all(c["complete"] and c["gap_count"] == 0 for c in cov)

    if not cov_ok:
        verdict = "D"
    elif apr_may_raw < jun_jul_raw * 0.55 and oos_wg < dev_wg * 0.55:
        if block_rate_oos > block_rate_dev * 1.15 and oos_blocked >= 3:
            verdict = "C"
        else:
            verdict = "A"
    elif block_rate_oos > block_rate_dev * 1.2 and oos_wg >= dev_wg * 0.7:
        verdict = "B"
    elif oos_blocked > oos_allowed and block_rate_oos > 0.35:
        verdict = "B"
    else:
        verdict = "C" if apr_may_raw < jun_jul_raw * 0.7 else "A"

    md_parts = [
        "# E1R 4-Monats-Funnel: April/Mai Forensik",
        "",
        f"Lauf: `e1r_4month_oos/` | Fenster 2026-04-01 .. 2026-07-31 | Coins: {', '.join(COINS)}",
        "",
        "## 1. Signal-Funnel pro Monat (gesamt)",
        "",
        md_table_funnel(total_rows),
        "",
        "### Pro Coin",
        "",
    ]
    for sym in COINS:
        md_parts.append(f"#### {sym}")
        md_parts.append("")
        rows = [
            (
                m,
                funnel_by_coin[sym].get(m, {}).get("raw", 0),
                funnel_by_coin[sym].get(m, {}).get("without_guard", 0),
                funnel_by_coin[sym].get(m, {}).get("with_guard", 0),
                funnel_by_coin[sym].get(m, {}).get("e1r_blocked", 0),
                funnel_by_coin[sym].get(m, {}).get("e1r_allowed", 0),
            )
            for m in MONTHS
        ]
        md_parts.append(md_table_funnel(rows))
        md_parts.append("")

    md_parts += [
        "## 2. Warum blockiert? (with_guard, April/Mai)",
        "",
        "Klassen: `e1r_not_active_allowed` | `e1r_active_bull_rising3_blocked` | `pending_reclaim_allowed` | `other_not_e1r`",
        "",
    ]
    for sym in COINS:
        md_parts.append(f"### {sym}")
        for m in ("2026-04", "2026-05"):
            c = classify[sym][m]
            md_parts.append(
                f"- **{m}**: not_active={c.get('e1r_not_active_allowed',0)}, "
                f"active_blocked={c.get('e1r_active_bull_rising3_blocked',0)}, "
                f"pending={c.get('pending_reclaim_allowed',0)}, other={c.get('other_not_e1r',0)}"
            )
        md_parts.append("")

    md_parts += ["## 3. E1R-Episoden (Overlap April/Mai)", ""]
    if episode_rows:
        md_parts.append("| Coin | Start | Ende | Dauer h | Baseline-Signale | blockiert | end_reason |")
        md_parts.append("|---|---|---|---:|---:|---:|---|")
        for er in sorted(episode_rows, key=lambda x: x["start"]):
            md_parts.append(
                f"| {er['symbol']} | {er['start']} | {er['end'] or '—'} | {er['duration_hours']} | "
                f"{er['baseline_signals_inside']} | {er['blocked_inside']} | {er.get('end_reason','')} |"
            )
    else:
        md_parts.append("_Keine E1R-Episoden mit Overlap Apr/Mai._")

    md_parts += ["", "## 4. Baseline-Dichte (pro Tag)", "", "\n".join(density_lines), ""]

    md_parts += [
        "## 5. Floor-Guard / Scanner (Apr/Mai vs Jun/Jul, Summe 3 Coins)",
        "",
        f"- Raw Scanner (setup + down_4h): Apr+May **{apr_may_raw}** vs Jun+Jul **{jun_jul_raw}**",
        f"- with_guard (dedup): Apr+May **{apr_may_wg}** vs Jun+Jul **{jun_jul_wg}**",
        "",
        "| Monat | setup ohne down_4h | floor_block_events | guard_block (neuer Pool) |",
        "|---|---:|---:|---:|",
    ]
    for m in MONTHS:
        fa = floor_agg[m]
        md_parts.append(
            f"| {m} | {fa['setup_no_down4h']} | {fa['floor_block_events']} | {fa['guard_block_new_pool']} |"
        )

    md_parts += ["", "## 6. Daten-Coverage", ""]
    for c in cov:
        md_parts.append(
            f"- **{c['symbol']}**: 4mo bars={c['bars_in_4mo_window']} (exp~{c['expected_15m_bars_approx']}), "
            f"complete={c['complete']}, gaps={c['gap_count']}, warmup_ema_bars={c['warmup_bars_with_ema200']}, "
            f"per_month={c['bars_per_month']}"
        )

    md_parts += [
        "",
        "## 7. Ursache-Zusammenfassung",
        "",
        f"**Verdikt: {verdict}**",
        "",
        "| Kennzahl | Apr+May | Jun+Jul |",
        "|---|---:|---:|",
        f"| with_guard | {oos_wg} | {jun_jul_wg} |",
        f"| E1R allowed | {oos_allowed} | {dev_allowed} |",
        f"| E1R blocked | {oos_blocked} | {totals['2026-06'].get('e1r_blocked',0)+totals['2026-07'].get('e1r_blocked',0)} |",
        f"| Block-Rate (blocked/with_guard) | {block_rate_oos:.1%} | {block_rate_dev:.1%} |",
        f"| Raw scanner bars | {apr_may_raw} | {jun_jul_raw} |",
        "",
        "- **A** = wenig Trades hauptsächlich wenig Baseline-Signale",
        "- **B** = hauptsächlich E1R-Blockierung",
        "- **C** = Mischung",
        "- **D** = Daten-/Coverage-Problem",
        "",
    ]

    out_md = OUT_DIR / "APR_MAY_FUNNEL_REPORT.md"
    out_json = OUT_DIR / "apr_may_funnel_v1.json"
    out_md.write_text("\n".join(md_parts), encoding="utf-8")
    payload = {
        "totals_by_month": {m: dict(totals[m]) for m in MONTHS},
        "by_coin": funnel_by_coin,
        "classify_apr_may": {s: {m: dict(classify[s][m]) for m in ("2026-04", "2026-05")} for s in COINS},
        "episodes_apr_may_overlap": episode_rows,
        "floor_extra": floor_extra,
        "coverage": cov,
        "verdict": verdict,
    }
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("Wrote", out_md, out_json, "verdict", verdict)


if __name__ == "__main__":
    main()

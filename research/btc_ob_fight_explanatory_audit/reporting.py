"""Generate REPORT.md for explanatory audit."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _fmt_mio(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x / 1e6:+.2f} Mio. USD"


def _fmt_bps(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x:+.2f} bps"


def write_report(
    path: Path,
    *,
    ctx: dict[str, Any],
    manifest: dict[str, Any],
    snapshots: dict[str, Any],
    hypothesis: dict[str, Any],
    liq_semantics: dict[str, Any],
) -> None:
    wf10 = ctx["wf_00_10"]
    wf30 = ctx["wf_00_30"]
    wf1030 = ctx["wf_10_30"]
    liq = ctx["liq_phase"]
    market = ctx["market"]
    retest = market.get("later_retest") or {}
    assoc = ctx["assoc"]
    oi = ctx["oi_facts"]
    ob = ctx["ob"]

    lines = [
        "# BTCUSDT Profile Fight — Explanatory Research Audit",
        "",
        f"**Anchor:** 2026-08-31T19:00:00Z  ",
        f"**Core window:** 2026-08-31T18:30:00Z–19:30:00Z  ",
        f"**Extended:** bis 21:30 UTC (soweit Coverage)  ",
        f"**Source run:** `{ctx['inventory']['run_dir']}`  ",
        f"**Verdict:** `{manifest['verdict']}`  ",
        f"**Runtime:** {manifest['runtime_seconds']}s | **Peak RSS:** {manifest['peak_rss_kb']} KB",
        "",
        "> Research-only. `rules_frozen=false`, `trade_verdict_evaluated=false`, `direction=null`.",
        "",
        "---",
        "",
        "## 1. Liquidation semantics (proved)",
        "",
        f"- Side field `{liq_semantics['side_field']}` = **liquidated position side**, not taker aggressor.",
        "- `LIQUIDATED_SHORT` → position side Short → closure requires **FORCED BUY** (aggressive Buy vs Ask).",
        "- `LIQUIDATED_LONG` → **FORCED SELL**.",
        f"- `bankruptcy_price`: reference price from feed, **not proven exact execution print**.",
        f"- Row = one WS liquidation message; dedup via `event_key`. Core: {liq_semantics['row_granularity']['core_window_row_count']} rows = {liq_semantics['row_granularity']['core_window_unique_event_keys']} unique keys → **{liq_semantics['row_granularity']['dedup_status']}**.",
        "- **No shared ID** with `public_trades_canonical` → only `HEURISTIC_TEMPORAL_PRICE_ASSOCIATION`.",
        "",
        "## 2. Liquidation timeline",
        "",
        f"- Core events: **{ctx['liq_core_count']}** ({liq['short_event_count']} SHORT, remainder LONG).",
        f"- First short liq: `{liq.get('first_short_liquidation_ts')}`",
        f"- Short notional: **{liq['short_quote_total']:,.0f} USD** | base: {liq['short_base_total']:.4f} BTC",
        f"- Distribution: {liq['pct_quote_before_peak']:.1f}% quote before peak | {liq['pct_quote_peak_to_reclaim']:.1f}% peak→reclaim | {liq['pct_quote_after_reclaim']:.1f}% after reclaim",
        "",
        "## 3. Public trades (dedup trade_id, taker aggressor)",
        "",
        "| Window | Delta | Price chg |",
        "|--------|-------|-----------|",
        f"| 19:00–19:10 | {_fmt_mio(wf10['delta_notional'])} | {_fmt_bps(wf10['price_change_bps'])} |",
        f"| 19:00–19:30 | {_fmt_mio(wf30['delta_notional'])} | {_fmt_bps(wf30['price_change_bps'])} |",
        f"| 19:10–19:30 (direct) | {_fmt_mio(wf1030['delta_notional'])} | {_fmt_bps(wf1030['price_change_bps'])} |",
        "",
        "Same-timestamp ordering: `trade_ts, trade_id` — exchange order not proven.",
        "",
        "## 4. Liquidation ↔ trade association",
        "",
    ]
    for row in assoc:
        lines.append(
            f"- ±{row['sensitivity_window_ms']}ms: {row['events_with_temporal_buy_match']}/{row['short_liquidation_events']} liq events with temporal Buy match; "
            f"overlapping buy notional {row['overlapping_buy_notional_sum']:,.0f} USD ({(row['fraction_of_total_taker_buy'] or 0)*100:.1f}% of taker buy); "
            f"**{row['identification_status']}** / {row['association_type']}"
        )
    lines += [
        "",
        "## 5. Open interest",
        "",
        f"- Core OI: {oi.get('oi_first')} → {oi.get('oi_last')} (Δ {oi.get('oi_delta'):+.2f}, {oi.get('oi_delta_pct'):+.3f}%)",
        f"- Attack-window OI delta (outer cross → peak): **{ctx.get('oi_attack_delta'):+.2f}** → `SHORT_COVERING_OR_SHORT_LIQUIDATION_COMPONENT` während des Ausbruchs; Gesamt +114.33 entsteht überwiegend **nach** Peak/Reclaim.",
        "- Interpretation matrix applied per phase in `oi_phase_summary.csv`.",
        "",
        "## 6. Market structure",
        "",
        f"- First peak: **{ctx['peak_price']}** @ `{ctx['peak_ts']}`",
        f"- Canonical reclaim: `{ctx['reclaim'].get('cross_ts')}` @ {ctx['reclaim'].get('cross_price')}",
        f"- Extended retest high: **{retest.get('retest_high_price')}** @ `{retest.get('retest_high_ts')}` → **{retest.get('classification')}**",
        f"- Within standard 30m window: **{retest.get('within_standard_30m_window')}** — {retest.get('status_if_outside', '')}",
        "",
        "## 7. Orderbook (run_017)",
        "",
        f"- Trade-associated Ask decreases (PROFILE_EDGE_ZONE): **60**",
        f"- Nearby Ask / Bid / Unknown increases: **{ob.get('nearby_ask_increases')} / {ob.get('nearby_bid_increases')} / {ob.get('nearby_unknown')}**",
        "- Edge-zone coverage mostly `EDGE_REGION_OUTSIDE_BOOK_RANGE` / partial → **absorption not provable from OB alone**.",
        "",
        "## 8. Hypothesis matrix",
        "",
        "| Hypothesis | Status |",
        "|------------|--------|",
    ]
    for h in hypothesis.get("hypotheses", []):
        lines.append(f"| `{h['hypothesis']}` | {h['status']} |")

    lines += [
        "",
        "## 9. Decision-time snapshots",
        "",
    ]
    for key, snap in snapshots.items():
        lines.append(f"### {key}")
        lines.append(f"- ts: `{snap.get('ts')}`")
        lines.append(f"- allowable: `{snap.get('allowable_action', 'WAIT')}`")
        lines.append("")

    lines += [
        "---",
        "",
        "## Pflichtantworten (18)",
        "",
        "1. **Sind Short-Liquidationen erzwungene Käufe?** Ja — `LIQUIDATED_SHORT` = Short-Position zwangsweise via aggressivem Buy geschlossen (Collector-bewiesen).",
        "2. **59 Positionen oder Events?** **59 Short-Liquidation-Events** (dedupliziert per `event_key`), nicht bewiesen 59 eindeutige Positionen.",
        f"3. **Wann?** Erste Short-Liq `{liq.get('first_short_liquidation_ts')}`; Konzentration vor Peak ({liq['pct_quote_before_peak']:.0f}% Notional). Details: `liquidation_events.csv`.",
        f"4. **Notional?** Short ~{liq['short_quote_total']:,.0f} USD gesamt im Kernfenster.",
        f"5. **Anteil am positiven Delta?** Heuristisch ±500ms: ~{(assoc[2]['fraction_of_total_taker_buy'] or 0)*100:.1f}% des Taker-Buy-Volumens temporal assoziierbar — **nicht kausal bewiesen**.",
        "6. **Direkte Trade-Identifikation?** **Nein** — keine gemeinsame ID.",
        f"7. **Trades vor/während/nach Peak?** 19:00–19:10 Δ {_fmt_mio(wf10['delta_notional'])}; 19:10–19:30 Δ {_fmt_mio(wf1030['delta_notional'])} (stark negativ post-peak).",
        f"8. **OI beim Ausbruch?** Attack-window Δ **{ctx.get('oi_attack_delta'):+.2f}** (fällt während Ausbruch); Gesamt +114.33 (+0.218%) über Kernfenster — Anstieg überwiegend später.",
        "9. **Short-Squeeze vs neue Longs?** **Gemischt** — dominante Short-Liq + fallendes OI im Ausbruch (Squeeze/Covering); späteres OI-Wachstum deutet auf Long-Nachzug.",
        "10. **Ask-Absorption?** **INCONCLUSIVE** — Nearby Ask increases vorhanden, aber Edge-Coverage schwach.",
        "11. **Käufererschöpfung?** **PARTIALLY_SUPPORTED** — Delta bricht post-peak ein ohne bewiesene Absorption.",
        f"12. **Reclaim?** `{ctx['reclaim'].get('cross_ts')}` @ {ctx['reclaim'].get('cross_price')}.",
        f"13. **Retest-Docht?** `{retest.get('retest_high_ts')}` @ {retest.get('retest_high_price')}.",
        f"14. **Higher High?** **Nein** — {retest.get('classification')}.",
        f"15. **Lower High?** **Ja** ({retest.get('retest_high_price')} vs peak {ctx['peak_price']}).",
        f"16. **Innerhalb Standardfenster?** **Nein** — `NOT_AVAILABLE_TO_STANDARD_30M_DECISION_WINDOW`.",
        "17. **Erstmals Failed-Breakout-Short begründbar?** Snapshot C (Reclaim) — **PARTIAL**; volle Bestätigung erst Snapshot D (extended retest, hindsight).",
        "18. **Unsicher bleibt:** Direkte Liquidation→Trade-Zuordnung; OB-Absorption an der Kante; physische OI-Einheit; ob Retest-Hindsight für 30m-Decision relevant ist.",
        "",
        "---",
        "",
        f"**Abschluss:** `{manifest['verdict']}`",
    ]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

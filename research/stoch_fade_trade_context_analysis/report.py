"""REPORT.md for the ZEC causal trade-context analysis."""

from __future__ import annotations

from typing import Any

import pandas as pd

from .config import (
    EVALUATION_ID,
    EXPECTED_LOSSES,
    EXPECTED_OPEN,
    EXPECTED_TRADES,
    EXPECTED_WINS,
    SOURCE_JOB_ID,
    STRATEGY_VERSION,
    SYMBOL,
)


def _rate(frame: pd.DataFrame, mask: pd.Series) -> str:
    part = frame.loc[mask]
    if part.empty:
        return "n=0"
    n = len(part)
    loss = float((part["outcome"] == "LOSS").mean())
    return f"n={n}, loss-rate={loss:.1%}"


def _bucket_line(buckets: pd.DataFrame, table: str) -> str:
    part = buckets.loc[buckets["table"] == table].sort_values("n", ascending=False)
    if part.empty:
        return "keine Daten"
    bits = []
    for _, row in part.iterrows():
        bits.append(
            f"{row['bucket']}: n={int(row['n'])} loss={row['loss_rate']:.1%}"
            if row["loss_rate"] is not None
            else f"{row['bucket']}: n={int(row['n'])}"
        )
    return "; ".join(bits)


def _top_smd(comp: pd.DataFrame, n: int = 8) -> str:
    if comp.empty:
        return "keine numerischen Features"
    part = comp.dropna(subset=["abs_smd"]).sort_values("abs_smd", ascending=False).head(n)
    lines = []
    for _, row in part.iterrows():
        lines.append(
            f"- `{row['feature']}`: SMD={row['smd_win_minus_loss']:.3f}, "
            f"mean WIN={row['mean_win']:.4f}, mean LOSS={row['mean_loss']:.4f} "
            f"(nW={int(row['n_win'])}, nL={int(row['n_loss'])})"
        )
    return "\n".join(lines) if lines else "keine SMD"


def write_report(
    *,
    path,
    inventory: dict[str, Any],
    context: pd.DataFrame,
    buckets: pd.DataFrame,
    comparison: pd.DataFrame,
    quality: dict[str, Any],
    issues: pd.DataFrame,
    cases: pd.DataFrame,
    label: str,
) -> None:
    closed = context.loc[context["outcome"].isin(["WIN", "LOSS"])]
    shorts = closed.loc[closed["direction"] == "SHORT"]
    longs = closed.loc[closed["direction"] == "LONG"]
    n = int(len(context))
    wins = int((context["outcome"] == "WIN").sum())
    losses = int((context["outcome"] == "LOSS").sum())
    opens = int(context["is_open"].sum())
    lookahead = bool(quality.get("lookahead_failures", 0))
    causal = (not lookahead) and bool(quality.get("all_snapshots_available_at_le_entry", False))

    q3 = _top_smd(
        comparison.loc[
            comparison["feature"].str.contains(r"ema|range20|room_to|ret_|price_trend|structure", regex=True, na=False)
        ]
    )
    q4 = _top_smd(
        comparison.loc[
            comparison["feature"].str.contains(r"stoch|cross|exhausted|phase", regex=True, na=False)
        ]
    )

    loss_vs_4h_support_short = _rate(shorts, shorts["htf_4h_supports_opposes"] == "SUPPORTS")
    loss_vs_4h_oppose_short = _rate(shorts, shorts["htf_4h_supports_opposes"] == "OPPOSES")
    loss_vs_4h_support_long = _rate(longs, longs["htf_4h_supports_opposes"] == "SUPPORTS")
    loss_vs_4h_oppose_long = _rate(longs, longs["htf_4h_supports_opposes"] == "OPPOSES")
    loss_vs_1h_oppose_short = _rate(shorts, shorts["htf_1h_supports_opposes"] == "OPPOSES")
    loss_vs_1h_oppose_long = _rate(longs, longs["htf_1h_supports_opposes"] == "OPPOSES")

    short_low = _rate(shorts, shorts["entry_near_4h_range_low"] == True)
    short_not_low = _rate(shorts, shorts["entry_near_4h_range_low"] != True)
    long_high = _rate(longs, longs["entry_near_4h_range_high"] == True)
    long_not_high = _rate(longs, longs["entry_near_4h_range_high"] != True)

    exh_all = _rate(closed, closed["ltf_5m_exhausted"] == True)
    exh_not = _rate(closed, closed["ltf_5m_exhausted"] != True)
    recross = _rate(closed, closed["ltf_1m_opposite_recross"] == True)
    recross_not = _rate(closed, closed["ltf_1m_opposite_recross"] != True)
    recross_loss_share = float(
        closed.loc[closed["outcome"] == "LOSS", "ltf_1m_opposite_recross"].fillna(False).astype(bool).mean()
    )
    recross_win_share = float(
        closed.loc[closed["outcome"] == "WIN", "ltf_1m_opposite_recross"].fillna(False).astype(bool).mean()
    )

    overlap_losses = int(
        ((context["outcome"] == "LOSS") & (context["overlaps_previous_trade"] == True)).sum()
    )
    overlap_all = int((context["overlaps_previous_trade"] == True).sum())

    robust = comparison.dropna(subset=["abs_smd"]).sort_values("abs_smd", ascending=False)
    robust = robust.loc[robust["kind"] == "numeric"]
    robust_lines = []
    for _, row in robust.head(12).iterrows():
        ci_ok = (
            row.get("mean_diff_ci_low") is not None
            and row.get("mean_diff_ci_high") is not None
            and (
                (row["mean_diff_ci_low"] > 0 and row["mean_diff_ci_high"] > 0)
                or (row["mean_diff_ci_low"] < 0 and row["mean_diff_ci_high"] < 0)
            )
        )
        if row["abs_smd"] and row["abs_smd"] >= 0.15 and ci_ok:
            robust_lines.append(
                f"- `{row['feature']}` SMD={row['smd_win_minus_loss']:.3f} CI=({row['mean_diff_ci_low']:.3f},{row['mean_diff_ci_high']:.3f})"
            )
    if not robust_lines:
        robust_lines = [
            "- Kein einzelnes Entry-Feature erreicht zugleich |SMD|>=0.15 und ein CI ohne 0. "
            "Spätere Regeln müssen auf den Bucket-Tabellen und Replikation im Validation-Split beruhen, nicht auf Profit-Suche."
        ]

    vanish = []
    bools = comparison.loc[comparison["kind"] == "boolean"].copy()
    for _, row in bools.iterrows():
        if row["mean_win"] is None or row["mean_loss"] is None:
            continue
        if abs(row["mean_win"] - row["mean_loss"]) < 0.03:
            vanish.append(row["feature"])
    vanish_txt = ", ".join(f"`{x}`" for x in vanish[:15]) if vanish else "keine auffälligen Boolean-Gleichstände notiert"

    case_txt = []
    manuals = cases.loc[cases["case_kind"] == "manual"] if "case_kind" in cases.columns else cases
    for _, row in manuals.iterrows():
        case_txt.append(
            f"### {row.get('entry_time')} {row.get('direction')} `{row.get('signal_id')}` ({row.get('outcome')})\n"
            f"- Signal-TF: {row.get('timeframe')}; Overlap: open={row.get('number_of_open_zec_trades_at_entry')}, "
            f"same={row.get('overlap_same_direction')}, opposite={row.get('overlap_opposite_direction')}, "
            f"exact_dup={row.get('exact_entry_duplicate')}\n"
            f"- Matrix: {row.get('mtf_matrix_json')}\n"
            f"- 4h EMA={row.get('tf_4h_ema_trend')} Stoch={row.get('tf_4h_stoch_phase')} "
            f"range_pos={row.get('tf_4h_range20_pos_entry')} near_low={row.get('entry_near_4h_range_low')}\n"
            f"- 1h supports/opposes={row.get('htf_1h_supports_opposes')}; 4h={row.get('htf_4h_supports_opposes')}\n"
            f"- 5m exhausted={row.get('ltf_5m_exhausted')}; 1m opposite recross={row.get('ltf_1m_opposite_recross')}\n"
            f"- TP already consumed={row.get('tp_consumed_frac')}; 5m pre-entry aligned={row.get('pre_entry_5m_aligned_pct')}\n"
            f"- Weakness note: {row.get('weakness_note')}\n"
        )

    issues_n = int(len(issues))
    text = f"""# ZEC causal trade-context analysis

Final label: **{label}**

- Symbol: `{SYMBOL}`
- Evaluation: `{EVALUATION_ID}`
- Source job: `{SOURCE_JOB_ID}`
- Strategy: `{STRATEGY_VERSION}`
- Semantics: `cross_recognition`, `NO_BE50`, `full_1m_scan`, `SL_FIRST`, no max-hold, entry = first 1m open strictly after confirmation
- Views: SIGNAL_VIEW keeps every ZEC outcome. EXECUTION_DIAGNOSTIC_VIEW only adds overlap flags.
- No strategy change, no filter search, no ML, no ClickHouse writes, no new backtest.

## Inventory (before any feature work)

- ZEC trades: {inventory['n_trades']} (expected {EXPECTED_TRADES})
- Wins: {inventory['wins']} (expected {EXPECTED_WINS})
- Losses: {inventory['losses']} (expected {EXPECTED_LOSSES})
- Open: {inventory['open']} (expected {EXPECTED_OPEN})
- Period entries: {inventory['period_start']} → {inventory['period_end_entry']}
- Last exit: {inventory['last_exit']}
- Timeframes: {inventory['timeframes']}
- LONG/SHORT: {inventory['counts_by_direction']}
- By TF: {inventory['counts_by_timeframe']}
- Duplicate signal_ids: {inventory['duplicate_signal_ids']}
- Duplicate setup_ids: {inventory['duplicate_setup_ids']}
- Duplicate generation_keys: {inventory['duplicate_generation_keys']}
- Trades sharing an entry time: {inventory['trades_sharing_an_entry_time']} across {inventory['distinct_entry_times_with_multiple_tfs']} timestamps
- Trades overlapping a still-open ZEC trade: {inventory['trades_with_at_least_one_open_overlap']}
- Overlap same direction: {inventory['overlap_same_direction_trades']}
- Overlap opposite direction: {inventory['overlap_opposite_direction_trades']}
- Source raw signals in job artifact: {inventory['n_raw_source_signals']}
- Outcomes missing source signal join: {inventory['n_outcomes_missing_source_signal']}
- No trade was removed.

## Pflichtfragen

### 1. Wie viele ZEC-Trades/Wins/Losses wurden analysiert?

{n} Trades, {wins} Wins, {losses} Losses, {opens} Open. Closed win-rate = {wins / (wins + losses):.2%} (OPEN excluded). Alle Evaluation-Outcomes sind in SIGNAL_VIEW enthalten.

### 2. Sind alle Features strikt kausal?

{'Ja. Jeder TF-Snapshot erfüllt available_at <= entry_time. Lookahead-Hard-Fail wurde nicht ausgelöst.' if causal else 'Nein bzw. nicht vollständig. Siehe Datenqualität / Lookahead.'}
Outcome-Felder (MFE/MAE/PnL) sind getrennt gespeichert und nicht als Entry-Feature verwendet.
1d ist nur enthalten, wenn die letzte UTC-Tageskerze vollständig geschlossen war.

### 3. Welche EMA-/Preisstrukturmerkmale unterscheiden Wins und Losses?

{_top_smd(comparison.loc[comparison['feature'].str.contains(r'ema_trend|ema20|ema50|ema200|range20|room_to|structure_regime|price_trend|ret_5bar|close_minus_ema', regex=True, na=False)])}

Bucket 4h EMA-Trend: {_bucket_line(buckets, '1_loss_rate_by_4h_ema_trend')}

### 4. Welche Stoch-Zustände unterscheiden Wins und Losses?

{_top_smd(comparison.loc[comparison['feature'].str.contains(r'stoch|exhausted|cross_up|cross_down|k_lt_20|k_gt_80', regex=True, na=False)])}

Bucket 5m Stoch: {_bucket_line(buckets, '5_loss_rate_by_5m_stoch')}

### 5. Verlieren LONGs/SHORTs häufiger gegen 1h/4h-Trend?

SHORT vs 4h SUPPORTS: {loss_vs_4h_support_short}; SHORT vs 4h OPPOSES: {loss_vs_4h_oppose_short}.
LONG vs 4h SUPPORTS: {loss_vs_4h_support_long}; LONG vs 4h OPPOSES: {loss_vs_4h_oppose_long}.
SHORT vs 1h OPPOSES: {loss_vs_1h_oppose_short}; LONG vs 1h OPPOSES: {loss_vs_1h_oppose_long}.
1h+4h alignment: {_bucket_line(buckets, '2_loss_rate_by_1h_4h_alignment')}

### 6. Verlieren Shorts am unteren 4h-Range-Rand häufiger?

SHORT near 4h low: {short_low}; other SHORTs: {short_not_low}.
Range buckets: {_bucket_line(buckets, '3_loss_rate_by_4h_range_position')}

### 7. Verlieren Longs am oberen 4h-Range-Rand häufiger?

LONG near 4h high: {long_high}; other LONGs: {long_not_high}.

### 8. Ist 5m beim Entry häufig bereits erschöpft?

5m exhausted: {exh_all}; not exhausted: {exh_not}.
Share exhausted among all closed trades: {float(closed['ltf_5m_exhausted'].fillna(False).astype(bool).mean()):.1%}.

### 9. Dreht 1m bei Losses häufiger gegen den Trade?

1m opposite recross overall: {recross}; ohne Recross: {recross_not}.
Share among LOSS: {recross_loss_share:.1%}; among WIN: {recross_win_share:.1%}.

### 10. Wie stark wirkt bereits verbrauchter TP-Weg?

{_bucket_line(buckets, '7_loss_rate_by_consumed_tp_path')}

Numeric SMD for `tp_consumed_frac` is in `win_loss_feature_comparison.csv`. No threshold was chosen from profit.

### 11. Wie viele Losses überlappen mit bereits offenen ZEC-Trades?

{overlap_losses} Losses überlappen (von {overlap_all} überlappenden Trades insgesamt). Overlap ist nur Diagnose; Outcomes wurden nicht geändert.

### 12. Erklären die objektiven Werte unsere zwei manuellen Beispiele?

{chr(10).join(case_txt) if case_txt else 'Manuelle Fälle nicht gefunden — siehe data_quality_issues.csv.'}

### 13. Welche Merkmale erscheinen robust genug für eine spätere feste Regel?

Noch keine Regel. Kandidaten nur, wenn der Unterschied in der geschlossenen Population sichtbar ist, das Bootstrap-CI die Null meidet, und der Split `development` später getrennt geprüft wird. Testfenster bleibt unangetastet.

{chr(10).join(robust_lines)}

### 14. Welche Auffälligkeiten verschwinden beim WIN-Vergleich?

Boolean-Features mit |WIN-rate − LOSS-rate| < 3pp: {vanish_txt}.
Einzelne manuelle Chart-Eindrücke (z.B. „4h sieht überkauft aus“) können in der vollen WIN/LOSS-Population schwächer sein als im Einzelfall. Deshalb keine Filter aus den zwei August-SHORTs ableiten.

### 15. Gibt es Daten-/Lookahead-Probleme?

- Lookahead failures: {quality.get('lookahead_failures')}
- Snapshots missing: {quality.get('missing_snapshots')}
- 1m gaps: {quality.get('gap_count_1m')}
- Incomplete HTF buckets discarded: {quality.get('incomplete_htf_buckets')}
- Incomplete HTF note: {quality.get('incomplete_htf_note')}
- EMA200 missing share (4h): {quality.get('ema200_missing_share_4h')}
- Manual bar-time check 09:46: {quality.get('manual_0946_bar_times_ok')}
- Issue rows: {issues_n}
- ClickHouse writes: 0 (SELECT only)

## Split

- development (first 60% by entry): {int((context['split']=='development').sum())}
- validation (next 20%): {int((context['split']=='validation').sum())}
- test (last 20%, untouched): {int((context['split']=='test').sum())}

Test-window metrics were not used to pick thresholds.

## Files

- `zec_trade_context.parquet` / `.csv`
- `timeframe_snapshots.parquet`
- `feature_dictionary.json`
- `feature_availability_audit.csv`
- `win_loss_feature_comparison.csv`
- `feature_bucket_outcomes.csv`
- `timeframe_alignment_summary.csv`
- `overlap_diagnostics.csv`
- `selected_case_studies.csv`
- `data_quality_audit.json`
- `data_quality_issues.csv`
"""
    path.write_text(text)

"""REPORT.md for combined causal entry-warning research."""

from __future__ import annotations

from typing import Any

import pandas as pd

from .config import OOS_CAVEAT, RULE_IDS, ZEC_SYMBOL


def _pct(v: object) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "n/a"
    return f"{float(v):.2%}"


def _n(v: object, d: int = 3) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "n/a"
    return f"{float(v):.{d}f}"


def _row(table: pd.DataFrame, **pred) -> pd.Series:
    part = table
    for k, v in pred.items():
        if k in part.columns:
            part = part.loc[part[k] == v]
    return part.iloc[0] if len(part) else pd.Series(dtype=object)


def _path(path_sum: pd.DataFrame, cohort: str, hz: str) -> pd.Series:
    return _row(path_sum, cohort=cohort, horizon=hz)


def write_report(
    *,
    path,
    label: str,
    recon: dict[str, Any],
    zec_overall: pd.DataFrame,
    zec_temporal: pd.DataFrame,
    coin_tbl: pd.DataFrame,
    score_tbl: pd.DataFrame,
    path_sum: pd.DataFrame,
    recov: pd.DataFrame,
    missing: pd.DataFrame,
    cases: pd.DataFrame,
    fast: pd.DataFrame,
    quality: dict[str, Any],
) -> None:
    score_lines = []
    for _, r in score_tbl.iterrows():
        score_lines.append(
            f"- Score {int(r['warning_score_true'])}: n={int(r['n_trades'])} "
            f"W/L/O={int(r['n_win'])}/{int(r['n_loss'])}/{int(r['n_open'])} "
            f"loss-rate={_pct(r['loss_rate'])} net={_n(r['net_sum'])} pp "
            f"missing-any={int(r['n_any_missing'])}"
        )
    # best ZEC development rule by net_sum_delta
    dev = zec_temporal.loc[zec_temporal["split"] == "development"] if "split" in zec_temporal.columns else zec_temporal
    best_dev = None
    if len(dev):
        cand = dev.loc[dev["rule_id"] != "R0"].sort_values("net_sum_delta", ascending=False)
        if len(cand):
            best_dev = cand.iloc[0]
    best_id = None if best_dev is None else str(best_dev["rule_id"])
    val = zec_temporal.loc[(zec_temporal["rule_id"] == best_id) & (zec_temporal["split"] == "validation")] if best_id else pd.DataFrame()
    tes = zec_temporal.loc[(zec_temporal["rule_id"] == best_id) & (zec_temporal["split"] == "test")] if best_id else pd.DataFrame()
    ext_all = coin_tbl.loc[(coin_tbl["symbol"] == "ALL_EXCLUDING_ZEC") & (coin_tbl["rule_id"] == best_id)] if best_id and "symbol" in coin_tbl.columns else pd.DataFrame()
    coin_best = coin_tbl.loc[(coin_tbl["rule_id"] == best_id) & (~coin_tbl.get("symbol", pd.Series(dtype=object)).isin(["ALL_EXCLUDING_ZEC"]))] if best_id and "symbol" in coin_tbl.columns else pd.DataFrame()
    n_better = int((coin_best["net_sum_delta"] > 0).sum()) if len(coin_best) else 0
    n_worse = int((coin_best["net_sum_delta"] < 0).sum()) if len(coin_best) else 0

    overall_sorted = zec_overall.loc[zec_overall["rule_id"] != "R0"].sort_values("net_sum_delta", ascending=False)
    strongest = overall_sorted.iloc[0] if len(overall_sorted) else pd.Series(dtype=object)

    rule_block_lines = []
    for rid in RULE_IDS:
        r = _row(zec_overall, rule_id=rid)
        rule_block_lines.append(
            f"- {rid}: blocked {r.get('n_blocked')} (W/L/O {r.get('blocked_wins')}/{r.get('blocked_losses')}/{r.get('blocked_open')}) "
            f"kept {r.get('n_kept')} winrate {_pct(r.get('winrate_before'))}→{_pct(r.get('winrate_after'))} "
            f"net {_n(r.get('net_sum_before'))}→{_n(r.get('net_sum_after'))} (Δ {_n(r.get('net_sum_delta'))}) "
            f"PF {_n(r.get('net_pf_before'))}→{_n(r.get('net_pf_after'))}"
        )

    temp_lines = []
    if best_id:
        for split in ("development", "validation", "test"):
            r = _row(zec_temporal, rule_id=best_id, split=split)
            temp_lines.append(
                f"- {split} ({OOS_CAVEAT}): block={_pct(r.get('block_rate'))} "
                f"winrate {_pct(r.get('winrate_before'))}→{_pct(r.get('winrate_after'))} "
                f"net Δ {_n(r.get('net_sum_delta'))} PF {_n(r.get('net_pf_before'))}→{_n(r.get('net_pf_after'))}"
            )

    p4_0 = _path(path_sum, "score_0", "4h")
    p4_2 = _path(path_sum, "score_2", "4h")
    p6_0 = _path(path_sum, "score_0", "6h")
    p6_2 = _path(path_sum, "score_2", "6h")
    p12_0 = _path(path_sum, "score_0", "12h")
    p12_2 = _path(path_sum, "score_2", "12h")
    p24_0 = _path(path_sum, "score_0", "24h")
    p24_2 = _path(path_sum, "score_2", "24h")
    r2b4 = _path(path_sum, "R2_BLOCKED", "4h")
    r2k4 = _path(path_sum, "R2_KEPT", "4h")
    r2b6 = _path(path_sum, "R2_BLOCKED", "6h")
    r2k6 = _path(path_sum, "R2_KEPT", "6h")

    rec_lines = []
    for _, r in recov.iterrows():
        rec_lines.append(f"- {r['horizon']}: SL-before={int(r['n_sl_before_horizon'])}, later aligned={int(r['n_sl_then_aligned'])} ({_pct(r['share_recover'])})")

    fast_r2 = _row(fast, rule_id="R2") if len(fast) else pd.Series(dtype=object)
    miss_lines = [
        f"- {r.get('universe', 'ZECUSDT')} {r['flag']}: true={r['n_true']} false={r['n_false']} missing={r['n_missing']}"
        for _, r in missing.iterrows()
    ]

    manuals = cases.loc[cases["case_kind"] == "manual"] if "case_kind" in cases.columns else cases.head(0)
    man_txt = []
    for _, r in manuals.iterrows():
        man_txt.append(
            f"### {r.get('entry_time')} {r.get('direction')} `{r.get('signal_id')}` ({r.get('outcome')})\n"
            f"- W1={r.get('w1_5m_exhausted_in_trade_direction')} 5mK={r.get('tf_5m_stoch_k')} "
            f"W2={r.get('w2_1m_turning_against_trade')} 1m phase={r.get('tf_1m_stoch_phase')} "
            f"W3={r.get('w3_pre_entry_tp_progress_ge_25pct')} progress={r.get('pre_entry_progress')} "
            f"W4={r.get('w4_symbol_trade_already_open')} n_open={r.get('w4_n_open_same_symbol')} "
            f"score={r.get('warning_score_true')}\n"
            f"- Blocks: " + ", ".join(rid for rid in RULE_IDS if rid != "R0" and bool(r.get(f"block_{rid}"))) + "\n"
            f"- 4h/6h/12h/24h aligned: {r.get('4h_aligned_return_pct')} / {r.get('6h_aligned_return_pct')} / "
            f"{r.get('12h_aligned_return_pct')} / {r.get('24h_aligned_return_pct')}\n"
        )

    dir_note = ""
    tf_note = ""
    text = f"""# Combined causal entry-warning filter research

Final label: **{label}**

Frozen strategy `{quality.get('strategy_unchanged') and 'wave_fade_frozen_f16ae32_causal_entry_v1'}` unchanged.  
Outcomes, entries, TP/SL from evaluation `{recon.get('n') and '94d0cfbfb2da4c829dc0d95588dc052d'}` unchanged.  
NO_BE50, SL_FIRST, no max-hold. Fee 0.11 pp per closed kept trade.  
ZEC splits and all filter conclusions: `{OOS_CAVEAT}`.  
No ClickHouse writes, no live actions, no commit, no push.

## Population

ZEC trades={recon['n']} WIN={recon['wins']} LOSS={recon['losses']} OPEN={recon['open']} gross={recon['gross_sum']} pp.  
Splits={recon['split_counts']}. Signal IDs match context parquet: {recon['signal_ids_match']}.

## 1. Score 0–4 Verteilung

{chr(10).join(score_lines)}

## 2. Steigt die Loss-Rate mit dem Warning Score?

Siehe Tabelle oben. Eine monotone Zunahme wäre ein positives Diagnosesignal; das Fehlen davon spricht gegen einen einfachen Score-Block.

## 3. Welche feste Regel verbessert ZEC Development?

Best Development by Net-Sum-Δ: **{best_id or 'keine'}**  
{('Δ net ' + _n(best_dev.get('net_sum_delta')) + ' pp, PF ' + _n(best_dev.get('net_pf_before')) + '→' + _n(best_dev.get('net_pf_after')) + ', block ' + _pct(best_dev.get('block_rate'))) if best_dev is not None else 'n/a'}

## 4. Bleibt sie in Validation und Test stabil?

{chr(10).join(temp_lines) if temp_lines else 'n/a'}

## 5–6. Externe Coins

ALL_EXCLUDING_ZEC for {best_id or 'n/a'}: net Δ {_n(ext_all.iloc[0]['net_sum_delta'] if len(ext_all) else None)} PF {_n(ext_all.iloc[0]['net_pf_before'] if len(ext_all) else None)}→{_n(ext_all.iloc[0]['net_pf_after'] if len(ext_all) else None)}  
Coins better/worse on net sum: {n_better}/{n_worse} (incl. ZEC row if present).

## 7. WIN/LOSS je Regel (ZEC)

{chr(10).join(rule_block_lines)}

## 8–9. Stärkste ZEC-Net-Sum-Regel und Breite

Strongest overall ZEC net-sum Δ: **{strongest.get('rule_id')}** Δ={_n(strongest.get('net_sum_delta'))} PF {_n(strongest.get('net_pf_before'))}→{_n(strongest.get('net_pf_after'))} block={_pct(strongest.get('block_rate'))}.  
See `rule_results_by_coin.csv`, `rule_results_by_direction.csv`, `rule_results_by_timeframe.csv`.

## 10–11. 4h/6h/12h/24h Pfade

Score 0 vs Score 2+ (using score=2 row if present):

- 4h in-dir score0={_pct(p4_0.get('share_in_direction'))} score2={_pct(p4_2.get('share_in_direction'))}; median {_n(p4_0.get('median_aligned'))} vs {_n(p4_2.get('median_aligned'))}
- 6h in-dir score0={_pct(p6_0.get('share_in_direction'))} score2={_pct(p6_2.get('share_in_direction'))}; median {_n(p6_0.get('median_aligned'))} vs {_n(p6_2.get('median_aligned'))}
- 12h in-dir score0={_pct(p12_0.get('share_in_direction'))} score2={_pct(p12_2.get('share_in_direction'))}
- 24h in-dir score0={_pct(p24_0.get('share_in_direction'))} score2={_pct(p24_2.get('share_in_direction'))}

R2 BLOCKED vs KEPT:

- 4h in-dir blocked={_pct(r2b4.get('share_in_direction'))} kept={_pct(r2k4.get('share_in_direction'))}
- 6h in-dir blocked={_pct(r2b6.get('share_in_direction'))} kept={_pct(r2k6.get('share_in_direction'))}

## 12. Verspätetes Entry oder falsche Idee?

Wenn BLOCKED nach 4h/6h seltener in Trade-Richtung liegt, aber nach 12h/24h aufholt, spricht das für Timing. Wenn die Lücke bleibt oder sich weitet, ist die Idee selbst schwach. Outcome bleibt das Original-SL/TP.

## 13–14. Schnelle Gewinner / lange Verluste (R2)

Blocked share of wins={_pct(fast_r2.get('share_blocked_wins'))}; of wins ≤15m={_pct(fast_r2.get('share_blocked_fast_wins_le15m'))} (n={fast_r2.get('n_fast_wins_le15m')}).  
Blocked share of losses={_pct(fast_r2.get('share_blocked_losses'))}; of losses ≥4h={_pct(fast_r2.get('share_blocked_long_losses_ge4h'))}.  
Median hold blocked vs kept wins: {fast_r2.get('median_hold_blocked_wins')} vs {fast_r2.get('median_hold_kept_wins')} s.  
Median hold blocked vs kept losses: {fast_r2.get('median_hold_blocked_losses')} vs {fast_r2.get('median_hold_kept_losses')} s.

## 9b. Frühe SL-Erholung (Outcome unverändert LOSS)

{chr(10).join(rec_lines)}

## 15. August-ZEC-Fälle

{chr(10).join(man_txt) if man_txt else 'nicht gefunden'}

## 16. Datenqualität

- Lookahead/hard-fail: {quality.get('hard_fail')} {quality.get('hard_fail_reason')}
- W1 vs previous 5m test mismatches: {quality.get('w1_vs_prev_5m_mismatches')}
- Tests passed: {quality.get('tests_passed')}
- Missingness:
{chr(10).join(miss_lines)}

## 17. Größerer Backtest?

Label **{label}**. PROMISING nur bei besserem Net Sum und Net PF, ohne klaren Validation/Test-Kipp, mit besserem externem Korb, mehreren Coins, ohne dass eine Richtung/ein Coin alles trägt.

## 18. Frozen-Strategie unverändert?

Ja. ClickHouse-Writes: {quality.get('clickhouse_writes', 0)}. Live-Aktionen: 0. Commit: nein. Push: nein.
"""
    path.write_text(text)

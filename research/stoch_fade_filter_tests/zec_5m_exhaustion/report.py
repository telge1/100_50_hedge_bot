"""REPORT.md for the frozen 5m-exhaustion entry-block test."""

from __future__ import annotations

from typing import Any

import pandas as pd


def _pct(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "n/a"
    return f"{float(value):.2%}"


def _n(value: object, digits: int = 3) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "n/a"
    return f"{float(value):.{digits}f}"


def choose_label(
    *,
    blocked_run: bool,
    zec_base: dict[str, Any],
    zec_kept: dict[str, Any],
    temporal: pd.DataFrame,
    external: pd.DataFrame,
    kept_closed: int,
) -> str:
    if blocked_run:
        return "ZEC_5M_EXHAUSTION_FILTER_BLOCKED"
    base_sum = zec_base.get("net_sum")
    kept_sum = zec_kept.get("net_sum")
    base_pf = zec_base.get("net_pf")
    kept_pf = zec_kept.get("net_pf")
    sum_up = kept_sum is not None and base_sum is not None and kept_sum > base_sum + 1e-9
    pf_up = kept_pf is not None and base_pf is not None and kept_pf > base_pf + 1e-9
    enough = kept_closed >= 400
    signs = []
    if temporal is not None and not temporal.empty and "net_sum_delta" in temporal.columns:
        signs = [float(x) for x in temporal["net_sum_delta"].tolist() if pd.notna(x)]
    mixed_time = bool(signs) and any(s > 1e-9 for s in signs) and any(s < -1e-9 for s in signs)
    ext = external.loc[external["symbol"] == "ALL_EXCLUDING_ZEC"] if external is not None and not external.empty else pd.DataFrame()
    ext_sum_up = False
    if len(ext):
        ext_sum_up = float(ext.iloc[0].get("net_sum_after") or 0) > float(ext.iloc[0].get("net_sum_before") or 0) + 1e-9
    if sum_up and pf_up and enough and not mixed_time and ext_sum_up:
        return "ZEC_5M_EXHAUSTION_FILTER_PROMISING"
    if not sum_up or mixed_time or (len(ext) and not ext_sum_up):
        if sum_up or pf_up:
            return "ZEC_5M_EXHAUSTION_FILTER_INCONCLUSIVE"
        return "ZEC_5M_EXHAUSTION_FILTER_NOT_CONFIRMED"
    return "ZEC_5M_EXHAUSTION_FILTER_INCONCLUSIVE"


def write_report(
    *,
    path,
    label: str,
    zec_base: dict[str, Any],
    zec_kept: dict[str, Any],
    blocked: dict[str, Any],
    horizon: pd.DataFrame,
    recovery: dict[str, Any],
    temporal: pd.DataFrame,
    external: pd.DataFrame,
    manuals: pd.DataFrame,
    quality: dict[str, Any],
    fast_wins: dict[str, Any],
) -> None:
    def hz(cohort: str, name: str) -> pd.Series:
        hit = horizon.loc[(horizon["cohort"] == cohort) & (horizon["horizon"] == name)]
        return hit.iloc[0] if len(hit) else pd.Series(dtype=object)

    b4 = hz("BASELINE", "4h")
    k4 = hz("KEPT", "4h")
    x4 = hz("BLOCKED", "4h")
    b6 = hz("BASELINE", "6h")
    k6 = hz("KEPT", "6h")
    x6 = hz("BLOCKED", "6h")
    man_txt = []
    for _, row in manuals.iterrows():
        man_txt.append(
            f"### {row.get('entry_time')} {row.get('direction')} `{row.get('signal_id')}`\n"
            f"- Decision: **{row.get('decision')}** (exhausted={row.get('stoch_exhausted_in_trade_direction')})\n"
            f"- 5m K={row.get('tf_5m_stoch_k')} D={row.get('tf_5m_stoch_d')} "
            f"bar {row.get('tf_5m_source_bar_open')} → {row.get('tf_5m_source_bar_close')}\n"
            f"- 1m: phase={row.get('tf_1m_stoch_phase')} K={row.get('tf_1m_stoch_k')} exhausted={row.get('tf_1m_stoch_exhausted_in_trade_direction')}\n"
            f"- Outcome unchanged: {row.get('outcome')} hold_s={row.get('hold_seconds')} "
            f"gross={row.get('pnl_pct_gross')} net={row.get('pnl_pct_net')}\n"
            f"- Price 1h/2h/4h/6h/12h: {row.get('1h_price')} / {row.get('2h_price')} / "
            f"{row.get('4h_price')} / {row.get('6h_price')} / {row.get('12h_price')}\n"
            f"- Aligned 4h/6h: {row.get('4h_aligned_return_pct')} / {row.get('6h_aligned_return_pct')}\n"
            f"- In-trade MFE/MAE 4h: {row.get('4h_in_trade_mfe_pct')} / {row.get('4h_in_trade_mae_pct')}; "
            f"TP/SL touch {row.get('4h_in_trade_tp_touched')}/{row.get('4h_in_trade_sl_touched')}\n"
            f"- Post-exit 4h aligned from entry: {row.get('4h_post_exit_aligned_from_entry_pct')}\n"
        )
    temp_lines = []
    if temporal is not None and not temporal.empty:
        for _, row in temporal.iterrows():
            temp_lines.append(
                f"- {row.get('split_label')} ({row.get('oos_caveat')}): n={row.get('trades')} "
                f"blockrate={_pct(row.get('block_rate'))} "
                f"winrate { _pct(row.get('winrate_before'))} → {_pct(row.get('winrate_after'))} "
                f"net_sum { _n(row.get('net_sum_before'))} → {_n(row.get('net_sum_after'))} "
                f"(Δ {_n(row.get('net_sum_delta'))}) "
                f"net_pf {_n(row.get('net_pf_before'))} → {_n(row.get('net_pf_after'))} "
                f"blocked W/L {row.get('blocked_wins')}/{row.get('blocked_losses')}"
            )
    ext_lines = []
    if external is not None and not external.empty:
        for _, row in external.iterrows():
            ext_lines.append(
                f"- {row.get('symbol')}: n={row.get('trades')} block={_pct(row.get('block_rate'))} "
                f"blocked W/L {row.get('blocked_wins')}/{row.get('blocked_losses')} "
                f"winrate {_pct(row.get('winrate_before'))}→{_pct(row.get('winrate_after'))} "
                f"net_sum {_n(row.get('net_sum_before'))}→{_n(row.get('net_sum_after'))} "
                f"net_pf {_n(row.get('net_pf_before'))}→{_n(row.get('net_pf_after'))}"
            )
    rec4 = recovery.get("4h", {})
    rec6 = recovery.get("6h", {})
    text = f"""# ZEC 5m exhaustion entry-block test

Final label: **{label}**

Rule: `BLOCK_5M_EXHAUSTED_IN_TRADE_DIRECTION`  
Feature (frozen): last fully closed 5m StochRSI %K; LONG if K>80; SHORT if K<20; missing K → not blocked.  
D and phase-turning are **not** part of this flag. Copied into `rule_manifest.json` from the ZEC context analysis.  
Outcomes unchanged: original TP/SL, NO_BE50, SL_FIRST, no max-hold, full 1m scan.  
No ClickHouse writes. No strategy change. Validation/Test on ZEC are `{quality.get('oos_caveat')}`.

## 1. Wie viele Trades blockiert die Regel?

ZEC: **{blocked.get('blocked_trades')}** of {zec_base.get('trades')} ({_pct((blocked.get('blocked_trades') or 0) / (zec_base.get('trades') or 1))}).  
Kept: **{zec_kept.get('trades')}**.

## 2. Wie viele Losses und Wins werden blockiert?

Blocked wins: **{blocked.get('blocked_wins')}**. Blocked losses: **{blocked.get('blocked_losses')}**. Blocked open: **{blocked.get('blocked_open')}**.  
Loss/Win ratio among blocked: {_n(blocked.get('blocked_loss_to_win_ratio'), 3)}.

## 3. Verbessert sich Net Sum und Net PF?

| | BASELINE | KEPT (filter) |
|---|---:|---:|
| Gross sum (pp) | {_n(zec_base.get('gross_sum'))} | {_n(zec_kept.get('gross_sum'))} |
| Gross PF | {_n(zec_base.get('gross_pf'))} | {_n(zec_kept.get('gross_pf'))} |
| Fees (pp) | {_n(zec_base.get('fees_total_pp'))} | {_n(zec_kept.get('fees_total_pp'))} |
| Net sum (pp) | {_n(zec_base.get('net_sum'))} | {_n(zec_kept.get('net_sum'))} |
| Net PF | {_n(zec_base.get('net_pf'))} | {_n(zec_kept.get('net_pf'))} |
| Winrate | {_pct(zec_base.get('winrate'))} | {_pct(zec_kept.get('winrate'))} |
| Net mean | {_n(zec_base.get('net_mean'))} | {_n(zec_kept.get('net_mean'))} |
| Net median | {_n(zec_base.get('net_median'))} | {_n(zec_kept.get('net_median'))} |
| Longest loss streak | {zec_base.get('longest_loss_streak')} | {zec_kept.get('longest_loss_streak')} |

Missed net profit: {_n(blocked.get('missed_net_profit'))} pp. Avoided net loss: {_n(blocked.get('avoided_net_loss'))} pp.  
Net sum removed by blocking: {_n(blocked.get('net_sum_removed'))} pp (negative means the blocked set was a net loser).

## 4. Bleiben genügend Trades?

Kept closed: {zec_kept.get('closed')} of {zec_base.get('closed')} closed. Open remaining: {zec_kept.get('open')}.

## 5. Hält die Wirkung zeitlich?

ZEC splits are Development / Temporal Validation / Temporal Test, all `{quality.get('oos_caveat')}` because the 5m hypothesis was seen on the full ZEC population.

{chr(10).join(temp_lines)}

## 6. Hält die Wirkung auf anderen Coins?

Same frozen rule. No coin dropped.

{chr(10).join(ext_lines)}

## 7. Was passiert durchschnittlich nach 4h?

Market path (outcome not rewritten). Share still in trade-direction / median aligned return / still open:

- BASELINE: in-dir={_pct(b4.get('share_in_direction'))}, median={_n(b4.get('median_aligned_return'))} pp, still-open={_pct(b4.get('share_still_open'))}, TP-touch={_pct(b4.get('share_tp_touched_in_trade'))}, SL-touch={_pct(b4.get('share_sl_touched_in_trade'))}, n_ok={b4.get('n_ok')} unavailable={b4.get('n_unavailable')}
- KEPT: in-dir={_pct(k4.get('share_in_direction'))}, median={_n(k4.get('median_aligned_return'))} pp, still-open={_pct(k4.get('share_still_open'))}
- BLOCKED: in-dir={_pct(x4.get('share_in_direction'))}, median={_n(x4.get('median_aligned_return'))} pp, still-open={_pct(x4.get('share_still_open'))}

Median market MFE/MAE BASELINE: {_n(b4.get('median_market_mfe'))} / {_n(b4.get('median_market_mae'))} pp.

## 8. Was passiert durchschnittlich nach 6h?

- BASELINE: in-dir={_pct(b6.get('share_in_direction'))}, median={_n(b6.get('median_aligned_return'))} pp, still-open={_pct(b6.get('share_still_open'))}, TP-touch={_pct(b6.get('share_tp_touched_in_trade'))}, SL-touch={_pct(b6.get('share_sl_touched_in_trade'))}, n_ok={b6.get('n_ok')} unavailable={b6.get('n_unavailable')}
- KEPT: in-dir={_pct(k6.get('share_in_direction'))}, median={_n(k6.get('median_aligned_return'))} pp
- BLOCKED: in-dir={_pct(x6.get('share_in_direction'))}, median={_n(x6.get('median_aligned_return'))} pp

Median market MFE/MAE BASELINE: {_n(b6.get('median_market_mfe'))} / {_n(b6.get('median_market_mae'))} pp.

## 9. Wie viele frühe SLs erholen sich später?

Recovery = SL exit at or before the horizon, then aligned market return from entry at that horizon is > 0 (would have been in profit if not stopped). Outcome stays SL.

- 4h: SL-before={rec4.get('n_sl_before_horizon')}, then aligned={rec4.get('n_sl_before_horizon_then_aligned')} (share {_pct(rec4.get('share_sl_early_that_recover'))})
- 6h: SL-before={rec6.get('n_sl_before_horizon')}, then aligned={rec6.get('n_sl_before_horizon_then_aligned')} (share {_pct(rec6.get('share_sl_early_that_recover'))})
- WIN with MAE then TP-touch by 4h (approx): {rec4.get('n_win_with_mae_then_tp')}
- LOSS with MFE then SL by 4h (approx): {rec4.get('n_loss_with_mfe_then_sl')}

## 10. Werden schnelle Gewinner überproportional blockiert?

Blocked share among all ZEC wins: {_pct(fast_wins.get('share_blocked_all_wins'))}.  
Among wins with hold ≤ 15m: {_pct(fast_wins.get('share_blocked_wins_le_15m'))} (n={fast_wins.get('n_wins_le_15m')}).  
Median hold blocked wins: {fast_wins.get('median_hold_blocked_wins')} s vs kept wins {fast_wins.get('median_hold_kept_wins')} s.

## 11. Werden die beiden untersuchten ZEC-Losses blockiert?

{chr(10).join(man_txt) if man_txt else 'Manuelle Fälle fehlen.'}

## 12. Ist die Regel stabil genug für einen größeren Backtest?

Label **{label}**. A larger backtest is justified only if ZEC net sum and net PF both improve, enough trades remain, temporal deltas do not flip sign, and the external-coin basket (excluding ZEC) also improves. Otherwise keep the rule frozen for diagnosis only.

## Quality

- Lookahead: {quality.get('lookahead_failures')}
- Stored vs recomputed ZEC flag mismatches: {quality.get('zec_flag_recompute_mismatches')}
- Stored vs formula(K) mismatches: {quality.get('zec_flag_formula_mismatches')}
- Baseline gross sum check: {quality.get('baseline_gross_ok')}
- Tests passed: {quality.get('tests_passed')}
- ClickHouse writes: 0
"""
    path.write_text(text)

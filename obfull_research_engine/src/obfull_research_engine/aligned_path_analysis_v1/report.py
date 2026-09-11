"""Markdown / terminal report. Not a trading signal."""

from __future__ import annotations

from typing import Any

import pandas as pd


def _row(df: pd.DataFrame, **kwargs: Any) -> dict[str, Any]:
    if df is None or df.empty:
        return {}
    work = df
    for k, v in kwargs.items():
        if k in work.columns:
            work = work[work[k] == v]
    if work.empty:
        return {}
    return work.iloc[0].to_dict()


def _pct(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "n/a"
    return f"{float(val):.3f}"


def _ms(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "n/a"
    return f"{float(val):.0f}"


def _share(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "n/a"
    return f"{100.0 * float(val):.1f}%"


def render_markdown(payload: dict[str, Any], *, by_batch: pd.DataFrame) -> str:
    plan = payload.get("price_plan") or {}
    hash_info = payload.get("hc_hash") or {}
    res = payload.get("resources") or {}
    a1800 = _row(by_batch, compare_group="ALIGNED", batch_id="pooled", level="episode", horizon_seconds=1800)
    a60 = _row(by_batch, compare_group="ALIGNED", batch_id="pooled", level="episode", horizon_seconds=60)
    a300 = _row(by_batch, compare_group="ALIGNED", batch_id="pooled", level="episode", horizon_seconds=300)
    a900 = _row(by_batch, compare_group="ALIGNED", batch_id="pooled", level="episode", horizon_seconds=900)
    hs = _row(by_batch, compare_group="HC_STRICT", batch_id="pooled", level="episode", horizon_seconds=1800)
    ho = _row(by_batch, compare_group="HC_WITH_OI_MIXED", batch_id="pooled", level="episode", horizon_seconds=1800)
    cf = _row(by_batch, compare_group="ALIGNED_BUT_CONFLICTING", batch_id="pooled", level="episode", horizon_seconds=1800)
    b1 = _row(by_batch, compare_group="ALIGNED", batch_id="batch_1", level="episode", horizon_seconds=1800)
    b2 = _row(by_batch, compare_group="ALIGNED", batch_id="batch_2", level="episode", horizon_seconds=1800)

    def block(title: str, r: dict[str, Any]) -> str:
        return (
            f"### {title}\n"
            f"- n={r.get('n')} complete={r.get('n_complete')} clusters={r.get('n_clusters')} flag={r.get('sample_flag')} warn={r.get('warnings')}\n"
            f"- Profit-first {r.get('n_profit_first')} ({_share(r.get('share_profit_first'))}); "
            f"adverse-first {r.get('n_adverse_first')} ({_share(r.get('share_adverse_first'))})\n"
            f"- Median MAE before first profit: {_pct(r.get('median_mae_before_first_profit_pct'))} %\n"
            f"- Median time underwater before first profit: {_ms(r.get('median_time_underwater_before_first_profit_ms'))} ms\n"
            f"- Median time to first profit: {_ms(r.get('median_time_to_first_profit_ms'))} ms\n"
            f"- +0.10 before −0.10: {_share(r.get('share_plus_0_10_before_minus_0_10'))}; "
            f"+0.20 before −0.20: {_share(r.get('share_plus_0_20_before_minus_0_20'))}; "
            f"+0.50 before −0.50: {_share(r.get('share_plus_0_50_before_minus_0_50'))}\n"
            f"- Median MFE {_pct(r.get('median_mfe_pct'))} %; median MAE {_pct(r.get('median_mae_pct'))} %\n"
            f"- MFE before MAE: {_share(r.get('share_mfe_before_mae'))}; MAE before MFE: {_share(r.get('share_mae_before_mfe'))}\n"
            f"- Median giveback {_pct(r.get('median_giveback_from_mfe_pct'))} pp; "
            f"giveback fraction {_pct(r.get('median_giveback_fraction_of_mfe'))}; "
            f"retained {_pct(r.get('median_retained_profit_at_horizon_pct'))} % "
            f"(fraction {_pct(r.get('median_retained_fraction_of_mfe'))})\n"
            f"- Crossed back below entry after MFE: {_share(r.get('share_crossed_back_below_zero_after_mfe'))}\n"
            f"- Classes: held={r.get('n_direct_profit_held')} given_back={r.get('n_direct_profit_given_back')} "
            f"adverse_then_profit={r.get('n_adverse_then_profit')} no_recovery={r.get('n_adverse_no_recovery')} "
            f"chop={r.get('n_chop_around_entry')} flat={r.get('n_flat_or_insufficient')}\n"
        )

    return f"""# Path-Analyse nach eingefrorener HC-Klassifikation

Status: RESEARCH_PATH_ANALYSIS_ONLY. Kein Trading-Signal. HC-Klassen unverändert.

## 1. Preisquelle und Auflösung
- Path source: `{plan.get('path_source')}`
- Resolution: `{plan.get('path_resolution_ms')}`
- 100ms stored for HC episodes: `{((plan.get('stored_100ms') or {}).get('available_for_hc_episodes'))}`
- Fallback used: `{plan.get('used_fallback')}`
- Note: {plan.get('note')}
- Reference rule: causal 1s mid at detection if available (`available_at = state_ts + 1s`, age ≤ 1500 ms); otherwise first public trade with `trade_ts >= detection_available_at`. No look-ahead entry pick.

## 2. Auswertbare Episoden
- ALIGNED frozen: {payload.get('n_aligned')}
- Fully evaluable at 1800s: {payload.get('n_complete_1800')}
- Partial / missing: {payload.get('n_partial')}

## 3–13. ALIGNED pooled 1800s (warning: not independent)
{block('ALIGNED pooled 1800s', a1800)}

## Horizon fade (ALIGNED pooled episode)
- 60s retained median {_pct(a60.get('median_retained_profit_at_horizon_pct'))} %; giveback fraction {_pct(a60.get('median_giveback_fraction_of_mfe'))}; below-entry after MFE {_share(a60.get('share_crossed_back_below_zero_after_mfe'))}
- 300s retained median {_pct(a300.get('median_retained_profit_at_horizon_pct'))} %; giveback fraction {_pct(a300.get('median_giveback_fraction_of_mfe'))}
- 900s retained median {_pct(a900.get('median_retained_profit_at_horizon_pct'))} %; giveback fraction {_pct(a900.get('median_giveback_fraction_of_mfe'))}
- 1800s retained median {_pct(a1800.get('median_retained_profit_at_horizon_pct'))} %; giveback fraction {_pct(a1800.get('median_giveback_fraction_of_mfe'))}

## 14. Klassenunterschiede (pooled episode 1800s)
{block('HC_STRICT', hs)}
{block('HC_WITH_OI_MIXED', ho)}
{block('ALIGNED_BUT_CONFLICTING', cf)}

## 15. Batch 1 vs Batch 2 (ALIGNED episode 1800s)
{block('Batch 1', b1)}
{block('Batch 2', b2)}

## 16. Sample-Warnungen
- {payload.get('sample_warnings')}
- Pooled slices are descriptive only and are not an independent confirmation.

## 17. Result-Pfade
- {payload.get('out_dir')}
- HC run (read-only): {payload.get('hc_run_dir')}

## 18. Laufzeit und Ressourcen
- elapsed_s={res.get('elapsed_s')} rss_mb_start={res.get('rss_mb_start')} rss_mb_peak={res.get('rss_mb_peak')}

## 19. HC-Klassifikation unverändert
- classification.csv sha256 before: `{hash_info.get('classification_sha256_before')}`
- classification.csv sha256 after: `{hash_info.get('classification_sha256_after')}`
- file unchanged: `{hash_info.get('classification_file_unchanged')}`
- recomputed blind hash: `{hash_info.get('recomputed_blind_hash')}`
- stored blind hash: `{hash_info.get('stored_blind_hash')}`
- hashes match: `{hash_info.get('blind_hash_unchanged')}`
- classes rewritten: false
- shards rebuilt: false
- Full-OB replay: false
- ClickHouse write: false
- commit/push: false
"""


def render_terminal(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "ALIGNED PATH ANALYSIS V1",
            f"Path source: {(payload.get('price_plan') or {}).get('path_source')}",
            f"n_aligned={payload.get('n_aligned')} n_complete_1800={payload.get('n_complete_1800')}",
            f"Blind hash unchanged: {(payload.get('hc_hash') or {}).get('blind_hash_unchanged')}",
            f"Out: {payload.get('out_dir')}",
        ]
    )

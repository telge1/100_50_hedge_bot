"""Artifact writers + REPORT.md for trend forecast validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def write_artifacts(
    output_dir: Path,
    *,
    run_config: dict[str, Any],
    data_quality: dict[str, Any],
    warmup_state: dict[str, Any],
    trace: pd.DataFrame,
    signals: pd.DataFrame,
    outcomes: pd.DataFrame,
    summaries: dict[str, pd.DataFrame],
    causal_guards: dict[str, Any],
    hedge_diag: dict[str, Any],
    write_candle_trace: bool = True,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    def put(name: str, path: Path) -> None:
        paths[name] = str(path)

    p = output_dir / "run_config.json"
    _write_json(p, run_config)
    put("run_config", p)

    p = output_dir / "data_quality.json"
    _write_json(p, data_quality)
    put("data_quality", p)

    p = output_dir / "warmup_state.json"
    _write_json(p, warmup_state)
    put("warmup_state", p)

    p = output_dir / "causal_guards.json"
    _write_json(p, causal_guards)
    put("causal_guards", p)

    p = output_dir / "hedge_relevance_diagnosis.json"
    _write_json(p, hedge_diag)
    put("hedge_relevance_diagnosis", p)

    if write_candle_trace and not trace.empty:
        p = output_dir / "scanner_candle_trace.parquet"
        # Keep a lean column set for size
        keep = [
            c
            for c in trace.columns
            if c
            in {
                "timestamp",
                "decision_time",
                "open",
                "high",
                "low",
                "close",
                "period",
                "protected_structure_state",
                "previous_protected_structure_state",
                "major_direction",
                "external_bos_up",
                "external_bos_down",
                "choch_side",
                "protected_high",
                "protected_low",
                "micro_swing_high",
                "micro_swing_low",
                "ema_9",
                "ema_20",
                "ema_59",
                "ema_200",
                "adx",
                "plus_di",
                "minus_di",
                "atr_14",
                "last_visible_30m_timestamp",
                "last_visible_4h_timestamp",
                "htf_30m_fully_closed",
                "htf_4h_fully_closed",
                "htf_both_closed",
                "m30_major_direction",
                "h4_major_direction",
                "transition_reason",
            }
            or c.startswith("m30_")
            or c.startswith("h4_")
        ]
        trace.loc[:, keep].to_parquet(p, index=False)
        put("scanner_candle_trace", p)

    p = output_dir / "scanner_signals.csv"
    signals.to_csv(p, index=False)
    put("scanner_signals", p)

    p = output_dir / "signal_outcomes.csv"
    outcomes.to_csv(p, index=False)
    put("signal_outcomes", p)

    for key, frame in summaries.items():
        p = output_dir / f"{key}.csv"
        frame.to_csv(p, index=False)
        put(key, p)

    # Optional extremes
    if not outcomes.empty:
        stats = outcomes.loc[outcomes["include_in_stats"] == True]  # noqa: E712
        fail = stats.loc[stats["primary_outcome"] == "FAILURE"].copy()
        if not fail.empty:
            fail = fail.sort_values("mae_pct", ascending=False).head(50)
            p = output_dir / "worst_failures.csv"
            fail.to_csv(p, index=False)
            put("worst_failures", p)
        win = stats.loc[stats["primary_outcome"] == "SUCCESS"].copy()
        if not win.empty:
            win = win.sort_values("mfe_pct", ascending=False).head(50)
            p = output_dir / "best_continuations.csv"
            win.to_csv(p, index=False)
            put("best_continuations", p)

    report = build_report_markdown(
        run_config=run_config,
        data_quality=data_quality,
        warmup_state=warmup_state,
        signals=signals,
        outcomes=outcomes,
        summaries=summaries,
        causal_guards=causal_guards,
        hedge_diag=hedge_diag,
        paths=paths,
    )
    p = output_dir / "REPORT.md"
    p.write_text(report, encoding="utf-8")
    put("REPORT", p)
    return paths


def _rate_line(summaries: dict[str, pd.DataFrame], signal_type: str, period: str) -> str:
    df = summaries.get("summary_by_signal")
    if df is None or df.empty:
        return "_n/a_"
    sub = df.loc[(df["signal_type"] == signal_type) & (df["development_or_oos"] == period)]
    if sub.empty:
        return "_no signals_"
    r = sub.iloc[0]
    return (
        f"n={int(r['signal_count'])}, "
        f"success_ex_open={r['success_rate_excluding_open']}, "
        f"target_first={r['target_first_rate']}, "
        f"median_mfe={r['median_mfe_pct']}, median_mae={r['median_mae_pct']}"
    )


def build_report_markdown(
    *,
    run_config: dict[str, Any],
    data_quality: dict[str, Any],
    warmup_state: dict[str, Any],
    signals: pd.DataFrame,
    outcomes: pd.DataFrame,
    summaries: dict[str, pd.DataFrame],
    causal_guards: dict[str, Any],
    hedge_diag: dict[str, Any],
    paths: dict[str, str],
) -> str:
    stats_sig = signals.loc[signals["include_in_stats"] == True] if not signals.empty else signals  # noqa: E712
    n_bull = int((stats_sig["signal_type"] == "BULLISH_EXTERNAL_BOS_AFTER_PULLBACK").sum()) if not stats_sig.empty else 0
    n_bear = int((stats_sig["signal_type"] == "BEARISH_EXTERNAL_BOS_AFTER_PULLBACK").sum()) if not stats_sig.empty else 0
    n_choch = int(stats_sig["signal_type"].astype(str).str.contains("CHOCH").sum()) if not stats_sig.empty else 0

    # Honest OOS vs DEV conclusion
    by = summaries.get("summary_by_signal")
    oos_ok = False
    if by is not None and not by.empty:
        bos = by.loc[
            by["signal_type"].isin(
                [
                    "BULLISH_EXTERNAL_BOS_AFTER_PULLBACK",
                    "BEARISH_EXTERNAL_BOS_AFTER_PULLBACK",
                ]
            )
            & (by["development_or_oos"] == "out_of_sample")
        ]
        if not bos.empty:
            rates = [r for r in bos["success_rate_excluding_open"].tolist() if r is not None]
            # Require meaningful failure coverage + rate not trivially 1.0 from missing invalidation
            fail_n = int(bos["failure_count"].sum()) if "failure_count" in bos.columns else 0
            oos_ok = bool(rates) and (sum(rates) / len(rates) >= 0.55) and fail_n >= 0
            # If every decided path is success with zero failures across BOS OOS, treat as inconclusive
            if int(bos["failure_count"].sum()) == 0 and int(bos["success_count"].sum()) > 0:
                oos_ok = False

    if by is not None and not by.empty:
        bos_oos = by.loc[
            by["signal_type"].str.contains("BOS_AFTER", na=False)
            & (by["development_or_oos"] == "out_of_sample")
        ]
        mean_oos = None
        if not bos_oos.empty:
            vals = [float(x) for x in bos_oos["success_rate_excluding_open"] if pd.notna(x)]
            mean_oos = sum(vals) / len(vals) if vals else None
        zero_fail = not bos_oos.empty and int(bos_oos["failure_count"].sum()) == 0
    else:
        mean_oos = None
        zero_fail = False

    if zero_fail:
        conclusion = (
            "BOS continuation paths often reach soft percent targets when invalidation is missing or "
            "rarely touched; treat headline success rates as **upper-bound reachability**, not proven edge. "
            "With a corrected prior-bar protected invalidation, compare failure/first-touch tables before "
            "any hedge test."
        )
    elif oos_ok and mean_oos is not None and mean_oos >= 0.55:
        conclusion = (
            "Out-of-sample BOS continuation rates clear a weak 55% decided-outcome bar — "
            "forecast power is **suggestive, not proven**. A later small hedge simulation may be test-worthy "
            "only after first-touch/MAE diagnostics remain stable."
        )
    else:
        conclusion = (
            "Out-of-sample results do **not** support claiming reliable forecast power for continuation BOS signals. "
            "Treat the scanner as descriptive structure, not a standalone predictive edge, until broader OOS evidence exists."
        )

    lines = [
        "# APTUSDT Trend Forecast Validation",
        "",
        "## 1. Goal",
        "",
        "Measure whether existing C3.4/C3.5 trend-scanner events predict subsequent price paths — "
        "no hedge positions, no new signals, no runtime changes.",
        "",
        "## 2. Scanner components reused",
        "",
        "- `enrich_indicators` + `attach_structure_edges` / `apply_protected_structure` (C3.4B)",
        "- `aggregate_candles` (30m) + `aggregate_complete_from_5m` (4h)",
        "- `asof_htf_context` causal HTF attach",
        "- Signal types mapped from rising edges of existing structure flags (no second market structure)",
        "",
        "## 3. Data source and quality",
        "",
        f"- source: `{data_quality.get('data_source')}` ({data_quality.get('table_or_path')})",
        f"- first/last: `{data_quality.get('first_timestamp')}` → `{data_quality.get('last_timestamp')}`",
        f"- n_5m: **{data_quality.get('n_candles_5m')}**, missing≈{data_quality.get('missing_5m_candles')}, "
        f"largest_gap_min={data_quality.get('largest_gap_minutes')}",
        f"- fallback_used: {data_quality.get('fallback_used')}",
        "",
        "## 4. Warm-up",
        "",
        f"- effective start: `{warmup_state.get('effective_warmup_start')}` end `{warmup_state.get('warmup_end')}`",
        f"- candles: **{warmup_state.get('warmup_candles')}**, days≈{warmup_state.get('warmup_days')}",
        f"- scanner_state_ready: **{warmup_state.get('scanner_state_ready')}**",
        f"- protected H/L: {warmup_state.get('protected_high')} / {warmup_state.get('protected_low')}",
        f"- major_trend: {warmup_state.get('major_trend_label')}",
        "",
        "## 5. Development / Out-of-Sample",
        "",
        f"- development: `{run_config.get('development_start')}` → `{run_config.get('development_end')}`",
        f"- oos: `{run_config.get('out_of_sample_start')}` → end of history",
        "",
        "## 6. Causal replay semantics",
        "",
        "- Decision uses only candles ≤ t; forecast stored at close of t; outcomes from t+1.",
        "- HTF bars usable only when fully closed (`htf_close_decision ≤ 5m decision_time`).",
        "- Ambiguous same-candle target+invalidation → `AMBIGUOUS` (not success); conservative bound recorded.",
        "",
        "## 7–8. Signal / outcome definitions",
        "",
        "- Primary continuation: `*_EXTERNAL_BOS_AFTER_PULLBACK` vs protected invalidation.",
        "- CHOCH exported separately (not auto-continuation).",
        "- Targets: percent, ATR multiples, structure levels — all reported, none cherry-picked.",
        "",
        "## 9. BOS bullish",
        "",
        f"- DEV: {_rate_line(summaries, 'BULLISH_EXTERNAL_BOS_AFTER_PULLBACK', 'development')}",
        f"- OOS: {_rate_line(summaries, 'BULLISH_EXTERNAL_BOS_AFTER_PULLBACK', 'out_of_sample')}",
        "",
        "## 10. BOS bearish",
        "",
        f"- DEV: {_rate_line(summaries, 'BEARISH_EXTERNAL_BOS_AFTER_PULLBACK', 'development')}",
        f"- OOS: {_rate_line(summaries, 'BEARISH_EXTERNAL_BOS_AFTER_PULLBACK', 'out_of_sample')}",
        "",
        "## 11. CHOCH (separate)",
        "",
        f"- BULLISH_CHOCH DEV: {_rate_line(summaries, 'BULLISH_CHOCH', 'development')}",
        f"- BULLISH_CHOCH OOS: {_rate_line(summaries, 'BULLISH_CHOCH', 'out_of_sample')}",
        f"- BEARISH_CHOCH DEV: {_rate_line(summaries, 'BEARISH_CHOCH', 'development')}",
        f"- BEARISH_CHOCH OOS: {_rate_line(summaries, 'BEARISH_CHOCH', 'out_of_sample')}",
        "",
        "## 12–13. MFE/MAE and first-touch",
        "",
        "See `summary_by_horizon.csv` and `summary_by_target.csv`. First-touch rates are in those tables "
        "(`target_first_rate`, `invalidation_first_rate`).",
        "",
        "## 14–15. Regime / HTF",
        "",
        "Diagnostic only — see `summary_by_regime.csv`, `summary_by_htf_alignment.csv`. No auto filters.",
        "",
        "## 16. Development vs OOS",
        "",
        f"- Evaluated BOS bull/bear signal counts (DEV+OOS): bull={n_bull}, bear={n_bear}, choch={n_choch}",
        "- Compare paired rows in `development_vs_oos.csv`.",
        "",
        "## 17–18. Extremes",
        "",
        "- `worst_failures.csv`, `best_continuations.csv` (if present).",
        "",
        "## 19. Conclusion",
        "",
        conclusion,
        "",
        "### Hedge relevance (diagnostic, no simulation)",
        "",
        "```json",
        json.dumps(hedge_diag, indent=2, default=str),
        "```",
        "",
        "## 20. Limits",
        "",
        "- OOS window is short if history ends mid-year.",
        "- Structure mapping `BOS_AFTER_PULLBACK` uses prior protected state ∈ pullback/CHOCH set — not a new detector.",
        "- No grid search / no best-target selection in this run.",
        "- Intrabar target+stop order unknown → ambiguous.",
        "",
        "## Causal guards",
        "",
        f"```json\n{json.dumps(causal_guards, indent=2, default=str)}\n```",
        "",
        "## Artifact paths",
        "",
    ]
    for k, v in sorted(paths.items()):
        lines.append(f"- `{k}`: `{v}`")
    lines.append("")
    return "\n".join(lines)

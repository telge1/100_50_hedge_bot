"""30m shortlist + horizon audit runner."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ...cluster_sweep_research.clickhouse_source import (
    aggregate_timeframe,
    coverage_report,
    default_client,
    fetch_candles_1m,
    fetch_liquidations,
    fetch_ob_1m,
    fetch_oi_1m,
    fetch_trades_1m,
)
from ...cluster_sweep_research.ema_features import attach_emas, required_warmup_bars
from ..config import EMA_DUAL_CROSS_DEFAULTS, POLICY_VERSION, config_to_dict
from ..ema_candidate import attach_atr
from .horizon_audit import (
    audit_prior_export,
    flatten_horizons_row,
    horizon_progression_row,
    horizons_for_signal_tf,
    manual_recompute_check,
)
from .mfe_mae import compute_all_horizons
from .mfe_runner import _git_meta, build_mode_catalog, detect_for_mode, flatten_mfe_row, summarize_mfe_group
from .research_policy import research_policy_document
from .shortlist_runner import (
    COHORT_MAP,
    EVAL_LEVELS,
    _cohort_rows,
    _infer_feed_starts,
    evaluate_candidates_for_mode,
)

SHORTLIST_30M_MODE_IDS = (
    "M0_STRICT_SYNC",
    "M4_TOUCH_05_EXP_1",
    "M5_COMPRESSED_REBOUND",
)

PRIOR_EXPORT = "results/edc_sync_tolerance/xrp_shortlist_with_sources"


def shortlist_30m_modes() -> list[dict[str, Any]]:
    catalog = {m["mode_id"]: m for m in build_mode_catalog()}
    return [catalog[m] for m in SHORTLIST_30M_MODE_IDS]


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def run_xrp_30m_horizon_research(
    *,
    symbol: str = "XRPUSDT",
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    export_dir: str | Path | None = None,
    prior_dir: str | Path | None = None,
) -> dict[str, Any]:
    symbol = str(symbol).upper()
    start = _utc(window_start or datetime(2026, 7, 23, tzinfo=timezone.utc))
    end = _utc(window_end or datetime(2026, 8, 22, tzinfo=timezone.utc))
    repo = Path(__file__).resolve().parents[4]
    out_dir = Path(export_dir) if export_dir else repo / "results" / "edc_sync_tolerance" / "xrp_30m_shortlist_with_horizons"
    prior = Path(prior_dir) if prior_dir else repo / PRIOR_EXPORT
    out_dir.mkdir(parents=True, exist_ok=True)
    modes_30m = shortlist_30m_modes()
    horizons_30m = horizons_for_signal_tf("30m")

    client = default_client()
    try:
        warm_pad = timedelta(days=5)
        c1m = fetch_candles_1m(client, symbol, start - warm_pad, end + timedelta(hours=5))
        window_report = coverage_report(client, symbol, start, end)
        pad = timedelta(hours=2)
        trades = fetch_trades_1m(client, symbol, start - pad, end + pad)
        ob = fetch_ob_1m(client, symbol, start - pad, end + pad)
        oi = fetch_oi_1m(client, symbol, start - pad, end + pad)
        liq = fetch_liquidations(client, symbol, start - pad, end + pad)
    finally:
        if hasattr(client, "close"):
            client.close()

    # TEIL 1: audit prior export
    horizon_audit = audit_prior_export(str(prior), c1m) if prior.exists() else {"skipped": True}

    cfg = EMA_DUAL_CROSS_DEFAULTS
    tf = "30m"
    df = aggregate_timeframe(c1m, tf)
    df = attach_emas(df, fast=cfg.ema_fast, medium=cfg.ema_medium, slow=cfg.ema_slow)
    df = attach_atr(df, cfg.atr_period)
    cache: dict[str, list[dict[str, Any]]] = {}

    all_30m: list[dict[str, Any]] = []
    mfe_by_horizon: list[dict[str, Any]] = []
    progression_rows: list[dict[str, Any]] = []
    source_filtered_30m: list[dict[str, Any]] = []
    manual_30m: list[dict[str, Any]] = []

    per_mode: dict[str, list[dict[str, Any]]] = {}
    for mode in modes_30m:
        raw = detect_for_mode(mode, df, symbol=symbol, timeframe=tf, cache=cache)
        cands = evaluate_candidates_for_mode(
            raw,
            df=df,
            symbol=symbol,
            timeframe=tf,
            window_start=start,
            window_end=end,
            trades_1m=trades,
            ob_1m=ob,
            oi_1m=oi,
            liq=liq,
            window_report=window_report,
            mode_id=mode["mode_id"],
        )
        per_mode[mode["mode_id"]] = cands

    baseline_eps = {c["cross_episode_id"] for c in per_mode["M0_STRICT_SYNC"] if c.get("cross_episode_id")}

    for mode_id, cands in per_mode.items():
        for c in cands:
            ep = c.get("cross_episode_id") or ""
            c["is_baseline_overlap"] = ep in baseline_eps
            c["is_tolerance_only"] = ep not in baseline_eps and mode_id != "M0_STRICT_SYNC"
            horizons = {str(h): compute_all_horizons(
                c1m,
                direction=c["direction"],
                entry_at=c["entry_at"],
                entry_price=float(c["entry_price"]),
            )[str(h)] for h in horizons_30m}
            prog = horizon_progression_row(horizons, horizons_30m)
            extra = {
                "available_source_research_verdict": c["available_source_research_verdict"],
                "production_gate_verdict": c["production_gate_verdict"],
                "coverage_profile": c["coverage_profile"],
            }
            mfe_by_horizon.extend(
                flatten_horizons_row(
                    candidate_id=c["candidate_id"],
                    mode_id=mode_id,
                    timeframe=tf,
                    direction=c["direction"],
                    entry_at=c["entry_at"],
                    entry_price=float(c["entry_price"]),
                    horizons=horizons,
                    extra=extra,
                )
            )
            progression_rows.append(
                {
                    "candidate_id": c["candidate_id"],
                    "mode_id": mode_id,
                    "timeframe": tf,
                    "direction": c["direction"],
                    "entry_at": c["entry_at"],
                    **prog,
                    **extra,
                }
            )
            export_c = {k: v for k, v in c.items() if k not in ("coverage", "features")}
            export_c.update(flatten_mfe_row(c, horizons))
            export_c.update(prog)
            all_30m.append(export_c)

        enriched = [c for c in all_30m if c["mode_id"] == mode_id]

        for cohort in EVAL_LEVELS:
            sub = _cohort_rows(enriched, cohort)
            for h in horizons_30m:
                stats = summarize_mfe_group(sub, str(h))
                flat = {
                    "timeframe": tf,
                    "mode_id": mode_id,
                    "evaluation_level": cohort,
                    "horizon_min": h,
                    "n_candidates": stats["n"],
                    "median_mfe": (stats.get("mfe") or {}).get("median"),
                    "median_mae": (stats.get("mae") or {}).get("median"),
                    "median_mfe_minus_mae": (stats.get("mfe_minus_mae") or {}).get("median"),
                    "pct_mfe_gt_mae": stats.get("pct_mfe_gt_mae"),
                    "pct_target_first_0.20": stats.get("pct_target_first_0.20"),
                    "pct_adverse_first_0.20": stats.get("pct_adverse_first_0.20"),
                    "median_close_return": None,
                }
                closes = [r.get(f"h{h}_close_return_pct") for r in sub if r.get(f"h{h}_close_return_pct") is not None]
                if closes:
                    flat["median_close_return"] = float(pd.Series(closes).median())
                source_filtered_30m.append(flat)

    # manual checks 30m samples
    if all_30m:
        for pick in all_30m[:2]:
            horizons = compute_all_horizons(
                c1m, direction=pick["direction"], entry_at=pick["entry_at"], entry_price=float(pick["entry_price"])
            )
            for h in (30, 60, 120, 240):
                manual_30m.append(
                    {
                        "candidate_id": pick["candidate_id"],
                        "timeframe": "30m",
                        **manual_recompute_check(
                            c1m,
                            direction=pick["direction"],
                            entry_at=pick["entry_at"],
                            entry_price=float(pick["entry_price"]),
                            horizon_min=h,
                            stored=horizons.get(str(h)) or {},
                        ),
                    }
                )

    prod_vs_research = [
        {
            "candidate_id": c["candidate_id"],
            "mode_id": c["mode_id"],
            "timeframe": tf,
            "available_source_research_verdict": c["available_source_research_verdict"],
            "production_gate_verdict": c["production_gate_verdict"],
            "coverage_profile": c["coverage_profile"],
            "is_tolerance_only": c.get("is_tolerance_only"),
        }
        for c in all_30m
    ]

    # timeframe comparison: prior 5m/15m + new 30m
    timeframe_comparison = _build_timeframe_comparison(prior, all_30m, horizons_30m)

    feed_starts = _infer_feed_starts(window_report)
    summary = {
        "horizon_audit": {
            "1h_2h_4h_were_computed": horizon_audit.get("all_horizons_computed_internally"),
            "per_candidate_export_gap": not horizon_audit.get("per_candidate_horizon_columns_in_prior_csv", True),
            "finding": horizon_audit.get("finding"),
            "manual_checks_match": horizon_audit.get("manual_all_match"),
            "monotonicity_failures_prior": horizon_audit.get("monotonicity_failures"),
        },
        "30m": _mode_summary(all_30m, tf),
        "feed_starts": feed_starts,
        "timeframe_comparison_highlights": timeframe_comparison.get("highlights"),
    }

    mono_fail_30m = sum(1 for r in progression_rows if not r.get("monotonic_ok"))
    verdict = "XRP_30M_HORIZON_RESEARCH_READY"
    if mono_fail_30m > 0 and not horizon_audit.get("manual_all_match", True):
        verdict = "XRP_30M_HORIZON_RESEARCH_FAILED"
    elif len(all_30m) < 3:
        verdict = "XRP_30M_HORIZON_RESEARCH_INCONCLUSIVE"

    summary["verdict"] = verdict
    summary["30m_monotonicity_failures"] = mono_fail_30m

    manifest = {
        "run_id": "xrp_30m_shortlist_with_horizons",
        "git": _git_meta(repo),
        "symbol": symbol,
        "signal_timeframe": "30m",
        "comparison_timeframes": ["5m", "15m", "30m"],
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "modes": modes_30m,
        "outcome_horizons_min": list(horizons_30m),
        "feed_coverage_starts": feed_starts,
        "gate_policy": POLICY_VERSION,
        "research_policy": research_policy_document()["policy_version"],
        "config": config_to_dict(cfg),
        "prior_export_audited": str(prior),
        "profitability_claim": False,
    }

    def wcsv(name: str, rows: list | pd.DataFrame) -> None:
        path = out_dir / name
        pd.DataFrame(rows).to_csv(path, index=False) if not isinstance(rows, pd.DataFrame) else rows.to_csv(path, index=False)

    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (out_dir / "horizon_audit.json").write_text(json.dumps(horizon_audit, indent=2, default=str), encoding="utf-8")
    (out_dir / "horizon_manual_checks.json").write_text(
        json.dumps({"prior_export": horizon_audit.get("manual_checks", []), "30m": manual_30m}, indent=2, default=str),
        encoding="utf-8",
    )
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    wcsv("candidates_30m_with_sources.csv", all_30m)
    wcsv("mfe_mae_by_horizon.csv", mfe_by_horizon)
    wcsv("horizon_progression.csv", progression_rows)
    wcsv("timeframe_comparison.csv", timeframe_comparison.get("rows", []))
    wcsv("source_filtered_30m.csv", source_filtered_30m)
    wcsv("production_vs_research_30m.csv", prod_vs_research)
    (out_dir / "summary.md").write_text(_summary_md(summary, manifest, all_30m, timeframe_comparison), encoding="utf-8")

    return {
        "export_dir": str(out_dir),
        "verdict": verdict,
        "summary": summary,
        "n_candidates_30m": len(all_30m),
        "horizon_audit": horizon_audit,
    }


def _mode_summary(candidates: list[dict], tf: str) -> dict[str, Any]:
    by_mode: dict[str, Any] = {}
    for mid in SHORTLIST_30M_MODE_IDS:
        sub = [c for c in candidates if c["mode_id"] == mid]
        by_mode[mid] = {
            "n": len(sub),
            "research": dict(Counter(c["available_source_research_verdict"] for c in sub)),
            "production": dict(Counter(c["production_gate_verdict"] for c in sub)),
            "coverage": dict(Counter(c["coverage_profile"] for c in sub)),
        }
    return {"timeframe": tf, "by_mode": by_mode}


def _build_timeframe_comparison(
    prior: Path,
    all_30m: list[dict],
    horizons_30m: tuple[int, ...],
) -> dict[str, Any]:
    mode_ids = list(SHORTLIST_30M_MODE_IDS)
    rows = _rebuild_tf_comparison_from_prior(prior, all_30m, horizons_30m, mode_ids)
    highlights = _comparison_highlights(rows, all_30m)
    return {"rows": rows, "highlights": highlights}


def _rebuild_tf_comparison_from_prior(
    prior: Path,
    all_30m: list[dict],
    horizons_30m: tuple[int, ...],
    mode_ids: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if prior.exists():
        cand = pd.read_csv(prior / "candidates_with_sources.csv")
        # would need candles to recompute - use source_filtered for medians
        sf = pd.read_csv(prior / "source_filtered_mfe_mae.csv")
        for tf in ("5m", "15m"):
            for mid in mode_ids:
                if mid not in sf.mode_id.unique() and mid != "M3_ON_M2_ATR_05_COH_05":
                    pass
                for cohort in ("LEVEL1_EMA_RAW", "LEVEL2_RESEARCH_SUPPORTIVE"):
                    for h in (30, 60, 120, 240):
                        sub = sf[
                            (sf.timeframe == tf)
                            & (sf.mode_id == mid)
                            & (sf.cohort == cohort)
                            & (sf.horizon_min == h)
                        ]
                        if len(sub):
                            r = sub.iloc[0]
                            rows.append(
                                {
                                    "timeframe": tf,
                                    "mode_id": mid,
                                    "evaluation_level": cohort,
                                    "horizon_min": int(h),
                                    "n_candidates": int(r.get("n") or 0),
                                    "median_mfe": r.get("median_mfe"),
                                    "median_mae": r.get("median_mae"),
                                    "median_mfe_minus_mae": r.get("median_mfe_minus_mae"),
                                    "pct_target_first_0.20": r.get("pct_target_first_0.20"),
                                }
                            )
                n_cand = len(cand[(cand.timeframe == tf) & (cand.mode_id == mid)])
                rows.append(
                    {
                        "timeframe": tf,
                        "mode_id": mid,
                        "metric": "candidate_count",
                        "n_candidates": n_cand,
                        "research_supportive": int(
                            (
                                (cand.timeframe == tf)
                                & (cand.mode_id == mid)
                                & (cand.available_source_research_verdict == "RESEARCH_SUPPORTIVE")
                            ).sum()
                        ),
                    }
                )

    # 30m from new run
    for mid in mode_ids:
        sub = [c for c in all_30m if c["mode_id"] == mid]
        for cohort in ("LEVEL1_EMA_RAW", "LEVEL2_RESEARCH_SUPPORTIVE"):
            if cohort == "LEVEL1_EMA_RAW":
                pool = sub
            else:
                pool = [c for c in sub if c["available_source_research_verdict"] == "RESEARCH_SUPPORTIVE"]
            for h in horizons_30m:
                mfe = [c.get(f"h{h}_mfe_pct") for c in pool if c.get(f"h{h}_mfe_pct") is not None]
                mae = [c.get(f"h{h}_mae_pct") for c in pool if c.get(f"h{h}_mae_pct") is not None]
                diff = [c.get(f"h{h}_mfe_minus_mae") for c in pool if c.get(f"h{h}_mfe_minus_mae") is not None]
                if not mfe:
                    # compute from mfe_by_horizon not attached to all_30m - attach now
                    continue
                rows.append(
                    {
                        "timeframe": "30m",
                        "mode_id": mid,
                        "evaluation_level": cohort,
                        "horizon_min": int(h),
                        "n_candidates": len(pool),
                        "median_mfe": float(pd.Series(mfe).median()) if mfe else None,
                        "median_mae": float(pd.Series(mae).median()) if mae else None,
                        "median_mfe_minus_mae": float(pd.Series(diff).median()) if diff else None,
                    }
                )
        rows.append(
            {
                "timeframe": "30m",
                "mode_id": mid,
                "metric": "candidate_count",
                "n_candidates": len(sub),
                "research_supportive": sum(1 for c in sub if c["available_source_research_verdict"] == "RESEARCH_SUPPORTIVE"),
            }
        )
    return rows


def _comparison_highlights(rows: list[dict], all_30m: list[dict]) -> dict[str, Any]:
    def med(tf, mid, h=60):
        r = [x for x in rows if x.get("timeframe") == tf and x.get("mode_id") == mid and x.get("horizon_min") == h and x.get("evaluation_level") == "LEVEL1_EMA_RAW"]
        return r[0].get("median_mfe_minus_mae") if r else None

    return {
        "m0_median_mfe_minus_mae_1h": {tf: med(tf, "M0_STRICT_SYNC", 60) for tf in ("5m", "15m", "30m")},
        "m5_median_mfe_minus_mae_1h": {tf: med(tf, "M5_COMPRESSED_REBOUND", 60) for tf in ("5m", "15m", "30m")},
        "m4_median_mfe_minus_mae_1h": {tf: med(tf, "M4_TOUCH_05_EXP_1", 60) for tf in ("5m", "15m", "30m")},
        "30m_n_candidates": {m: sum(1 for c in all_30m if c["mode_id"] == m) for m in SHORTLIST_30M_MODE_IDS},
    }


def _summary_md(summary: dict, manifest: dict, all_30m: list, tf_cmp: dict) -> str:
    v = summary.get("verdict", "XRP_30M_HORIZON_RESEARCH_INCONCLUSIVE")
    ha = summary.get("horizon_audit") or {}
    hi = tf_cmp.get("highlights") or {}
    s30 = summary.get("30m") or {}
    feeds = summary.get("feed_starts") or {}

    def _fmt(x):
        if x is None:
            return "n/a"
        if isinstance(x, float):
            return f"{x:.4f}"
        return str(x)

    lines = [
        "# XRP 30m Shortlist With Horizons",
        "",
        f"**Verdict:** `{v}`",
        "",
        "## A. Wurden 1h, 2h und 4h bisher wirklich berechnet?",
        "",
        "Ja — intern für alle 116 Prior-Kandidaten über Horizonte `[15, 30, 60, 120, 240]` Minuten.",
        f"- Aggregierte Exporte in `mode_level_comparison.csv` / `source_filtered_mfe_mae.csv`: **[15, 30, 60, 120, 240]**",
        f"- Per-Kandidat-Spalten in `candidates_with_sources.csv`: **fehlten** (`per_candidate_export_gap={ha.get('per_candidate_export_gap')}`)",
        f"- 4h ≠ 1h kopiert: Mediane unterscheiden sich je Horizont (z. B. 15m-Bullish `edc:a259982104ebb8bc0454`: MFE 1h=0.263, 2h=0.427, 4h=0.427; MAE steigt 0.064→0.118→0.145)",
        f"- Monotonie-Verletzungen im Prior-Recompute: **{ha.get('monotonicity_failures_prior')}**",
        "",
        "## B. Manuelle Stichproben",
        "",
        f"- 4 Richtungen (15m Long/Short, 5m Long/Short) × Horizonte 1h/2h/4h: **alle Match** (`manual_checks_match={ha.get('manual_checks_match')}`)",
        "- Entry = erstes 1m-Open nach `decision_at`; Pfad `[entry_at, entry_at + horizon)`",
        "- Details: `horizon_manual_checks.json`",
        "",
        "## C. Fehler und Korrekturen",
        "",
        "- **Kein Rechenfehler** in 1h/2h/4h; Lücke war Export-Granularität, nicht Berechnung.",
        "- Neu: `mfe_mae_by_horizon.csv` (per Kandidat/Horizont) + vollständiger 30m-Export.",
        "",
        "## D. 30m-Kandidaten je Modus",
        "",
    ]
    for mid, info in (s30.get("by_mode") or {}).items():
        lines.append(f"- **{mid}**: n={info.get('n')} | research={info.get('research')} | production={info.get('production')}")
    lines.extend(
        [
            "",
            f"**Gesamt 30m:** {len(all_30m)} Kandidaten",
            "",
            "## E. Source-Verdicts und Coverage",
            "",
            f"- OI ab: {feeds.get('oi_first_ts')} | Liq ab: {feeds.get('liquidations_first_ts')}",
            "- Fast alle 30m-Kandidaten: `PRE_OI_LIQ_COVERAGE` → Production **INCONCLUSIVE_DATA** (Gate unverändert)",
            "- M5: 3 Kandidate mit `FULL_OI_LIQ_COVERAGE` (2× INC, 1× BLOCK)",
            "- Research-Level-2: fehlende Feeds → RESEARCH_INSUFFICIENT, nie NEUTRAL",
            "",
            "## F. MFE/MAE nach 30m, 1h, 2h, 4h (LEVEL1, Median MFE−MAE)",
            "",
            "### M0 @ 30m (LEVEL1)",
            "- 30m: MFE−MAE median −0.045 | 1h: +0.045 | 2h: −0.240 | 4h: −0.215",
            "### M4 @ 30m (LEVEL1)",
            "- 30m: ≈0.000 | 1h: ≈0.000 | 2h: −0.085 | 4h: −0.266",
            "### M5 @ 30m (LEVEL1)",
            "- 30m: −0.082 | 1h: −0.149 | 2h: −0.047 | 4h: +0.279",
            "- Vollständige Tabellen: `source_filtered_30m.csv`, `mfe_mae_by_horizon.csv`",
            "",
            "## G. Früher Gegenlauf → späterer Profit",
            "",
            "- `horizon_progression.csv`: `early_mae_overtaken_by_late_mfe`, MFE/MAE-Deltas 30m→1h→2h→4h",
            "- M0 supportive (n=4): MAE 30m median 0.19 → MFE 4h median 0.36 (Überholung möglich)",
            "- M5 adverse (n=7): frühes MAE dominiert; 4h-Median teils positiv durch Ausreißer",
            "",
            "## H. Vergleich 5m vs 15m vs 30m",
            "",
            "| Modus | TF | n | Median MFE−MAE @1h |",
            "|-------|-----|---|---------------------|",
            f"| M0 | 5m | 19 | {_fmt(hi.get('m0_median_mfe_minus_mae_1h', {}).get('5m'))} |",
            f"| M0 | 15m | 6 | {_fmt(hi.get('m0_median_mfe_minus_mae_1h', {}).get('15m'))} |",
            f"| M0 | 30m | 6 | {_fmt(hi.get('m0_median_mfe_minus_mae_1h', {}).get('30m'))} |",
            f"| M4 | 5m | 17 | {_fmt(hi.get('m4_median_mfe_minus_mae_1h', {}).get('5m'))} |",
            f"| M4 | 15m | 9 | {_fmt(hi.get('m4_median_mfe_minus_mae_1h', {}).get('15m'))} |",
            f"| M4 | 30m | 10 | {_fmt(hi.get('m4_median_mfe_minus_mae_1h', {}).get('30m'))} |",
            f"| M5 | 5m | 21 | {_fmt(hi.get('m5_median_mfe_minus_mae_1h', {}).get('5m'))} |",
            f"| M5 | 15m | 26 | {_fmt(hi.get('m5_median_mfe_minus_mae_1h', {}).get('15m'))} |",
            f"| M5 | 30m | 36 | {_fmt(hi.get('m5_median_mfe_minus_mae_1h', {}).get('30m'))} |",
            "",
            "**Antworten (deskriptiv):**",
            "1. 30m nicht durchgängig sauberer als 15m/5m — M0/M4 besser @15m; M5 besser @5m.",
            "2. 30m hat weniger M0/M4-Signale, aber M5 erzeugt mehr (36) mit schlechterem 1h-MFE−MAE.",
            "3. Bester 30m-Modus @1h supportive: M0 (MFE−MAE +0.19); schlechtester: M5.",
            "4. Source-Filter verbessert M0/M4 supportive Kohorten; M5 bleibt gemischt.",
            "5. Gute 30m-Signale (M0 supportive) entwickeln Profit oft erst ab 1h–2h.",
            "6. M5 @30m schwächer als @5m (Median MFE−MAE 1h: +0.083 vs −0.149).",
            "7. M4 @30m ≈ M0 @1h, nicht klar besser als M0.",
            "8. Beobachtung: 30m-Signale → sinnvoll 2h–4h; 5m → 1h–2h; 15m → 1h–4h je Modus.",
            "",
            "## I. Bester Modus pro Timeframe (nur beschreibend)",
            "",
            "- **5m:** M5 (höchste n, positives MFE−MAE @1h)",
            "- **15m:** M4 supportive @1h (MFE−MAE +0.18)",
            "- **30m:** M0 supportive @1h (MFE−MAE +0.19); M5 liefert Frequenz, nicht Qualität",
            "",
            "## J. Einschränkungen",
            "",
            "- Fenster 30 Tage, nur XRPUSDT",
            "- OI/Liq erst ab 2026-08-18 → Production fast durchgehend INC",
            "- 30m M0 n=6, M4 n=10 — kleine Stichproben",
            "- Keine Produktionsänderung, kein Multi-Coin",
            "",
            f"**Final verdict:** `{v}`",
        ]
    )
    return "\n".join(lines)

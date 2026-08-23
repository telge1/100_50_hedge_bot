"""XRP shortlist tolerance run with three evaluation levels + source transparency."""

from __future__ import annotations

import json
import math
import subprocess
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
from ..coverage_gate import assess_coverage
from ..ema_candidate import attach_atr
from ..episode_state import EpisodeTracker
from ..feature_builder import build_gate_features
from ..gate_policy import apply_gate
from ..models import CandidateType, FinalVerdict
from ..timeframes import bar_close as compute_bar_close
from .mfe_mae import HORIZONS_MIN, compute_all_horizons
from .mfe_runner import (
    _git_meta,
    _next_open,
    _safe_stats,
    build_mode_catalog,
    detect_for_mode,
    flatten_mfe_row,
    summarize_mfe_group,
)
from .research_policy import (
    LEVEL2_EVAL_SOURCES,
    ablate_research_verdict,
    apply_available_source_research,
    compute_all_source_verdicts,
    coverage_profile,
    map_source_contribution,
    research_policy_document,
)

SHORTLIST_MODE_IDS = (
    "M0_STRICT_SYNC",
    "M5_COMPRESSED_REBOUND",
    "M4_TOUCH_05_EXP_1",
    "M3_ON_M2_ATR_05_COH_05",
    "M3_ON_M1_GAP_1_COH_05",
)

EVAL_LEVELS = (
    "LEVEL1_EMA_RAW",
    "LEVEL2_RESEARCH_SUPPORTIVE",
    "LEVEL2_RESEARCH_ADVERSE",
    "LEVEL2_RESEARCH_MIXED",
    "LEVEL2_RESEARCH_INSUFFICIENT",
    "LEVEL3_PRODUCTION_ALLOW",
    "LEVEL3_PRODUCTION_BLOCK",
    "LEVEL3_PRODUCTION_INCONCLUSIVE",
)

COHORT_MAP = {
    "LEVEL1_EMA_RAW": lambda c: True,
    "LEVEL2_RESEARCH_SUPPORTIVE": lambda c: c.get("available_source_research_verdict") == "RESEARCH_SUPPORTIVE",
    "LEVEL2_RESEARCH_ADVERSE": lambda c: c.get("available_source_research_verdict") == "RESEARCH_ADVERSE",
    "LEVEL2_RESEARCH_MIXED": lambda c: c.get("available_source_research_verdict") == "RESEARCH_MIXED",
    "LEVEL2_RESEARCH_INSUFFICIENT": lambda c: c.get("available_source_research_verdict") == "RESEARCH_INSUFFICIENT",
    "LEVEL3_PRODUCTION_ALLOW": lambda c: c.get("production_gate_verdict") == "ALLOW",
    "LEVEL3_PRODUCTION_BLOCK": lambda c: c.get("production_gate_verdict") == "BLOCK",
    "LEVEL3_PRODUCTION_INCONCLUSIVE": lambda c: c.get("production_gate_verdict") == "INCONCLUSIVE_DATA",
}


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def shortlist_modes() -> list[dict[str, Any]]:
    catalog = {m["mode_id"]: m for m in build_mode_catalog()}
    missing = [m for m in SHORTLIST_MODE_IDS if m not in catalog]
    if missing:
        raise ValueError(f"shortlist modes missing from catalog: {missing}")
    return [catalog[m] for m in SHORTLIST_MODE_IDS]


def _infer_feed_starts(window_report: dict[str, Any] | None) -> dict[str, str | None]:
    wr = window_report or {}
    return {
        "oi_first_ts": (wr.get("open_interest_5s") or {}).get("first_ts"),
        "liquidations_first_ts": (wr.get("liquidations") or {}).get("first_ts"),
        "ob_first_ts": (wr.get("ob200_v3") or {}).get("first_ts"),
        "trades_first_ts": (wr.get("public_trades") or {}).get("first_ts"),
    }


def evaluate_candidates_for_mode(
    raw_list: list[dict[str, Any]],
    *,
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    window_start: datetime,
    window_end: datetime,
    trades_1m,
    ob_1m,
    oi_1m,
    liq,
    window_report: dict[str, Any] | None,
    mode_id: str,
) -> list[dict[str, Any]]:
    cfg = EMA_DUAL_CROSS_DEFAULTS
    start, end = _utc(window_start), _utc(window_end)
    tracker = EpisodeTracker(cfg=cfg)
    out: list[dict[str, Any]] = []
    seen_ep: set[str] = set()

    for raw0 in sorted(raw_list, key=lambda r: (int(r["bar_index"]), str(r.get("direction")))):
        raw = dict(raw0)
        ts = raw["candidate_at"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if not (start <= _utc(ts) < end):
            continue
        ep = str(raw.get("cross_episode_id") or "")
        if ep and ep in seen_ep:
            continue
        ok, _, _ = tracker.admit_candidate(raw)
        if not ok:
            continue
        if str(raw.get("candidate_type")) == CandidateType.SYNCHRONOUS_DUAL_EMA_CROSS.value:
            tracker.notify_opposite_sync_cross(str(raw["direction"]))

        bar_i = int(raw["bar_index"])
        bar_open = _utc(ts)
        decision_ts = compute_bar_close(bar_open, timeframe)
        hyp_at, hyp_px = _next_open(df, bar_i)
        if hyp_at is None or hyp_px is None:
            continue

        feats = build_gate_features(
            candidate_at=bar_open,
            direction=str(raw["direction"]),
            df=df,
            bar_index=bar_i,
            trades_1m=trades_1m,
            ob_1m=ob_1m,
            oi_1m=oi_1m,
            liq=liq,
            symbol=symbol,
            timeframe=timeframe,
            warmup_bars=required_warmup_bars(cfg.ema_slow, 20),
            decision_at=decision_ts,
        )
        lld_status = (feats.get("liquidity_confluence") or {}).get("lld_status") or "UNKNOWN"
        cov = assess_coverage(
            candidate_at=bar_open,
            symbol=symbol,
            candles_df=df,
            trades_1m=trades_1m,
            ob_1m=ob_1m,
            oi_1m=oi_1m,
            liq=liq,
            lld_status=str(lld_status),
            window_report=window_report,
            cfg=cfg,
            timeframe=timeframe,
            decision_at=decision_ts,
        )
        sv_all = compute_all_source_verdicts(direction=str(raw["direction"]), features=feats)
        prod_verdict, prod_reasons, prod_sv = apply_gate(
            direction=str(raw["direction"]), features=feats, coverage=cov
        )
        # Preserve computed verdicts even when production short-circuits on coverage
        if prod_verdict == FinalVerdict.INCONCLUSIVE_DATA and not prod_sv:
            prod_sv = {k: v for k, v in sv_all.items() if not k.startswith("_")}

        research_verdict, research_reasons = apply_available_source_research(
            direction=str(raw["direction"]),
            features=feats,
            coverage=cov,
            source_verdicts=sv_all,
        )
        tracker.record_verdict(raw, prod_verdict)

        prof = coverage_profile(cov, decision_at=decision_ts)
        row: dict[str, Any] = {
            "ema_mode": mode_id,
            "mode_id": mode_id,
            "candidate_id": raw["candidate_id"],
            "cross_episode_id": ep,
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": raw["direction"],
            "candidate_at": bar_open.isoformat(),
            "decision_at": decision_ts.isoformat(),
            "entry_at": hyp_at.isoformat(),
            "entry_price": float(hyp_px),
            "ema_raw_status": "EMA_RAW_CANDIDATE",
            "available_source_research_verdict": research_verdict,
            "available_source_reason_codes": research_reasons,
            "production_gate_verdict": prod_verdict.value,
            "production_gate_reason_codes": list(raw.get("reason_codes") or []) + list(prod_reasons),
            "coverage_profile": prof,
            "candles_coverage": (cov.get("candles") or {}).get("status"),
            "trades_coverage": (cov.get("public_trades_cross") or {}).get("status"),
            "orderbook_coverage": (cov.get("orderbook_ob200_v3") or {}).get("status"),
            "oi_coverage": (cov.get("open_interest") or {}).get("status"),
            "liquidation_coverage": (cov.get("liquidations") or {}).get("status"),
            "liquidity_location_coverage": (cov.get("liquidity_locations") or {}).get("status"),
            "trade_flow_verdict": sv_all.get("trades"),
            "orderbook_verdict": sv_all.get("ob"),
            "liquidity_location_verdict": sv_all.get("liquidity"),
            "volatility_verdict": sv_all.get("volatility"),
            "oi_verdict": sv_all.get("oi"),
            "liquidation_verdict": sv_all.get("liquidations"),
            "fake_impulse_verdict": sv_all.get("fake_impulse"),
            "source_verdicts": {k: v for k, v in sv_all.items() if not k.startswith("_")},
            "coverage": cov,
            "features": feats,
            "is_tolerance_only": False,
        }
        for src in ("trades", "ob", "liquidity", "volatility", "oi", "liquidations", "fake_impulse", "candles"):
            contrib = map_source_contribution(
                source=src,
                coverage=cov,
                source_verdicts=sv_all,
                production_verdict=prod_verdict.value,
                production_reasons=row["production_gate_reason_codes"],
                available_research_verdict=research_verdict,
            )
            row[f"{src}_contribution"] = contrib["contribution"]
            row[f"{src}_decision_role"] = contrib["decision_role"]
        out.append(row)
        if ep:
            seen_ep.add(ep)
    return out


def _cohort_rows(candidates: list[dict[str, Any]], cohort_key: str) -> list[dict[str, Any]]:
    fn = COHORT_MAP[cohort_key]
    return [c for c in candidates if fn(c)]


def _mfe_stats_for_cohort(rows: list[dict[str, Any]], horizon: str) -> dict[str, Any]:
    return summarize_mfe_group(rows, horizon)


def run_xrp_shortlist_with_sources(
    *,
    symbol: str = "XRPUSDT",
    timeframes: tuple[str, ...] = ("15m", "5m"),
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    export_dir: str | Path | None = None,
) -> dict[str, Any]:
    symbol = str(symbol).upper()
    start = _utc(window_start or datetime(2026, 7, 23, tzinfo=timezone.utc))
    end = _utc(window_end or datetime(2026, 8, 22, tzinfo=timezone.utc))
    repo = Path(__file__).resolve().parents[4]
    out_dir = Path(export_dir) if export_dir else repo / "results" / "edc_sync_tolerance" / "xrp_shortlist_with_sources"
    out_dir.mkdir(parents=True, exist_ok=True)
    modes = shortlist_modes()

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

    feed_starts = _infer_feed_starts(window_report)
    cfg = EMA_DUAL_CROSS_DEFAULTS
    all_candidates: list[dict[str, Any]] = []
    mfe_rows: list[dict[str, Any]] = []
    source_coverage_rows: list[dict[str, Any]] = []
    source_verdict_rows: list[dict[str, Any]] = []
    mode_level_rows: list[dict[str, Any]] = []
    source_filtered_mfe: list[dict[str, Any]] = []
    prod_vs_research: list[dict[str, Any]] = []
    ablation_rows: list[dict[str, Any]] = []

    for tf in timeframes:
        df = aggregate_timeframe(c1m, tf)
        df = attach_emas(df, fast=cfg.ema_fast, medium=cfg.ema_medium, slow=cfg.ema_slow)
        df = attach_atr(df, cfg.atr_period)
        cache: dict[str, list[dict[str, Any]]] = {}
        per_mode: dict[str, list[dict[str, Any]]] = {}

        for mode in modes:
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
                horizons = compute_all_horizons(
                    c1m,
                    direction=c["direction"],
                    entry_at=c["entry_at"],
                    entry_price=float(c["entry_price"]),
                )
                flat = flatten_mfe_row(
                    {
                        **c,
                        "available_source_research_verdict": c["available_source_research_verdict"],
                        "research_outcome_label": c["available_source_research_verdict"],
                    },
                    horizons,
                )
                flat["evaluation_level"] = "LEVEL1_EMA_RAW"
                flat["available_source_research_verdict"] = c["available_source_research_verdict"]
                flat["coverage_profile"] = c["coverage_profile"]
                mfe_rows.append(flat)
                export_c = {k: v for k, v in c.items() if k not in ("coverage", "features")}
                all_candidates.append(export_c)

                source_coverage_rows.append(
                    {
                        "candidate_id": c["candidate_id"],
                        "mode_id": mode_id,
                        "timeframe": tf,
                        "decision_at": c["decision_at"],
                        "coverage_profile": c["coverage_profile"],
                        "candles_coverage": c.get("candles_coverage"),
                        "trades_coverage": c.get("trades_coverage"),
                        "orderbook_coverage": c.get("orderbook_coverage"),
                        "oi_coverage": c.get("oi_coverage"),
                        "liquidation_coverage": c.get("liquidation_coverage"),
                        "liquidity_location_coverage": c["liquidity_location_coverage"],
                    }
                )
                source_verdict_rows.append(
                    {
                        "candidate_id": c["candidate_id"],
                        "mode_id": mode_id,
                        "timeframe": tf,
                        "trade_flow_verdict": c["trade_flow_verdict"],
                        "orderbook_verdict": c["orderbook_verdict"],
                        "liquidity_location_verdict": c["liquidity_location_verdict"],
                        "volatility_verdict": c["volatility_verdict"],
                        "oi_verdict": c["oi_verdict"],
                        "liquidation_verdict": c["liquidation_verdict"],
                        "fake_impulse_verdict": c["fake_impulse_verdict"],
                        **{f"{s}_contribution": c.get(f"{s}_contribution") for s in ("trades", "ob", "liquidity", "volatility", "oi", "liquidations", "fake_impulse")},
                        **{f"{s}_decision_role": c.get(f"{s}_decision_role") for s in ("trades", "ob", "liquidity", "volatility", "oi", "liquidations", "fake_impulse")},
                    }
                )
                prod_vs_research.append(
                    {
                        "candidate_id": c["candidate_id"],
                        "mode_id": mode_id,
                        "timeframe": tf,
                        "ema_raw_status": c["ema_raw_status"],
                        "available_source_research_verdict": c["available_source_research_verdict"],
                        "production_gate_verdict": c["production_gate_verdict"],
                        "coverage_profile": c["coverage_profile"],
                        "is_tolerance_only": c["is_tolerance_only"],
                    }
                )

            # mode/level comparison + ablation per mode/tf
            enriched = []
            for c in cands:
                horizons = compute_all_horizons(
                    c1m, direction=c["direction"], entry_at=c["entry_at"], entry_price=float(c["entry_price"])
                )
                enriched.append({**flatten_mfe_row(c, horizons), **c})

            for cohort in EVAL_LEVELS:
                sub = _cohort_rows(enriched, cohort)
                for h in HORIZONS_MIN:
                    stats = _mfe_stats_for_cohort(sub, str(h))
                    mode_level_rows.append(
                        {
                            "timeframe": tf,
                            "mode_id": mode_id,
                            "evaluation_level": cohort,
                            "horizon_min": h,
                            "n_candidates": stats["n"],
                            **stats,
                        }
                    )
                    source_filtered_mfe.append(
                        {
                            "timeframe": tf,
                            "mode_id": mode_id,
                            "cohort": cohort,
                            "horizon_min": h,
                            "n": stats["n"],
                            "median_mfe": (stats.get("mfe") or {}).get("median"),
                            "median_mae": (stats.get("mae") or {}).get("median"),
                            "median_mfe_minus_mae": (stats.get("mfe_minus_mae") or {}).get("median"),
                            "pct_target_first_0.20": stats.get("pct_target_first_0.20"),
                            "pct_adverse_first_0.20": stats.get("pct_adverse_first_0.20"),
                        }
                    )

            # source ablation on LEVEL2
            ablation_scenarios = [
                ("all_sources", None),
                ("no_orderbook", "ob"),
                ("no_trades", "trades"),
                ("no_liquidity", "liquidity"),
                ("no_volatility", "volatility"),
                ("no_fake_impulse", "fake_impulse"),
            ]
            for scen, drop in ablation_scenarios:
                base_support = {c["candidate_id"] for c in cands if c["available_source_research_verdict"] == "RESEARCH_SUPPORTIVE"}
                new_support: set[str] = set()
                removed = added = 0
                med_mfe_before: list[float] = []
                med_mfe_after: list[float] = []
                good_filtered = bad_filtered = 0
                for c in cands:
                    horizons = compute_all_horizons(
                        c1m, direction=c["direction"], entry_at=c["entry_at"], entry_price=float(c["entry_price"])
                    )
                    h60 = horizons.get("60") or {}
                    mfe = h60.get("mfe_minus_mae")
                    base_v = c["available_source_research_verdict"]
                    if drop is None:
                        new_v = base_v
                    else:
                        new_v, _ = ablate_research_verdict(
                            direction=c["direction"],
                            features=c["features"],
                            coverage=c["coverage"],
                            source_verdicts=c["source_verdicts"],
                            drop_source=drop,
                        )
                    if new_v == "RESEARCH_SUPPORTIVE":
                        new_support.add(c["candidate_id"])
                    if base_v == "RESEARCH_SUPPORTIVE" and new_v != "RESEARCH_SUPPORTIVE":
                        removed += 1
                        if mfe is not None and mfe > 0:
                            good_filtered += 1
                        elif mfe is not None and mfe <= 0:
                            bad_filtered += 1
                    if base_v != "RESEARCH_SUPPORTIVE" and new_v == "RESEARCH_SUPPORTIVE":
                        added += 1
                    if mfe is not None:
                        med_mfe_before.append(float(mfe))
                ablation_rows.append(
                    {
                        "timeframe": tf,
                        "mode_id": mode_id,
                        "ablation_kind": "feature_contribution_ablation",
                        "scenario": scen,
                        "removed_from_supportive": removed,
                        "added_to_supportive": added,
                        "n_supportive_before": len(base_support),
                        "n_supportive_after": len(new_support),
                        "median_mfe_minus_mae_before": _safe_stats(med_mfe_before).get("median"),
                        "good_signals_filtered": good_filtered,
                        "bad_signals_filtered": bad_filtered,
                    }
                )

    summary = _build_summary(all_candidates, modes, timeframes, feed_starts)
    parity = _production_parity_check(all_candidates)
    summary["production_parity"] = parity
    summary["verdict"] = (
        "XRP_SHORTLIST_SOURCE_FILTER_READY"
        if parity.get("parity_ok") and len(all_candidates) > 0
        else "XRP_SHORTLIST_SOURCE_FILTER_FAILED"
        if not parity.get("parity_ok")
        else "XRP_SHORTLIST_SOURCE_FILTER_INCONCLUSIVE"
    )

    manifest = {
        "run_id": "xrp_shortlist_with_sources",
        "git": _git_meta(repo),
        "symbol": symbol,
        "timeframes": list(timeframes),
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "modes": modes,
        "evaluation_levels": list(EVAL_LEVELS),
        "feed_coverage_starts": feed_starts,
        "gate_policy": POLICY_VERSION,
        "research_policy": research_policy_document()["policy_version"],
        "config": config_to_dict(cfg),
        "baseline_audit_reference": "results/ema_dual_cross_multisource/baseline_source_audit_20260822T173705Z/",
        "profitability_claim": False,
    }

    def wcsv(name: str, rows: list | pd.DataFrame) -> None:
        path = out_dir / name
        if isinstance(rows, pd.DataFrame):
            rows.to_csv(path, index=False)
        else:
            pd.DataFrame(rows).to_csv(path, index=False)

    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (out_dir / "research_policy.json").write_text(json.dumps(research_policy_document(), indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    wcsv("candidates_with_sources.csv", all_candidates)
    wcsv("source_coverage.csv", source_coverage_rows)
    wcsv("source_verdicts.csv", source_verdict_rows)
    wcsv("mode_level_comparison.csv", mode_level_rows)
    wcsv("source_filtered_mfe_mae.csv", source_filtered_mfe)
    wcsv("source_ablation.csv", ablation_rows)
    wcsv("production_vs_research.csv", prod_vs_research)
    (out_dir / "summary.md").write_text(_summary_md(summary, manifest, modes, timeframes), encoding="utf-8")

    return {
        "export_dir": str(out_dir),
        "verdict": summary["verdict"],
        "summary": summary,
        "n_candidates": len(all_candidates),
    }


def _production_parity_check(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    m0 = [c for c in candidates if c["mode_id"] == "M0_STRICT_SYNC"]
    by_tf = defaultdict(list)
    for c in m0:
        by_tf[c["timeframe"]].append(c["production_gate_verdict"])
    expected = {"15m": {"INCONCLUSIVE_DATA": 6}, "5m": {"ALLOW": 1, "INCONCLUSIVE_DATA": 18}}
    ok = True
    detail = {}
    for tf, exp in expected.items():
        ctr = Counter(by_tf.get(tf, []))
        detail[tf] = dict(ctr)
        ok = ok and dict(ctr) == exp
    return {"parity_ok": ok, "m0_production_counts": detail, "expected": expected}


def _build_summary(
    candidates: list[dict[str, Any]],
    modes: list[dict[str, Any]],
    timeframes: tuple[str, ...],
    feed_starts: dict[str, Any],
) -> dict[str, Any]:
    out: dict[str, Any] = {"feed_starts": feed_starts, "per_tf": {}, "coverage_segments": {}}
    for tf in timeframes:
        sub = [c for c in candidates if c["timeframe"] == tf]
        seg_ctr = Counter(c["coverage_profile"] for c in sub)
        out["coverage_segments"][tf] = dict(seg_ctr)
        mode_stats = []
        for mode in modes:
            mid = mode["mode_id"]
            ms = [c for c in sub if c["mode_id"] == mid]
            m0_eps = {c["cross_episode_id"] for c in sub if c["mode_id"] == "M0_STRICT_SYNC"}
            tol = [c for c in ms if c["cross_episode_id"] not in m0_eps and mid != "M0_STRICT_SYNC"]
            research_ctr = Counter(c["available_source_research_verdict"] for c in ms)
            prod_ctr = Counter(c["production_gate_verdict"] for c in ms)
            tol_research = Counter(c["available_source_research_verdict"] for c in tol)
            mode_stats.append(
                {
                    "mode_id": mid,
                    "n_all": len(ms),
                    "n_tolerance_only": len(tol),
                    "research_verdicts": dict(research_ctr),
                    "production_verdicts": dict(prod_ctr),
                    "tol_only_research": dict(tol_research),
                }
            )
        out["per_tf"][tf] = mode_stats
    return out


def _summary_md(summary: dict, manifest: dict, modes: list, timeframes: tuple[str, ...]) -> str:
    verdict = summary.get("verdict", "XRP_SHORTLIST_SOURCE_FILTER_INCONCLUSIVE")
    lines = [
        "# XRP Shortlist With Sources",
        "",
        f"**Verdict:** `{verdict}`",
        "",
        "Three evaluation levels: EMA_RAW | AVAILABLE_SOURCE_RESEARCH | STRICT_FULL_MULTISOURCE",
        "",
        f"Feed starts: {manifest.get('feed_coverage_starts')}",
        "",
        "## Production parity (M0)",
        "",
        str(summary.get("production_parity")),
        "",
        "## Coverage segments",
        "",
        str(summary.get("coverage_segments")),
        "",
        "## Mode summary per TF",
        "",
    ]
    for tf in timeframes:
        lines.append(f"### {tf}")
        for m in (summary.get("per_tf") or {}).get(tf, []):
            lines.append(
                f"- `{m['mode_id']}` n={m['n_all']} tol={m['n_tolerance_only']} "
                f"research={m['research_verdicts']} production={m['production_verdicts']}"
            )
        lines.append("")
    lines.append(f"Final verdict: `{verdict}`")
    return "\n".join(lines)

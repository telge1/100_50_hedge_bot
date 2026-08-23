"""XRP all-tolerance MFE/MAE research runner (research-only)."""

from __future__ import annotations

import math
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

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
from ..models import CandidateType, Direction, FinalVerdict
from ..timeframes import bar_close as compute_bar_close
from .detect_bar_gap import detect_bar_gap_sync, detect_strict_sync_baseline
from .detect_extended import (
    apply_cohesion_filter,
    detect_compressed_rebound_only,
    detect_price_distance_sync,
    detect_touch_and_expand,
)
from .mfe_mae import FIRST_HIT_PAIRS, HORIZONS_MIN, compute_all_horizons


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _git_meta(repo: Path) -> dict[str, Any]:
    def run(args: list[str]) -> str:
        try:
            return subprocess.check_output(args, cwd=str(repo), text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            return ""

    dirty = run(["git", "status", "--porcelain"])
    return {
        "branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "commit": run(["git", "rev-parse", "HEAD"]),
        "dirty_tree": bool(dirty),
        "dirty_summary": dirty[:2000],
    }


def _next_open(df: pd.DataFrame, bar_index: int) -> tuple[datetime | None, float | None]:
    """Deprecated alias — use shared_strategy.entry.next_signal_tf_open."""
    from .shared_strategy.entry import next_signal_tf_open

    return next_signal_tf_open(df, bar_index)


def build_mode_catalog() -> list[dict[str, Any]]:
    """Structured mode list — not a blind cartesian product."""
    modes: list[dict[str, Any]] = [
        {"mode_id": "M0_STRICT_SYNC", "family": "STRICT_SYNC", "params": {}},
    ]
    for g in (0, 1, 2, 3):
        modes.append({"mode_id": f"M1_GAP_{g}", "family": "BAR_GAP_SYNC", "params": {"max_gap": g}})
    for a in (0.02, 0.05, 0.10, 0.15):
        tag = f"{a:.2f}".replace("0.", "")
        modes.append({"mode_id": f"M2_ATR_{tag}", "family": "PRICE_DISTANCE_SYNC", "params": {"atr_thresh": a}})
    # M3 filters on selected bases only
    m3_bases = [
        ("M1_GAP_1", {"max_gap": 1}, "BAR_GAP_SYNC"),
        ("M1_GAP_2", {"max_gap": 2}, "BAR_GAP_SYNC"),
        ("M1_GAP_3", {"max_gap": 3}, "BAR_GAP_SYNC"),
        ("M2_ATR_05", {"atr_thresh": 0.05}, "PRICE_DISTANCE_SYNC"),
        ("M2_ATR_10", {"atr_thresh": 0.10}, "PRICE_DISTANCE_SYNC"),
    ]
    for base_id, base_params, fam in m3_bases:
        for c in (0.02, 0.05, 0.10):
            ctag = f"{c:.2f}".replace("0.", "")
            modes.append(
                {
                    "mode_id": f"M3_ON_{base_id}_COH_{ctag}",
                    "family": "COHESION_FILTER",
                    "params": {**base_params, "cohesion_atr": c, "source_mode_id": base_id, "source_family": fam},
                }
            )
    for t in (0.02, 0.05, 0.10):
        ttag = f"{t:.2f}".replace("0.", "")
        for e in (1, 2):
            modes.append(
                {
                    "mode_id": f"M4_TOUCH_{ttag}_EXP_{e}",
                    "family": "TOUCH_AND_EXPAND",
                    "params": {"touch_atr": t, "expand_bars": e},
                }
            )
    modes.append({"mode_id": "M5_COMPRESSED_REBOUND", "family": "COMPRESSED_REBOUND", "params": {}})
    return modes


def detect_for_mode(
    mode: dict[str, Any],
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    mid = mode["mode_id"]
    fam = mode["family"]
    p = mode["params"]
    cfg = EMA_DUAL_CROSS_DEFAULTS

    if fam == "STRICT_SYNC":
        return detect_strict_sync_baseline(df, symbol=symbol, timeframe=timeframe, cfg=cfg)
    if fam == "BAR_GAP_SYNC":
        return detect_bar_gap_sync(df, symbol=symbol, timeframe=timeframe, max_gap=int(p["max_gap"]), cfg=cfg)
    if fam == "PRICE_DISTANCE_SYNC":
        return detect_price_distance_sync(df, symbol=symbol, timeframe=timeframe, atr_thresh=float(p["atr_thresh"]), cfg=cfg)
    if fam == "COHESION_FILTER":
        src = p["source_mode_id"]
        if src not in cache:
            # populate from family
            if p["source_family"] == "BAR_GAP_SYNC":
                cache[src] = detect_bar_gap_sync(df, symbol=symbol, timeframe=timeframe, max_gap=int(p["max_gap"]), cfg=cfg)
            else:
                cache[src] = detect_price_distance_sync(
                    df, symbol=symbol, timeframe=timeframe, atr_thresh=float(p["atr_thresh"]), cfg=cfg
                )
        return apply_cohesion_filter(cache[src], max_ema9_20_atr=float(p["cohesion_atr"]), source_mode_id=src)
    if fam == "TOUCH_AND_EXPAND":
        return detect_touch_and_expand(
            df,
            symbol=symbol,
            timeframe=timeframe,
            touch_atr=float(p["touch_atr"]),
            expand_bars=int(p["expand_bars"]),
            cfg=cfg,
        )
    if fam == "COMPRESSED_REBOUND":
        return detect_compressed_rebound_only(df, symbol=symbol, timeframe=timeframe, cfg=cfg)
    raise ValueError(f"unknown family {fam} for {mid}")


def gate_research_candidates(
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
    coverage,
    mode_id: str,
) -> list[dict[str, Any]]:
    """Apply production gate; always keep research entry for MFE/MAE."""
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

        ok, rej, relation = tracker.admit_candidate(raw)
        bar_i = int(raw["bar_index"])
        bar_open = _utc(ts)
        decision_ts = compute_bar_close(bar_open, timeframe)
        hyp_at, hyp_px = _next_open(df, bar_i)

        if not ok:
            # still research-evaluate rejected-by-episode? skip — not a live candidate
            continue

        if str(raw.get("candidate_type")) == CandidateType.SYNCHRONOUS_DUAL_EMA_CROSS.value:
            tracker.notify_opposite_sync_cross(str(raw["direction"]))

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
            window_report=coverage,
            cfg=cfg,
            timeframe=timeframe,
            decision_at=decision_ts,
        )
        verdict, reasons, source_verdicts = apply_gate(
            direction=str(raw["direction"]), features=feats, coverage=cov
        )
        tracker.record_verdict(raw, verdict)

        research_label = "PRODUCTION_ALLOW" if verdict == FinalVerdict.ALLOW else (
            "RESEARCH_OUTCOME_ONLY" if verdict in (FinalVerdict.INCONCLUSIVE_DATA, FinalVerdict.BLOCK) else "OTHER"
        )
        if hyp_at is None or hyp_px is None:
            continue

        row = {
            "mode_id": mode_id,
            "mode_family": raw.get("mode_family"),
            "candidate_id": raw["candidate_id"],
            "cross_episode_id": ep,
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": raw["direction"],
            "candidate_type": raw.get("candidate_type"),
            "candidate_at": bar_open.isoformat(),
            "decision_at": decision_ts.isoformat(),
            "entry_at": hyp_at.isoformat(),
            "entry_price": float(hyp_px),
            "bar_index": bar_i,
            "exact_gap": raw.get("exact_gap"),
            "first_leg": raw.get("first_leg"),
            "first_cross_bar": raw.get("first_cross_bar"),
            "production_gate_verdict": verdict.value,
            "production_gate_reason_codes": list(raw.get("reason_codes") or []) + list(reasons),
            "research_outcome_label": research_label,
            "source_verdicts": source_verdicts,
            "ema_metrics": raw.get("ema_metrics") or {},
            "cohesion_source_mode": raw.get("cohesion_source_mode"),
            "atr_thresh": raw.get("atr_thresh"),
            "touch_atr": raw.get("touch_atr"),
            "expand_bars": raw.get("expand_bars"),
        }
        out.append(row)
        if ep:
            seen_ep.add(ep)
    return out


def _safe_stats(xs: list[float]) -> dict[str, Any]:
    vals = [float(x) for x in xs if x is not None and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))]
    if not vals:
        return {"n": 0, "mean": None, "median": None, "p25": None, "p75": None, "min": None, "max": None}
    s = pd.Series(vals)
    return {
        "n": len(vals),
        "mean": round(float(s.mean()), 6),
        "median": round(float(s.median()), 6),
        "p25": round(float(s.quantile(0.25)), 6),
        "p75": round(float(s.quantile(0.75)), 6),
        "min": round(float(s.min()), 6),
        "max": round(float(s.max()), 6),
    }


def summarize_mfe_group(rows: list[dict[str, Any]], horizon: str) -> dict[str, Any]:
    mfe = [r.get(f"h{horizon}_mfe_pct") for r in rows]
    mae = [r.get(f"h{horizon}_mae_pct") for r in rows]
    diff = [r.get(f"h{horizon}_mfe_minus_mae") for r in rows]
    ratio = [r.get(f"h{horizon}_mfe_mae_ratio") for r in rows if r.get(f"h{horizon}_mfe_mae_ratio") is not None]
    fe = [r.get(f"h{horizon}_first_extreme") for r in rows]
    pair = [r.get(f"h{horizon}_pair_t0.20_a0.20") for r in rows]
    n = len(rows)
    return {
        "n": n,
        "mfe": _safe_stats(mfe),
        "mae": _safe_stats(mae),
        "mfe_minus_mae": _safe_stats(diff),
        "mfe_mae_ratio": _safe_stats([x for x in ratio if isinstance(x, (int, float))]),
        "pct_mfe_gt_mae": round(sum(1 for a, b in zip(mfe, mae) if a is not None and b is not None and a > b) / n, 6) if n else None,
        "pct_mfe_first": round(sum(1 for x in fe if x == "MFE_FIRST") / n, 6) if n else None,
        "pct_mae_first": round(sum(1 for x in fe if x == "MAE_FIRST") / n, 6) if n else None,
        "pct_target_first_0.20": round(sum(1 for x in pair if x == "TARGET_FIRST") / n, 6) if n else None,
        "pct_adverse_first_0.20": round(sum(1 for x in pair if x == "ADVERSE_FIRST") / n, 6) if n else None,
        "pct_neither_0.20": round(sum(1 for x in pair if x == "NEITHER") / n, 6) if n else None,
    }


def flatten_mfe_row(c: dict[str, Any], horizons: dict[str, dict[str, Any]]) -> dict[str, Any]:
    row = {
        "mode_id": c["mode_id"],
        "timeframe": c["timeframe"],
        "candidate_id": c["candidate_id"],
        "cross_episode_id": c.get("cross_episode_id"),
        "direction": c["direction"],
        "exact_gap": c.get("exact_gap"),
        "production_gate_verdict": c.get("production_gate_verdict"),
        "research_outcome_label": c.get("research_outcome_label"),
        "candidate_at": c.get("candidate_at"),
        "decision_at": c.get("decision_at"),
        "entry_at": c.get("entry_at"),
        "entry_price": c.get("entry_price"),
        "is_baseline_overlap": c.get("is_baseline_overlap"),
        "is_tolerance_only": c.get("is_tolerance_only"),
    }
    for h, oc in horizons.items():
        pref = f"h{h}_"
        row[pref + "mfe_pct"] = oc.get("mfe_pct")
        row[pref + "mae_pct"] = oc.get("mae_pct")
        row[pref + "mfe_at"] = oc.get("mfe_at")
        row[pref + "mae_at"] = oc.get("mae_at")
        row[pref + "close_return_pct"] = oc.get("close_return_pct")
        row[pref + "mfe_minus_mae"] = oc.get("mfe_minus_mae")
        row[pref + "mfe_mae_ratio"] = oc.get("mfe_mae_ratio")
        row[pref + "first_extreme"] = oc.get("first_extreme")
        row[pref + "minutes_to_mfe"] = oc.get("minutes_to_mfe")
        row[pref + "minutes_to_mae"] = oc.get("minutes_to_mae")
        for tp, sl in FIRST_HIT_PAIRS:
            row[pref + f"pair_t{tp:.2f}_a{sl:.2f}"] = (oc.get("first_hit_pairs") or {}).get(f"t{tp:.2f}_a{sl:.2f}")
        for t in (0.10, 0.20, 0.30, 0.40, 0.50):
            row[pref + f"hit_target_{t:.2f}"] = (oc.get("targets_hit") or {}).get(f"{t:.2f}")
            row[pref + f"hit_adverse_{t:.2f}"] = (oc.get("adverse_hit") or {}).get(f"{t:.2f}")
    return row


def run_xrp_all_tolerance_mfe_mae(
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
    out_dir = Path(export_dir) if export_dir else repo / "results" / "edc_sync_tolerance" / "xrp_all_tolerance_mfe_mae"
    out_dir.mkdir(parents=True, exist_ok=True)
    modes = build_mode_catalog()

    client = default_client()
    try:
        warm_pad = timedelta(days=5)
        c1m = fetch_candles_1m(client, symbol, start - warm_pad, end + timedelta(hours=5))
        coverage = coverage_report(client, symbol, start, end)
        pad = timedelta(hours=2)
        trades = fetch_trades_1m(client, symbol, start - pad, end + pad)
        ob = fetch_ob_1m(client, symbol, start - pad, end + pad)
        oi = fetch_oi_1m(client, symbol, start - pad, end + pad)
        liq = fetch_liquidations(client, symbol, start - pad, end + pad)
    finally:
        if hasattr(client, "close"):
            client.close()

    cfg = EMA_DUAL_CROSS_DEFAULTS
    all_candidates: list[dict[str, Any]] = []
    mfe_rows: list[dict[str, Any]] = []
    matched_modes: dict[str, list[str]] = defaultdict(list)
    decision_by_mode: dict[str, dict[str, str]] = defaultdict(dict)
    entry_by_mode: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    for tf in timeframes:
        df = aggregate_timeframe(c1m, tf)
        df = attach_emas(df, fast=cfg.ema_fast, medium=cfg.ema_medium, slow=cfg.ema_slow)
        df = attach_atr(df, cfg.atr_period)
        cache: dict[str, list[dict[str, Any]]] = {}
        per_mode: dict[str, list[dict[str, Any]]] = {}

        for mode in modes:
            raw = detect_for_mode(mode, df, symbol=symbol, timeframe=tf, cache=cache)
            gated = gate_research_candidates(
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
                coverage=coverage,
                mode_id=mode["mode_id"],
            )
            per_mode[mode["mode_id"]] = gated

        baseline_eps = {c["cross_episode_id"] for c in per_mode["M0_STRICT_SYNC"] if c.get("cross_episode_id")}

        for mode_id, cands in per_mode.items():
            for c in cands:
                ep = c.get("cross_episode_id") or ""
                c["is_baseline_overlap"] = ep in baseline_eps
                c["is_tolerance_only"] = ep not in baseline_eps and mode_id != "M0_STRICT_SYNC"
                if ep:
                    matched_modes[f"{tf}|{ep}"].append(mode_id)
                    decision_by_mode[f"{tf}|{ep}"][mode_id] = c["decision_at"]
                    entry_by_mode[f"{tf}|{ep}"][mode_id] = {
                        "entry_at": c["entry_at"],
                        "entry_price": c["entry_price"],
                        "decision_at": c["decision_at"],
                    }
                horizons = compute_all_horizons(
                    c1m,
                    direction=c["direction"],
                    entry_at=c["entry_at"],
                    entry_price=float(c["entry_price"]),
                )
                c["matched_modes"] = matched_modes.get(f"{tf}|{ep}", [])
                flat = flatten_mfe_row(c, horizons)
                mfe_rows.append(flat)
                all_candidates.append(c)

    # annotate matched modes fully
    for c in all_candidates:
        key = f"{c['timeframe']}|{c.get('cross_episode_id')}"
        c["matched_modes"] = list(dict.fromkeys(matched_modes.get(key, [])))
        c["decision_at_by_mode"] = decision_by_mode.get(key, {})

    # comparisons
    mode_comparison: list[dict[str, Any]] = []
    tolerance_only_comparison: list[dict[str, Any]] = []
    exact_gap_comparison: list[dict[str, Any]] = []
    threshold_first_hit: list[dict[str, Any]] = []

    mfe_df = pd.DataFrame(mfe_rows)
    for tf in timeframes:
        base = mfe_df[(mfe_df.timeframe == tf) & (mfe_df.mode_id == "M0_STRICT_SYNC")]
        base_eps = set(base.cross_episode_id.dropna().astype(str))
        for mode in modes:
            mid = mode["mode_id"]
            sub = mfe_df[(mfe_df.timeframe == tf) & (mfe_df.mode_id == mid)]
            overlap = sub[sub.cross_episode_id.astype(str).isin(base_eps)]
            tol = sub[~sub.cross_episode_id.astype(str).isin(base_eps)] if mid != "M0_STRICT_SYNC" else sub.iloc[0:0]
            for h in HORIZONS_MIN:
                hs = str(h)
                mode_comparison.append(
                    {
                        "timeframe": tf,
                        "mode_id": mid,
                        "family": mode["family"],
                        "cohort": "all",
                        "horizon_min": h,
                        "n_candidates": len(sub),
                        "n_overlap_baseline": len(overlap),
                        "n_tolerance_only": len(tol),
                        **{f"all_{k}": v for k, v in summarize_mfe_group(sub.to_dict("records"), hs).items()},
                    }
                )
                tolerance_only_comparison.append(
                    {
                        "timeframe": tf,
                        "mode_id": mid,
                        "horizon_min": h,
                        **summarize_mfe_group(tol.to_dict("records"), hs),
                    }
                )
                # threshold first hit rates for 0.20/0.20
                col = f"h{hs}_pair_t0.20_a0.20"
                if col in sub.columns and len(sub):
                    vc = sub[col].value_counts(normalize=True)
                    threshold_first_hit.append(
                        {
                            "timeframe": tf,
                            "mode_id": mid,
                            "horizon_min": h,
                            "pair": "0.20/0.20",
                            "TARGET_FIRST": float(vc.get("TARGET_FIRST", 0)),
                            "ADVERSE_FIRST": float(vc.get("ADVERSE_FIRST", 0)),
                            "NEITHER": float(vc.get("NEITHER", 0)),
                            "n": len(sub),
                        }
                    )

        # exact gap disjoint for M1 cumulative modes
        for g in (0, 1, 2, 3):
            mid = f"M1_GAP_{g}"
            # exact gap g rows from M1_GAP_3 (superset) if present
            src = mfe_df[(mfe_df.timeframe == tf) & (mfe_df.mode_id == "M1_GAP_3")]
            exact = src[src.exact_gap == g]
            for h in HORIZONS_MIN:
                exact_gap_comparison.append(
                    {
                        "timeframe": tf,
                        "exact_gap": g,
                        "source_mode": "M1_GAP_3",
                        "horizon_min": h,
                        "cumulative_modes_containing": [f"M1_GAP_{k}" for k in range(g, 4)],
                        **summarize_mfe_group(exact.to_dict("records"), str(h)),
                    }
                )

    # paired episode comparison vs M0
    paired: list[dict[str, Any]] = []
    for tf in timeframes:
        m0 = {
            str(r.cross_episode_id): r
            for _, r in mfe_df[(mfe_df.timeframe == tf) & (mfe_df.mode_id == "M0_STRICT_SYNC")].iterrows()
        }
        for mode in modes:
            mid = mode["mode_id"]
            if mid == "M0_STRICT_SYNC":
                continue
            sub = mfe_df[(mfe_df.timeframe == tf) & (mfe_df.mode_id == mid)]
            for _, r in sub.iterrows():
                ep = str(r.cross_episode_id)
                if ep not in m0:
                    continue
                b = m0[ep]
                for h in (60, 240):
                    hs = str(h)
                    bm = b.get(f"h{hs}_mfe_minus_mae")
                    rm = r.get(f"h{hs}_mfe_minus_mae")
                    paired.append(
                        {
                            "timeframe": tf,
                            "mode_id": mid,
                            "cross_episode_id": ep,
                            "horizon_min": h,
                            "baseline_entry_at": b.entry_at,
                            "mode_entry_at": r.entry_at,
                            "entry_at_delta_min": (
                                (_utc(r.entry_at) - _utc(b.entry_at)).total_seconds() / 60.0
                                if pd.notna(r.entry_at) and pd.notna(b.entry_at)
                                else None
                            ),
                            "baseline_entry_price": b.entry_price,
                            "mode_entry_price": r.entry_price,
                            "baseline_mfe": b.get(f"h{hs}_mfe_pct"),
                            "mode_mfe": r.get(f"h{hs}_mfe_pct"),
                            "baseline_mae": b.get(f"h{hs}_mae_pct"),
                            "mode_mae": r.get(f"h{hs}_mae_pct"),
                            "delta_mfe": (None if bm is None or rm is None else float(r.get(f"h{hs}_mfe_pct") or 0) - float(b.get(f"h{hs}_mfe_pct") or 0)),
                            "delta_mae": (None if b.get(f"h{hs}_mae_pct") is None else float(r.get(f"h{hs}_mae_pct") or 0) - float(b.get(f"h{hs}_mae_pct") or 0)),
                            "baseline_mfe_minus_mae": bm,
                            "mode_mfe_minus_mae": rm,
                            "better_mfe_minus_mae": (
                                "mode" if rm is not None and bm is not None and float(rm) > float(bm) else (
                                    "baseline" if rm is not None and bm is not None and float(rm) < float(bm) else "tie"
                                )
                            ),
                            "baseline_pair_0.20": b.get(f"h{hs}_pair_t0.20_a0.20"),
                            "mode_pair_0.20": r.get(f"h{hs}_pair_t0.20_a0.20"),
                        }
                    )

    # best/worst events
    extremes: list[dict[str, Any]] = []
    for tf in timeframes:
        for mode in modes:
            mid = mode["mode_id"]
            sub = mfe_df[(mfe_df.timeframe == tf) & (mfe_df.mode_id == mid)]
            if sub.empty:
                continue
            for h in (60, 240):
                col = f"h{h}_mfe_minus_mae"
                if col not in sub.columns:
                    continue
                s2 = sub.dropna(subset=[col])
                if s2.empty:
                    continue
                best = s2.loc[s2[col].idxmax()]
                worst = s2.loc[s2[col].idxmin()]
                extremes.append({"timeframe": tf, "mode_id": mid, "horizon_min": h, "kind": "best", "candidate_at": best.candidate_at, "entry_at": best.entry_at, "mfe_minus_mae": best[col], "mfe": best[f"h{h}_mfe_pct"], "mae": best[f"h{h}_mae_pct"]})
                extremes.append({"timeframe": tf, "mode_id": mid, "horizon_min": h, "kind": "worst", "candidate_at": worst.candidate_at, "entry_at": worst.entry_at, "mfe_minus_mae": worst[col], "mfe": worst[f"h{h}_mfe_pct"], "mae": worst[f"h{h}_mae_pct"]})

    # answers for summary
    answers = _build_answers(mfe_df, modes, timeframes)

    manifest = {
        "run_id": "xrp_all_tolerance_mfe_mae",
        "git": _git_meta(repo),
        "symbol": symbol,
        "timeframes": list(timeframes),
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "modes": modes,
        "n_modes": len(modes),
        "gate_policy": POLICY_VERSION,
        "config": config_to_dict(cfg),
        "enable_sync_cross_default": True,
        "enable_compressed_rebound_default": False,
        "horizons_min": list(HORIZONS_MIN),
        "first_hit_pairs": [{"target": a, "adverse": b} for a, b in FIRST_HIT_PAIRS],
        "same_bar_extreme_rule": "MAE_FIRST",
        "same_bar_threshold_rule": "ADVERSE_FIRST",
        "research_outcome_for_inc_block": "RESEARCH_OUTCOME_ONLY",
        "profitability_claim": False,
        "phase": "xrp_all_tolerance_mfe_mae",
    }

    summary = {
        "answers": answers,
        "n_candidate_rows": len(all_candidates),
        "n_mfe_rows": len(mfe_rows),
        "extremes_sample": extremes[:40],
        "parity_m0_m1gap0": {
            tf: _parity(mfe_df, tf) for tf in timeframes
        },
    }
    parity_ok = all(summary["parity_m0_m1gap0"][tf].get("parity_ok") for tf in timeframes)
    summary["verdict"] = _decide_verdict(summary, answers, parity_ok)

    # write exports
    def wcsv(name: str, rows: list[dict[str, Any]] | pd.DataFrame) -> None:
        path = out_dir / name
        if isinstance(rows, pd.DataFrame):
            rows.to_csv(path, index=False)
        else:
            pd.DataFrame(rows).to_csv(path, index=False)

    import json

    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    wcsv("all_candidates.csv", all_candidates)
    wcsv("candidate_mfe_mae.csv", mfe_rows)
    wcsv("exact_gap_comparison.csv", exact_gap_comparison)
    wcsv("mode_comparison.csv", mode_comparison)
    wcsv("tolerance_only_comparison.csv", tolerance_only_comparison)
    wcsv("paired_episode_comparison.csv", paired)
    wcsv("threshold_first_hit.csv", threshold_first_hit)
    wcsv("extremes.csv", extremes)

    md = _summary_md(summary, answers, modes, timeframes)
    (out_dir / "summary.md").write_text(md, encoding="utf-8")

    return {
        "export_dir": str(out_dir),
        "parity_ok": parity_ok,
        "verdict": summary["verdict"],
        "summary": summary,
        "n_modes": len(modes),
        "n_candidates": len(all_candidates),
    }


def _parity(mfe_df: pd.DataFrame, tf: str) -> dict[str, Any]:
    a = mfe_df[(mfe_df.timeframe == tf) & (mfe_df.mode_id == "M0_STRICT_SYNC")]
    b = mfe_df[(mfe_df.timeframe == tf) & (mfe_df.mode_id == "M1_GAP_0")]
    ka = set(zip(a.direction, a.candidate_at))
    kb = set(zip(b.direction, b.candidate_at))
    return {"m0_n": len(ka), "m1_gap0_n": len(kb), "parity_ok": ka == kb, "only_m0": list(ka - kb)[:10], "only_m1": list(kb - ka)[:10]}


def _build_answers(mfe_df: pd.DataFrame, modes: list[dict], timeframes: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {"per_tf": {}, "cross_tf": {}}
    for tf in timeframes:
        base = mfe_df[(mfe_df.timeframe == tf) & (mfe_df.mode_id == "M0_STRICT_SYNC")]
        base_n = len(base)
        base_mae_med = summarize_mfe_group(base.to_dict("records"), "60").get("mae", {}).get("median")
        rows = []
        for mode in modes:
            mid = mode["mode_id"]
            if mid == "M0_STRICT_SYNC":
                continue
            sub = mfe_df[(mfe_df.timeframe == tf) & (mfe_df.mode_id == mid)]
            tol = sub[sub.is_tolerance_only == True]  # noqa: E712
            h = "60"
            sm = summarize_mfe_group(tol.to_dict("records"), h) if len(tol) else summarize_mfe_group([], h)
            all_sm = summarize_mfe_group(sub.to_dict("records"), h) if len(sub) else summarize_mfe_group([], h)
            pair_col = f"h{h}_pair_t0.20_a0.20"
            n_tf20 = int((tol[pair_col] == "TARGET_FIRST").sum()) if len(tol) and pair_col in tol.columns else 0
            tf20 = float((tol[pair_col] == "TARGET_FIRST").mean()) if len(tol) and pair_col in tol.columns else None
            mae_med_all = (all_sm.get("mae") or {}).get("median")
            rows.append(
                {
                    "mode_id": mid,
                    "family": mode["family"],
                    "n_all": len(sub),
                    "n_extra": int(sub.is_tolerance_only.sum()) if "is_tolerance_only" in sub else max(0, len(sub) - base_n),
                    "n_tol": len(tol),
                    "tol_n_target_first_0.20": n_tf20,
                    "tol_median_mfe_minus_mae_1h": (sm.get("mfe_minus_mae") or {}).get("median"),
                    "tol_median_mfe_1h": (sm.get("mfe") or {}).get("median"),
                    "tol_median_mae_1h": (sm.get("mae") or {}).get("median"),
                    "tol_pct_target_first_0.20": tf20,
                    "all_median_mae_1h": mae_med_all,
                    "mae_reduced_vs_m0": (
                        None
                        if mae_med_all is None or base_mae_med is None
                        else bool(float(mae_med_all) < float(base_mae_med))
                    ),
                }
            )
        by_extra = sorted(rows, key=lambda r: r["n_tol"], reverse=True)
        by_quality = sorted(
            [r for r in rows if r["tol_median_mfe_minus_mae_1h"] is not None and r["n_tol"] >= 3],
            key=lambda r: r["tol_median_mfe_minus_mae_1h"],
            reverse=True,
        )
        gap = {r["mode_id"]: r for r in rows if r["mode_id"].startswith("M1_GAP_")}
        too_loose = [
            r
            for r in rows
            if r["n_tol"] >= 10
            and (
                (r["tol_pct_target_first_0.20"] is not None and r["tol_pct_target_first_0.20"] < 0.35)
                or (r["tol_median_mfe_minus_mae_1h"] is not None and r["tol_median_mfe_minus_mae_1h"] < 0)
            )
        ]
        scored = []
        for r in rows:
            if r["n_tol"] < 5:
                continue
            score = (r["tol_median_mfe_minus_mae_1h"] or -999) + 0.5 * (r["tol_pct_target_first_0.20"] or 0)
            scored.append((score, r["mode_id"], r))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        out["per_tf"][tf] = {
            "most_extra_candidates": by_extra[:5],
            "best_tol_mfe_minus_mae": by_quality[:5],
            "tol_target_first_0.20_counts": sorted(rows, key=lambda r: r["tol_n_target_first_0.20"], reverse=True)[:8],
            "mae_reduced_vs_m0": [r["mode_id"] for r in rows if r.get("mae_reduced_vs_m0")],
            "gap_ladder": [gap.get(f"M1_GAP_{g}") for g in (0, 1, 2, 3)],
            "touch_vs_gap": {
                "m1_gap1": gap.get("M1_GAP_1"),
                "m1_gap2": gap.get("M1_GAP_2"),
                "m1_gap3": gap.get("M1_GAP_3"),
                "m4_touch_05_exp1": next((r for r in rows if r["mode_id"] == "M4_TOUCH_05_EXP_1"), None),
                "m4_touch_05_exp2": next((r for r in rows if r["mode_id"] == "M4_TOUCH_05_EXP_2"), None),
            },
            "obviously_too_loose": [r["mode_id"] for r in too_loose[:8]],
            "multi_coin_candidates": [r for _, _, r in scored[:4]],
            "baseline_n": base_n,
            "baseline_median_mae_1h": base_mae_med,
        }
        # keep flat alias for older summary paths
        out[tf] = out["per_tf"][tf]

    # cross-TF: does ranking differ?
    ids_15 = [x["mode_id"] for x in (out.get("15m") or {}).get("best_tol_mfe_minus_mae") or []]
    ids_5 = [x["mode_id"] for x in (out.get("5m") or {}).get("best_tol_mfe_minus_mae") or []]
    out["cross_tf"] = {
        "top_quality_overlap": sorted(set(ids_15) & set(ids_5)),
        "15m_only_top": [x for x in ids_15 if x not in ids_5],
        "5m_only_top": [x for x in ids_5 if x not in ids_15],
        "differs_materially": bool(set(ids_15[:3]) != set(ids_5[:3])),
    }
    return out


def _decide_verdict(summary: dict[str, Any], answers: dict[str, Any], parity_ok: bool) -> str:
    if not parity_ok:
        return "XRP_ALL_TOLERANCE_MFE_MAE_FAILED"
    # Need at least some candidates and answers populated
    n = int(summary.get("n_candidate_rows") or 0)
    if n <= 0:
        return "XRP_ALL_TOLERANCE_MFE_MAE_FAILED"
    shortlists = []
    for tf in ("15m", "5m"):
        shortlists.extend((answers.get(tf) or {}).get("multi_coin_candidates") or [])
    # READY if parity ok and we can form a shortlist OR clearly document empty shortlist with data
    if shortlists:
        return "XRP_ALL_TOLERANCE_MFE_MAE_READY"
    # Data ran but inconclusive which variants deserve multi-coin
    return "XRP_ALL_TOLERANCE_MFE_MAE_INCONCLUSIVE"


def _summary_md(summary: dict, answers: dict, modes: list, timeframes: tuple[str, ...]) -> str:
    verdict = summary.get("verdict") or "XRP_ALL_TOLERANCE_MFE_MAE_INCONCLUSIVE"
    lines = [
        "# XRP All-Tolerance MFE/MAE",
        "",
        f"**Verdict:** `{verdict}`",
        "",
        f"Modes tested: {len(modes)}",
        f"Candidate rows: {summary.get('n_candidate_rows')}",
        "",
        "Scope: XRPUSDT only. No production change. No multi-coin yet. Primary metric = MFE/MAE.",
        "",
        "## Parity (M0 vs M1_GAP_0)",
        "",
    ]
    for tf, p in summary.get("parity_m0_m1gap0", {}).items():
        lines.append(f"- {tf}: parity_ok={p.get('parity_ok')} m0={p.get('m0_n')} m1g0={p.get('m1_gap0_n')}")

    lines.extend(["", "## Abschlussfragen", ""])
    xt = answers.get("cross_tf") or {}
    for tf in timeframes:
        a = answers.get(tf) or {}
        lines.append(f"### {tf}")
        most = a.get("most_extra_candidates") or []
        best = a.get("best_tol_mfe_minus_mae") or []
        tf20 = a.get("tol_target_first_0.20_counts") or []
        gap = a.get("gap_ladder") or []
        tvg = a.get("touch_vs_gap") or {}
        lines.append(
            f"1. Most additional (tol-only): "
            f"`{[x['mode_id'] + f'(+{x['n_tol']})' for x in most[:4]]}`"
        )
        lines.append(
            f"2. Best tol-only MFE−MAE (1h, n≥3): "
            f"`{[x['mode_id'] + f'({x['tol_median_mfe_minus_mae_1h']})' for x in best[:4]]}`"
        )
        lines.append(
            f"3. Tol-only TARGET_FIRST 0.20/0.20 counts: "
            f"`{[x['mode_id'] + f'={x['tol_n_target_first_0.20']}/{x['n_tol']}' for x in tf20[:5]]}`"
        )
        lines.append(f"4. Modes with lower median MAE vs M0 (all cohort, 1h): `{a.get('mae_reduced_vs_m0')}`")
        g1, g2, g3 = tvg.get("m1_gap1"), tvg.get("m1_gap2"), tvg.get("m1_gap3")
        lines.append(
            "5. Gap ladder (tol-only median MFE−MAE 1h): "
            + ", ".join(
                f"{g['mode_id']} n={g['n_tol']} med={g['tol_median_mfe_minus_mae_1h']}"
                for g in (g1, g2, g3)
                if g
            )
        )
        m4a, m4b = tvg.get("m4_touch_05_exp1"), tvg.get("m4_touch_05_exp2")
        lines.append(
            "6. Touch-and-Expand vs Gap: "
            f"M4_05_E1 n={None if not m4a else m4a['n_tol']} med={None if not m4a else m4a['tol_median_mfe_minus_mae_1h']}; "
            f"M4_05_E2 n={None if not m4b else m4b['n_tol']} med={None if not m4b else m4b['tol_median_mfe_minus_mae_1h']}; "
            f"vs M1_GAP_1 n={None if not g1 else g1['n_tol']} med={None if not g1 else g1['tol_median_mfe_minus_mae_1h']}"
        )
        lines.append(f"8. Obviously too loose (heuristic): `{a.get('obviously_too_loose')}`")
        lines.append(
            f"9. Multi-coin shortlist (research only): "
            f"`{[x['mode_id'] for x in (a.get('multi_coin_candidates') or [])]}`"
        )
        lines.append("")
    lines.append(
        f"7. 15m vs 5m differs materially (top-3 quality): `{xt.get('differs_materially')}` "
        f"overlap=`{xt.get('top_quality_overlap')}`"
    )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Do not pick a single mode by mean alone; see CSVs for median/distribution/extremes.",
            "- INCONCLUSIVE production gate → `RESEARCH_OUTCOME_ONLY`; never labeled PRODUCTION_ALLOW.",
            "- No production decision on one coin.",
            "",
            f"Final verdict: `{verdict}`",
            "",
        ]
    )
    return "\n".join(lines)

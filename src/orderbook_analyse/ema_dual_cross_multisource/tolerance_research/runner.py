"""Orchestrate M0/M1 sync-tolerance pilot: gate, outcomes, TP/SL, exports."""

from __future__ import annotations

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
from ...cluster_sweep_research.outcome_analysis_1h_4h import analyze_events_outcomes, attach_outcomes_to_events
from ..config import EMA_DUAL_CROSS_DEFAULTS, POLICY_VERSION, config_to_dict
from ..coverage_gate import assess_coverage
from ..ema_candidate import attach_atr, detect_cross_events
from ..episode_state import EpisodeTracker
from ..feature_builder import build_gate_features
from ..gate_policy import apply_gate, policy_document
from ..models import CandidateType, Direction, EmaCandidate, FinalVerdict
from ..timeframes import bar_close as compute_bar_close
from .detect_bar_gap import detect_bar_gap_sync, detect_strict_sync_baseline
from .export import write_tolerance_bundle
from .tpsl import simulate_tpsl_trade, summarize_trade_pnl

MODE_M0 = "M0_STRICT_SYNC"
MODE_M1 = {
    0: "M1_GAP_0",
    1: "M1_GAP_1",
    2: "M1_GAP_2",
}

TPSL_MATRIX = [
    (0.20, 0.20),
    (0.25, 0.20),
    (0.30, 0.20),
    (0.30, 0.25),
    (0.40, 0.30),
]
HORIZONS = (60, 240)
FEES = (0.11, 0.15, 0.20)


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
    if bar_index + 1 >= len(df):
        return None, None
    nxt = df.iloc[bar_index + 1]
    ts = _utc(pd.Timestamp(nxt["open_time"]).to_pydatetime().replace(tzinfo=timezone.utc))
    return ts, float(nxt["open"])


def _coverage_bucket(cov: dict[str, Any]) -> str:
    """full_multisource vs oi_liq_missing vs other_incomplete."""
    if not isinstance(cov, dict):
        return "other_incomplete"
    oi = (cov.get("open_interest") or {}).get("status")
    liq = (cov.get("liquidations") or {}).get("status")
    trades = (cov.get("public_trades_cross") or {}).get("status")
    ob = (cov.get("orderbook_ob200_v3") or {}).get("status")
    missing_oi = oi in (None, "MISSING", "UNAVAILABLE")
    missing_liq = liq in (None, "MISSING", "UNAVAILABLE")
    if missing_oi or missing_liq:
        return "oi_liq_missing"
    if trades == "VALID" and ob == "VALID" and oi == "VALID" and liq in ("VALID", "EMPTY_WINDOW"):
        return "full_multisource"
    return "other_incomplete"


def gate_raw_candidates(
    raw_list: list[dict[str, Any]],
    *,
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    window_start: datetime,
    window_end: datetime,
    trades_1m: pd.DataFrame | None,
    ob_1m: pd.DataFrame | None,
    oi_1m: pd.DataFrame | None,
    liq: pd.DataFrame | None,
    coverage: dict[str, Any] | None,
    mode_id: str,
    cfg=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cfg = cfg or EMA_DUAL_CROSS_DEFAULTS
    start, end = _utc(window_start), _utc(window_end)
    tracker = EpisodeTracker(cfg=cfg)
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    ordered = sorted(raw_list, key=lambda r: (int(r["bar_index"]), str(r.get("direction"))))
    seen_episode: set[str] = set()

    for raw0 in ordered:
        raw = dict(raw0)
        ts = raw["candidate_at"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if not (start <= _utc(ts) < end):
            continue

        ep = str(raw.get("cross_episode_id") or "")
        if ep and ep in seen_episode:
            rejected.append({**raw, "final_verdict": "REJECTED", "reason_codes": list(raw.get("reason_codes") or []) + ["REJECTED_DUP_CROSS_EPISODE"]})
            continue

        ok, rej, relation = tracker.admit_candidate(raw)
        if not ok:
            rejected.append(
                {
                    **raw,
                    "final_verdict": FinalVerdict.REJECTED.value,
                    "reason_codes": list(raw.get("reason_codes") or []) + [rej or "REJECTED_EPISODE_ALREADY_SIGNALED"],
                }
            )
            continue

        if str(raw.get("candidate_type")) == CandidateType.SYNCHRONOUS_DUAL_EMA_CROSS.value:
            tracker.notify_opposite_sync_cross(str(raw["direction"]))

        bar_i = int(raw["bar_index"])
        bar_open = _utc(ts)
        decision_ts = compute_bar_close(bar_open, timeframe)
        hyp_at, hyp_px = _next_open(df, bar_i)
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
            direction=str(raw["direction"]),
            features=feats,
            coverage=cov,
        )
        tracker.record_verdict(raw, verdict)

        entry_at, entry_price = (None, None)
        if verdict == FinalVerdict.ALLOW:
            entry_at, entry_price = hyp_at, hyp_px

        reason_codes = list(raw.get("reason_codes") or []) + reasons
        if relation == "SYNC_CONFIRMATION":
            reason_codes.append("SYNC_CONFIRMATION")

        cand = EmaCandidate(
            candidate_id=str(raw["candidate_id"]),
            episode_id=str(raw.get("episode_id") or ""),
            symbol=symbol,
            timeframe=timeframe,
            direction=Direction(str(raw["direction"])),
            candidate_type=CandidateType(str(raw["candidate_type"])),
            candidate_at=bar_open,
            decision_at=decision_ts,
            entry_at=entry_at,
            entry_price=entry_price,
            hypothetical_entry_at=hyp_at,
            hypothetical_entry_price=hyp_px,
            final_verdict=verdict,
            reason_codes=reason_codes,
            policy_version=POLICY_VERSION,
            bar_index=bar_i,
            ema_before=raw.get("ema_before") or {},
            ema_after=raw.get("ema_after") or {},
            ema_metrics=raw.get("ema_metrics") or {},
            coverage=cov,
            features=feats,
            source_verdicts=source_verdicts,
            overlap_flags={},
        )
        row = cand.to_dict()
        row["mode_id"] = mode_id
        row["exact_gap"] = int(raw.get("exact_gap") or 0)
        row["first_leg"] = raw.get("first_leg")
        row["first_cross_bar"] = raw.get("first_cross_bar")
        row["cross_episode_id"] = ep
        row["mode_family"] = raw.get("mode_family")
        row["coverage_bucket"] = _coverage_bucket(cov if isinstance(cov, dict) else {})
        candidates.append(row)
        if ep:
            seen_episode.add(ep)

    return candidates, rejected


def attach_outcomes(candidates: list[dict[str, Any]], symbol: str, timeframe: str, df: pd.DataFrame, c1m: pd.DataFrame) -> list[dict[str, Any]]:
    pseudo = []
    for c in candidates:
        ref_at = c.get("hypothetical_entry_at")
        ref_px = c.get("hypothetical_entry_price")
        if not ref_at or ref_px is None:
            continue
        pseudo.append(
            {
                "event_id": c["candidate_id"],
                "final_status": "CONFIRMED",
                "direction": c["direction"],
                "confirmation_at": c.get("decision_at") or c.get("candidate_at"),
                "entry_at": ref_at,
                "entry_price": ref_px,
                "cluster_id": None,
            }
        )
    if not pseudo:
        return candidates
    outcomes = analyze_events_outcomes(pseudo, c1m, symbol=symbol, strategy_timeframe=timeframe, strategy_candles=df)
    merged = attach_outcomes_to_events(pseudo, outcomes["events_outcomes"])
    by_id = {m["event_id"]: m.get("outcomes_1h_4h") for m in merged}
    out = []
    for c in candidates:
        oc = by_id.get(c["candidate_id"])
        if oc:
            c = dict(c)
            c["outcomes_1h_4h"] = oc
        out.append(c)
    return out


def _first_hit_bucket(oc: dict[str, Any], horizon: str, thresh: str) -> str:
    fh = (oc or {}).get(f"first_hit_{horizon}") or {}
    return str(fh.get(thresh) or "NEITHER")


def build_funnel_rows(
    *,
    timeframe: str,
    mode_id: str,
    max_gap: int | None,
    candidates: list[dict[str, Any]],
    baseline_episode_ids: set[str],
    n_raw_ema_events: int,
) -> list[dict[str, Any]]:
    rows = []
    groups = {
        "all": candidates,
        "exact_gap_0": [c for c in candidates if int(c.get("exact_gap") or 0) == 0],
        "exact_gap_1": [c for c in candidates if int(c.get("exact_gap") or 0) == 1],
        "exact_gap_2": [c for c in candidates if int(c.get("exact_gap") or 0) == 2],
    }
    for gname, subset in groups.items():
        if gname.startswith("exact_gap_") and max_gap is not None:
            g = int(gname.split("_")[-1])
            if g > (max_gap if max_gap is not None else 99):
                continue
        n = len(subset)
        allows = [c for c in subset if c.get("final_verdict") == "ALLOW"]
        blocks = [c for c in subset if c.get("final_verdict") == "BLOCK"]
        incs = [c for c in subset if c.get("final_verdict") == "INCONCLUSIVE_DATA"]
        overlap = [c for c in subset if c.get("cross_episode_id") in baseline_episode_ids]
        tol_only = [c for c in subset if c.get("cross_episode_id") not in baseline_episode_ids]
        rows.append(
            {
                "timeframe": timeframe,
                "mode_id": mode_id,
                "max_gap": max_gap,
                "group": gname,
                "n_raw_ema_events": n_raw_ema_events,
                "n_candidates": n,
                "n_allow": len(allows),
                "n_block": len(blocks),
                "n_inconclusive": len(incs),
                "n_baseline_overlap": len(overlap),
                "n_tolerance_only": len(tol_only),
                "n_allow_tolerance_only": sum(1 for c in tol_only if c.get("final_verdict") == "ALLOW"),
                "n_full_multisource": sum(1 for c in subset if c.get("coverage_bucket") == "full_multisource"),
                "n_oi_liq_missing": sum(1 for c in subset if c.get("coverage_bucket") == "oi_liq_missing"),
            }
        )
    return rows


def run_mode_on_frame(
    *,
    mode_id: str,
    max_gap: int | None,
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
    c1m: pd.DataFrame,
    cfg=None,
) -> dict[str, Any]:
    cfg = cfg or EMA_DUAL_CROSS_DEFAULTS
    if mode_id == MODE_M0:
        raw = detect_strict_sync_baseline(df, symbol=symbol, timeframe=timeframe, cfg=cfg)
        _, rejected_prod = detect_cross_events(df, symbol=symbol, timeframe=timeframe, cfg=cfg)
        n_raw = len(rejected_prod) + len(raw)
    else:
        assert max_gap is not None
        raw = detect_bar_gap_sync(df, symbol=symbol, timeframe=timeframe, max_gap=max_gap, cfg=cfg)
        n_raw = len(raw)  # filled later by audit
        rejected_prod = []

    gated, rejected = gate_raw_candidates(
        raw,
        df=df,
        symbol=symbol,
        timeframe=timeframe,
        window_start=window_start,
        window_end=window_end,
        trades_1m=trades_1m,
        ob_1m=ob_1m,
        oi_1m=oi_1m,
        liq=liq,
        coverage=coverage,
        mode_id=mode_id,
        cfg=cfg,
    )
    gated = attach_outcomes(gated, symbol, timeframe, df, c1m)
    return {
        "mode_id": mode_id,
        "max_gap": max_gap,
        "raw": raw,
        "candidates": gated,
        "rejected_gate": rejected,
        "rejected_prod": rejected_prod,
        "n_raw_ema_events": n_raw,
    }


def run_tpsl_for_candidates(
    candidates: list[dict[str, Any]],
    c1m: pd.DataFrame,
    *,
    mode_id: str,
    timeframe: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    allows = [c for c in candidates if c.get("final_verdict") == "ALLOW" and c.get("entry_at") and c.get("entry_price") is not None]
    for c in allows:
        for tp, sl in TPSL_MATRIX:
            for horizon in HORIZONS:
                for fee in FEES:
                    sim = simulate_tpsl_trade(
                        c1m,
                        direction=str(c["direction"]),
                        entry_at=c["entry_at"],
                        entry_price=float(c["entry_price"]),
                        tp_pct=tp,
                        sl_pct=sl,
                        horizon_minutes=horizon,
                        fee_roundtrip_pct=fee,
                    )
                    rows.append(
                        {
                            "mode_id": mode_id,
                            "timeframe": timeframe,
                            "candidate_id": c["candidate_id"],
                            "cross_episode_id": c.get("cross_episode_id"),
                            "exact_gap": c.get("exact_gap"),
                            "direction": c["direction"],
                            "coverage_bucket": c.get("coverage_bucket"),
                            "is_tolerance_only": None,  # filled later
                            "tp_pct": tp,
                            "sl_pct": sl,
                            "horizon_min": horizon,
                            "fee_roundtrip_pct": fee,
                            **sim,
                        }
                    )
    return rows


def aggregate_tpsl_summaries(trade_rows: list[dict[str, Any]], baseline_eps: set[str]) -> list[dict[str, Any]]:
    buckets: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for t in trade_rows:
        ep = t.get("cross_episode_id")
        tol = ep not in baseline_eps if ep else False
        t["is_tolerance_only"] = tol
        key = (
            t["mode_id"],
            t["timeframe"],
            int(t.get("exact_gap") if t.get("exact_gap") is not None else -1),
            float(t["tp_pct"]),
            float(t["sl_pct"]),
            int(t["horizon_min"]),
            float(t["fee_roundtrip_pct"]),
            "tolerance_only" if tol else "baseline_overlap",
        )
        buckets[key].append(t)
    # also all (not split)
    buckets_all: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for t in trade_rows:
        key = (
            t["mode_id"],
            t["timeframe"],
            int(t.get("exact_gap") if t.get("exact_gap") is not None else -1),
            float(t["tp_pct"]),
            float(t["sl_pct"]),
            int(t["horizon_min"]),
            float(t["fee_roundtrip_pct"]),
            "all",
        )
        buckets_all[key].append(t)

    out = []
    for store, label_from_key in ((buckets, True), (buckets_all, False)):
        for key, trades in store.items():
            mode_id, tf, exact_gap, tp, sl, horizon, fee, cohort = key
            s = summarize_trade_pnl(trades)
            out.append(
                {
                    "mode_id": mode_id,
                    "timeframe": tf,
                    "exact_gap": exact_gap,
                    "tp_pct": tp,
                    "sl_pct": sl,
                    "horizon_min": horizon,
                    "fee_roundtrip_pct": fee,
                    "cohort": cohort,
                    **s,
                }
            )
    return out


def parity_m0_vs_m1_gap0(m0: list[dict[str, Any]], m1g0: list[dict[str, Any]]) -> dict[str, Any]:
    def keyset(rows: list[dict[str, Any]]) -> set[tuple]:
        out = set()
        for r in rows:
            ts = r.get("candidate_at")
            if hasattr(ts, "isoformat"):
                ts = ts.isoformat()
            out.add((str(r.get("direction")), str(ts), int(r.get("bar_index"))))
        return out

    a, b = keyset(m0), keyset(m1g0)
    return {
        "m0_n": len(a),
        "m1_gap0_n": len(b),
        "only_m0": sorted(a - b),
        "only_m1_gap0": sorted(b - a),
        "parity_ok": a == b,
    }


def run_sync_tolerance_pilot(
    *,
    symbol: str = "XRPUSDT",
    timeframes: tuple[str, ...] = ("15m", "5m"),
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    export_root: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    symbol = str(symbol).upper()
    start = _utc(window_start or datetime(2026, 7, 23, tzinfo=timezone.utc))
    end = _utc(window_end or datetime(2026, 8, 22, tzinfo=timezone.utc))
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    repo = Path(__file__).resolve().parents[4]
    out_dir = Path(export_root) if export_root else repo / "results" / "edc_sync_tolerance" / run_id
    cfg = EMA_DUAL_CROSS_DEFAULTS

    client = default_client()
    try:
        warm_pad = timedelta(days=5)
        c1m_full = fetch_candles_1m(client, symbol, start - warm_pad, end + timedelta(hours=5))
        coverage = coverage_report(client, symbol, start, end)
        pad = timedelta(hours=2)
        trades = fetch_trades_1m(client, symbol, start - pad, end + pad)
        ob = fetch_ob_1m(client, symbol, start - pad, end + pad)
        oi = fetch_oi_1m(client, symbol, start - pad, end + pad)
        liq = fetch_liquidations(client, symbol, start - pad, end + pad)
    finally:
        if hasattr(client, "close"):
            client.close()

    all_candidates: list[dict[str, Any]] = []
    funnel: list[dict[str, Any]] = []
    outcomes_rows: list[dict[str, Any]] = []
    trades_rows: list[dict[str, Any]] = []
    summary_by_mode: dict[str, Any] = {}
    parity_report: dict[str, Any] = {}
    rejected_reuse_audit: dict[str, Any] = {}
    episode_decision_by_mode: dict[str, dict[str, str]] = defaultdict(dict)
    matched_modes: dict[str, list[str]] = defaultdict(list)

    for tf in timeframes:
        df = aggregate_timeframe(c1m_full, tf)
        df = attach_emas(df, fast=cfg.ema_fast, medium=cfg.ema_medium, slow=cfg.ema_slow)
        df = attach_atr(df, cfg.atr_period)

        mode_results: dict[str, dict[str, Any]] = {}
        for mode_id, max_gap in (
            (MODE_M0, None),
            (MODE_M1[0], 0),
            (MODE_M1[1], 1),
            (MODE_M1[2], 2),
        ):
            mode_results[mode_id] = run_mode_on_frame(
                mode_id=mode_id,
                max_gap=max_gap,
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
                c1m=c1m_full,
                cfg=cfg,
            )

        parity_report[tf] = parity_m0_vs_m1_gap0(
            mode_results[MODE_M0]["raw"],
            mode_results[MODE_M1[0]]["raw"],
        )
        # also gated verdict parity on candidate_at keys
        def gated_keys(cands):
            s = set()
            for c in cands:
                ts = c.get("candidate_at")
                if hasattr(ts, "isoformat"):
                    ts = ts.isoformat()
                s.add((c.get("direction"), str(ts), c.get("final_verdict")))
            return s

        parity_report[tf]["gated_parity_ok"] = gated_keys(mode_results[MODE_M0]["candidates"]) == gated_keys(
            mode_results[MODE_M1[0]]["candidates"]
        )

        baseline_eps = {c["cross_episode_id"] for c in mode_results[MODE_M0]["candidates"] if c.get("cross_episode_id")}
        # rejected stagger reuse audit
        rej = mode_results[MODE_M0]["rejected_prod"]
        stag = [r for r in rej if "REJECTED_STAGGERED_CROSS" in str(r.get("reason_codes"))]
        lag_counts: dict[str, int] = defaultdict(int)
        for r in stag:
            lag = (r.get("ema_metrics") or {}).get("cross_lag_bars")
            lag_counts[str(lag)] += 1
        m1g2_eps = {c["cross_episode_id"] for c in mode_results[MODE_M1[2]]["candidates"]}
        rejected_reuse_audit[tf] = {
            "n_rejected_staggered": len(stag),
            "stagger_lag_counts": dict(lag_counts),
            "n_m1_gap2_candidates": len(mode_results[MODE_M1[2]]["candidates"]),
            "n_m1_gap2_tolerance_only": sum(
                1 for c in mode_results[MODE_M1[2]]["candidates"] if c.get("cross_episode_id") not in baseline_eps
            ),
        }

        for mode_id, res in mode_results.items():
            for c in res["candidates"]:
                ep = c.get("cross_episode_id")
                if ep:
                    matched_modes[ep].append(f"{tf}:{mode_id}")
                    decision = c.get("decision_at")
                    if hasattr(decision, "isoformat"):
                        decision = decision.isoformat()
                    episode_decision_by_mode[ep][f"{tf}:{mode_id}"] = str(decision)
                c = dict(c)
                c["is_baseline_overlap"] = c.get("cross_episode_id") in baseline_eps
                c["is_tolerance_only"] = c.get("cross_episode_id") not in baseline_eps
                all_candidates.append(c)
                oc = c.get("outcomes_1h_4h") or {}
                outcomes_rows.append(
                    {
                        "mode_id": mode_id,
                        "timeframe": tf,
                        "candidate_id": c["candidate_id"],
                        "cross_episode_id": c.get("cross_episode_id"),
                        "exact_gap": c.get("exact_gap"),
                        "direction": c["direction"],
                        "final_verdict": c.get("final_verdict"),
                        "is_tolerance_only": c["is_tolerance_only"],
                        "coverage_bucket": c.get("coverage_bucket"),
                        "mfe_1h_pct": oc.get("mfe_1h_pct"),
                        "mae_1h_pct": oc.get("mae_1h_pct"),
                        "close_return_1h_pct": oc.get("close_return_1h_pct"),
                        "first_extreme_1h": oc.get("first_extreme_1h"),
                        "first_hit_1h_0.20": _first_hit_bucket(oc, "1h", "0.20"),
                        "mfe_4h_pct": oc.get("mfe_4h_pct"),
                        "mae_4h_pct": oc.get("mae_4h_pct"),
                        "close_return_4h_pct": oc.get("close_return_4h_pct"),
                        "first_extreme_4h": oc.get("first_extreme_4h"),
                        "first_hit_4h_0.30": _first_hit_bucket(oc, "4h", "0.30"),
                    }
                )

            n_raw = res["n_raw_ema_events"]
            if mode_id != MODE_M0:
                # approximate raw EMA events from production rejects + sync
                n_raw = len(mode_results[MODE_M0]["rejected_prod"]) + len(mode_results[MODE_M0]["raw"])
            funnel.extend(
                build_funnel_rows(
                    timeframe=tf,
                    mode_id=mode_id,
                    max_gap=res["max_gap"],
                    candidates=res["candidates"],
                    baseline_episode_ids=baseline_eps,
                    n_raw_ema_events=n_raw,
                )
            )
            trows = run_tpsl_for_candidates(res["candidates"], c1m_full, mode_id=mode_id, timeframe=tf)
            for t in trows:
                t["is_tolerance_only"] = t.get("cross_episode_id") not in baseline_eps
            trades_rows.extend(trows)

            summary_by_mode[f"{tf}:{mode_id}"] = {
                "n_candidates": len(res["candidates"]),
                "n_allow": sum(1 for c in res["candidates"] if c.get("final_verdict") == "ALLOW"),
                "n_block": sum(1 for c in res["candidates"] if c.get("final_verdict") == "BLOCK"),
                "n_inconclusive": sum(1 for c in res["candidates"] if c.get("final_verdict") == "INCONCLUSIVE_DATA"),
                "exact_gap_counts": {
                    str(g): sum(1 for c in res["candidates"] if int(c.get("exact_gap") or 0) == g) for g in (0, 1, 2)
                },
                "tolerance_only_allow": sum(
                    1
                    for c in res["candidates"]
                    if c.get("final_verdict") == "ALLOW" and c.get("cross_episode_id") not in baseline_eps
                ),
            }

    # annotate matched_modes on candidates
    for c in all_candidates:
        ep = c.get("cross_episode_id")
        c["matched_modes"] = matched_modes.get(ep or "", [])
        c["decision_at_by_mode"] = episode_decision_by_mode.get(ep or "", {})

    # combined portfolio: one trade per cross_episode_id preferring M0 then smallest gap
    portfolio_pref = []
    by_ep: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in all_candidates:
        if c.get("final_verdict") != "ALLOW":
            continue
        by_ep[str(c.get("cross_episode_id"))].append(c)

    def pref_key(c: dict[str, Any]) -> tuple:
        mode = c.get("mode_id") or ""
        gap = int(c.get("exact_gap") or 0)
        # M0 first, then M1 by gap, then timeframe 15m before 5m
        mrank = 0 if mode == MODE_M0 else 1
        tf_rank = 0 if c.get("timeframe") == "15m" else 1
        return (mrank, gap, tf_rank)

    for ep, rows in by_ep.items():
        chosen = sorted(rows, key=pref_key)[0]
        portfolio_pref.append(
            {
                "cross_episode_id": ep,
                "chosen_mode": chosen.get("mode_id"),
                "timeframe": chosen.get("timeframe"),
                "exact_gap": chosen.get("exact_gap"),
                "n_mode_hits": len(rows),
            }
        )

    tpsl_summary = aggregate_tpsl_summaries(trades_rows, set())  # cohort already set per-row
    # recompute with proper baseline sets per timeframe
    baseline_by_tf: dict[str, set[str]] = {}
    for c in all_candidates:
        if c.get("mode_id") == MODE_M0 and c.get("cross_episode_id"):
            baseline_by_tf.setdefault(str(c.get("timeframe")), set()).add(c["cross_episode_id"])
    for t in trades_rows:
        tf = str(t.get("timeframe"))
        t["is_tolerance_only"] = t.get("cross_episode_id") not in baseline_by_tf.get(tf, set())

    tpsl_summary = aggregate_tpsl_summaries(trades_rows, set())

    manifest = {
        "run_id": run_id,
        "git": _git_meta(repo),
        "symbol": symbol,
        "timeframes": list(timeframes),
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "modes": [MODE_M0, MODE_M1[0], MODE_M1[1], MODE_M1[2]],
        "mode_params": {
            MODE_M0: {"detector": "detect_cross_events", "max_gap": 0},
            MODE_M1[0]: {"detector": "detect_bar_gap_sync", "max_gap": 0},
            MODE_M1[1]: {"detector": "detect_bar_gap_sync", "max_gap": 1},
            MODE_M1[2]: {"detector": "detect_bar_gap_sync", "max_gap": 2},
        },
        "gate_policy": POLICY_VERSION,
        "config": config_to_dict(cfg),
        "enable_sync_cross": True,
        "enable_compressed_rebound": False,
        "tpsl_matrix": [{"tp_pct": a, "sl_pct": b} for a, b in TPSL_MATRIX],
        "horizons_min": list(HORIZONS),
        "fees_roundtrip_pct": list(FEES),
        "same_bar_rule": "SL_FIRST",
        "horizon_exit": "last_1m_close_within_horizon",
        "code_version": {
            "package": "ema_dual_cross_multisource.tolerance_research",
            "phase": "phase1_m0_m1_gaps_0_1_2",
        },
        "parity": parity_report,
        "cross_episode_rules": {
            "id_fields": ["symbol", "timeframe", "direction", "first_cross_bar", "first_leg"],
            "within_mode_dedup": "one trade per cross_episode_id",
            "combined_portfolio": "prefer M0, else smallest exact_gap, else 15m over 5m",
        },
        "profitability_claim": False,
    }

    summary_by_mode["tpsl_aggregates"] = tpsl_summary
    summary_by_mode["combined_portfolio_episodes"] = portfolio_pref
    summary_by_mode["parity"] = parity_report

    # markdown summary
    lines = [
        f"# EDC Sync Tolerance Phase-1 — {run_id}",
        "",
        f"Symbol `{symbol}` · Window `{start.isoformat()}` → `{end.isoformat()}`",
        "",
        "## Parity M0 vs M1_GAP_0",
        "",
    ]
    for tf, p in parity_report.items():
        lines.append(f"- **{tf}**: raw_parity={p.get('parity_ok')} gated_parity={p.get('gated_parity_ok')} m0={p.get('m0_n')} m1g0={p.get('m1_gap0_n')}")
    lines.extend(["", "## Funnel (group=all)", ""])
    for row in funnel:
        if row.get("group") != "all":
            continue
        lines.append(
            f"- {row['timeframe']} {row['mode_id']}: cand={row['n_candidates']} ALLOW={row['n_allow']} "
            f"BLOCK={row['n_block']} INC={row['n_inconclusive']} tol_only={row['n_tolerance_only']}"
        )
    lines.extend(["", "## Notes", "", "- No production parameter recommendation from XRP-only pilot.", ""])
    summary_md = "\n".join(lines)

    paths = write_tolerance_bundle(
        out_dir,
        {
            "manifest": manifest,
            "candidates_all": all_candidates,
            "funnel": funnel,
            "outcomes_rows": outcomes_rows,
            "trades_rows": trades_rows,
            "summary_by_mode": summary_by_mode,
            "rejected_reuse_audit": rejected_reuse_audit,
            "summary_md": summary_md,
        },
    )
    # also dump tpsl aggregates csv-friendly inside summary
    (out_dir / "tpsl_summary.csv").write_text(pd.DataFrame(tpsl_summary).to_csv(index=False), encoding="utf-8")

    parity_all_ok = all(p.get("parity_ok") and p.get("gated_parity_ok") for p in parity_report.values())
    return {
        "run_id": run_id,
        "export_dir": str(out_dir),
        "export_paths": paths,
        "parity": parity_report,
        "parity_all_ok": parity_all_ok,
        "summary_by_mode": summary_by_mode,
        "funnel": funnel,
        "n_candidates": len(all_candidates),
        "n_trade_rows": len(trades_rows),
        "policy": policy_document(),
    }

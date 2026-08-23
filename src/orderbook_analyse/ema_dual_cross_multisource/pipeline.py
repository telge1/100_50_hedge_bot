"""EMA dual-cross + multi-source gate pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pandas as pd

from ..cluster_sweep_research.ema_features import attach_emas, required_warmup_bars
from .config import EMA_DUAL_CROSS_DEFAULTS, EmaDualCrossConfig, STRATEGY_ID, STRATEGY_VERSION, config_to_dict
from .coverage_gate import assess_coverage
from .ema_candidate import attach_atr, detect_cross_events
from .episode_state import EpisodeTracker
from .export import write_export_bundle
from .feature_builder import build_gate_features
from .gate_policy import apply_gate, policy_document
from .models import CandidateType, Direction, EmaCandidate, FinalVerdict
from .timeframes import bar_close as compute_bar_close

__all__ = ["STRATEGY_ID", "STRATEGY_VERSION", "run_ema_dual_cross_on_candles"]

_CTYPE_PRIORITY = {
    CandidateType.SYNCHRONOUS_DUAL_EMA_CROSS.value: 0,
    CandidateType.COMPRESSED_EMA59_REBOUND.value: 1,
}


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _next_open(df: pd.DataFrame, bar_index: int) -> tuple[datetime | None, float | None]:
    if bar_index + 1 >= len(df):
        return None, None
    nxt = df.iloc[bar_index + 1]
    ts = _utc(pd.Timestamp(nxt["open_time"]).to_pydatetime().replace(tzinfo=timezone.utc))
    return ts, float(nxt["open"])


def run_ema_dual_cross_on_candles(
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    window_start: datetime,
    window_end: datetime,
    trades_1m: pd.DataFrame | None = None,
    ob_1m: pd.DataFrame | None = None,
    oi_1m: pd.DataFrame | None = None,
    liq: pd.DataFrame | None = None,
    coverage: dict[str, Any] | None = None,
    cfg: EmaDualCrossConfig | None = None,
    export_dir: str | None = None,
    attach_outcomes: bool = True,
) -> dict[str, Any]:
    cfg = cfg or EMA_DUAL_CROSS_DEFAULTS
    symbol = str(symbol).strip().upper()
    start = _utc(window_start)
    end = _utc(window_end)

    df = candles.sort_values("open_time").reset_index(drop=True).copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    df = attach_emas(df, fast=cfg.ema_fast, medium=cfg.ema_medium, slow=cfg.ema_slow)
    df = attach_atr(df, cfg.atr_period)

    raw_valid, rejected = detect_cross_events(df, symbol=symbol, timeframe=timeframe, cfg=cfg)
    raw_valid.sort(key=lambda r: (int(r["bar_index"]), _CTYPE_PRIORITY.get(str(r.get("candidate_type")), 9)))

    tracker = EpisodeTracker(cfg=cfg)
    candidates: list[dict[str, Any]] = []

    for raw in raw_valid:
        ts = raw["candidate_at"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if not (start <= _utc(ts) < end):
            continue

        ok, rej, relation = tracker.admit_candidate(raw)
        if not ok:
            raw = dict(raw)
            raw["final_verdict"] = FinalVerdict.REJECTED.value
            raw["reason_codes"] = list(raw.get("reason_codes") or []) + [rej or "REJECTED_EPISODE_ALREADY_SIGNALED"]
            rejected.append(raw)
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

        entry_at, entry_price = None, None
        if verdict == FinalVerdict.ALLOW:
            entry_at, entry_price = hyp_at, hyp_px

        reason_codes = list(raw.get("reason_codes") or []) + reasons
        if relation == "SYNC_CONFIRMATION":
            reason_codes.append("SYNC_CONFIRMATION")
        elif relation == "QUALITY_UPGRADE":
            reason_codes.append("QUALITY_UPGRADE")

        overlap_flags: dict[str, Any] = {}
        if relation:
            overlap_flags["research_relation"] = relation

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
            policy_version="EMA_MULTI_SOURCE_GATE_V1",
            bar_index=bar_i,
            ema_before=raw.get("ema_before") or {},
            ema_after=raw.get("ema_after") or {},
            ema_metrics=raw.get("ema_metrics") or {},
            coverage=cov,
            features=feats,
            source_verdicts=source_verdicts,
            overlap_flags=overlap_flags,
        )
        candidates.append(cand.to_dict())

    if attach_outcomes and candidates:
        candidates = _attach_outcomes(candidates, symbol, timeframe, df)

    run_id = "edc-" + uuid4().hex[:12]
    summary = {
        "n_candidates": len(candidates),
        "n_allow": sum(1 for c in candidates if c.get("final_verdict") == "ALLOW"),
        "n_block": sum(1 for c in candidates if c.get("final_verdict") == "BLOCK"),
        "n_inconclusive": sum(1 for c in candidates if c.get("final_verdict") == "INCONCLUSIVE_DATA"),
        "n_rejected_crosses": len(rejected),
        "n_sync_cross": sum(
            1 for c in candidates if c.get("candidate_type") == CandidateType.SYNCHRONOUS_DUAL_EMA_CROSS.value
        ),
        "n_rebound": sum(
            1 for c in candidates if c.get("candidate_type") == CandidateType.COMPRESSED_EMA59_REBOUND.value
        ),
        "profitability_claim": False,
    }
    meta = {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "run_id": run_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": "UTC",
        "warmup_bars": required_warmup_bars(cfg.ema_slow, 20),
        "architecture": "EMA_CANDIDATE_GATE → MULTI_SOURCE → ALLOW/BLOCK/INCONCLUSIVE",
        "lld_role": "CONFLUENCE_ONLY",
        **summary,
    }
    bundle = {
        "meta": meta,
        "candidates": candidates,
        "rejected_ema_crosses": rejected,
        "summary": summary,
        "coverage": coverage or {},
        "policy": policy_document(),
    }
    export_paths = {}
    if export_dir:
        export_paths = write_export_bundle(
            bundle,
            export_dir,
            run_config=config_to_dict(cfg),
            policy=policy_document(),
        )
    bundle["export_paths"] = export_paths
    return bundle


def _attach_outcomes(candidates: list[dict[str, Any]], symbol: str, timeframe: str, df: pd.DataFrame) -> list[dict[str, Any]]:
    try:
        from ..cluster_sweep_research.outcome_analysis_1h_4h import analyze_events_outcomes, attach_outcomes_to_events
        from ..cluster_sweep_research.clickhouse_source import default_client, fetch_candles_1m
        from datetime import timedelta

        entries = []
        pseudo_events = []
        for c in candidates:
            ref_at = c.get("hypothetical_entry_at")
            ref_px = c.get("hypothetical_entry_price")
            if not ref_at or ref_px is None:
                continue
            pseudo_events.append(
                {
                    "event_id": c["candidate_id"],
                    "final_status": "CONFIRMED",
                    "direction": c["direction"],
                    "confirmation_at": c.get("decision_at") or c.get("candidate_at"),
                    "entry_at": ref_at,
                    "entry_price": ref_px,
                    "cluster_id": (c.get("features") or {}).get("liquidity_confluence", {}).get("primary_cluster", {}).get("cluster_id"),
                }
            )
            entries.append(datetime.fromisoformat(str(ref_at).replace("Z", "+00:00")))
        if not pseudo_events:
            return candidates
        load_start = min(entries)
        load_end = max(entries) + timedelta(hours=4, minutes=5)
        client = default_client()
        try:
            c1m = fetch_candles_1m(client, symbol, load_start, load_end)
        finally:
            if hasattr(client, "close"):
                client.close()
        outcomes = analyze_events_outcomes(pseudo_events, c1m, symbol=symbol, strategy_timeframe=timeframe, strategy_candles=df)
        merged = attach_outcomes_to_events(pseudo_events, outcomes["events_outcomes"])
        by_id = {m["event_id"]: m.get("outcomes_1h_4h") for m in merged}
        out = []
        for c in candidates:
            oc = by_id.get(c["candidate_id"])
            if oc:
                c = dict(c)
                c["outcomes_1h_4h"] = oc
            out.append(c)
        return out
    except Exception as exc:  # noqa: BLE001
        for c in candidates:
            c.setdefault("outcomes_1h_4h", {"status": "FAILED", "error": str(exc)})
        return candidates

"""Shared cluster-sweep run pipeline (CLI + dashboard). Research-only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from uuid import uuid4

import pandas as pd

from .audit_export import earliest_confirmation, event_audit_row, final_status
from .cluster_adapter import CausalVerdict, run_lld_pools
from .ema_features import attach_emas, required_warmup_bars
from .event_detector import dedupe_related_events, detect_candidates
from .feature_enrichment import enrich_event_orderflow
from .models import ConfirmationVariant, SetupDirection, SweepEvent
from .outcome_evaluator import evaluate_outcomes

STRATEGY_ID = "cluster_sweep_ema_9_20_59"
STRATEGY_VERSION = "cluster_sweep_ema_9_20_59_v1"


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def run_cluster_sweep_on_candles(
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    window_start: datetime,
    window_end: datetime,
    minimum_cluster_pools: int = 3,
    expire_bars: int = 24,
    approach_bps: float = 25.0,
    confirmation_variants: Sequence[str] | None = None,
    dedupe: bool = True,
    trades_1m: pd.DataFrame | None = None,
    ob_1m: pd.DataFrame | None = None,
    oi_1m: pd.DataFrame | None = None,
    liq: pd.DataFrame | None = None,
    coverage: dict[str, Any] | None = None,
    evaluate: bool = True,
) -> dict[str, Any]:
    """Detect + enrich events on a preloaded OHLCV frame (warmup included)."""
    symbol = str(symbol).strip().upper()
    start = _as_utc(window_start)
    end = _as_utc(window_end)
    if end <= start:
        raise ValueError("window_end must be after window_start")
    if minimum_cluster_pools < 1:
        raise ValueError("minimum_cluster_pools must be >= 1")

    df = candles.sort_values("open_time").reset_index(drop=True).copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    df = attach_emas(df)
    lld = run_lld_pools(df, symbol=symbol, timeframe=timeframe)
    events: list[SweepEvent] = []
    if lld.verdict == CausalVerdict.CAUSAL_REUSABLE:
        events = detect_candidates(
            df,
            symbol=symbol,
            timeframe=timeframe,
            pools=lld.pools,
            approach_bps=approach_bps,
            expire_bars=expire_bars,
            minimum_cluster_pools=minimum_cluster_pools,
            require_cluster_entry=True,
        )

        def in_win(e: SweepEvent) -> bool:
            t = e.t_first_touch or e.t_entry or e.t_price_cross_ema59
            if t is None:
                return False
            tt = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            return start <= tt < end

        events = [e for e in events if in_win(e)]
        if dedupe:
            events = dedupe_related_events(events)
        allowed = set(confirmation_variants) if confirmation_variants else None
        for e in events:
            if allowed is not None:
                for k, v in list(e.confirmations.items()):
                    if k not in allowed and v.get("fired"):
                        # keep measurement but flag as not selected for primary
                        v["selected"] = False
                    elif v.get("fired"):
                        v["selected"] = True
            enrich_event_orderflow(e, trades_1m=trades_1m, ob_1m=ob_1m, oi_1m=oi_1m, liq=liq)
            if evaluate:
                evaluate_outcomes(e, df)

    run_id = "csr-" + uuid4().hex[:12]
    rows = [event_audit_row(e) for e in events]
    # Attach full EMA audit from features
    for e, row in zip(events, rows):
        row["ema_audit"] = (e.features or {}).get("ema_audit")
        row["invalidation_reason"] = (e.features or {}).get("invalidation_reason")
        row["prior_touch_count"] = (e.features or {}).get("prior_touch_count")
        row["dedupe_group"] = (e.features or {}).get("dedupe_group")

    status_counts: dict[str, int] = {}
    for e in events:
        s = final_status(e)
        status_counts[s] = status_counts.get(s, 0) + 1
    conf_counts: dict[str, int] = {}
    for e in events:
        for k, v in (e.confirmations or {}).items():
            if v.get("fired"):
                conf_counts[k] = conf_counts.get(k, 0) + 1

    debug_low_pool = minimum_cluster_pools < 3
    return {
        "meta": {
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_VERSION,
            "run_id": run_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": "UTC",
            "minimum_cluster_pools": minimum_cluster_pools,
            "debug_low_pool_zones": debug_low_pool,
            "warmup_bars": required_warmup_bars(),
            "lld_verdict": lld.verdict.value,
            "lld_reason": lld.reason,
            "n_events": len(events),
            "n_bullish": sum(1 for e in events if e.setup_direction == SetupDirection.BULLISH),
            "n_bearish": sum(1 for e in events if e.setup_direction == SetupDirection.BEARISH),
            "status_counts": status_counts,
            "confirmation_variant_counts": conf_counts,
            "profitability_claim": False,
        },
        "coverage": coverage or {},
        "events": rows,
        "raw_events": events,
        "candles": df,
        "pools": lld.pools if lld.verdict == CausalVerdict.CAUSAL_REUSABLE else [],
    }

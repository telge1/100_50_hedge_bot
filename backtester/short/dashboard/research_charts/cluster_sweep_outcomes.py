"""Dashboard adapter: 1m CH candles + cluster-sweep 1h/4h outcome analysis."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .cluster_sweep_backtester import candles_to_frame
from .oa_import import load_cluster_sweep
from .service import candle_objects

OA_RESULTS_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse/results/cluster_sweep_research/backtester_outcomes")


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_1m_candles_range(
    symbol: str,
    start: datetime,
    end: datetime,
) -> Any:
    """Read-only 1m candles from ClickHouse signal_generator.candles_1m."""
    candles = candle_objects(
        symbol,
        "1m",
        start=int(_utc(start).timestamp()),
        end=int(_utc(end).timestamp()),
        limit=5000,
        allow_stale=True,
    )
    return candles_to_frame(candles)


def enrich_backtest_with_outcomes(
    backtest_result: dict[str, Any],
    *,
    export_dir: Path | None = None,
) -> dict[str, Any]:
    """Attach 1h/4h outcomes to confirmed entries; optional export."""
    events = backtest_result.get("events") or []
    meta = backtest_result.get("meta") or {}
    sym = str(meta.get("symbol") or "")
    tf = str(meta.get("timeframe") or "5m")
    if not sym or not events:
        backtest_result["outcome_analysis"] = {"status": "NO_EVENTS"}
        return backtest_result

    oa = load_cluster_sweep()
    from orderbook_analyse.cluster_sweep_research.ema_features import attach_emas  # noqa: WPS433
    from orderbook_analyse.cluster_sweep_research.outcome_analysis_1h_4h import (  # noqa: WPS433
        analyze_events_outcomes,
        attach_outcomes_to_events,
        eligible_events,
        write_export_bundle,
    )

    eligible = eligible_events(events)
    if not eligible:
        backtest_result["outcome_analysis"] = {"status": "NO_CONFIRMED_ENTRIES"}
        return backtest_result

    entries = []
    for e in eligible:
        t = e.get("entry_at")
        if not t:
            continue
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        entries.append(_utc(dt))
    load_start = min(entries)
    load_end = max(entries) + timedelta(hours=4, minutes=5)

    candles_1m = load_1m_candles_range(sym, load_start, load_end)

    # strategy TF with EMA for confirm/entry/conservative
    warm = int(oa["required_warmup_bars"](59, 40))
    try:
        bar_m = int(tf.replace("m", ""))
    except ValueError:
        bar_m = 5
    strat_start = load_start - timedelta(minutes=warm * bar_m)
    strat_candles = candle_objects(
        sym,
        tf,
        start=int(strat_start.timestamp()),
        end=int(load_end.timestamp()),
        limit=3000,
        allow_stale=True,
    )
    strat_df = attach_emas(candles_to_frame(strat_candles))

    bundle = analyze_events_outcomes(
        events,
        candles_1m,
        symbol=sym,
        strategy_timeframe=tf,
        strategy_candles=strat_df,
    )
    backtest_result["events"] = attach_outcomes_to_events(events, bundle["events_outcomes"])
    backtest_result["outcome_analysis"] = {
        "status": "OK",
        "summary": bundle["summary"],
        "episodes": bundle["episodes"],
        "formulas": bundle["formulas"],
        "n_analyzed": len([r for r in bundle["events_outcomes"] if r.get("entry_variant") == "AGGRESSIVE"]),
        "candle_1m_rows": len(candles_1m),
        "candle_1m_range": {
            "start": load_start.isoformat(),
            "end": load_end.isoformat(),
        },
    }

    run_config = {
        "symbol": sym,
        "timeframe": tf,
        "window_start": meta.get("start"),
        "window_end": meta.get("end"),
        "candle_source": "clickhouse_candles_1m",
        "strategy_id": meta.get("strategy_id"),
        "run_id": meta.get("run_id"),
    }
    out_dir = export_dir or (OA_RESULTS_ROOT / f"{sym.lower()}_{tf}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    paths = write_export_bundle(bundle, out_dir, run_config=run_config)
    backtest_result["outcome_analysis"]["export_paths"] = paths
    backtest_result["outcome_analysis"]["export_dir"] = str(out_dir)
    return backtest_result

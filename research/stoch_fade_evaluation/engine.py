"""Apply existing NO_BE50 evaluator to Frozen Tier-A job signals. No formula copy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .config import (
    EXIT_POLICY,
    PNL_BASIS,
    SIGNAL_SCOPE,
    SIGNAL_STRATEGY_VERSION,
    ensure_sg_on_path,
    iso_z,
)
from .identity import frozen_outcome_identity


def generation_key(row: dict[str, Any]) -> str:
    open_ts = str(row.get("candle_open_time") or "")[:19]
    return "|".join(
        [
            str(row.get("symbol") or ""),
            str(row.get("timeframe") or ""),
            str(row.get("direction") or row.get("trade_direction") or "").upper(),
            str(row.get("signal_type") or "wave_fade"),
            open_ts,
        ]
    )


def candles_to_be50_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close"])
    out = df.copy()
    if "timestamp" not in out.columns:
        out["timestamp"] = pd.to_datetime(out["open_time"], utc=True)
    else:
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    return out[["timestamp", "open", "high", "low", "close"]].sort_values("timestamp").reset_index(drop=True)


def outcome_window_for_signals(signals: list[dict[str, Any]]) -> tuple[datetime, datetime, dict[str, int]]:
    ensure_sg_on_path()
    from signal_generator.strategy.wave_fade.parameters import STRATEGY_MAX_HOLD_BY_TF

    holds = dict(STRATEGY_MAX_HOLD_BY_TF)
    entries: list[datetime] = []
    max_hold = 24 * 60
    for row in signals:
        raw = row.get("entry_time") or row.get("confirmation_available_at") or row.get("candle_close_time")
        if not raw:
            continue
        ts = pd.Timestamp(raw)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        entries.append(ts.to_pydatetime())
        tf = str(row.get("timeframe") or "15m")
        max_hold = max(max_hold, int(holds.get(tf, 24 * 60)))
    if not entries:
        now = datetime.now(timezone.utc)
        return now, now, holds
    start = min(entries)
    end = max(entries) + timedelta(minutes=max_hold) + timedelta(minutes=1)
    return start, end, holds


def _as_of_from_candles(df: pd.DataFrame) -> datetime | None:
    if df is None or df.empty:
        return None
    last = pd.to_datetime(df["timestamp"], utc=True).max()
    return (last + timedelta(minutes=1)).to_pydatetime()


def evaluate_tier_a_signals(
    signals: list[dict[str, Any]],
    c1m: pd.DataFrame,
    *,
    evaluation_id: str,
    source_job_id: str,
    as_of: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Call existing evaluate_signal_no_be50 once per Frozen Tier-A signal. Independent, no dedup."""
    ensure_sg_on_path()
    identity = frozen_outcome_identity()
    from signal_generator.pipeline.outcome_eval import evaluate_signal_no_be50, summarize_trade_views

    allowed = {"WIN", "LOSS", "OPEN"}
    frame = candles_to_be50_frame(c1m)
    as_of_u = as_of or _as_of_from_candles(frame)
    views = []
    rows: list[dict[str, Any]] = []
    last_ts = None
    if not frame.empty:
        last_ts = pd.to_datetime(frame["timestamp"], utc=True).max()
    candle_data_to = iso_z(last_ts.to_pydatetime()) if last_ts is not None else None
    horizon_end = iso_z(as_of_u) if as_of_u is not None else None

    for raw in signals:
        if not raw.get("tier_a"):
            continue
        if str(raw.get("strategy_version") or SIGNAL_STRATEGY_VERSION) != SIGNAL_STRATEGY_VERSION:
            continue
        sid = str(raw.get("signal_id") or "")
        if not sid:
            continue
        payload = dict(raw)
        view = evaluate_signal_no_be50(payload, frame, as_of=as_of_u)
        result = str(view.result or "").upper()
        display = str(getattr(view, "display_result", None) or result).upper()
        if result not in allowed or display not in allowed or result.startswith("BE") or "BE /" in display:
            raise RuntimeError(f"NO_BE50_RESULT_VIOLATION:{sid}:{result}:{display}")
        api = view.as_api()
        if api.get("be50_activated") or str(api.get("exit_reason") or "").upper() == "BE":
            raise RuntimeError(f"NO_BE50_BE_EXIT_VIOLATION:{sid}")
        views.append(view)
        is_open = result == "OPEN"
        rows.append(
            {
                "evaluation_id": evaluation_id,
                "source_job_id": source_job_id,
                "signal_id": sid,
                "generation_key": generation_key(payload),
                "symbol": payload.get("symbol"),
                "timeframe": payload.get("timeframe"),
                "direction": str(payload.get("direction") or "").upper(),
                "signal_type": payload.get("signal_type") or "wave_fade",
                "signal_strategy_version": SIGNAL_STRATEGY_VERSION,
                "strategy_version": SIGNAL_STRATEGY_VERSION,
                "entry_time": api.get("entry_time") or payload.get("entry_time"),
                "entry_price": api.get("entry_price") if api.get("entry_price") is not None else payload.get("entry_price"),
                "tp_price": payload.get("tp_price"),
                "initial_sl_price": payload.get("sl_price"),
                "final_sl_price": payload.get("sl_price"),
                "exit_policy": EXIT_POLICY,
                "outcome": result,
                "display_result": display,
                "exit_reason": api.get("exit_reason"),
                "exit_time": api.get("exit_time"),
                "exit_price": api.get("exit_price"),
                "pnl_pct_gross": api.get("pnl_pct"),
                "pnl_pct_net": None,
                "pnl_basis": PNL_BASIS,
                "duration_seconds": api.get("duration_seconds"),
                "be_activated": False,
                "be_activation_time": None,
                "candle_data_to": candle_data_to,
                "horizon_end": horizon_end,
                "is_open": is_open,
                "error_code": api.get("ambiguity_flag") or None,
                "ambiguity_flag": api.get("ambiguity_flag"),
                "signal_scope": SIGNAL_SCOPE,
                "execution_dedup_applied": False,
                "source": "FROZEN_RESEARCH_EVALUATION",
                "outcomes_computed": True,
            }
        )

    summary = summarize_trade_views(views)
    summary["signal_scope"] = SIGNAL_SCOPE
    summary["execution_dedup_applied"] = False
    summary["exit_policy"] = EXIT_POLICY
    summary["signal_strategy_version"] = SIGNAL_STRATEGY_VERSION
    summary["pnl_basis"] = PNL_BASIS
    summary["be50_activated_count"] = 0
    summary["be50_exit_count"] = 0
    summary["win_rate_denominator"] = "wins+losses (OPEN excluded)"
    summary["filtered_signals"] = len(rows)
    identity["as_of"] = horizon_end
    identity["candle_data_to"] = candle_data_to
    return rows, summary, identity

"""Apply Frozen-signal NO_BE50 full-1m TP/SL evaluation. Max-hold does not cap this path."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .config import (
    CONFIRMATION_POLICY,
    EXIT_POLICY,
    PNL_BASIS,
    SIGNAL_SCOPE,
    SIGNAL_STRATEGY_VERSION,
    iso_z,
)
from .full_1m_scan import evaluate_signal_no_be50_full_1m, parse_utc, truncate_to_pin
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


def outcome_window_for_signals(
    signals: list[dict[str, Any]],
    *,
    candle_data_to: datetime | None = None,
) -> tuple[datetime, datetime, dict[str, int]]:
    """Load from earliest entry through pinned last closed 1m (exclusive end = pin+1m).

    STRATEGY_MAX_HOLD is not used as a load cap on this path.
    """
    entries: list[datetime] = []
    for row in signals:
        raw = row.get("entry_time") or row.get("confirmation_available_at") or row.get("candle_close_time")
        parsed = parse_utc(raw)
        if parsed:
            entries.append(parsed)
    if not entries:
        now = datetime.now(timezone.utc)
        return now, now, {}
    start = min(entries)
    if candle_data_to is not None:
        pin = candle_data_to
        if pin.tzinfo is None:
            pin = pin.replace(tzinfo=timezone.utc)
        end = pin + pd.Timedelta(minutes=1).to_pytimedelta()
    else:
        end = max(entries) + pd.Timedelta(minutes=1).to_pytimedelta()
    return start, end, {"max_hold_applied_to_window": 0}


def evaluate_tier_a_signals(
    signals: list[dict[str, Any]],
    c1m: pd.DataFrame,
    *,
    evaluation_id: str,
    source_job_id: str,
    candle_data_to: datetime | None = None,
    as_of: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    identity = frozen_outcome_identity()
    allowed = {"WIN", "LOSS", "OPEN"}
    pin = candle_data_to or as_of
    frame = truncate_to_pin(candles_to_be50_frame(c1m), pin)
    last_ts = None
    if not frame.empty:
        last_ts = pd.to_datetime(frame["timestamp"], utc=True).max().to_pydatetime()
    candle_to = iso_z(last_ts) if last_ts is not None else (iso_z(pin) if pin else None)
    horizon_end = iso_z(last_ts + pd.Timedelta(minutes=1).to_pytimedelta()) if last_ts is not None else None

    rows: list[dict[str, Any]] = []
    wins = losses = opens = 0
    gp = gl = 0.0
    for raw in signals:
        if not raw.get("tier_a"):
            continue
        if str(raw.get("strategy_version") or SIGNAL_STRATEGY_VERSION) != SIGNAL_STRATEGY_VERSION:
            continue
        sid = str(raw.get("signal_id") or "")
        if not sid:
            continue
        payload = dict(raw)
        api = evaluate_signal_no_be50_full_1m(payload, frame, candle_data_to=pin)
        result = str(api.get("result") or "").upper()
        display = str(api.get("display_result") or result).upper()
        if result not in allowed or display not in allowed or result.startswith("BE") or "BE /" in display:
            raise RuntimeError(f"NO_BE50_RESULT_VIOLATION:{sid}:{result}:{display}")
        if result == "WIN":
            wins += 1
            if api.get("pnl_pct") is not None:
                gp += float(api["pnl_pct"])
        elif result == "LOSS":
            losses += 1
            if api.get("pnl_pct") is not None:
                gl += float(api["pnl_pct"])
        else:
            opens += 1
        rows.append(
            {
                "evaluation_id": evaluation_id,
                "source_job_id": source_job_id,
                "signal_id": sid,
                "setup_id": payload.get("setup_id"),
                "generation_key": generation_key(payload),
                "symbol": payload.get("symbol"),
                "timeframe": payload.get("timeframe"),
                "direction": str(payload.get("direction") or "").upper(),
                "signal_type": payload.get("signal_type") or "wave_fade",
                "strategy_version": SIGNAL_STRATEGY_VERSION,
                "signal_strategy_version": SIGNAL_STRATEGY_VERSION,
                "confirmation_policy": payload.get("confirmation_policy") or CONFIRMATION_POLICY,
                "confirmation_source": payload.get("confirmation_source") or CONFIRMATION_POLICY,
                "end_ts": payload.get("end_ts"),
                "end_available_at": payload.get("end_available_at"),
                "recognition_ts": payload.get("recognition_ts"),
                "recognition_available_at": payload.get("recognition_available_at"),
                "confirmation_available_at": payload.get("confirmation_available_at"),
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
                "candle_data_to": candle_to,
                "horizon_end": horizon_end,
                "is_open": result == "OPEN",
                "error_code": api.get("ambiguity_flag") or None,
                "ambiguity_flag": api.get("ambiguity_flag"),
                "signal_scope": SIGNAL_SCOPE,
                "execution_dedup_applied": False,
                "max_hold_applied": False,
                "barrier_scan": "FULL_1M_UNTIL_TOUCH_OR_HISTORY_END",
                "source": "FROZEN_RESEARCH_EVALUATION",
                "outcomes_computed": True,
            }
        )

    closed = wins + losses
    summary = {
        "signals": len(rows),
        "wins": wins,
        "losses": losses,
        "open": opens,
        "win_rate_pct": (wins / closed * 100.0) if closed else None,
        "gross_profit_pct": gp,
        "gross_loss_pct": gl,
        "total_pnl_pct": gp + gl,
        "pnl_basis": PNL_BASIS,
        "signal_scope": SIGNAL_SCOPE,
        "execution_dedup_applied": False,
        "exit_policy": EXIT_POLICY,
        "signal_strategy_version": SIGNAL_STRATEGY_VERSION,
        "be50_activated_count": 0,
        "be50_exit_count": 0,
        "win_rate_denominator": "wins+losses (OPEN excluded)",
        "filtered_signals": len(rows),
        "max_hold_applied": False,
        "barrier_scan": "FULL_1M_UNTIL_TOUCH_OR_HISTORY_END",
    }
    identity["as_of"] = horizon_end
    identity["candle_data_to"] = candle_to
    identity["outcome_engine"] = "evaluate_signal_no_be50_full_1m"
    identity["max_hold_applied"] = False
    identity["barrier_scan"] = "FULL_1M_UNTIL_TOUCH_OR_HISTORY_END"
    return rows, summary, identity

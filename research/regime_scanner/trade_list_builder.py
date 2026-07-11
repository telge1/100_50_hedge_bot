"""Build trade lists for regime batch audits from continuous result JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pandas as pd

TradeFilter = Literal["positive_closed", "negative_closed", "all_closed"]

POSITIVE_CLOSED_STATUSES = {
    "closed",
    "finished",
    "closed_undercovered_final_exit",
}
NEGATIVE_CLOSED_STATUSES = {
    "closed_negative_pnl",
    "closed_undercovered_final_exit",  # only when pnl < 0
}
OPEN_STATUSES = {"open", "unfinished", "error"}


def _pnl_of(run: dict[str, Any]) -> float | None:
    for key in ("overall_pnl", "realized_pnl", "pnl"):
        if run.get(key) is None:
            continue
        try:
            return float(run[key])
        except (TypeError, ValueError):
            continue
    return None


def _trade_id_of(run: dict[str, Any], fallback_index: int) -> str:
    for key in ("trade_block_id", "trade_id", "run_id"):
        value = run.get(key)
        if value:
            return str(value)
    number = run.get("trade_number")
    if number is not None:
        return f"backtest_long_continuous_trade_{int(number):04d}"
    return f"backtest_long_continuous_trade_{fallback_index + 1:04d}"


def absolute_start_index(run: dict[str, Any]) -> int:
    """Absolute candle index = input_slice_start_index + relative start_index."""
    relative = int(run.get("start_index") or 0)
    slice_start = int(run.get("input_slice_start_index") or 0)
    return slice_start + relative


def resolve_start_candle_open(
    candles: pd.DataFrame,
    *,
    absolute_index: int,
    fallback_start_time: object | None = None,
) -> pd.Timestamp:
    """Resolve start candle open UTC from absolute index into the candle frame."""
    if absolute_index < 0 or absolute_index >= len(candles):
        if fallback_start_time is not None:
            ts = pd.Timestamp(fallback_start_time)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            return ts
        raise IndexError(f"absolute start_index {absolute_index} out of candle range")
    ts = pd.Timestamp(candles.iloc[absolute_index]["timestamp"])
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def is_positive_closed(run: dict[str, Any]) -> bool:
    status = str(run.get("final_status") or "").strip().lower()
    pnl = _pnl_of(run)
    if pnl is None or pnl <= 0:
        return False
    if status in OPEN_STATUSES:
        return False
    if status == "closed_negative_pnl":
        return False
    # closed / finished / undercovered-with-positive-pnl
    return status in {"closed", "finished", "closed_undercovered_final_exit"} or (
        status.startswith("closed") and "negative" not in status
    )


def is_negative_closed(run: dict[str, Any]) -> bool:
    status = str(run.get("final_status") or "").strip().lower()
    pnl = _pnl_of(run)
    if pnl is None or pnl >= 0:
        return False
    if status in OPEN_STATUSES:
        return False
    return (
        status in {"closed_negative_pnl", "closed", "finished", "closed_undercovered_final_exit"}
        or status.startswith("closed")
    )


def extract_trades_from_result_file(
    result_file: str | Path,
    *,
    candles: pd.DataFrame,
    trade_filter: TradeFilter = "positive_closed",
    candle_interval_minutes: int = 5,
) -> dict[str, Any]:
    """Extract deduplicated trade rows with causal decision times."""
    path = Path(result_file)
    payload = json.loads(path.read_text(encoding="utf-8"))
    runs = payload.get("runs") or []
    aggregate = (payload.get("aggregate") or [{}])[0]

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, run in enumerate(runs):
        if trade_filter == "positive_closed" and not is_positive_closed(run):
            continue
        if trade_filter == "negative_closed" and not is_negative_closed(run):
            continue
        if trade_filter == "all_closed":
            status = str(run.get("final_status") or "").lower()
            if status in OPEN_STATUSES:
                continue
            if _pnl_of(run) is None:
                continue

        trade_id = _trade_id_of(run, idx)
        if trade_id in seen:
            continue
        seen.add(trade_id)

        abs_idx = absolute_start_index(run)
        start_open = resolve_start_candle_open(
            candles,
            absolute_index=abs_idx,
            fallback_start_time=run.get("start_time"),
        )
        decision = start_open + pd.Timedelta(minutes=int(candle_interval_minutes))
        pnl = _pnl_of(run)
        category = (
            "positive_closed"
            if trade_filter == "positive_closed"
            else ("negative_closed" if trade_filter == "negative_closed" else "closed")
        )
        selected.append(
            {
                "trade_id": trade_id,
                "start_index": abs_idx,
                "relative_start_index": int(run.get("start_index") or 0),
                "input_slice_start_index": int(run.get("input_slice_start_index") or 0),
                "start_candle_open_utc": start_open.isoformat(),
                "decision_time_after_close_utc": decision.isoformat(),
                "pnl": pnl,
                "status": str(run.get("final_status") or ""),
                "category": category,
                "start_time_utc": run.get("start_time"),
            }
        )

    return {
        "result_file": str(path),
        "trade_filter": trade_filter,
        "trade_count": len(selected),
        "trades": selected,
        "aggregate": {
            "trades_started": aggregate.get("trades_started"),
            "closed_count": aggregate.get("closed_count"),
            "successful_closed_count": aggregate.get("successful_closed_count"),
            "negative_pnl_closed_count": aggregate.get("negative_pnl_closed_count"),
            "total_pnl": aggregate.get("total_pnl"),
            "open_count": aggregate.get("open_count"),
            "unfinished_count": aggregate.get("unfinished_count"),
        },
        "source_runs": len(runs),
    }

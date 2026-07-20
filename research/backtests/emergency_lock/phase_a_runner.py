"""Phase A runner: open hedge, emergency full-lock, prove frozen basket PnL."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from research.backtests.candle_loader import (
    DEFAULT_DATA_DIR,
    load_candles_for_symbol,
)

from .config import EmergencyLockRecoveryConfig
from .cost_model import (
    conservative_emergency_short_fill_price,
    apply_long_open_slippage,
    apply_short_open_slippage,
    funding_payment_usdt,
)
from .position_ledger import (
    PositionLedger,
    emergency_trigger_price,
    qty_from_notional,
)

TRACE_FIELDS = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "state",
    "long_qty",
    "long_avg",
    "short_qty",
    "short_avg",
    "net_qty",
    "unrealized_long_pnl",
    "unrealized_short_pnl",
    "realized_long_pnl",
    "realized_short_pnl",
    "total_fees",
    "slippage_cost",
    "funding_cost",
    "basket_net_pnl",
    "trigger_price",
    "lock_timestamp",
    "lock_price",
    "short_avg_after_lock",
    "frozen_deficit_usdt",
)


class EmergencyLockError(ValueError):
    """Raised when a Phase A run cannot proceed safely."""


def _parse_ts(value: object) -> datetime:
    if isinstance(value, datetime):
        ts = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _ts_iso(value: object | None) -> str | None:
    if value is None:
        return None
    return _parse_ts(value).isoformat()


def resolve_start_index(
    candles: Sequence[dict[str, Any]],
    cfg: EmergencyLockRecoveryConfig,
) -> int:
    if cfg.start_index is not None and cfg.start_timestamp is not None:
        raise EmergencyLockError("provide only one of start_index or start_timestamp")
    if cfg.start_index is not None:
        idx = int(cfg.start_index)
        if idx < 0 or idx >= len(candles):
            raise EmergencyLockError(
                f"start_index {idx} out of range for {len(candles)} candles"
            )
        return idx
    if cfg.start_timestamp is not None:
        target = _parse_ts(cfg.start_timestamp)
        for i, row in enumerate(candles):
            if _parse_ts(row["timestamp"]) == target:
                return i
        raise EmergencyLockError(
            f"start_timestamp {cfg.start_timestamp!r} not found in candle series"
        )
    return 0


def load_phase_a_candles(cfg: EmergencyLockRecoveryConfig) -> list[dict[str, Any]]:
    """Load Feather candles only (no database)."""
    return load_candles_for_symbol(
        symbol=cfg.symbol,
        timeframe=cfg.timeframe,
        data_dir=DEFAULT_DATA_DIR,
        limit=None,
    )


def _candle_triggers(candle: dict[str, Any], trigger_price: float, cfg: EmergencyLockRecoveryConfig) -> bool:
    if cfg.trigger_price_source != "low":
        raise EmergencyLockError(
            f"unsupported trigger_price_source: {cfg.trigger_price_source}"
        )
    if cfg.intrabar_mode != "conservative":
        raise EmergencyLockError(f"unsupported intrabar_mode: {cfg.intrabar_mode}")
    return float(candle["low"]) <= float(trigger_price)


def _maybe_apply_funding(
    ledger: PositionLedger,
    *,
    cfg: EmergencyLockRecoveryConfig,
    mark_price: float,
    bars_since_start: int,
    candle_minutes: int = 5,
) -> float:
    if not cfg.funding_enabled or cfg.funding_rate_per_interval == 0.0:
        return 0.0
    interval_bars = max(1, int(cfg.funding_interval_hours * 60 // candle_minutes))
    if bars_since_start <= 0 or bars_since_start % interval_bars != 0:
        return 0.0
    payment = funding_payment_usdt(
        long_qty=ledger.long_qty,
        short_qty=ledger.short_qty,
        mark_price=mark_price,
        funding_rate=cfg.funding_rate_per_interval,
    )
    if payment != 0.0:
        ledger.apply_funding(payment)
    return payment


def run_phase_a(
    cfg: EmergencyLockRecoveryConfig,
    candles: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run Phase A emergency full-lock and return summary + trace rows."""
    rows = list(candles) if candles is not None else load_phase_a_candles(cfg)
    if not rows:
        raise EmergencyLockError("no candles loaded")

    start_i = resolve_start_index(rows, cfg)
    window = rows[start_i:]
    if cfg.max_candles is not None and cfg.max_candles > 0:
        window = window[: int(cfg.max_candles)]
    if not window:
        raise EmergencyLockError("empty candle window after start resolution")

    start_candle = window[0]
    entry_ref = float(start_candle["close"])
    if entry_ref <= 0.0:
        raise EmergencyLockError("start candle close must be positive")

    long_fill = apply_long_open_slippage(
        reference_price=entry_ref, slippage_bps=cfg.slippage_bps
    )
    short_fill = apply_short_open_slippage(
        reference_price=entry_ref, slippage_bps=cfg.slippage_bps
    )

    # Notionals sized off the reference start close (not slipped fills),
    # matching the research brief: qty = notional / entry_price.
    long_qty = qty_from_notional(
        notional_usdt=cfg.initial_long_notional_usdt, price=entry_ref
    )
    short_qty = qty_from_notional(
        notional_usdt=cfg.initial_short_notional_usdt, price=entry_ref
    )

    ledger = PositionLedger()
    ledger.open_long(
        qty=long_qty,
        fill_price=long_fill,
        fee_rate=cfg.fee_rate,
        reference_price=entry_ref,
    )
    ledger.open_short(
        qty=short_qty,
        fill_price=short_fill,
        fee_rate=cfg.fee_rate,
        reference_price=entry_ref,
    )

    trigger = emergency_trigger_price(
        long_avg=ledger.long_avg, emergency_trigger_pct=cfg.emergency_trigger_pct
    )

    state = "open_hedge"
    lock_timestamp: str | None = None
    lock_price: float | None = None
    short_avg_after_lock: float | None = None
    basket_pnl_at_lock: float | None = None
    frozen_deficit_usdt: float | None = None
    fees_at_lock: float | None = None
    funding_at_lock: float | None = None
    post_lock_pnls: list[float] = []
    post_lock_explained_drift: list[float] = []

    if _candle_triggers(start_candle, trigger, cfg):
        if cfg.start_below_trigger_policy == "reject":
            raise EmergencyLockError(
                "start candle already at/below emergency trigger "
                f"(low={start_candle['low']}, trigger={trigger})"
            )
        # lock_immediately handled in the loop on bar 0

    trace: list[dict[str, Any]] = []
    lock_triggered = False

    for offset, candle in enumerate(window):
        mark = float(candle["close"])
        _maybe_apply_funding(
            ledger,
            cfg=cfg,
            mark_price=mark,
            bars_since_start=offset,
        )

        if not lock_triggered and _candle_triggers(candle, trigger, cfg):
            fill = conservative_emergency_short_fill_price(
                trigger_price=trigger,
                candle_low=float(candle["low"]),
                slippage_bps=cfg.slippage_bps,
            )
            ledger.emergency_short_top_up(
                fill_price=fill,
                fee_rate=cfg.fee_rate,
                reference_price=trigger,
                qty_tolerance=cfg.qty_tolerance,
            )
            if abs(ledger.long_qty - ledger.short_qty) > cfg.qty_tolerance:
                raise EmergencyLockError(
                    "full lock failed: long_qty != short_qty after top-up"
                )
            if ledger.short_qty > ledger.long_qty + cfg.qty_tolerance:
                raise EmergencyLockError("short overhedge after emergency top-up")

            lock_triggered = True
            state = "full_lock"
            lock_timestamp = _ts_iso(candle["timestamp"])
            lock_price = fill
            short_avg_after_lock = float(ledger.short_avg)
            basket_pnl_at_lock = ledger.basket_net_pnl(mark)
            # Frozen deficit: locked basket value excluding cash costs at lock.
            frozen_deficit_usdt = (
                ledger.unrealized_long_pnl(mark)
                + ledger.unrealized_short_pnl(mark)
                + ledger.realized_long_pnl
                + ledger.realized_short_pnl
            )
            fees_at_lock = float(ledger.total_fees)
            funding_at_lock = float(ledger.funding_cost)

        snap = ledger.snapshot(mark)
        row = {
            "timestamp": _ts_iso(candle["timestamp"]),
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": mark,
            "state": state,
            **{k: snap[k] for k in (
                "long_qty",
                "long_avg",
                "short_qty",
                "short_avg",
                "net_qty",
                "unrealized_long_pnl",
                "unrealized_short_pnl",
                "realized_long_pnl",
                "realized_short_pnl",
                "total_fees",
                "slippage_cost",
                "funding_cost",
                "basket_net_pnl",
            )},
            "trigger_price": trigger,
            "lock_timestamp": lock_timestamp,
            "lock_price": lock_price,
            "short_avg_after_lock": short_avg_after_lock,
            "frozen_deficit_usdt": frozen_deficit_usdt,
        }
        trace.append(row)

        if lock_triggered:
            post_lock_pnls.append(float(snap["basket_net_pnl"]))
            # Explained component: fees/funding accrued since lock.
            explained = -(
                (float(ledger.total_fees) - float(fees_at_lock or 0.0))
                + (float(ledger.funding_cost) - float(funding_at_lock or 0.0))
            )
            expected = float(basket_pnl_at_lock) + explained
            post_lock_explained_drift.append(
                abs(float(snap["basket_net_pnl"]) - expected)
            )

    bars_after_lock = max(0, len(post_lock_pnls) - 1) if lock_triggered else 0
    if lock_triggered and post_lock_pnls:
        max_drift_raw = max(post_lock_pnls) - min(post_lock_pnls)
        max_unexplained = max(post_lock_explained_drift) if post_lock_explained_drift else 0.0
    else:
        max_drift_raw = 0.0
        max_unexplained = 0.0

    # Without post-lock cost changes, raw drift must be ~0.
    # With costs, unexplained residual must be ~0.
    full_lock_ok = (
        lock_triggered
        and abs(ledger.long_qty - ledger.short_qty) <= cfg.qty_tolerance
        and max_unexplained <= cfg.pnl_tolerance
    )

    summary: dict[str, Any] = {
        "symbol": cfg.symbol,
        "timeframe": cfg.timeframe,
        "start_timestamp": _ts_iso(start_candle["timestamp"]),
        "start_index": start_i,
        "entry_price": entry_ref,
        "long_fill_price": long_fill,
        "short_fill_price": short_fill,
        "trigger_price": trigger,
        "lock_triggered": lock_triggered,
        "lock_timestamp": lock_timestamp,
        "lock_price": lock_price,
        "long_qty": float(ledger.long_qty),
        "short_qty": float(ledger.short_qty),
        "long_avg": float(ledger.long_avg),
        "short_avg_after_lock": short_avg_after_lock,
        "basket_pnl_at_lock": basket_pnl_at_lock,
        "frozen_deficit_usdt": frozen_deficit_usdt,
        "maximum_post_lock_pnl_drift_without_costs": float(max_drift_raw),
        "maximum_post_lock_unexplained_drift": float(max_unexplained),
        "fees": float(ledger.total_fees),
        "opening_fees": float(ledger.opening_fees),
        "lock_fees": float(ledger.lock_fees),
        "slippage_cost": float(ledger.slippage_cost),
        "funding_cost": float(ledger.funding_cost),
        "bars_processed": len(trace),
        "bars_after_lock": bars_after_lock,
        "full_lock_invariant_passed": bool(full_lock_ok),
        "final_basket_net_pnl": float(ledger.basket_net_pnl(float(window[-1]["close"]))),
        "config": asdict(cfg),
    }
    return {"summary": summary, "trace": trace, "ledger": ledger}


def write_phase_a_outputs(
    result: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    trace_path = out / "per_bar_trace.csv"
    summary_path = out / "summary.json"

    with trace_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRACE_FIELDS)
        writer.writeheader()
        for row in result["trace"]:
            writer.writerow({k: row.get(k) for k in TRACE_FIELDS})

    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(result["summary"], handle, indent=2, sort_keys=True)
        handle.write("\n")

    return {"per_bar_trace_csv": trace_path, "summary_json": summary_path}


def run_phase_a_to_disk(
    cfg: EmergencyLockRecoveryConfig,
    candles: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = run_phase_a(cfg, candles=candles)
    paths = write_phase_a_outputs(result, cfg.output_dir)
    result["output_paths"] = {k: str(v) for k, v in paths.items()}
    return result

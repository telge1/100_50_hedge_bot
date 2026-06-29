"""Historical mini-backtest runner over 5m candle series (Phase 4)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from fixed_cycle_hedge_bot.models import FillEvent

from .backtest_report import BacktestResult, build_fill_log_entry, build_order_log_entry
from .hedge_bot_original_simulator import HedgeBotOriginalSimulator, Signal
from .simulated_order_book import SyntheticCandle


def normalize_candles(symbol: str, candles: Iterable[Any]) -> list[SyntheticCandle]:
    normalized: list[SyntheticCandle] = []
    for row in candles:
        if isinstance(row, SyntheticCandle):
            normalized.append(row)
        else:
            normalized.append(SyntheticCandle.from_row(symbol.upper(), dict(row)))
    return normalized


def _is_trade_closed(sim: HedgeBotOriginalSimulator) -> bool:
    return (
        sim.book.long_qty <= 1e-12
        and sim.book.short_qty <= 1e-12
        and not sim.book.active_orders()
    )


def _cycles_seen(strategy_state: dict[str, Any]) -> int | None:
    active = int(strategy_state.get("active_cycle_index") or 0)
    completed = int(strategy_state.get("completed_cycle_count") or 0)
    value = max(active, completed)
    return value if value > 0 else None


def _update_drawdown(
    *,
    cumulative_pnl: float,
    peak_pnl: float,
    max_drawdown: float,
) -> tuple[float, float]:
    peak = max(peak_pnl, cumulative_pnl)
    drawdown = peak - cumulative_pnl
    return peak, max(max_drawdown, drawdown)


def _append_fill_logs(
    result: BacktestResult,
    sim: HedgeBotOriginalSimulator,
    fills: list[FillEvent],
    *,
    timestamp: datetime | None,
) -> float:
    pnl_delta = 0.0
    for fill in fills:
        entry = build_fill_log_entry(fill, sim.book, timestamp=timestamp)
        result.fill_log.append(entry)
        pnl_delta += float(entry["closed_pnl"])
    result.fills_count = len(result.fill_log)
    return pnl_delta


def run_historical_backtest(
    symbol: str,
    direction: str,
    candles: Iterable[Any],
    *,
    max_candles: int | None = None,
    max_fills_per_candle: int = 1,
    conservative_fill_order: bool = True,
    initial_notional_usdt: float = 100.0,
) -> BacktestResult:
    """Run a conservative mini-backtest over a 5m candle series.

    Flow:
    1. Initialize original hedge strategy (long → FixedCycle, short → ShortFixedCycle).
    2. Entry fills at the first candle close.
    3. Process subsequent candles one-by-one with ``process_candle``.
    4. At most ``max_fills_per_candle`` resting fills per candle (default 1).
    5. Resting orders placed after a fill are only checked from the next candle.

    Stops when the trade is flat with no active orders, ``max_candles`` is reached,
    or an exception occurs.
    """
    signal: Signal = "short" if str(direction).lower() == "short" else "long"
    symbol_upper = symbol.upper()
    candle_list = normalize_candles(symbol_upper, candles)
    if not candle_list:
        return BacktestResult(
            symbol=symbol_upper,
            direction=signal,
            final_status="error",
            exit_reason="no_candles",
            error="candle series is empty",
        )

    first_candle = candle_list[0]
    sim = HedgeBotOriginalSimulator(
        signal=signal,
        symbol=symbol_upper,
        candle_close=float(first_candle.close),
    )
    result = BacktestResult(
        symbol=symbol_upper,
        direction=signal,
        start_time=first_candle.timestamp,
        entry_price=float(first_candle.close),
    )

    cumulative_pnl = 0.0
    peak_pnl = 0.0
    max_drawdown = 0.0

    try:
        entry_result = sim.run_entry_smoke()
        result.orders_submitted = sim.orders_submitted
        for order in entry_result.resting_orders:
            result.order_log.append(
                build_order_log_entry(order, timestamp=first_candle.timestamp)
            )

        entry_pnl = _append_fill_logs(
            result,
            sim,
            entry_result.entry_fills,
            timestamp=first_candle.timestamp,
        )
        cumulative_pnl += entry_pnl
        peak_pnl, max_drawdown = _update_drawdown(
            cumulative_pnl=cumulative_pnl,
            peak_pnl=peak_pnl,
            max_drawdown=max_drawdown,
        )

        loop_candles = candle_list[1:]
        if max_candles is not None:
            loop_candles = loop_candles[: max(0, int(max_candles))]

        for candle in loop_candles:
            sim.candle = candle
            candle_result = sim.process_candle(
                candle,
                max_fills_per_candle=max_fills_per_candle,
                conservative_fill_order=conservative_fill_order,
            )
            result.candles_processed += 1
            result.end_time = candle.timestamp

            pnl_delta = _append_fill_logs(
                result,
                sim,
                candle_result.candle_fills,
                timestamp=candle.timestamp,
            )
            cumulative_pnl += pnl_delta
            peak_pnl, max_drawdown = _update_drawdown(
                cumulative_pnl=cumulative_pnl,
                peak_pnl=peak_pnl,
                max_drawdown=max_drawdown,
            )
            result.orders_submitted = sim.orders_submitted

            if _is_trade_closed(sim):
                result.final_status = "closed"
                result.exit_reason = "flat_no_active_orders"
                break
        else:
            if max_candles is not None and result.candles_processed >= int(max_candles):
                result.final_status = "max_candles"
                result.exit_reason = "max_candles_reached"
            else:
                result.final_status = "open"
                result.exit_reason = "series_end_with_open_positions"

        result.realized_pnl = cumulative_pnl
        if initial_notional_usdt > 0:
            result.realized_pnl_pct = (cumulative_pnl / float(initial_notional_usdt)) * 100.0
            result.max_drawdown_pct = (max_drawdown / float(initial_notional_usdt)) * 100.0
        result.active_orders_count = len(sim.book.active_orders())
        result.cycles_seen = _cycles_seen(dict(sim.runtime_state.strategy_state))
    except Exception as exc:
        result.final_status = "error"
        result.exit_reason = "exception"
        result.error = str(exc)
    finally:
        sim.close()

    if result.end_time is None:
        result.end_time = first_candle.timestamp
    return result

"""Historical mini-backtest runner over 5m candle series (Phase 4/7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from fixed_cycle_hedge_bot.models import FillEvent

from .backtest_config_loader import (
    DEFAULT_LONG_CONFIG_PATH,
    DEFAULT_SHORT_CONFIG_PATH,
    ConfigSource,
    apply_config_load_result_to_backtest_result,
    resolve_backtest_config,
)
from .backtest_report import BacktestResult, build_fill_log_entry
from .debug_report import finalize_backtest_debug
from .fill_models import resolve_fill_model_config
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
    candle: SyntheticCandle | None,
    candle_index: int | None,
) -> float:
    pnl_delta = 0.0
    for fill in fills:
        metadata = dict(fill.metadata or {})
        entry = build_fill_log_entry(
            fill,
            sim.book,
            timestamp=fill.occurred_at,
            candle_index=candle_index,
            candle=candle,
            order_check_price=metadata.get("order_check_price"),
        )
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
    fill_model: str = "conservative",
    max_fills_per_candle: int | None = None,
    conservative_fill_order: bool = True,
    initial_notional_usdt: float = 100.0,
    config_source: ConfigSource = "test",
    long_config_path: str | Path = DEFAULT_LONG_CONFIG_PATH,
    short_config_path: str | Path = DEFAULT_SHORT_CONFIG_PATH,
    file_config_path: str | Path | None = None,
) -> BacktestResult:
    """Run a mini-backtest over a 5m candle series."""
    signal: Signal = "short" if str(direction).lower() == "short" else "long"
    symbol_upper = symbol.upper()
    candle_list = normalize_candles(symbol_upper, candles)
    fill_config = resolve_fill_model_config(
        fill_model=fill_model,
        max_fills_per_candle=max_fills_per_candle,
    )
    if not candle_list:
        return BacktestResult(
            symbol=symbol_upper,
            direction=signal,
            final_status="error",
            exit_reason="no_candles",
            error="candle series is empty",
            open_reason_detail="no_candles",
            fill_model=fill_config.fill_model,
            max_fills_per_candle=fill_config.max_fills_per_candle,
        )

    first_candle = candle_list[0]
    config_load = resolve_backtest_config(
        config_source=config_source,
        signal=signal,
        symbol=symbol_upper,
        long_config_path=long_config_path,
        short_config_path=short_config_path,
        file_config_path=file_config_path,
    )
    sim = HedgeBotOriginalSimulator(
        signal=signal,
        symbol=symbol_upper,
        candle_close=float(first_candle.close),
        config_load=config_load,
    )
    sim.candle_index = 0
    result = BacktestResult(
        symbol=symbol_upper,
        direction=signal,
        start_time=first_candle.timestamp,
        entry_price=float(first_candle.close),
        fill_model=fill_config.fill_model,
        max_fills_per_candle=fill_config.max_fills_per_candle,
    )
    apply_config_load_result_to_backtest_result(result, config_load)

    cumulative_pnl = 0.0
    peak_pnl = 0.0
    max_drawdown = 0.0

    try:
        entry_result = sim.run_entry_smoke()
        result.orders_submitted = sim.orders_submitted

        entry_pnl = _append_fill_logs(
            result,
            sim,
            entry_result.entry_fills,
            candle=first_candle,
            candle_index=0,
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

        for loop_index, candle in enumerate(loop_candles, start=1):
            sim.candle = candle
            sim.candle_index = loop_index
            candle_result = sim.process_candle(
                candle,
                fill_model=fill_config.fill_model,
                max_fills_per_candle=fill_config.max_fills_per_candle,
                conservative_fill_order=conservative_fill_order,
            )
            result.candles_processed += 1
            result.end_time = candle.timestamp

            if candle_result.same_candle_fill_count > 1:
                result.same_candle_fills_count += 1
            result.paired_exit_fills_count += candle_result.paired_exit_fills_count

            pnl_delta = _append_fill_logs(
                result,
                sim,
                candle_result.candle_fills,
                candle=candle,
                candle_index=loop_index,
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
        result.cycles_seen = _cycles_seen(dict(sim.runtime_state.strategy_state))
        finalize_backtest_debug(result, sim, candles=candle_list)
    except Exception as exc:
        result.final_status = "error"
        result.exit_reason = "exception"
        result.error = str(exc)
        result.open_reason_detail = f"error:{exc}"
        try:
            finalize_backtest_debug(result, sim, candles=candle_list)
        except Exception:
            pass
    finally:
        sim.close()

    if result.end_time is None:
        result.end_time = first_candle.timestamp
    return result

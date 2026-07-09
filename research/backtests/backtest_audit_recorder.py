from __future__ import annotations

"""Backtest-only runtime audit recorder for fills and addon events.

Phase 1: only minimal data model and fill instrumentation.
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


@dataclass
class FillAuditRecord:
    # Identity / sequencing
    global_event_sequence: int
    event_sequence_in_candle: int
    candle_index: Optional[int]
    order_created_timestamp: Optional[str]
    fill_timestamp: Optional[str]
    event_type: str
    order_id: str
    order_purpose: Optional[str]
    order_side: Optional[str]
    reduce_only: bool

    # Fill data
    requested_qty: float
    executed_qty: float
    fill_price: float
    created_candle_index: Optional[int]
    fill_candle_index: Optional[int]

    # Main position before fill
    long_qty_before: float
    long_avg_before: float
    short_qty_before: float
    short_avg_before: float

    # Main position after fill
    long_qty_after: float
    long_avg_after: float
    short_qty_after: float
    short_avg_after: float

    # PnL components
    closed_pnl: float
    gross_pnl: Optional[float]
    entry_fee: Optional[float]
    exit_fee: Optional[float]
    fee_rate: Optional[float]

    # Misc
    record_source: str = "SimulatedOrderBook.apply_fill"
    runtime_logged: bool = True


@dataclass
class AddonAuditRecord:
    """Addon recovery runtime audit record (Phase 2).

    This is a backtest-only data model that captures detailed pre/post state for
    Blocker Addon Short Recovery, including:

    - full addon-short subaccount state
    - main-book state around the event
    - entry / close parameters
    - profit-usage / long-reduce linkage
    - aggregate PnL / usage statistics
    """

    global_event_sequence: int
    event_sequence_in_candle: int
    candle_index: Optional[int]
    event_timestamp: Optional[str]
    event_type: str
    event_reason: Optional[str]
    record_source: str
    runtime_logged: bool = True

    # Trade / recovery identity
    trade_id: Optional[str] = None
    addon_trade_id: Optional[int] = None
    recovery_active_before: Optional[bool] = None
    recovery_active_after: Optional[bool] = None
    recovery_activation_candle_index: Optional[int] = None
    recovery_completion_candle_index: Optional[int] = None
    recovery_completed_before: Optional[bool] = None
    recovery_completed_after: Optional[bool] = None

    # Addon state before event
    has_open_addon_short_before: Optional[bool] = None
    addon_short_qty_before: Optional[float] = None
    addon_short_entry_price_before: Optional[float] = None
    addon_short_avg_before: Optional[float] = None
    addon_short_trade_count_before: Optional[int] = None
    previous_low_before: Optional[float] = None
    lowest_price_since_entry_before: Optional[float] = None
    maximum_favorable_move_pct_before: Optional[float] = None
    last_addon_close_price_before: Optional[float] = None
    last_addon_close_candle_index_before: Optional[int] = None

    # Addon state after event
    has_open_addon_short_after: Optional[bool] = None
    addon_short_qty_after: Optional[float] = None
    addon_short_entry_price_after: Optional[float] = None
    addon_short_avg_after: Optional[float] = None
    addon_short_trade_count_after: Optional[int] = None
    previous_low_after: Optional[float] = None
    lowest_price_since_entry_after: Optional[float] = None
    maximum_favorable_move_pct_after: Optional[float] = None
    last_addon_close_price_after: Optional[float] = None
    last_addon_close_candle_index_after: Optional[int] = None

    # Main-book state around event
    long_qty_before: Optional[float] = None
    long_avg_before: Optional[float] = None
    normal_short_qty_before: Optional[float] = None
    normal_short_avg_before: Optional[float] = None
    long_qty_after: Optional[float] = None
    long_avg_after: Optional[float] = None
    normal_short_qty_after: Optional[float] = None
    normal_short_avg_after: Optional[float] = None
    combined_short_qty_before: Optional[float] = None
    combined_short_qty_after: Optional[float] = None
    remaining_gap_before: Optional[float] = None
    remaining_gap_after: Optional[float] = None

    # Entry / reentry specific
    requested_entry_qty: Optional[float] = None
    executed_entry_qty: Optional[float] = None
    entry_price: Optional[float] = None
    entry_trigger_price: Optional[float] = None
    entry_reference_low: Optional[float] = None
    entry_distance_pct: Optional[float] = None
    first_entry_or_reentry: Optional[str] = None
    reentry_buffer_pct: Optional[float] = None
    remaining_gap_before_entry: Optional[float] = None
    remaining_gap_after_entry: Optional[float] = None

    # Close specific
    requested_close_qty: Optional[float] = None
    executed_close_qty: Optional[float] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None
    tp_price: Optional[float] = None
    rebound_price: Optional[float] = None
    hard_stop_price: Optional[float] = None
    maximum_favorable_move_pct_at_close: Optional[float] = None
    gross_pnl: Optional[float] = None
    net_pnl: Optional[float] = None
    fee_rate: Optional[float] = None
    entry_fee: Optional[float] = None
    exit_fee: Optional[float] = None

    # Profit usage / long-reduce linkage
    configured_profit_usage_fraction: Optional[float] = None
    short_profit_available: Optional[float] = None
    short_profit_usable: Optional[float] = None
    long_loss_per_unit: Optional[float] = None
    raw_reduce_qty: Optional[float] = None
    requested_reduce_qty: Optional[float] = None
    executed_reduce_qty: Optional[float] = None
    reduce_price: Optional[float] = None
    long_avg_before_reduce: Optional[float] = None
    long_qty_before_reduce: Optional[float] = None
    long_qty_after_reduce: Optional[float] = None
    long_reduce_closed_pnl: Optional[float] = None
    associated_addon_trade_id: Optional[int] = None
    associated_addon_close_event_sequence: Optional[int] = None

    # Aggregates before/after
    addon_short_realized_profit_before: Optional[float] = None
    addon_short_realized_profit_after: Optional[float] = None
    addon_short_realized_loss_before: Optional[float] = None
    addon_short_realized_loss_after: Optional[float] = None
    addon_short_net_realized_pnl_before: Optional[float] = None
    addon_short_net_realized_pnl_after: Optional[float] = None
    long_reduce_total_qty_before: Optional[float] = None
    long_reduce_total_qty_after: Optional[float] = None
    long_reduce_total_pnl_before: Optional[float] = None
    long_reduce_total_pnl_after: Optional[float] = None

    # Direct linkage to concrete fill records (for long-reduce execution).
    related_fill_order_id: Optional[str] = None
    related_fill_event_sequence: Optional[int] = None
    related_fill_event_sequence_in_candle: Optional[int] = None


@dataclass
class BacktestAuditRecorder:
    """Backtest-only audit recorder for a single backtest run.

    - Optional and disabled by default.
    - Does not influence strategy decisions or fill ordering.
    """

    enabled: bool = False
    fills: List[FillAuditRecord] = field(default_factory=list)
    addon_events: List[AddonAuditRecord] = field(default_factory=list)

    _global_event_sequence: int = 0
    _event_sequence_in_candle: int = 0
    _current_candle_index: Optional[int] = None

    def next_event_sequence(self, candle_index: Optional[int]) -> Tuple[int, int]:
        """Return (global_seq, seq_in_candle) for the next event.

        - global_event_sequence is strictly monotonically increasing.
        - event_sequence_in_candle resets to 1 when candle_index changes.
        """
        if not self.enabled:
            # Even when disabled, keep internal counters unchanged and
            # return 0, 0 to signal "no sequencing".
            return 0, 0

        if candle_index is None or candle_index != self._current_candle_index:
            self._current_candle_index = candle_index
            self._event_sequence_in_candle = 0

        self._global_event_sequence += 1
        self._event_sequence_in_candle += 1
        return self._global_event_sequence, self._event_sequence_in_candle

    def record_fill(self, record: FillAuditRecord) -> None:
        if not self.enabled:
            return
        self.fills.append(record)

    def record_addon_event(self, record: AddonAuditRecord) -> None:
        if not self.enabled:
            return
        self.addon_events.append(record)


"""Immutable Strategy Lab V2 run/trade result contracts (P2A).

No execution, adapters, loaders, or PnL simulation. Values are carried as
typed facts from a future adapter; arithmetic is not recomputed here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.phase1_contracts import (
    VersionedUniverseRefV2,
)
from orderbook_analyse.strategy_lab.models.enums import ModelingStatus, RateUnit, SideName
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion, StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import RateValue, TimeframeValue

_STRATEGY_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ZERO_OFFSET = timedelta(0)


class TradeExitReasonV2(str, Enum):
    """Closed exit reasons from the EDC canonical TP/SL engine.

    Legacy source: ``tpsl_pnl_engine.simulate_tpsl_trade`` /
    ``shared_strategy.outcomes.simulate_canonical_trade``.

    Adapter mapping (legacy → V2):
      - ``TP_EXIT`` → ``tp_exit``
      - ``SL_EXIT`` → ``sl_exit``
      - ``TIME_EXIT`` → ``time_exit``
      - ``COVERAGE_MISSING`` → ``coverage_missing``
      - ``INCOMPLETE_OUTCOME_HORIZON`` → ``incomplete_outcome_horizon``
    """

    TP_EXIT = "tp_exit"
    SL_EXIT = "sl_exit"
    TIME_EXIT = "time_exit"
    COVERAGE_MISSING = "coverage_missing"
    INCOMPLETE_OUTCOME_HORIZON = "incomplete_outcome_horizon"


class StrategyRunStatusV2(str, Enum):
    """Closed run statuses from the EDC multicoin coin-backtest path.

    Legacy sources:
      - ``coin_backtest.run_one_coin`` → ``COMPLETE``, ``FAILED_PARITY``
      - ``multicoin_frozen_validation.runner`` / ``checkpoint`` → ``FAILED``

    Adapter mapping (legacy → V2):
      - ``COMPLETE`` → ``complete``
      - ``FAILED`` → ``failed``
      - ``FAILED_PARITY`` → ``failed_parity``
    """

    COMPLETE = "complete"
    FAILED = "failed"
    FAILED_PARITY = "failed_parity"


_RESOLVED_EXIT_REASONS = frozenset(
    {
        TradeExitReasonV2.TP_EXIT,
        TradeExitReasonV2.SL_EXIT,
        TradeExitReasonV2.TIME_EXIT,
    }
)
_UNRESOLVED_EXIT_REASONS = frozenset(
    {
        TradeExitReasonV2.COVERAGE_MISSING,
        TradeExitReasonV2.INCOMPLETE_OUTCOME_HORIZON,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceEventIdV2:
    """Opaque candidate/event id from legacy engines (not ``StableIdentifier``).

    Legacy forms (preserved verbatim, never normalized):
      - EDC: ``edc:`` + sha1 hex prefix (``ema_candidate.make_candidate_id``)
      - Cluster: ``csw:`` + sha1 hex prefix (``event_detector.make_event_id``)
    """

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise TypeError("SourceEventIdV2.value must be exact str")
        if not self.value or self.value != self.value.strip():
            raise ValueError(
                "SourceEventIdV2.value must be non-empty without padding"
            )


def _require_utc_datetime(value: object, *, field_name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{field_name} must be exact datetime")
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    offset = value.utcoffset()
    if offset is None or offset != _ZERO_OFFSET:
        raise ValueError(f"{field_name} must be UTC (zero offset); no normalization")
    return value


def _require_decimal(value: object, *, field_name: str) -> Decimal:
    if type(value) is not Decimal:
        raise TypeError(
            f"{field_name} must be exact Decimal (float/int/bool not accepted)"
        )
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategyTradeV2:
    """One isolated strategy trade outcome (no market simulation in this type)."""

    source_event_id: SourceEventIdV2
    symbol: str
    side: SideName
    decision_time: datetime
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime | None
    exit_price: Decimal | None
    exit_reason: TradeExitReasonV2
    gross_return_pct: Decimal | None
    roundtrip_cost_pct: Decimal
    net_return_pct: Decimal | None
    gross_pnl_usdt: Decimal | None
    costs_usdt: Decimal | None
    net_pnl_usdt: Decimal | None
    mode_id: StableIdentifier | None
    confirmation_policy: ResearchConfirmationPolicyV2 | None

    def __post_init__(self) -> None:
        if type(self.source_event_id) is not SourceEventIdV2:
            raise TypeError("source_event_id must be SourceEventIdV2")
        if type(self.symbol) is not str:
            raise TypeError("symbol must be exact str")
        if not self.symbol or self.symbol != self.symbol.strip():
            raise ValueError("symbol must be non-empty without padding")
        if type(self.side) is not SideName:
            raise TypeError("side must be SideName")
        if type(self.exit_reason) is not TradeExitReasonV2:
            raise TypeError("exit_reason must be TradeExitReasonV2")
        if self.mode_id is not None and type(self.mode_id) is not StableIdentifier:
            raise TypeError("mode_id must be StableIdentifier or None")
        if self.confirmation_policy is not None and type(
            self.confirmation_policy
        ) is not ResearchConfirmationPolicyV2:
            raise TypeError(
                "confirmation_policy must be ResearchConfirmationPolicyV2 or None"
            )

        decision_time = _require_utc_datetime(
            self.decision_time, field_name="decision_time"
        )
        entry_time = _require_utc_datetime(self.entry_time, field_name="entry_time")
        if entry_time < decision_time:
            raise ValueError("entry_time must be >= decision_time")

        entry_price = _require_decimal(self.entry_price, field_name="entry_price")
        if entry_price <= 0:
            raise ValueError("entry_price must be > 0")

        roundtrip_cost_pct = _require_decimal(
            self.roundtrip_cost_pct, field_name="roundtrip_cost_pct"
        )
        if roundtrip_cost_pct < 0:
            raise ValueError("roundtrip_cost_pct must be >= 0")

        if self.exit_reason in _RESOLVED_EXIT_REASONS:
            # Closed contract: tp/sl/time exits always carry a full outcome.
            if self.exit_time is None:
                raise ValueError("resolved exits require exit_time")
            exit_time = _require_utc_datetime(self.exit_time, field_name="exit_time")
            if exit_time < entry_time:
                raise ValueError("exit_time must be >= entry_time")
            if self.exit_price is None:
                raise ValueError("resolved exits require exit_price")
            exit_price = _require_decimal(self.exit_price, field_name="exit_price")
            if exit_price <= 0:
                raise ValueError("exit_price must be > 0")
            for name in (
                "gross_return_pct",
                "net_return_pct",
                "gross_pnl_usdt",
                "costs_usdt",
                "net_pnl_usdt",
            ):
                value = getattr(self, name)
                if value is None:
                    raise ValueError(f"resolved exits require {name}")
                _require_decimal(value, field_name=name)
        elif self.exit_reason in _UNRESOLVED_EXIT_REASONS:
            # Matches tpsl_pnl_engine + apply_costs when gross_return_pct is None:
            # exit/pnl fields absent; roundtrip_cost_pct remains explicit.
            if self.exit_time is not None:
                raise ValueError("unresolved exits require exit_time is None")
            if self.exit_price is not None:
                raise ValueError("unresolved exits require exit_price is None")
            for name in (
                "gross_return_pct",
                "net_return_pct",
                "gross_pnl_usdt",
                "costs_usdt",
                "net_pnl_usdt",
            ):
                if getattr(self, name) is not None:
                    raise ValueError(f"unresolved exits require {name} is None")
        else:  # pragma: no cover — enum is closed
            raise ValueError(f"unsupported exit_reason: {self.exit_reason!r}")

        # Intentionally no gross−cost / pnl arithmetic checks: legacy engine uses
        # float + round(..., 6); exact Decimal invariants belong in adapter parity.


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategyRunResultV2:
    """One compiled-strategy execution window result (no summary duplication)."""

    strategy_hash: str
    plugin_id: StableIdentifier
    plugin_contract_version: ContractVersion
    universe: VersionedUniverseRefV2
    start: datetime
    end: datetime
    symbols: tuple[str, ...]
    signal_timeframe: TimeframeValue
    execution_timeframe: TimeframeValue
    roundtrip_cost: RateValue
    slippage_status: ModelingStatus
    funding_status: ModelingStatus
    status: StrategyRunStatusV2
    candidate_count: int
    trades: tuple[StrategyTradeV2, ...]

    def __post_init__(self) -> None:
        if type(self.strategy_hash) is not str:
            raise TypeError("strategy_hash must be exact str")
        if not _STRATEGY_HASH_PATTERN.fullmatch(self.strategy_hash):
            raise ValueError(
                "strategy_hash must be exactly 64 lowercase hex characters"
            )
        if type(self.plugin_id) is not StableIdentifier:
            raise TypeError("plugin_id must be StableIdentifier")
        if type(self.plugin_contract_version) is not ContractVersion:
            raise TypeError("plugin_contract_version must be ContractVersion")
        if type(self.universe) is not VersionedUniverseRefV2:
            raise TypeError("universe must be VersionedUniverseRefV2")
        if type(self.signal_timeframe) is not TimeframeValue:
            raise TypeError("signal_timeframe must be TimeframeValue")
        if type(self.execution_timeframe) is not TimeframeValue:
            raise TypeError("execution_timeframe must be TimeframeValue")
        if type(self.roundtrip_cost) is not RateValue:
            raise TypeError("roundtrip_cost must be RateValue")
        if self.roundtrip_cost.unit is not RateUnit.PERCENT:
            raise ValueError("roundtrip_cost.unit must be RateUnit.PERCENT")
        if type(self.slippage_status) is not ModelingStatus:
            raise TypeError("slippage_status must be ModelingStatus")
        if type(self.funding_status) is not ModelingStatus:
            raise TypeError("funding_status must be ModelingStatus")
        if type(self.status) is not StrategyRunStatusV2:
            raise TypeError("status must be StrategyRunStatusV2")
        if type(self.candidate_count) is not int:
            raise TypeError(
                "candidate_count must be exact int (bool not accepted)"
            )
        if self.candidate_count < 0:
            raise ValueError("candidate_count must be >= 0")
        if type(self.trades) is not tuple:
            raise TypeError("trades must be a tuple")
        for trade in self.trades:
            if type(trade) is not StrategyTradeV2:
                raise TypeError("trades must contain only StrategyTradeV2")

        start = _require_utc_datetime(self.start, field_name="start")
        end = _require_utc_datetime(self.end, field_name="end")
        if end <= start:
            raise ValueError("end must be > start")

        if type(self.symbols) is not tuple:
            raise TypeError("symbols must be a tuple")
        if not self.symbols:
            raise ValueError("symbols must be non-empty")
        seen: set[str] = set()
        for symbol in self.symbols:
            if type(symbol) is not str:
                raise TypeError("symbols entries must be exact str")
            if not symbol or symbol != symbol.strip():
                raise ValueError(
                    "symbols entries must be non-empty without padding"
                )
            if symbol in seen:
                raise ValueError(f"duplicate symbol in symbols: {symbol!r}")
            seen.add(symbol)

        trade_count = len(self.trades)
        # Lab runs emit at most one trade per source event for a single compiled
        # strategy cell (unlike the legacy multi-cell matrix).
        if self.candidate_count < trade_count:
            raise ValueError("candidate_count must be >= trade_count")

        allowed_symbols = set(self.symbols)
        for trade in self.trades:
            if trade.symbol not in allowed_symbols:
                raise ValueError(
                    f"trade symbol {trade.symbol!r} is not in run symbols"
                )
            # Signal filter in legacy is half-open on candidate/event open time
            # ``[start, end)``. ``decision_time`` is signal-bar close and may equal
            # or exceed ``end`` when the bar straddles the exclusive end. Exit
            # (and near-window entry) may also fall after ``end`` due to outcome
            # padding — never enforce exit_time <= end or decision_time < end.
            if trade.decision_time < start:
                raise ValueError("trade.decision_time must be >= run.start")

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def gross_pnl_usdt(self) -> Decimal:
        total = Decimal("0")
        for trade in self.trades:
            if trade.gross_pnl_usdt is not None:
                total += trade.gross_pnl_usdt
        return total

    @property
    def costs_usdt(self) -> Decimal:
        total = Decimal("0")
        for trade in self.trades:
            if trade.costs_usdt is not None:
                total += trade.costs_usdt
        return total

    @property
    def net_pnl_usdt(self) -> Decimal:
        total = Decimal("0")
        for trade in self.trades:
            if trade.net_pnl_usdt is not None:
                total += trade.net_pnl_usdt
        return total

    @property
    def symbols_with_trades(self) -> tuple[str, ...]:
        return tuple(sorted({trade.symbol for trade in self.trades}))

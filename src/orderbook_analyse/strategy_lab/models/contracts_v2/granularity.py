"""Data granularity union for V2 data requirements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from orderbook_analyse.strategy_lab.models.strategy import DurationValue, TimeframeValue


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeframeGranularityV2:
    """Candle or aggregated bar timeframe granularity."""

    _schema_kind: ClassVar[str] = "timeframe"
    timeframe: TimeframeValue


@dataclass(frozen=True, slots=True, kw_only=True)
class EventStreamGranularityV2:
    """Native event stream without candle timeframe."""

    _schema_kind: ClassVar[str] = "event_stream"
    native_event_stream: bool

    def __post_init__(self) -> None:
        if self.native_event_stream is not True:
            raise ValueError(
                "EventStreamGranularityV2.native_event_stream must be True"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class SnapshotGranularityV2:
    """Point-in-time snapshot aligned to a bar boundary."""

    _schema_kind: ClassVar[str] = "snapshot"
    aligned_timeframe: TimeframeValue


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectedSignalTimeframeGranularityV2:
    """Granularity bound to the strategy's chosen signal timeframe."""

    _schema_kind: ClassVar[str] = "selected_signal_timeframe"
    binds_to_selected_signal_timeframe: bool

    def __post_init__(self) -> None:
        if self.binds_to_selected_signal_timeframe is not True:
            raise ValueError(
                "SelectedSignalTimeframeGranularityV2."
                "binds_to_selected_signal_timeframe must be True"
            )


DataGranularityV2 = (
    TimeframeGranularityV2
    | EventStreamGranularityV2
    | SnapshotGranularityV2
    | SelectedSignalTimeframeGranularityV2
)

_DATA_GRANULARITY_V2_TYPES: tuple[type, ...] = (
    TimeframeGranularityV2,
    EventStreamGranularityV2,
    SnapshotGranularityV2,
    SelectedSignalTimeframeGranularityV2,
)

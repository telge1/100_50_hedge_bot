"""ClickHouse database layer for the signal generator store."""

from __future__ import annotations

from signal_generator.db.candles import CandleRepository
from signal_generator.db.client import ClickHouseClient, get_client
from signal_generator.db.outcomes import SignalOutcomeRepository
from signal_generator.db.processing_state import ProcessingState, ProcessingStateRepository
from signal_generator.db.setup import apply_migrations, setup_clickhouse
from signal_generator.db.signals import SignalRepository

__all__ = [
    "CandleRepository",
    "ClickHouseClient",
    "ProcessingState",
    "ProcessingStateRepository",
    "SignalOutcomeRepository",
    "SignalRepository",
    "apply_migrations",
    "get_client",
    "setup_clickhouse",
]

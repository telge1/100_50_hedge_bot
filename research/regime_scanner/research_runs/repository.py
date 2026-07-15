"""Read-only repository API for research runs."""

from __future__ import annotations

from typing import Any

from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
from research.regime_scanner.research_runs.compare import compare_runs as _compare_runs
from research.regime_scanner.research_runs.store_memory import InMemoryResearchStore
from research.regime_scanner.research_runs.store_mysql import MySQLResearchStore

_STORE: Any | None = None


def _get_store() -> Any:
    global _STORE
    if _STORE is not None:
        return _STORE
    load_regime_db_env_file()
    config = load_regime_db_config()
    _STORE = MySQLResearchStore(config)
    return _STORE


def get_run(run_id: str) -> dict[str, Any] | None:
    return _get_store().get_run(run_id)


def list_runs(
    *,
    symbol: str | None = None,
    status: str | None = None,
    parameter_hash: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return _get_store().list_runs(
        symbol=symbol,
        status=status,
        parameter_hash=parameter_hash,
        limit=limit,
    )


def load_trend_states(run_id: str) -> list[dict[str, Any]]:
    return _get_store().load_trend_states(run_id)


def load_structure_events(run_id: str) -> list[dict[str, Any]]:
    return _get_store().load_structure_events(run_id)


def load_signals(run_id: str) -> list[dict[str, Any]]:
    return _get_store().load_signals(run_id)


def compare_runs(run_id_a: str, run_id_b: str) -> dict[str, Any]:
    return _compare_runs(_get_store(), run_id_a, run_id_b)


def memory_store() -> InMemoryResearchStore:
    return InMemoryResearchStore()

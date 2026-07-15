"""In-memory research-run store for unit tests."""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from research.regime_scanner.research_runs.parameters import parameters_json
from research.regime_scanner.research_runs.schema import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class InMemoryResearchStore:
    parameter_sets: dict[str, dict[str, Any]] = field(default_factory=dict)
    parameter_set_ids: dict[str, int] = field(default_factory=dict)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    trend_states: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    structure_events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    signals: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    metrics: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _next_param_id: int = 1

    def init_schema(self) -> None:
        return None

    def close(self) -> None:
        return None

    def ensure_parameter_set(
        self, *, parameter_hash: str, scanner_name: str, params: Any
    ) -> int:
        if parameter_hash in self.parameter_set_ids:
            return self.parameter_set_ids[parameter_hash]
        pid = self._next_param_id
        self._next_param_id += 1
        self.parameter_set_ids[parameter_hash] = pid
        self.parameter_sets[parameter_hash] = {
            "id": pid,
            "parameter_hash": parameter_hash,
            "scanner_name": scanner_name,
            "parameters_json": parameters_json(params),
            "created_at": _utcnow(),
        }
        return pid

    def create_running_run(self, row: dict[str, Any]) -> None:
        run_id = str(row["run_id"])
        if run_id in self.runs:
            raise ValueError(f"run already exists: {run_id}")
        self.runs[run_id] = copy.deepcopy(row)
        self.runs[run_id]["status"] = RUN_STATUS_RUNNING

    def save_completed_run(
        self,
        *,
        run_id: str,
        updates: dict[str, Any],
        trend_states: list[dict[str, Any]],
        structure_events: list[dict[str, Any]],
        signals: list[dict[str, Any]],
        metrics: list[dict[str, Any]],
    ) -> None:
        if run_id not in self.runs:
            raise ValueError(f"unknown run: {run_id}")
        merged = copy.deepcopy(self.runs[run_id])
        merged.update(updates)
        merged["status"] = RUN_STATUS_COMPLETED
        self._assert_unique_events(run_id, trend_states, "event_key", self.trend_states)
        self._assert_unique_events(run_id, structure_events, "event_key", self.structure_events)
        self._assert_unique_events(run_id, signals, "signal_key", self.signals)
        self.runs[run_id] = merged
        self.trend_states[run_id] = copy.deepcopy(trend_states)
        self.structure_events[run_id] = copy.deepcopy(structure_events)
        self.signals[run_id] = copy.deepcopy(signals)
        self.metrics[run_id] = copy.deepcopy(metrics)

    def mark_failed(self, run_id: str, *, error_type: str, error_message: str) -> None:
        if run_id not in self.runs:
            self.runs[run_id] = {"run_id": run_id}
        self.runs[run_id]["status"] = RUN_STATUS_FAILED
        self.runs[run_id]["error_type"] = error_type
        self.runs[run_id]["error_message"] = error_message[:4000]
        self.runs[run_id]["finished_at"] = _utcnow()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.runs.get(run_id)
        return copy.deepcopy(row) if row else None

    def find_run_by_fingerprint(
        self,
        run_fingerprint: str,
        *,
        status: str = "completed",
    ) -> dict[str, Any] | None:
        matches = [
            r
            for r in self.runs.values()
            if r.get("run_fingerprint") == run_fingerprint and r.get("status") == status
        ]
        if not matches:
            return None
        matches.sort(key=lambda r: r.get("started_at") or _utcnow(), reverse=True)
        return copy.deepcopy(matches[0])

    def list_runs(
        self,
        *,
        symbol: str | None = None,
        status: str | None = None,
        parameter_hash: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = list(self.runs.values())
        if symbol:
            rows = [r for r in rows if r.get("symbol") == symbol.upper()]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if parameter_hash:
            rows = [
                r
                for r in rows
                if self._param_hash_for_run(r) == parameter_hash
            ]
        rows.sort(key=lambda r: r.get("started_at") or _utcnow(), reverse=True)
        return copy.deepcopy(rows[:limit])

    def load_trend_states(self, run_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.trend_states.get(run_id, []))

    def load_structure_events(self, run_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.structure_events.get(run_id, []))

    def load_signals(self, run_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.signals.get(run_id, []))

    def count_candles(self) -> int:
        return 0

    def count_validation_runs(self) -> int:
        return 0

    @staticmethod
    def _assert_unique_events(
        run_id: str,
        rows: list[dict[str, Any]],
        key_field: str,
        store: dict[str, list[dict[str, Any]]],
    ) -> None:
        seen: set[str] = set()
        for row in rows:
            key = str(row[key_field])
            if key in seen:
                raise ValueError(f"duplicate {key_field} in run {run_id}: {key}")
            seen.add(key)

    def _param_hash_for_run(self, row: dict[str, Any]) -> str | None:
        pid = row.get("parameter_set_id")
        for ph, meta in self.parameter_sets.items():
            if meta["id"] == pid:
                return ph
        return None


def new_run_id() -> str:
    return str(uuid.uuid4())

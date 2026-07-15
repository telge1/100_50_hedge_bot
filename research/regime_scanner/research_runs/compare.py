"""Compare two research runs for reproducibility."""

from __future__ import annotations

from typing import Any, Protocol


class RunReader(Protocol):
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...
    def load_trend_states(self, run_id: str) -> list[dict[str, Any]]: ...
    def load_structure_events(self, run_id: str) -> list[dict[str, Any]]: ...
    def load_signals(self, run_id: str) -> list[dict[str, Any]]: ...


_COMPARE_FIELDS = (
    "run_fingerprint",
    "parameter_hash",
    "candle_hash_5m",
    "candle_hash_15m",
    "candle_hash_30m",
    "trend_state_hash",
    "structure_event_hash",
    "signal_hash",
    "combined_output_hash",
)


def compare_runs(
    reader: RunReader,
    run_id_a: str,
    run_id_b: str,
) -> dict[str, Any]:
    run_a = reader.get_run(run_id_a)
    run_b = reader.get_run(run_id_b)
    if run_a is None:
        raise ValueError(f"run not found: {run_id_a}")
    if run_b is None:
        raise ValueError(f"run not found: {run_id_b}")

    ph_a = run_a.get("parameter_hash_value") or run_a.get("parameter_hash")
    ph_b = run_b.get("parameter_hash_value") or run_b.get("parameter_hash")

    differences: list[dict[str, Any]] = []

    def _diff(field: str, a: Any, b: Any) -> None:
        if a != b:
            differences.append({"field": field, "a": a, "b": b})

    for field in _COMPARE_FIELDS:
        _diff(field, run_a.get(field), run_b.get(field))
    _diff("parameter_hash", ph_a, ph_b)

    counts_a = {
        "trend_states": len(reader.load_trend_states(run_id_a)),
        "structure_events": len(reader.load_structure_events(run_id_a)),
        "signals": len(reader.load_signals(run_id_a)),
    }
    counts_b = {
        "trend_states": len(reader.load_trend_states(run_id_b)),
        "structure_events": len(reader.load_structure_events(run_id_b)),
        "signals": len(reader.load_signals(run_id_b)),
    }
    for key in counts_a:
        _diff(f"{key}_count", counts_a[key], counts_b[key])

    if not differences and counts_a != counts_b:
        differences.append({"field": "row_counts", "a": counts_a, "b": counts_b})

    first = differences[0] if differences else None
    return {
        "equivalent": len(differences) == 0,
        "differences": differences,
        "first_divergence": first,
        "run_a": {
            "run_id": run_id_a,
            "run_fingerprint": run_a.get("run_fingerprint"),
            "parameter_hash": ph_a,
            "counts": counts_a,
        },
        "run_b": {
            "run_id": run_id_b,
            "run_fingerprint": run_b.get("run_fingerprint"),
            "parameter_hash": ph_b,
            "counts": counts_b,
        },
    }

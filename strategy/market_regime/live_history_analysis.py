from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Any, Iterable

import pymysql

from .db import MarketRegimeDBConfig, MarketRegimeStore
from .state_machine import _sanitize_routed_state


HORIZON_FIELDS = {
    "fast": "fast_state",
    "mid": "mid_state",
    "slow": "slow_state",
}

DECISION_TYPES = ("ALLOW", "WATCHLIST", "SKIP")

CONFIDENCE_BINS = (
    ("missing", None, None),
    ("<50", 0.0, 0.5),
    ("50-69", 0.5, 0.7),
    (">=70", 0.7, 1.0001),
)


def _percentage(value: float, total: int) -> float:
    return (value / total * 100.0) if total else 0.0


def _normalize_state(state: Any) -> str:
    normalized = str(state or "").strip()
    return normalized or "neutral"


def _normalize_decision(decision: Any) -> str:
    normalized = str(decision or "").strip().upper()
    return normalized or "UNKNOWN"


def _sanitize_confidence(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def aggregate_horizon_distribution(rows: Iterable[dict[str, Any]], *, top_states: int = 5) -> dict[str, dict[str, Any]]:
    rows_list = list(rows)
    result: dict[str, dict[str, Any]] = {}
    for horizon, field in HORIZON_FIELDS.items():
        result[horizon] = _aggregate_horizon(rows_list, field=field, total=len(rows_list), top_states=top_states)
    return result


def _aggregate_horizon(rows: list[dict[str, Any]], *, field: str, total: int, top_states: int) -> dict[str, Any]:
    state_counter = Counter(_normalize_state(row.get(field)) for row in rows)
    top_states_list = []
    for state, count in state_counter.most_common(top_states):
        top_states_list.append(
            {
                "state": state,
                "count": count,
                "percent": round(_percentage(count, total), 2),
            }
        )

    confidence_values = [value for value in (_sanitize_confidence(row.get("confidence")) for row in rows) if value is not None]
    confidence_summary = _build_confidence_summary(confidence_values, total)

    decision_distribution = _build_decision_distribution(rows, total)
    range_unclear_pct = _percentage(
        sum(1 for row in rows if _sanitize_routed_state(row.get("routed_state")) == "range_unclear"),
        total,
    )
    entry_allowed_pct = _percentage(
        sum(1 for row in rows if bool(row.get("entry_allowed"))),
        total,
    )

    return {
        "sample_count": total,
        "state_distribution": top_states_list,
        "confidence": confidence_summary,
        "decision_distribution": decision_distribution,
        "range_unclear_pct": round(range_unclear_pct, 2),
        "entry_allowed_pct": round(entry_allowed_pct, 2),
    }


def _build_confidence_summary(values: list[float], total_samples: int) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "sample_count": len(values),
        "average": None,
        "median": None,
        "min": None,
        "max": None,
        "bins": [],
    }
    if values:
        summary["average"] = round(sum(values) / len(values), 4)
        summary["median"] = round(median(values), 4)
        summary["min"] = round(min(values), 4)
        summary["max"] = round(max(values), 4)

    bin_counts: dict[str, int] = {name: 0 for name, *_ in CONFIDENCE_BINS}
    missing = max(total_samples - len(values), 0)
    bin_counts["missing"] = max(missing, 0)
    for value in values:
        for name, start, end in CONFIDENCE_BINS:
            if name == "missing":
                continue
            assert start is not None and end is not None
            if start <= value < end:
                bin_counts[name] = bin_counts.get(name, 0) + 1
                break

    summary["bins"] = [
        {
            "bin": name,
            "count": count,
            "percent": round(_percentage(count, total_samples), 2),
        }
        for name, count in bin_counts.items()
    ]
    return summary


def _build_decision_distribution(rows: list[dict[str, Any]], total: int) -> dict[str, dict[str, Any]]:
    counter = Counter(_normalize_decision(row.get("decision")) for row in rows)
    distribution: dict[str, dict[str, Any]] = {}
    for decision in DECISION_TYPES:
        count = counter.get(decision, 0)
        distribution[decision] = {
            "count": count,
            "percent": round(_percentage(count, total), 2),
        }
    other_count = sum(count for key, count in counter.items() if key not in DECISION_TYPES)
    distribution["OTHER"] = {
        "count": other_count,
        "percent": round(_percentage(other_count, total), 2),
    }
    return distribution


def compare_live_history_distributions(
    live_stats: dict[str, dict[str, Any]],
    history_stats: dict[str, dict[str, Any]] | None,
    *,
    max_state_deltas: int = 5,
) -> dict[str, dict[str, Any]]:
    comparison: dict[str, dict[str, Any]] = {}
    for horizon in HORIZON_FIELDS:
        live = live_stats.get(horizon)
        history = history_stats.get(horizon) if history_stats is not None else None
        comparison[horizon] = _compare_single_horizon(live, history, max_state_deltas)
    return comparison


def _compare_single_horizon(
    live: dict[str, Any] | None,
    history: dict[str, Any] | None,
    max_state_deltas: int,
) -> dict[str, Any]:
    if live is None:
        raise ValueError("Live stats are required for comparison")

    result = {
        "live": live,
        "history": history,
        "deltas": None,
    }
    if history is None:
        return result

    sample_delta = live["sample_count"] - history["sample_count"]
    range_delta = live["range_unclear_pct"] - history["range_unclear_pct"]
    entry_delta = live["entry_allowed_pct"] - history["entry_allowed_pct"]

    state_delta = _compute_state_delta(live["state_distribution"], history["state_distribution"], max_state_deltas)
    decision_delta = _compute_decision_delta(live["decision_distribution"], history["decision_distribution"])
    confidence_delta = _compute_confidence_delta(live["confidence"], history["confidence"])

    result["deltas"] = {
        "sample_count": sample_delta,
        "range_unclear_pct": round(range_delta, 2),
        "entry_allowed_pct": round(entry_delta, 2),
        "state_delta": state_delta,
        "decision_delta": decision_delta,
        "confidence_delta": confidence_delta,
    }
    return result


def _compute_state_delta(
    live_distribution: list[dict[str, Any]],
    history_distribution: list[dict[str, Any]],
    max_state_deltas: int,
) -> list[dict[str, Any]]:
    live_pct = {entry["state"]: entry["percent"] for entry in live_distribution}
    history_pct = {entry["state"]: entry["percent"] for entry in history_distribution}
    all_states = set(live_pct) | set(history_pct)
    delta_entries = []
    for state in all_states:
        delta_entries.append(
            {
                "state": state,
                "live_pct": round(live_pct.get(state, 0.0), 2),
                "history_pct": round(history_pct.get(state, 0.0), 2),
                "delta_pct": round(live_pct.get(state, 0.0) - history_pct.get(state, 0.0), 2),
            }
        )
    delta_entries.sort(key=lambda item: abs(item["delta_pct"]), reverse=True)
    return delta_entries[:max_state_deltas]


def _compute_decision_delta(
    live_distribution: dict[str, dict[str, Any]],
    history_distribution: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    delta_list = []
    all_decisions = set(live_distribution) | set(history_distribution)
    for decision in all_decisions:
        delta_list.append(
            {
                "decision": decision,
                "live_pct": round(live_distribution.get(decision, {}).get("percent", 0.0), 2),
                "history_pct": round(history_distribution.get(decision, {}).get("percent", 0.0), 2),
                "delta_pct": round(
                    live_distribution.get(decision, {}).get("percent", 0.0)
                    - history_distribution.get(decision, {}).get("percent", 0.0),
                    2,
                ),
            }
        )
    delta_list.sort(key=lambda item: abs(item["delta_pct"]), reverse=True)
    return delta_list


def _compute_confidence_delta(
    live_confidence: dict[str, Any],
    history_confidence: dict[str, Any],
) -> dict[str, float]:
    delta: dict[str, float] = {}
    for key in ("average", "median", "min", "max"):
        live_value = live_confidence.get(key)
        history_value = history_confidence.get(key)
        if live_value is not None and history_value is not None:
            delta[key] = round(live_value - history_value, 4)
    return delta


def load_history_rows(
    store: MarketRegimeStore,
    history_table: str | None,
    *,
    symbols: list[str] | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    if not history_table:
        return [], "history_table_not_specified"

    history_config = MarketRegimeDBConfig(
        host=store.config.host,
        port=store.config.port,
        user=store.config.user,
        password=store.config.password,
        database=store.config.database,
        raw_table=store.config.raw_table,
    )
    history_config.market_state_live_table = history_table
    history_store = MarketRegimeStore(config=history_config)
    try:
        rows = history_store.load_market_state_live_telemetry_rows(symbols=symbols, limit=limit)
        return rows, None
    except pymysql.MySQLError as err:
        return [], str(err)
    except Exception as err:  # pragma: no cover - defensive
        return [], str(err)

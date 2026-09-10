"""Full time/availability vocabulary for Episode-1 causal audit."""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt, last_complete_state
from .persist import event_available_at, ts_cell

# Research clocks (Episode 1 Full-OB archive):
# - exchange_event_time  := raw event_time_ns (exchange/event clock in the archive)
# - collector_received_at := raw receive_time_ns (local collector receive clock; EXISTS)
# - archive_time         := raw archive_time_ns
# Level-change event_available_at is a RESEARCH PROXY based on exchange_event_time
# ceil-to-100ms bucket end — it is NOT collector_received_at.

FIELD_VOCABULARY: dict[str, str] = {
    "exchange_event_time": (
        "Raw Full-OB event_time_ns converted to UTC. Exchange/event clock of the book update. "
        "Persisted as level_changes.event_time / book_resets.event_time."
    ),
    "collector_received_at": (
        "Raw Full-OB receive_time_ns converted to UTC. Local collector receive clock. "
        "EXISTS in the archive but is NOT persisted into sms1 level_changes and is NOT used "
        "as the research event_available_at for level changes."
    ),
    "raw_available_at": (
        "Research definition for book-reconstruction causality = exchange_event_time "
        "(exclusive as-of). Separately, collector_received_at is checked in the causal audit "
        "to confirm no collector look-ahead for Touch/Detection."
    ),
    "bucket_start": "Inclusive start of the 100ms state interval.",
    "bucket_end_exclusive": "Exclusive end of the 100ms state interval; equals state available_at.",
    "state_available_at": (
        "For 100ms rows: available_at = bucket_end_exclusive. "
        "Example: state for [20:19:02.100Z, 20:19:02.200Z) is available at 20:19:02.200Z. "
        "The in-progress bucket [20:19:02.200Z, 20:19:02.300Z) is only fully available at 20:19:02.300Z."
    ),
    "derived_event_time": "Semantic instant the derived statement refers to (touch/detection/wall as-of).",
    "derived_available_at": (
        "Persisted as event_available_at on WALL/TOUCH/DETECTION/REFILL. "
        "Must satisfy derived_available_at >= max(raw_available_at of used inputs) under the "
        "research exchange clock; collector receive is additionally verified in the audit."
    ),
    "detected_at": "On DETECTION rows: same as event_available_at for Episode 1.",
}

TIME_CONTRACT: dict[str, dict[str, str]] = {
    "states_100ms": {
        "event_time": "represented 100ms bucket (bucket_start)",
        "available_at": "bucket_end_exclusive; first time this finished state may be used",
        "bucket_end_exclusive": "same as available_at",
        "effective_bucket_start": "first instant of evidence in this bucket (partial first bucket)",
        "state_asof_exclusive": "reconstruct cutoff = available_at; events with exchange_event_time < cutoff",
    },
    "WALL": {
        "event_time": "instant the wall statement refers to (Episode-1: first_touch as-of book)",
        "event_available_at": "derived_available_at = earliest causal availability of that as-of book",
        "state_available_at": "last finished 100ms state with available_at <= event_time",
        "detected_at": "not used",
    },
    "LEVEL_CHANGE": {
        "event_time": "exchange_event_time of the raw book update",
        "event_available_at": (
            "RESEARCH PROXY = ceil_100ms(exchange_event_time) bucket end; "
            "not collector_received_at"
        ),
        "state_available_at": "same as event_available_at",
        "detected_at": "not used",
    },
    "REFILL": {
        "event_time": "exchange_event_time of the restoring book-update",
        "event_available_at": "RESEARCH PROXY = ceil_100ms(exchange_event_time) of restore",
        "state_available_at": "same as event_available_at",
        "detected_at": "not used",
    },
    "REMOVAL": {
        "event_time": "exchange_event_time of the depleting book-update",
        "event_available_at": "RESEARCH PROXY = ceil_100ms(exchange_event_time) of depletion",
        "state_available_at": "same as event_available_at",
        "detected_at": "not used",
    },
    "TOUCH": {
        "event_time": "episode first_touch instant",
        "event_available_at": "derived_available_at = first_touch (touch + as-of book known)",
        "state_available_at": "last finished 100ms state with available_at <= first_touch",
        "detected_at": "not used",
    },
    "DETECTION": {
        "event_time": "episode detection instant",
        "event_available_at": "max(event_time, availability of required inputs)",
        "state_available_at": "last finished 100ms state with available_at <= detection",
        "detected_at": "same as event_available_at for Episode 1",
    },
}

CAUSAL_INVARIANT = "derived_available_at >= max(raw_available_at of all used inputs)"
RESEARCH_ASSUMPTION = (
    "Book reconstruction uses exclusive exchange_event_time. "
    "Persisted level_change.event_available_at is ceil_100ms(exchange_event_time), "
    "not collector_received_at. Causal audit additionally verifies "
    "collector_received_at of the last applied OB input <= derived_available_at."
)


def last_state_available_at(states: list[dict[str, Any]], asof: Any) -> str | None:
    last = last_complete_state(states, _as_dt(asof))
    if last is None:
        return None
    return ts_cell(last["available_at"])


def processed_available_at(event_time: Any) -> Any:
    return event_available_at(_as_dt(event_time))

"""Read corrected sms1 tables. Uses available_at, never bucket_start as availability."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..drilldown.aggregation_100ms import (
    _as_dt,
    last_complete_state,
    reconstruct_book_asof_exclusive,
)
from .io_zst import body_rows, read_jsonl_zst
from .persist import pairs_to_map


REQUIRED_SMS1_TABLES = ("states_100ms", "initial_book", "book_resets", "level_changes")


def load_table(directory: Path, name: str, *, require: bool = False) -> list[dict[str, Any]]:
    path = directory / f"{name}.jsonl.zst"
    if not path.is_file():
        if require:
            raise FileNotFoundError(f"persisted sms1 readback missing required table: {path}")
        return []
    return body_rows(read_jsonl_zst(path))


def assert_persisted_sms1_tables(directory: Path) -> None:
    """Fail closed if derived readback cannot re-open required sms1 tables."""
    for name in REQUIRED_SMS1_TABLES:
        load_table(directory, name, require=True)


def last_state_asof(states: list[dict[str, Any]], asof: datetime) -> dict[str, Any] | None:
    """Last finished 100ms row with available_at <= asof. Does not use bucket_start."""
    return last_complete_state(states, asof)


def replay_payload_from_tables(directory: Path) -> dict[str, Any]:
    initial = load_table(directory, "initial_book")
    init = initial[0] if initial else {}
    resets = []
    for rec in load_table(directory, "book_resets"):
        resets.append(
            {
                **rec,
                "event_time": rec["event_time"],
                "bids": pairs_to_map(rec.get("bids")),
                "asks": pairs_to_map(rec.get("asks")),
            }
        )
    changes = load_table(directory, "level_changes")
    return {
        "initial_bids": pairs_to_map(init.get("bids")),
        "initial_asks": pairs_to_map(init.get("asks")),
        "initial_update_id": init.get("update_id"),
        "initial_sequence_id": init.get("sequence_id"),
        "initial_replay_epoch": init.get("replay_epoch"),
        "initial_checkpoint_id": init.get("checkpoint_or_snapshot_id"),
        "window_start": init.get("window_start"),
        "book_resets": resets,
        "level_changes": changes,
        "evidence_start": init.get("window_start"),
    }


def reconstruct_from_persist(directory: Path, until: datetime) -> dict[str, Any]:
    payload = replay_payload_from_tables(directory)
    return reconstruct_book_asof_exclusive(
        initial_bids=payload["initial_bids"],
        initial_asks=payload["initial_asks"],
        level_changes=payload["level_changes"],
        until=until,
        book_resets=payload["book_resets"],
        evidence_start=_as_dt(payload["evidence_start"]) if payload.get("evidence_start") else None,
        initial_update_id=payload.get("initial_update_id"),
        initial_sequence_id=payload.get("initial_sequence_id"),
        initial_replay_epoch=payload.get("initial_replay_epoch"),
        initial_checkpoint_id=payload.get("initial_checkpoint_id"),
    )

"""Serialize corrected sms1 tables. No created_at in table bodies."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt, _floor_bucket, book_map_sha256
from ..timeparse import format_utc_z
from . import BUCKET_MS, EPISODE_ID, SCHEMA_VERSION
from .classify_levels import classify_level_kind
from .io_zst import atomic_write_jsonl_zst, header, ts_cell


def levels_pairs(book: dict[float, float] | None) -> list[list[float]]:
    out: list[list[float]] = []
    for px, qty in sorted((float(p), float(q)) for p, q in (book or {}).items() if q and float(q) > 0):
        out.append([px, qty])
    return out


def pairs_to_map(pairs: list | dict | None) -> dict[float, float]:
    if pairs is None:
        return {}
    if isinstance(pairs, dict):
        return {float(k): float(v) for k, v in pairs.items() if v and float(v) > 0}
    out: dict[float, float] = {}
    for item in pairs:
        px, qty = float(item[0]), float(item[1])
        if qty > 0:
            out[px] = qty
    return out


def event_available_at(event_time: datetime, bucket_ms: int = BUCKET_MS) -> datetime:
    """Finished 100ms bucket that first contains this event. Event at T is in [floor(T), floor(T)+100)."""
    start = _floor_bucket(_as_dt(event_time), bucket_ms)
    return start + timedelta(milliseconds=bucket_ms)


def serialize_state(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = format_utc_z(value)
        else:
            out[key] = value
    out["event_time"] = ts_cell(row["bucket_start"])
    out["bucket_start"] = ts_cell(row["bucket_start"])
    out["bucket_end_exclusive"] = ts_cell(row["bucket_end_exclusive"])
    out["available_at"] = ts_cell(row["available_at"])
    out["effective_bucket_start"] = ts_cell(row["effective_bucket_start"])
    out["state_asof_exclusive"] = ts_cell(row.get("state_asof_exclusive") or row["available_at"])
    out["first_applied_event_ts"] = ts_cell(row.get("first_applied_event_ts"))
    out["last_applied_event_ts"] = ts_cell(row.get("last_applied_event_ts"))
    out["schema_version"] = SCHEMA_VERSION
    return out


def serialize_reset(reset: dict[str, Any], *, before_after: dict[str, Any] | None = None) -> dict[str, Any]:
    bids = reset.get("bids") or {}
    asks = reset.get("asks") or {}
    rec = {
        "event_time": ts_cell(reset["event_time"]),
        "event_available_at": ts_cell(event_available_at(_as_dt(reset["event_time"]))),
        "available_at": ts_cell(event_available_at(_as_dt(reset["event_time"]))),
        "event_type": reset.get("event_type"),
        "update_id": reset.get("update_id"),
        "sequence_id": reset.get("sequence_id"),
        "replay_epoch": reset.get("replay_epoch"),
        "source": reset.get("source"),
        "source_event_id": reset.get("source_event_id"),
        "checkpoint_or_snapshot_id": reset.get("checkpoint_or_snapshot_id"),
        "apply_order": reset.get("apply_order"),
        "n_bid_levels": len(bids),
        "n_ask_levels": len(asks),
        "book_map_sha256": book_map_sha256(bids, asks),
        "bids": levels_pairs(bids),
        "asks": levels_pairs(asks),
        "identity_reset": False,
        "changed_bid_levels": None,
        "changed_ask_levels": None,
    }
    if before_after:
        rec["changed_bid_levels"] = before_after.get("changed_bid_levels")
        rec["changed_ask_levels"] = before_after.get("changed_ask_levels")
        rec["identity_reset"] = int(before_after.get("changed_bid_levels") or 0) == 0 and int(
            before_after.get("changed_ask_levels") or 0
        ) == 0
        rec["best_bid_before"] = before_after.get("best_bid_before")
        rec["best_ask_before"] = before_after.get("best_ask_before")
        rec["best_bid_after"] = before_after.get("best_bid_after")
        rec["best_ask_after"] = before_after.get("best_ask_after")
    return rec


def serialize_change(event: dict[str, Any]) -> dict[str, Any]:
    et = _as_dt(event["event_time"])
    avail = event_available_at(et)
    kind = event.get("level_kind") or classify_level_kind(event.get("old_size"), event.get("new_size"))
    return {
        "event_time": ts_cell(et),
        "event_available_at": ts_cell(avail),
        "state_available_at": ts_cell(avail),
        "available_at": ts_cell(avail),
        "event_type": event.get("event_type") or "level_change",
        "level_kind": kind,
        "side": event.get("side"),
        "price": event.get("price"),
        "old_size": event.get("old_size"),
        "new_size": event.get("new_size"),
        "size_delta": event.get("size_delta"),
        "notional_delta": event.get("notional_delta"),
        "u": event.get("u"),
        "seq": event.get("seq"),
        "replay_epoch": event.get("replay_epoch", event.get("epoch")),
        "apply_order": event.get("apply_order"),
        "source_event_id": event.get("source_event_id"),
        "source": event.get("source"),
    }


def serialize_live_ref(ref: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    skip = {"bids", "asks"}
    for key, value in ref.items():
        if key in skip:
            continue
        if isinstance(value, datetime):
            out[key] = format_utc_z(value)
        else:
            out[key] = value
    if ref.get("cutoff_ts") is not None:
        out["cutoff_ts"] = ts_cell(ref["cutoff_ts"])
    return out


def serialize_initial_book(replay: dict[str, Any]) -> dict[str, Any]:
    bids = replay.get("initial_bids") or {}
    asks = replay.get("initial_asks") or {}
    return {
        "record_type": "initial_book",
        "event_time_semantics": replay.get("initial_book_event_time_semantics"),
        "contains": "event_time < evidence_start",
        "window_start": ts_cell(replay["window_start"]),
        "update_id": replay.get("initial_update_id"),
        "sequence_id": replay.get("initial_sequence_id"),
        "replay_epoch": replay.get("initial_replay_epoch"),
        "checkpoint_or_snapshot_id": replay.get("initial_checkpoint_id"),
        "n_bid_levels": len(bids),
        "n_ask_levels": len(asks),
        "book_map_sha256": book_map_sha256(bids, asks),
        "best_bid": max(bids) if bids else None,
        "best_ask": min(asks) if asks else None,
        "bids": levels_pairs(bids),
        "asks": levels_pairs(asks),
    }


def write_book_tables(
    directory: Path,
    *,
    states: list[dict[str, Any]],
    replay: dict[str, Any],
    config_hash: str,
    input_hash: str,
    live_refs: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Persist states/initial/resets/changes only. Derived events are written after disk readback."""
    directory.mkdir(parents=True, exist_ok=True)
    ba_by_id = {}
    for item in replay.get("checkpoint_before_after") or []:
        ba_by_id[str(item.get("source_event_id"))] = item
    hdr = dict(config_hash=config_hash, input_hash=input_hash, episode_id=EPISODE_ID)
    hashes: dict[str, str] = {}
    state_rows = [header(table="states_100ms", **hdr)]
    state_rows.extend(serialize_state(s) for s in states)
    hashes["states_100ms"] = atomic_write_jsonl_zst(directory / "states_100ms.jsonl.zst", state_rows)

    reset_rows = [header(table="book_resets", **hdr)]
    for rec in replay.get("book_resets") or []:
        reset_rows.append(serialize_reset(rec, before_after=ba_by_id.get(str(rec.get("source_event_id")))))
    hashes["book_resets"] = atomic_write_jsonl_zst(directory / "book_resets.jsonl.zst", reset_rows)

    change_rows = [header(table="level_changes", **hdr)]
    change_rows.extend(serialize_change(e) for e in replay.get("level_changes") or [])
    hashes["level_changes"] = atomic_write_jsonl_zst(directory / "level_changes.jsonl.zst", change_rows)

    init_rows = [header(table="initial_book", **hdr), serialize_initial_book(replay)]
    hashes["initial_book"] = atomic_write_jsonl_zst(directory / "initial_book.jsonl.zst", init_rows)

    if live_refs is not None:
        live_rows = [header(table="live_reference", **hdr)]
        live_rows.extend(serialize_live_ref(r) for r in live_refs)
        hashes["live_reference"] = atomic_write_jsonl_zst(directory / "live_reference.jsonl.zst", live_rows)
    return hashes


def write_derived_tables(
    directory: Path,
    *,
    walls: list[dict[str, Any]],
    level_removals: list[dict[str, Any]] | None = None,
    refill_candidates: list[dict[str, Any]] | None = None,
    confirmed_refills: list[dict[str, Any]] | None = None,
    touches: list[dict[str, Any]],
    detections: list[dict[str, Any]],
    config_hash: str,
    input_hash: str,
    refills: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    hdr = dict(config_hash=config_hash, input_hash=input_hash, episode_id=EPISODE_ID)
    hashes: dict[str, str] = {}
    hashes["walls"] = atomic_write_jsonl_zst(
        directory / "walls.jsonl.zst", [header(table="walls", **hdr), *walls]
    )
    hashes["touches"] = atomic_write_jsonl_zst(
        directory / "touches.jsonl.zst", [header(table="touches", **hdr), *touches]
    )
    hashes["detections"] = atomic_write_jsonl_zst(
        directory / "detections.jsonl.zst", [header(table="detections", **hdr), *detections]
    )
    if level_removals is not None:
        hashes["level_removals"] = atomic_write_jsonl_zst(
            directory / "level_removals.jsonl.zst", [header(table="level_removals", **hdr), *level_removals]
        )
    if refill_candidates is not None:
        hashes["refill_candidates"] = atomic_write_jsonl_zst(
            directory / "refill_candidates.jsonl.zst",
            [header(table="refill_candidates", **hdr), *refill_candidates],
        )
    if confirmed_refills is not None:
        hashes["confirmed_refills"] = atomic_write_jsonl_zst(
            directory / "confirmed_refills.jsonl.zst",
            [header(table="confirmed_refills", **hdr), *confirmed_refills],
        )
    if refills is not None:
        hashes["refill_removal"] = atomic_write_jsonl_zst(
            directory / "refill_removal.jsonl.zst", [header(table="refill_removal", **hdr), *refills]
        )
    return hashes


def write_tables(
    directory: Path,
    *,
    states: list[dict[str, Any]],
    replay: dict[str, Any],
    walls: list[dict[str, Any]],
    refills: list[dict[str, Any]],
    touches: list[dict[str, Any]],
    detections: list[dict[str, Any]],
    config_hash: str,
    input_hash: str,
) -> dict[str, str]:
    hashes = write_book_tables(
        directory, states=states, replay=replay, config_hash=config_hash, input_hash=input_hash
    )
    hashes.update(
        write_derived_tables(
            directory,
            walls=walls,
            touches=touches,
            detections=detections,
            refills=refills,
            config_hash=config_hash,
            input_hash=input_hash,
        )
    )
    return hashes

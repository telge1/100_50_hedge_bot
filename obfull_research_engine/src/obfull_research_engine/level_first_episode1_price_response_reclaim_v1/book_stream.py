"""Incremental full-L2 BBO (+ sizes) stream aligned to 100ms states."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ..drilldown.aggregation_100ms import (
    _as_dt,
    _apply_change,
    _apply_reset,
    merge_book_events,
    normalize_book_resets,
)
from ..level_first_episode1_corrected_sms1_persist_v1.reader import load_table, replay_payload_from_tables
from ..timeparse import format_utc_z
from . import TIME_BASIS


class BookStreamError(RuntimeError):
    """Fail-closed book / coverage error."""


def load_book_payload(persist_dir: Path) -> dict[str, Any]:
    return replay_payload_from_tables(Path(persist_dir))


def load_states(persist_dir: Path) -> list[dict[str, Any]]:
    return load_table(Path(persist_dir), "states_100ms", require=True)


def _event_available_at(payload: dict[str, Any], event_time: datetime) -> datetime:
    """Honest EVENT_TIME_ONLY: prefer persisted available_at; else event_time."""
    raw = payload.get("available_at")
    if raw is not None:
        return _as_dt(raw)
    return event_time


def iter_state_bbo(
    *,
    payload: dict[str, Any],
    states: list[dict[str, Any]],
    expected_epoch: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield causal BBO rows for each finished 100ms state.

    Book updates use exchange ``event_time < bucket_end_exclusive`` (same as
    reconstruct_book_asof_exclusive). Availability for the row is the max of
    state.available_at and last applied book event available_at.
    """
    resets = normalize_book_resets(payload.get("book_resets"), None)
    events = list(merge_book_events(payload["level_changes"], resets))
    bids = dict(payload["initial_bids"])
    asks = dict(payload["initial_asks"])
    epoch = payload.get("initial_replay_epoch")
    ei = 0
    last_book_avail: datetime | None = (
        _as_dt(payload["evidence_start"]) if payload.get("evidence_start") else None
    )
    last_book_event_time: datetime | None = None
    sorted_states = sorted(states, key=lambda s: _as_dt(s["bucket_start"]))

    for st in sorted_states:
        bstart = _as_dt(st["bucket_start"])
        bend = _as_dt(st.get("bucket_end_exclusive") or st.get("bucket_end"))
        state_avail = _as_dt(st.get("available_at") or bend)
        while ei < len(events) and events[ei][0] < bend:
            et, _o, _k, _i, kind, pev = events[ei]
            if kind == "reset":
                _apply_reset(bids, asks, pev)
            else:
                _apply_change(bids, asks, pev)
            if pev.get("replay_epoch") is not None:
                epoch = pev.get("replay_epoch")
            last_book_event_time = et
            last_book_avail = _event_available_at(pev, et)
            ei += 1

        bb = max(bids) if bids else None
        ba = min(asks) if asks else None
        crossed = bool(bb is not None and ba is not None and float(bb) >= float(ba))
        missing_bbo = bb is None or ba is None
        bsz = float(bids[bb]) if bb is not None and bb in bids else None
        asz = float(asks[ba]) if ba is not None and ba in asks else None
        max_in = state_avail
        if last_book_avail is not None and last_book_avail > max_in:
            max_in = last_book_avail

        epoch_ok = True
        if expected_epoch is not None and epoch is not None and int(epoch) != int(expected_epoch):
            epoch_ok = False

        coverage_ok = (
            not missing_bbo
            and not crossed
            and bsz is not None
            and asz is not None
            and bsz >= 0
            and asz >= 0
            and epoch_ok
            and state_avail > bstart
        )
        invalid_reason = None
        if missing_bbo:
            invalid_reason = "missing_bbo"
        elif crossed:
            invalid_reason = "crossed_book"
        elif bsz is None or asz is None:
            invalid_reason = "missing_bbo_size"
        elif not epoch_ok:
            invalid_reason = "replay_epoch_change"
        elif state_avail <= bstart:
            invalid_reason = "invalid_state_availability"

        look_ahead = bool(max_in > state_avail)
        yield {
            "bucket_start": format_utc_z(bstart),
            "bucket_end_exclusive": format_utc_z(bend),
            "state_available_at": format_utc_z(state_avail),
            "decision_time": format_utc_z(state_avail),
            "exchange_feature_time": format_utc_z(bend),
            "best_bid": bb,
            "best_ask": ba,
            "best_bid_size": bsz,
            "best_ask_size": asz,
            "replay_epoch": int(epoch) if epoch is not None else None,
            "last_book_event_time": format_utc_z(last_book_event_time) if last_book_event_time else None,
            "max_input_available_at": format_utc_z(max_in),
            "coverage_ok": coverage_ok,
            "look_ahead": look_ahead,
            "invalid_reason": invalid_reason,
            "time_basis": TIME_BASIS,
            "book_event_index": ei,
        }


def last_book_coverage_end(payload: dict[str, Any]) -> datetime | None:
    lcs = payload.get("level_changes") or []
    if not lcs:
        return None
    return max(_as_dt(r["event_time"]) for r in lcs)

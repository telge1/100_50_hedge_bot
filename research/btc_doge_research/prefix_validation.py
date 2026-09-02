"""Causal prefix-invariance proof for reconstructed OB200 events."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .config import PilotWindow
from .ob200_parser import OB200SegmentReader
from .pilot_runner import discover_sources


def prove_prefix_invariance(window: PilotWindow) -> dict[str, Any]:
    source = discover_sources(window)[0]
    first_end = window.start + timedelta(minutes=10)
    second_end = window.start + timedelta(minutes=20)

    first_reader = OB200SegmentReader(source, window.symbol)
    first = {
        event.event_key: event.content_fingerprint
        for event in first_reader.iter_full_books(window.start, first_end)
    }
    second_reader = OB200SegmentReader(source, window.symbol)
    second = {
        event.event_key: event.content_fingerprint
        for event in second_reader.iter_full_books(window.start, second_end)
        if event.event_time < first_end
    }
    return {
        "symbol": window.symbol,
        "source_file_id": source.source_file_id,
        "prefix_start": window.start,
        "first_cutoff_exclusive": first_end,
        "later_cutoff_exclusive": second_end,
        "first_prefix_events": len(first),
        "later_run_same_prefix_events": len(second),
        "content_fingerprints_equal": first == second,
        "future_events_change_finalized_prefix": False if first == second else True,
        "status": "PASS" if first == second else "FAIL",
    }

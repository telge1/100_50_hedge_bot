"""Load and validate MP batch events by event_id (not timestamp alone)."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pilot_cases import PILOT_CASES, PilotCase


def _ns_to_utc_label(ns: int | str) -> str:
    return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_events_csv(batch_dir: Path) -> dict[str, dict[str, str]]:
    path = Path(batch_dir) / "events_all.csv"
    return {r["event_id"]: r for r in csv.DictReader(path.open(encoding="utf-8"))}


def load_windows_csv(batch_dir: Path) -> dict[str, dict[str, str]]:
    path = Path(batch_dir) / "batch_windows.csv"
    return {r["window_id"]: r for r in csv.DictReader(path.open(encoding="utf-8"))}


def resolve_pilot_events(
    *,
    batch_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve the six frozen cases; fail closed on ambiguity."""
    events = load_events_csv(batch_dir)
    windows = load_windows_csv(batch_dir)
    # Index by trigger second label for ambiguity detection
    by_trig: dict[str, list[str]] = {}
    for eid, e in events.items():
        trig = e.get("trigger_ts_ns") or ""
        if trig in ("", "None"):
            continue
        lab = _ns_to_utc_label(trig)
        by_trig.setdefault(lab, []).append(eid)

    resolved: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for case in PILOT_CASES:
        e = events.get(case.event_id)
        if e is None:
            blockers.append({"event_id": case.event_id, "reason": "EVENT_ID_NOT_FOUND"})
            continue
        trig = e.get("trigger_ts_ns") or ""
        if trig in ("", "None"):
            blockers.append({"event_id": case.event_id, "reason": "MISSING_TRIGGER_TS"})
            continue
        trig_lab = _ns_to_utc_label(trig)
        if trig_lab != case.expected_trigger_utc:
            blockers.append(
                {
                    "event_id": case.event_id,
                    "reason": "TRIGGER_TS_MISMATCH",
                    "got": trig_lab,
                    "expected": case.expected_trigger_utc,
                }
            )
            continue
        peers = [x for x in by_trig.get(trig_lab, []) if x != case.event_id]
        # Same-second other events OK if different event_id; ambiguity only if
        # we could not uniquely identify via event_id (we always can). Document peers.
        wid = e.get("window_id") or ""
        w = windows.get(wid) or {}
        epoch = e.get("epoch_id") or e.get("replay_epoch") or w.get("replay_epoch") or ""
        resolved.append(
            {
                "case": case,
                "event": e,
                "window": w,
                "replay_epoch": epoch,
                "chain_version": w.get("chain_version") or "",
                "trigger_utc": trig_lab,
                "same_second_peer_event_ids": peers,
                "ambiguous": False,
            }
        )
    audit = {
        "n_requested": len(PILOT_CASES),
        "n_resolved": len(resolved),
        "n_blocked": len(blockers),
        "blockers": blockers,
        "ok": len(resolved) == len(PILOT_CASES) and not blockers,
    }
    return resolved, audit

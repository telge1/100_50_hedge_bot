"""Causal depth-2 entry-pool continuity from Phase 3A lifecycle (read-only)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class PoolAnchor:
    side: str  # upper | lower
    bottom: float
    top: float
    origins: frozenset[str]
    pool_track_id: str | None = None


def _iou(a_bot: float, a_top: float, b_bot: float, b_top: float) -> float:
    lo = max(a_bot, b_bot)
    hi = min(a_top, b_top)
    if hi <= lo:
        return 0.0
    inter = hi - lo
    union = max(a_top, b_top) - min(a_bot, b_bot)
    return inter / union if union > 0 else 0.0


def matches_anchor(anchor: PoolAnchor, event: dict[str, Any]) -> bool:
    if event.get("side") != anchor.side:
        return False
    try:
        origins = set(json.loads(event.get("origins_json") or "[]"))
    except json.JSONDecodeError:
        origins = set()
    if not origins & set(anchor.origins):
        return False
    bot = float(event["bottom_price"])
    top = float(event["top_price"])
    if abs(bot - anchor.bottom) < 1e-9 and abs(top - anchor.top) < 1e-9:
        return True
    return _iou(anchor.bottom, anchor.top, bot, top) >= 0.35


class LifecycleIndex:
    def __init__(self) -> None:
        self.by_bar: dict[str, list[dict[str, str]]] = {}

    @classmethod
    def load(cls) -> LifecycleIndex:
        idx = cls()
        for side, fname in (("upper", "upper_pool_lifecycle.csv"), ("lower", "lower_pool_lifecycle.csv")):
            path = ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1" / fname
            if not path.is_file():
                continue
            import csv

            with path.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    row = dict(row)
                    row["side"] = side
                    bar = row["causal_as_of_utc"]
                    idx.by_bar.setdefault(bar, []).append(row)
        return idx

    def events_at(self, bar_utc: str) -> list[dict]:
        return self.by_bar.get(bar_utc, [])


def depth2_matches_at_bar(anchor: PoolAnchor, bar_utc: str, idx: LifecycleIndex) -> list[dict]:
    out = []
    for e in idx.events_at(bar_utc):
        if e.get("event_type") not in ("POOL_NEW", "POOL_CONTINUED"):
            continue
        if int(float(e.get("depth") or 0)) != 2:
            continue
        if matches_anchor(anchor, e):
            out.append(e)
    return out


def disappeared_matches_at_bar(anchor: PoolAnchor, bar_utc: str, idx: LifecycleIndex) -> list[dict]:
    out = []
    for e in idx.events_at(bar_utc):
        if e.get("event_type") != "POOL_DISAPPEARED":
            continue
        if matches_anchor(anchor, e):
            out.append(e)
    return out


@dataclass
class ZoneStatus:
    state: str  # ACTIVE | INACTIVE | IDENTITY_AMBIGUOUS
    rule_id: str
    note: str


def zone_status_at_bar(anchor: PoolAnchor, bar_utc: str, idx: LifecycleIndex) -> ZoneStatus:
    matches = depth2_matches_at_bar(anchor, bar_utc, idx)
    amb = [m for m in matches if m.get("identity_confidence") == "NOT_VERIFIED"]
    if len(amb) > 0 and len(matches) > 1:
        return ZoneStatus(
            "IDENTITY_AMBIGUOUS",
            "EXEC-POOL-IDENTITY-01",
            "NOT_VERIFIED continuity with multiple depth-2 matches",
        )
    if matches:
        return ZoneStatus("ACTIVE", "EXEC-POOL-ACTIVE-01", f"{len(matches)} depth-2 match(es)")
    disappeared = disappeared_matches_at_bar(anchor, bar_utc, idx)
    if disappeared:
        return ZoneStatus(
            "INACTIVE",
            "EXEC-POOL-DEACTIVATED-01",
            "POOL_DISAPPEARED without depth-2 continuation at bar",
        )
    return ZoneStatus("INACTIVE", "EXEC-POOL-INACTIVE-01", "No depth-2 match at bar")


def anchor_from_signal(direction: str, pool_lower: float, pool_upper: float, pool_identity: str) -> PoolAnchor:
    origins = frozenset(pool_identity.split("|")) if pool_identity else frozenset()
    side = "upper" if direction == "SHORT" else "lower"
    return PoolAnchor(side=side, bottom=pool_lower, top=pool_upper, origins=origins)

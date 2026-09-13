"""Calendar UTC splits for Phase-2 baseline evaluation."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from .contract import AMBIGUOUS_OUTCOME, PRIMARY_CLASSES, SPLIT_BOUNDS
from .data import EpisodeRow


def assign_split(t0: datetime) -> str:
    for name, (start, end) in SPLIT_BOUNDS.items():
        if t0 < start:
            continue
        if end is None or t0 < end:
            return name
    raise ValueError(f"t0 outside frozen split coverage: {t0.isoformat()}")


def split_episodes(episodes: list[EpisodeRow]) -> dict[str, list[EpisodeRow]]:
    out = {name: [] for name in SPLIT_BOUNDS}
    for row in episodes:
        out[assign_split(row.t0_utc)].append(row)
    for name in out:
        out[name].sort(key=lambda r: (r.t0_utc, r.symbol, r.episode_id))
    return out


def assert_no_split_leak(splits: dict[str, list[EpisodeRow]]) -> None:
    ids = {}
    for name, rows in splits.items():
        for row in rows:
            if row.episode_id in ids:
                raise ValueError(
                    f"episode leak {row.episode_id} in {ids[row.episode_id]} and {name}"
                )
            ids[row.episode_id] = name
        # boundary checks
        start, end = SPLIT_BOUNDS[name]
        for row in rows:
            if row.t0_utc < start:
                raise ValueError(f"{name} contains t0 before start")
            if end is not None and row.t0_utc >= end:
                raise ValueError(f"{name} contains t0 at/after exclusive end")


def class_counts(rows: list[EpisodeRow]) -> dict[str, int]:
    counts = Counter(r.outcome for r in rows)
    return {cls: int(counts.get(cls, 0)) for cls in PRIMARY_CLASSES + (AMBIGUOUS_OUTCOME,)}


def primary_rows(rows: list[EpisodeRow]) -> list[EpisodeRow]:
    return [r for r in rows if r.outcome in PRIMARY_CLASSES]


def directional_rows(rows: list[EpisodeRow]) -> list[EpisodeRow]:
    from .contract import DIRECTIONAL_CLASSES

    return [r for r in rows if r.outcome in DIRECTIONAL_CLASSES]

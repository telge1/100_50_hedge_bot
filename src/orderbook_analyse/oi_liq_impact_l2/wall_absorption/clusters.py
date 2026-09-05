"""Deterministic flush clustering for F3 wall absorption."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import pandas as pd


@dataclass(frozen=True)
class FlushCluster:
    cluster_id: str
    symbol: str
    direction: str
    cluster_start: str
    cluster_end: str
    candidate_ids: tuple[str, ...]
    primary_candidate_id: str
    flush_minutes: int
    gap_minutes: int


def _cluster_id(symbol: str, direction: str, start: str) -> str:
    return f"oildisc_cluster:{symbol}:{direction}:{start}"


def build_flush_clusters(
    candidates: Sequence[Mapping[str, object]],
    *,
    gap_minutes: int = 1,
) -> list[FlushCluster]:
    """Cluster consecutive flush minutes; a gap of N non-flush minutes starts a new cluster."""
    if gap_minutes < 1:
        raise ValueError("gap_minutes must be >= 1")
    ordered = sorted(
        candidates,
        key=lambda row: (str(row["symbol"]), str(row["direction"]), str(row["minute"])),
    )
    clusters: list[FlushCluster] = []
    current: list[Mapping[str, object]] = []
    last_minute: pd.Timestamp | None = None
    symbol = ""
    direction = ""

    def flush_current() -> None:
        nonlocal current, last_minute
        if not current:
            return
        start = str(current[0]["minute"])
        end = str(current[-1]["minute"])
        ids = tuple(str(row["candidate_id"]) for row in current)
        clusters.append(
            FlushCluster(
                cluster_id=_cluster_id(symbol, direction, start),
                symbol=symbol,
                direction=direction,
                cluster_start=start,
                cluster_end=end,
                candidate_ids=ids,
                primary_candidate_id=ids[0],
                flush_minutes=len(current),
                gap_minutes=gap_minutes,
            )
        )
        current = []
        last_minute = None

    for row in ordered:
        minute = pd.Timestamp(str(row["minute"]), tz="UTC")
        symbol = str(row["symbol"])
        direction = str(row["direction"])
        if (
            current
            and last_minute is not None
            and (minute - last_minute) > pd.Timedelta(minutes=gap_minutes)
        ):
            flush_current()
        current.append(row)
        last_minute = minute
    flush_current()
    return clusters


def cluster_sensitivity_counts(
    candidates: Sequence[Mapping[str, object]],
    gaps: Iterable[int] = (1, 2, 3, 5),
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for gap in gaps:
        clusters = build_flush_clusters(candidates, gap_minutes=gap)
        for direction in ("LONG", "SHORT"):
            subset = [c for c in clusters if c.direction == direction]
            rows.append(
                {
                    "gap_minutes": gap,
                    "direction": direction,
                    "cluster_count": len(subset),
                    "candidate_count": sum(c.flush_minutes for c in subset),
                }
            )
    return rows

"""Distance distribution and STRUCTURE vs EXECUTION comparison."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from orderbook_analyse.execution_wall_detector.local_score import band_label
from orderbook_analyse.execution_wall_detector.types import (
    ExecutionWallParams,
    ExecutionWallSequence,
)
from orderbook_analyse.wall_toxicity_audit.data_access import (
    load_wall_sequences_from_csv,
)


def _median(vals: Sequence[float]) -> float | None:
    if not vals:
        return None
    return float(statistics.median(vals))


def _rate(n: int, d: int) -> float | None:
    if d <= 0:
        return None
    return n / d


def band_edges(params: ExecutionWallParams) -> list[float]:
    edges = sorted({0.0, *params.distance_bands_bps, params.max_distance_bps, 50.0})
    return edges


def distance_distribution(
    sequences: Sequence[ExecutionWallSequence],
    *,
    params: ExecutionWallParams,
) -> list[dict[str, Any]]:
    edges = band_edges(params)
    buckets: dict[str, list[ExecutionWallSequence]] = defaultdict(list)
    for seq in sequences:
        d = seq.min_distance_bps
        if d is None:
            continue
        label = band_label(d, edges)
        buckets[label].append(seq)

    # Ensure all expected labels appear
    labels: list[str] = []
    prev = edges[0]
    for e in edges[1:]:
        labels.append(f"{prev:g}-{e:g}")
        prev = e
    labels.append(f">{edges[-1]:g}")

    rows: list[dict[str, Any]] = []
    for label in labels:
        group = buckets.get(label, [])
        n = len(group)
        touched = sum(1 for s in group if s.touch_status == "TOUCHED" or s.touch_time)
        executed = sum(1 for s in group if s.executed_qty_estimate > 0)
        pulled = sum(1 for s in group if s.pulled_before_touch)
        absorp = sum(1 for s in group if s.absorption_candidate)
        break_att = sum(1 for s in group if s.breakout_attempted)
        accepted = sum(1 for s in group if s.breakout_accepted)
        failed = sum(1 for s in group if s.breakout_failed)
        lives = [s.lifetime_ms for s in group if s.lifetime_ms > 0]
        notionals = [
            s.peak_qty * s.representative_price for s in group if s.peak_qty > 0
        ]
        rows.append(
            {
                "band_label": label,
                "sequences": n,
                "candidates_proxy": n,  # sequence-level; candidate CSV has finer grain
                "touches": touched,
                "executed_walls": executed,
                "pulls_before_touch": pulled,
                "absorption_candidates": absorp,
                "breakout_attempts": break_att,
                "accepted_breakouts": accepted,
                "failed_breakouts": failed,
                "median_lifetime_ms": _median(lives),
                "median_peak_notional": _median(notionals),
                "touch_rate": _rate(touched, n),
            }
        )
    return rows


def candidate_distance_distribution(
    candidate_rows: Sequence[dict[str, Any]],
    *,
    params: ExecutionWallParams,
) -> list[dict[str, Any]]:
    edges = band_edges(params)
    counts: dict[str, int] = defaultdict(int)
    for row in candidate_rows:
        d = float(row.get("distance_bps") or 0)
        counts[band_label(d, edges)] += 1
    labels: list[str] = []
    prev = edges[0]
    for e in edges[1:]:
        labels.append(f"{prev:g}-{e:g}")
        prev = e
    labels.append(f">{edges[-1]:g}")
    return [
        {"band_label": lab, "candidates": counts.get(lab, 0)} for lab in labels
    ]


def structure_vs_execution_comparison(
    *,
    execution: Sequence[ExecutionWallSequence],
    structure_csv: Path | None,
    start: datetime | None,
    end: datetime | None,
    symbol: str,
) -> list[dict[str, Any]]:
    exec_row = _summary_row("EXECUTION_WALL", execution)
    struct_seqs: list[Any] = []
    if structure_csv is not None and structure_csv.exists():
        struct_seqs = load_wall_sequences_from_csv(
            structure_csv, symbol=symbol, start=start, end=end
        )
    struct_row = _structure_summary_row(struct_seqs)
    return [struct_row, exec_row]


def _summary_row(kind: str, seqs: Sequence[ExecutionWallSequence]) -> dict[str, Any]:
    n = len(seqs)
    touched = sum(1 for s in seqs if s.touch_status == "TOUCHED" or s.touch_time)
    pulled = sum(1 for s in seqs if s.pulled_before_touch)
    executed = sum(1 for s in seqs if s.executed_qty_estimate > 0)
    absorp = sum(1 for s in seqs if s.absorption_candidate)
    broken = sum(1 for s in seqs if s.breakout_attempted)
    accepted = sum(1 for s in seqs if s.breakout_accepted)
    failed = sum(1 for s in seqs if s.breakout_failed)
    dists = [s.min_distance_bps for s in seqs if s.min_distance_bps is not None]
    lives = [s.lifetime_ms for s in seqs if s.lifetime_ms > 0]
    sizes = [s.peak_qty * s.representative_price for s in seqs]
    remote = sum(1 for d in dists if d is not None and d > 50)
    near = sum(1 for d in dists if d is not None and d <= 30)
    return {
        "wall_type": kind,
        "count": n,
        "near_market_count_le_30bps": near,
        "remote_count_gt_50bps": remote,
        "median_min_distance_bps": _median([d for d in dists if d is not None]),
        "touch_count": touched,
        "touch_rate": _rate(touched, n),
        "median_lifetime_ms": _median(lives),
        "median_peak_notional": _median(sizes),
        "migration_rate": None,  # structure CSV does not expose; execution uses toxicity
        "pulling_rate": _rate(pulled, n),
        "execution_rate": _rate(executed, n),
        "absorption_rate": _rate(absorp, n),
        "break_rate": _rate(broken, n),
        "acceptance_rate": _rate(accepted, n),
        "failed_break_rate": _rate(failed, n),
        "data_quality_note": "execution path uses high-resolution local dominance",
    }


def _structure_summary_row(seqs: Sequence[Any]) -> dict[str, Any]:
    n = len(seqs)
    touched = sum(1 for s in seqs if s.touched or s.was_tested)
    pulled = sum(1 for s in seqs if s.disappeared_before_test)
    dists = [s.min_distance_bps for s in seqs if s.min_distance_bps is not None]
    lives = [
        (s.last_seen_ts - s.first_seen_ts).total_seconds() * 1000.0
        for s in seqs
        if s.first_seen_ts and s.last_seen_ts
    ]
    sizes = [
        float(s.first_notional or s.last_notional or 0.0)
        for s in seqs
    ]
    remote = sum(1 for d in dists if d is not None and d > 50)
    near = sum(1 for d in dists if d is not None and d <= 30)
    broken = sum(1 for s in seqs if getattr(s, "raw", {}).get("was_broken") in {"True", "true", "1", True})
    # raw may have confirmed_broken
    if not broken:
        broken = sum(
            1
            for s in seqs
            if str(getattr(s, "raw", {}).get("confirmed_broken") or "").lower()
            in {"1", "true", "t", "yes"}
        )
    return {
        "wall_type": "STRUCTURE_WALL",
        "count": n,
        "near_market_count_le_30bps": near,
        "remote_count_gt_50bps": remote,
        "median_min_distance_bps": _median([d for d in dists if d is not None]),
        "touch_count": touched,
        "touch_rate": _rate(touched, n),
        "median_lifetime_ms": _median(lives),
        "median_peak_notional": _median([x for x in sizes if x > 0]),
        "migration_rate": None,
        "pulling_rate": _rate(pulled, n),
        "execution_rate": None,
        "absorption_rate": None,
        "break_rate": _rate(broken, n),
        "acceptance_rate": None,
        "failed_break_rate": None,
        "data_quality_note": (
            "structure walls: large remote liquidity clusters; low touch is expected"
        ),
    }


def load_structure_csv_optional(path: str | Path | None, symbol: str) -> Path | None:
    if path:
        p = Path(path)
        return p if p.exists() else None
    root = Path(__file__).resolve().parents[3] / "results"
    preferred = [
        root / f"general_{symbol}" / "full_history" / "wall_sequences.csv",
        root / f"full_history_{symbol}_phase4" / "wall_sequences.csv",
        root / f"full_history_{symbol}_phase5" / "wall_sequences.csv",
    ]
    for p in preferred:
        if p.exists():
            return p
    from orderbook_analyse.wall_toxicity_audit.data_access import default_wall_sequences_csv

    return default_wall_sequences_csv(symbol)

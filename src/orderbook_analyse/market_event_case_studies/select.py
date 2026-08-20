"""Pure selection helpers for market-event case studies (no I/O)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

import numpy as np

COOLDOWN_M = 60
THR_075 = 0.0075
THR_100 = 0.0100
CASE_TYPES = (
    "long_big_move",
    "short_big_move",
    "flow_opposed_reversal",
    "flow_aligned_move",
    "failed_directional",
    "rare_confluence",
)


@dataclass
class CaseCandidate:
    case_type: str
    symbol: str
    event_time: datetime
    score: float
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        ts = self.event_time.strftime("%Y%m%d_%H%M")
        return f"{self.case_type}__{self.symbol}__{ts}"

    def report_relpath(self) -> str:
        """Relative folder under the study root."""
        ts = self.event_time.strftime("%Y%m%d_%H%M")
        subtype = self.meta.get("subtype") or self.case_type
        # Keep path filesystem-safe and unique
        safe_sub = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(subtype))
        return f"{safe_sub}/{self.symbol}_{ts}"


def apply_cooldown(
    events: Sequence[tuple[datetime, float, Any]],
    *,
    cooldown_m: int = COOLDOWN_M,
    prefer_higher_score: bool = True,
) -> list[tuple[datetime, float, Any]]:
    """Non-maximum suppression over time with cooldown.

    Sort by score (desc if ``prefer_higher_score``), then keep an event only if
    no already-kept event lies within ``cooldown_m`` minutes (absolute).
    """
    if not events:
        return []
    ordered = sorted(
        events,
        key=lambda x: ((-x[1] if prefer_higher_score else x[1]), x[0]),
    )
    kept: list[tuple[datetime, float, Any]] = []
    kept_times: list[datetime] = []
    cool = timedelta(minutes=cooldown_m)

    for t, score, payload in ordered:
        blocked = False
        for kt in kept_times:
            if abs((t - kt).total_seconds()) < cool.total_seconds():
                blocked = True
                break
        if blocked:
            continue
        kept.append((t, score, payload))
        kept_times.append(t)

    kept.sort(key=lambda x: x[0])
    return kept


def cooldown_per_symbol(
    candidates: Sequence[CaseCandidate],
    *,
    cooldown_m: int = COOLDOWN_M,
) -> list[CaseCandidate]:
    """Apply cooldown independently per (symbol, case_type)."""
    from collections import defaultdict

    groups: dict[tuple[str, str], list[CaseCandidate]] = defaultdict(list)
    for c in candidates:
        groups[(c.symbol, c.case_type)].append(c)

    out: list[CaseCandidate] = []
    for _, group in groups.items():
        packed = [(c.event_time, c.score, c) for c in group]
        kept = apply_cooldown(packed, cooldown_m=cooldown_m, prefer_higher_score=True)
        out.extend(payload for _, _, payload in kept)
    out.sort(key=lambda c: (c.case_type, c.symbol, c.event_time))
    return out


def select_top_n(candidates: Sequence[CaseCandidate], n: int) -> list[CaseCandidate]:
    if n <= 0:
        return []
    return sorted(candidates, key=lambda c: c.score, reverse=True)[:n]


def select_rare_confluence(
    candidates: Sequence[CaseCandidate],
    *,
    max_all: int = 40,
    top_abs: int = 20,
    random_fill: int = 20,
    seed: int = 42,
) -> list[CaseCandidate]:
    """If <= max_all keep all; else top by |move| + stratified/random remainder."""
    items = list(candidates)
    if len(items) <= max_all:
        return sorted(items, key=lambda c: (c.event_time, c.symbol))

    by_score = sorted(items, key=lambda c: abs(c.score), reverse=True)
    top = by_score[:top_abs]
    top_ids = {c.case_id for c in top}
    rest = [c for c in by_score[top_abs:] if c.case_id not in top_ids]

    # Stratify remaining by UTC day for even coverage, then RNG fill
    from collections import defaultdict

    by_day: dict[str, list[CaseCandidate]] = defaultdict(list)
    for c in rest:
        by_day[c.event_time.strftime("%Y-%m-%d")].append(c)

    rng = np.random.default_rng(seed)
    picked: list[CaseCandidate] = []
    days = sorted(by_day.keys())
    # Round-robin one from each day until random_fill
    while len(picked) < random_fill and any(by_day[d] for d in days):
        for d in days:
            if len(picked) >= random_fill:
                break
            bucket = by_day[d]
            if not bucket:
                continue
            idx = int(rng.integers(0, len(bucket)))
            picked.append(bucket.pop(idx))

    if len(picked) < random_fill:
        leftover = [c for d in days for c in by_day[d]]
        need = random_fill - len(picked)
        if leftover:
            take_idx = rng.choice(len(leftover), size=min(need, len(leftover)), replace=False)
            for i in np.atleast_1d(take_idx):
                picked.append(leftover[int(i)])

    out = top + picked
    # Deduplicate preserving order
    seen: set[str] = set()
    uniq: list[CaseCandidate] = []
    for c in out:
        if c.case_id in seen:
            continue
        seen.add(c.case_id)
        uniq.append(c)
    return uniq


def prefer_score_with_bonus(base: float, *, hit_100: bool, bonus: float = 0.001) -> float:
    """Prefer >=1% moves slightly in ranking without changing thresholds."""
    return float(base) + (bonus if hit_100 else 0.0)


def safe_case_dirname(symbol: str, event_time: datetime) -> str:
    return f"{symbol}_{event_time.strftime('%Y%m%d_%H%M')}"


def candidates_to_records(candidates: Iterable[CaseCandidate]) -> list[dict[str, Any]]:
    rows = []
    for c in candidates:
        rows.append(
            {
                "case_id": c.case_id,
                "case_type": c.case_type,
                "symbol": c.symbol,
                "event_time": c.event_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "score": c.score,
                "report_relpath": c.report_relpath(),
                **{f"meta_{k}": v for k, v in c.meta.items() if not isinstance(v, (dict, list))},
            }
        )
    return rows

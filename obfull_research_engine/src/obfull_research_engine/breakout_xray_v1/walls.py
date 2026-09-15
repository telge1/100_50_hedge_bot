"""Q95 wall selection (USDT notional) and causal lifecycle classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from .models import WallClass
from .ports import BookLevel, LevelChangeEvent
from .time_windows import dt_to_ns, ns_to_dt


@dataclass
class LevelEvent:
    event_time: datetime
    side: str
    price: float
    change_type: str  # ADD | UPDATE | REMOVE
    new_size: float
    old_size: float | None = None

    @property
    def notional_usdt(self) -> float:
        return float(self.price) * float(self.new_size)


@dataclass
class WallLifecycle:
    side: str
    price: float
    baseline_size: float
    max_size: float
    last_size: float
    size_base: float = 0.0
    notional_usdt: float = 0.0
    events: list[LevelEvent] = field(default_factory=list)
    classification: WallClass = WallClass.UNRESOLVED
    evidence: dict[str, Any] = field(default_factory=dict)
    local_rank: int | None = None
    distance_to_ref: float | None = None
    match: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "price": self.price,
            "baseline_size": self.baseline_size,
            "max_size": self.max_size,
            "last_size": self.last_size,
            "size_base": self.size_base,
            "notional_usdt": self.notional_usdt,
            "classification": self.classification.value,
            "evidence": self.evidence,
            "local_rank": self.local_rank,
            "distance_to_ref": self.distance_to_ref,
            "event_count": len(self.events),
            "match": self.match,
        }


def quantile(values: Sequence[float], q: float) -> float:
    if not values:
        raise ValueError("empty values for quantile")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q out of range")
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def wall_notional_usdt(price: float, size: float) -> float:
    return float(price) * float(size)


def select_relevant_walls(
    sizes_by_level: dict[tuple[str, float], float],
    *,
    ref_price: float,
    local_band_usd: float,
    q: float = 0.95,
    max_rank: int = 12,
) -> tuple[float, list[tuple[str, float, float]]]:
    """Legacy size-based selector (tests); prefer select_walls_by_notional."""
    in_band = {
        k: v
        for k, v in sizes_by_level.items()
        if abs(float(k[1]) - float(ref_price)) <= float(local_band_usd)
    }
    if not in_band:
        return 0.0, []
    thr = quantile(list(in_band.values()), q)
    candidates = [(s, p, sz) for (s, p), sz in in_band.items() if sz >= thr]
    candidates.sort(key=lambda x: x[2], reverse=True)
    return thr, candidates[:max_rank]


def accumulate_level_sizes(
    *,
    baseline_levels: Sequence[BookLevel | dict[str, Any]],
    events: Sequence[LevelChangeEvent | LevelEvent],
    window_start_ns: int,
    window_end_ns: int,
) -> dict[tuple[str, float], float]:
    """Track max size per level over half-open [window_start_ns, window_end_ns).

    Starts from baseline, then applies in-window events. Removed levels outside
    the later band filter are still tracked here for completeness.
    """
    sizes: dict[tuple[str, float], float] = {}
    for lvl in baseline_levels:
        if isinstance(lvl, BookLevel):
            side, price, size = lvl.side, float(lvl.price), float(lvl.size)
        else:
            side, price, size = str(lvl["side"]), float(lvl["price"]), float(lvl["size"])
        sizes[(side, price)] = size

    for ev in events:
        if isinstance(ev, LevelChangeEvent):
            ets = int(ev.event_time_ns)
            side, price, new_size = ev.side, float(ev.price), float(ev.new_size)
        else:
            ets = dt_to_ns(ev.event_time)
            side, price, new_size = ev.side, float(ev.price), float(ev.new_size)
        if ets < window_start_ns or ets >= window_end_ns:
            continue
        key = (side, price)
        prev = sizes.get(key, 0.0)
        # Max-observed size for Q95 (walls that grow into threshold stay eligible).
        # Size-0 removes do not create new candidates and do not wipe prior max.
        if new_size > 0:
            sizes[key] = max(prev, new_size)
        elif key not in sizes:
            sizes[key] = 0.0
    return sizes


def select_walls_by_notional(
    sizes_by_level: dict[tuple[str, float], float],
    *,
    ref_price: float,
    local_band_usd: float,
    q: float = 0.95,
    max_rank: int = 12,
) -> tuple[float, list[dict[str, Any]]]:
    """Q95 on USDT notional; only levels inside local_band_usd of ref."""
    in_band: dict[tuple[str, float], tuple[float, float]] = {}
    for (side, price), size in sizes_by_level.items():
        if abs(float(price) - float(ref_price)) > float(local_band_usd):
            continue
        if float(size) <= 0:
            continue
        notional = wall_notional_usdt(price, size)
        in_band[(side, price)] = (float(size), notional)
    if not in_band:
        return 0.0, []
    thr = quantile([n for _, n in in_band.values()], q)
    candidates = [
        {
            "side": s,
            "price": p,
            "size_base": sz,
            "notional_usdt": n,
        }
        for (s, p), (sz, n) in in_band.items()
        if n >= thr
    ]
    candidates.sort(key=lambda x: x["notional_usdt"], reverse=True)
    ranked = candidates[:max_rank]
    for i, c in enumerate(ranked, start=1):
        c["local_rank"] = i
        c["distance_to_ref"] = abs(float(c["price"]) - float(ref_price))
    return thr, ranked


def level_events_for_wall(
    events: Sequence[LevelChangeEvent],
    *,
    side: str,
    price: float,
    tick_tolerance: float = 0.0,
) -> list[LevelEvent]:
    out: list[LevelEvent] = []
    for e in events:
        if e.side != side:
            continue
        if abs(float(e.price) - float(price)) > tick_tolerance:
            continue
        out.append(
            LevelEvent(
                event_time=ns_to_dt(e.event_time_ns),
                side=e.side,
                price=float(e.price),
                change_type=e.change_type,
                new_size=float(e.new_size),
                old_size=None if e.old_size is None else float(e.old_size),
            )
        )
    out.sort(key=lambda x: x.event_time)
    return out


def classify_wall_lifecycle(
    *,
    baseline_size: float,
    events: Sequence[LevelEvent],
    first_aggressive_touch: datetime | None,
    hit_notional: float = 0.0,
    baseline_complete: bool = True,
    neighbor_growth: bool = False,
    side: str | None = None,
    price: float | None = None,
    matching_confidence: str | None = None,
) -> WallLifecycle:
    if not events and baseline_size <= 0:
        side_v, price_v = side or "?", price if price is not None else 0.0
    else:
        side_v = side or (events[0].side if events else "?")
        price_v = price if price is not None else (events[0].price if events else 0.0)
    max_size = max([baseline_size] + [e.new_size for e in events], default=baseline_size)
    last_size = events[-1].new_size if events else baseline_size
    life = WallLifecycle(
        side=side_v,
        price=float(price_v),
        baseline_size=baseline_size,
        max_size=max_size,
        last_size=last_size,
        size_base=baseline_size,
        notional_usdt=wall_notional_usdt(float(price_v), baseline_size),
        events=list(events),
    )
    if not baseline_complete:
        life.classification = WallClass.UNRESOLVED_BASELINE
        life.evidence["reason"] = "UNRESOLVED_BASELINE"
        return life

    if matching_confidence == "LOW" and hit_notional <= 0 and first_aggressive_touch is None:
        # insufficient temporal association
        if events and not _has_rich_path(events, baseline_size):
            life.classification = WallClass.UNRESOLVED
            life.evidence = {"reason": "LOW_MATCHING_CONFIDENCE"}
            return life

    vanished = last_size <= 1e-9 and max_size > 0
    grew = last_size > baseline_size * 1.05 and last_size > 0
    decayed = last_size < baseline_size * 0.95 and last_size > 1e-9

    # Refill: size dropped then recovered — requires path evidence, not first/last alone
    sizes = [baseline_size] + [e.new_size for e in events]
    had_dip = any(s < baseline_size * 0.5 for s in sizes)
    recovered = last_size >= baseline_size * 0.9 and had_dip
    if recovered and hit_notional > 0 and len(events) >= 2:
        life.classification = WallClass.REFILL
        life.evidence = {"hit_notional": hit_notional, "had_dip": True}
        return life

    if vanished and first_aggressive_touch is not None:
        vanish_t = None
        for e in events:
            if e.new_size <= 1e-9:
                vanish_t = e.event_time
                break
        touch = first_aggressive_touch.astimezone(timezone.utc)
        if vanish_t is not None and vanish_t.astimezone(timezone.utc) < touch:
            life.classification = WallClass.PULL
            life.evidence = {
                "vanish_utc": vanish_t.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "touch_utc": touch.isoformat().replace("+00:00", "Z"),
            }
            return life
        if neighbor_growth:
            life.classification = WallClass.RELOCATION
            life.evidence = {
                "neighbor_growth": True,
                "hit_notional": hit_notional,
            }
            return life
        if vanish_t is not None and vanish_t.astimezone(timezone.utc) >= touch:
            life.classification = WallClass.CONSUMPTION
            life.evidence = {
                "vanish_utc": vanish_t.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "touch_utc": touch.isoformat().replace("+00:00", "Z"),
                "hit_notional": hit_notional,
            }
            return life

    if hit_notional > 0 and not vanished and last_size >= baseline_size * 0.5:
        life.classification = WallClass.ABSORPTION
        life.evidence = {
            "hit_notional": hit_notional,
            "remaining_frac": last_size / max(baseline_size, 1e-12),
        }
        return life

    if neighbor_growth and vanished:
        life.classification = WallClass.RELOCATION
        life.evidence = {"neighbor_growth": True}
        return life

    if grew and _has_rich_path(events, baseline_size):
        life.classification = WallClass.GROWTH
        return life
    if decayed and _has_rich_path(events, baseline_size):
        life.classification = WallClass.DECAY
        return life

    # Refuse safe class from first_size/last_size alone
    if len(events) < 2 and first_aggressive_touch is None and hit_notional <= 0:
        life.classification = WallClass.UNRESOLVED
        life.evidence = {"reason": "insufficient_path_evidence"}
        return life

    life.classification = WallClass.UNRESOLVED
    life.evidence = {"reason": "insufficient_evidence"}
    return life


def _has_rich_path(events: Sequence[LevelEvent], baseline_size: float) -> bool:
    if len(events) < 2:
        return False
    sizes = [baseline_size] + [e.new_size for e in events]
    return max(sizes) != min(sizes)

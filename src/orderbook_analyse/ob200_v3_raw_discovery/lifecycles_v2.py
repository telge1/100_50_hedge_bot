"""V2 wall lifecycles, overlap audit, non-overlapping primary chains."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from orderbook_analyse.ob200_v3_raw_discovery.walls import WallEvent

# Completion classes V2
COMPLETION_CLASSES = (
    "TOUCH_ONLY",
    "ABSORBED",
    "PULLED",
    "BROKEN_NO_RECLAIM",
    "BROKEN_RECLAIMED",
    "COMPLETE_PRIMARY",  # touch + (absorb|pull|break) + reclaim, primary non-overlap
    "PARTIAL",
)


@dataclass
class WallLifecycle:
    lifecycle_id: str
    symbol: str
    side: str
    direction: str
    wall_price: float
    appear_ts: int
    approach_ts: int | None
    touch_ts: int | None
    absorption_ts: int | None
    pull_ts: int | None
    break_ts: int | None
    reclaim_ts: int | None
    end_ts: int
    peak_qty: float
    completion_class: str
    n_touch_events: int
    source_file: str


@dataclass
class ChainV2:
    chain_id: str
    lifecycle_id: str
    symbol: str
    direction: str
    is_primary: bool
    completion_class: str
    touch_ts: int | None
    absorption_ts: int | None
    pull_ts: int | None
    break_ts: int | None
    reclaim_ts: int | None
    wall_price: float
    stages: str
    cooldown_s: float
    overlap_group: str


@dataclass
class OverlapAuditRow:
    symbol: str
    direction: str
    v1_chains_total: int
    v1_chains_complete: int
    v1_complete_share_of_touches: float
    overlapping_complete_pairs: int
    mean_complete_chain_overlap_s: float
    root_cause: str
    lifecycles: int
    primary_chains_v2: int
    complete_primary_v2: int


def _price_key(price: float, mid: float) -> float:
    # cluster walls within ~2 bps
    if mid <= 0:
        return round(price, 4)
    tick = mid * 2e-4  # 2 bps
    return round(price / tick) * tick if tick > 0 else price


def build_wall_lifecycles(
    events: Sequence[WallEvent],
    *,
    cluster_bps: float = 2.0,
    cooldown_ms: int = 60_000,
    seed: int = 42,
) -> list[WallLifecycle]:
    """Group atomic wall events into price-clustered lifecycles with cooldown."""
    by_key: dict[tuple[str, str], list[WallEvent]] = defaultdict(list)
    for ev in events:
        by_key[(ev.symbol, ev.side)].append(ev)

    lifecycles: list[WallLifecycle] = []
    n = 0
    for (symbol, side), group in sorted(by_key.items()):
        group = sorted(group, key=lambda e: e.ts_ms)
        active: dict[str, Any] | None = None

        def _close_active() -> None:
            nonlocal n, active
            if active is None:
                return
            n += 1
            touch = active.get("touch_ts")
            absorb = active.get("absorption_ts")
            pull = active.get("pull_ts")
            brk = active.get("break_ts")
            reclaim = active.get("reclaim_ts")
            if touch and reclaim and (absorb or pull or brk):
                cls = "COMPLETE_PRIMARY"
            elif brk and reclaim:
                cls = "BROKEN_RECLAIMED"
            elif brk and not reclaim:
                cls = "BROKEN_NO_RECLAIM"
            elif absorb and not brk:
                cls = "ABSORBED"
            elif pull and not brk:
                cls = "PULLED"
            elif touch:
                cls = "TOUCH_ONLY"
            else:
                cls = "PARTIAL"
            lifecycles.append(
                WallLifecycle(
                    lifecycle_id=f"lc_{seed}_{n}",
                    symbol=symbol,
                    side=side,
                    direction=active["direction"],
                    wall_price=active["price"],
                    appear_ts=active["appear_ts"],
                    approach_ts=active.get("approach_ts"),
                    touch_ts=touch,
                    absorption_ts=absorb,
                    pull_ts=pull,
                    break_ts=brk,
                    reclaim_ts=reclaim,
                    end_ts=active["end_ts"],
                    peak_qty=active["peak_qty"],
                    completion_class=cls,
                    n_touch_events=active["n_touch"],
                    source_file=active["source_file"],
                )
            )
            active = None

        for ev in group:
            mid = ev.mid or ev.wall_price
            pk = _price_key(ev.wall_price, mid)
            # open new lifecycle on APPEAR or if cooldown elapsed / price moved
            if active is not None:
                same = abs(active["price"] - ev.wall_price) / max(mid, 1e-12) * 10000 <= cluster_bps
                cooled = ev.ts_ms - active["end_ts"] > cooldown_ms
                if (not same) or (cooled and ev.event_type == "WALL_APPEAR"):
                    _close_active()

            if active is None:
                if ev.event_type not in {
                    "WALL_APPEAR",
                    "WALL_APPROACH",
                    "WALL_TOUCH",
                }:
                    continue
                active = {
                    "price": ev.wall_price,
                    "pk": pk,
                    "direction": ev.direction,
                    "appear_ts": ev.ts_ms,
                    "approach_ts": None,
                    "touch_ts": None,
                    "absorption_ts": None,
                    "pull_ts": None,
                    "break_ts": None,
                    "reclaim_ts": None,
                    "end_ts": ev.ts_ms,
                    "peak_qty": ev.wall_qty,
                    "n_touch": 0,
                    "source_file": ev.source_file,
                }

            assert active is not None
            active["end_ts"] = max(active["end_ts"], ev.ts_ms)
            active["peak_qty"] = max(active["peak_qty"], ev.wall_qty)
            if ev.event_type == "WALL_APPROACH" and active["approach_ts"] is None:
                active["approach_ts"] = ev.ts_ms
            elif ev.event_type == "WALL_TOUCH":
                active["n_touch"] += 1
                if active["touch_ts"] is None:
                    active["touch_ts"] = ev.ts_ms
            elif ev.event_type == "WALL_ABSORPTION_PROXY" and active["absorption_ts"] is None:
                active["absorption_ts"] = ev.ts_ms
            elif ev.event_type == "WALL_PULL" and active["pull_ts"] is None:
                active["pull_ts"] = ev.ts_ms
            elif ev.event_type == "WALL_BREAK" and active["break_ts"] is None:
                active["break_ts"] = ev.ts_ms
            elif ev.event_type == "WALL_RECLAIM" and active["reclaim_ts"] is None:
                active["reclaim_ts"] = ev.ts_ms

        _close_active()
    return lifecycles


def build_chains_v2(
    lifecycles: Sequence[WallLifecycle],
    *,
    seed: int = 42,
) -> list[ChainV2]:
    """One primary non-overlapping chain per lifecycle (non-overlap by construction)."""
    chains: list[ChainV2] = []
    by_sym_dir: dict[tuple[str, str], list[WallLifecycle]] = defaultdict(list)
    for lc in lifecycles:
        by_sym_dir[(lc.symbol, lc.direction)].append(lc)

    n = 0
    for (symbol, direction), group in sorted(by_sym_dir.items()):
        group = sorted(group, key=lambda x: x.appear_ts)
        last_end = -10**18
        for lc in group:
            n += 1
            stages = []
            if lc.touch_ts:
                stages.append("TOUCH")
            if lc.absorption_ts:
                stages.append("ABSORPTION")
            if lc.pull_ts:
                stages.append("PULL")
            if lc.break_ts:
                stages.append("BREAK")
            if lc.reclaim_ts:
                stages.append("RECLAIM")
            # primary if does not overlap previous primary window
            is_primary = lc.appear_ts >= last_end
            if is_primary:
                last_end = lc.end_ts
            cls = lc.completion_class
            if is_primary and cls == "COMPLETE_PRIMARY":
                pass
            elif is_primary and lc.touch_ts and lc.reclaim_ts and (lc.absorption_ts or lc.pull_ts or lc.break_ts):
                cls = "COMPLETE_PRIMARY"
            elif not is_primary and cls == "COMPLETE_PRIMARY":
                cls = "PARTIAL"  # demote overlapping duplicate lifecycle
            chains.append(
                ChainV2(
                    chain_id=f"v2_{seed}_{n}",
                    lifecycle_id=lc.lifecycle_id,
                    symbol=symbol,
                    direction=direction,
                    is_primary=is_primary,
                    completion_class=cls,
                    touch_ts=lc.touch_ts,
                    absorption_ts=lc.absorption_ts,
                    pull_ts=lc.pull_ts,
                    break_ts=lc.break_ts,
                    reclaim_ts=lc.reclaim_ts,
                    wall_price=lc.wall_price,
                    stages=">".join(stages) if stages else "NONE",
                    cooldown_s=60.0,
                    overlap_group=f"{symbol}:{direction}",
                )
            )
    return chains


def audit_v1_chain_overcount(
    v1_chains: Sequence[Any],
    events: Sequence[WallEvent],
    lifecycles: Sequence[WallLifecycle],
    chains_v2: Sequence[ChainV2],
) -> list[OverlapAuditRow]:
    """Explain V1 1-chain-per-TOUCH overcount vs V2 primary lifecycles."""
    rows: list[OverlapAuditRow] = []
    for symbol in sorted({e.symbol for e in events} | {c.symbol for c in v1_chains}):
        for direction in ("LONG", "SHORT"):
            v1 = [c for c in v1_chains if c.symbol == symbol and c.direction == direction]
            complete = [c for c in v1 if getattr(c, "complete", False)]
            touches = [
                e
                for e in events
                if e.symbol == symbol and e.direction == direction and e.event_type == "WALL_TOUCH"
            ]
            # overlap among complete chains: intervals [touch, reclaim]
            intervals = []
            for c in complete:
                t0 = c.touch_ts
                t1 = c.reclaim_ts or c.break_ts or c.touch_ts
                if t0 is not None and t1 is not None:
                    intervals.append((t0, t1))
            intervals.sort()
            overlap_pairs = 0
            overlap_secs: list[float] = []
            for i in range(len(intervals)):
                for j in range(i + 1, len(intervals)):
                    a0, a1 = intervals[i]
                    b0, b1 = intervals[j]
                    if b0 >= a1:
                        break
                    ov = min(a1, b1) - max(a0, b0)
                    if ov > 0:
                        overlap_pairs += 1
                        overlap_secs.append(ov / 1000.0)
            lcs = [x for x in lifecycles if x.symbol == symbol and x.direction == direction]
            prim = [
                c
                for c in chains_v2
                if c.symbol == symbol and c.direction == direction and c.is_primary
            ]
            complete_p = [c for c in prim if c.completion_class == "COMPLETE_PRIMARY"]
            rows.append(
                OverlapAuditRow(
                    symbol=symbol,
                    direction=direction,
                    v1_chains_total=len(v1),
                    v1_chains_complete=len(complete),
                    v1_complete_share_of_touches=(
                        len(complete) / len(touches) if touches else 0.0
                    ),
                    overlapping_complete_pairs=overlap_pairs,
                    mean_complete_chain_overlap_s=(
                        sum(overlap_secs) / len(overlap_secs) if overlap_secs else 0.0
                    ),
                    root_cause=(
                        "V1 emits one chain per WALL_TOUCH; consecutive touches on the same "
                        "wall share reclaim windows → near 1:1 complete/touch overcount."
                    ),
                    lifecycles=len(lcs),
                    primary_chains_v2=len(prim),
                    complete_primary_v2=len(complete_p),
                )
            )
    return rows


def funnel_v2(
    lifecycles: Sequence[WallLifecycle],
    chains: Sequence[ChainV2],
) -> list[dict[str, Any]]:
    rows = []
    for symbol in sorted({lc.symbol for lc in lifecycles} | {c.symbol for c in chains}):
        for direction in ("LONG", "SHORT"):
            lcs = [x for x in lifecycles if x.symbol == symbol and x.direction == direction]
            prim = [c for c in chains if c.symbol == symbol and c.direction == direction and c.is_primary]

            def n_cls(name: str) -> int:
                return sum(1 for x in lcs if x.completion_class == name)

            rows.append(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "lifecycles": len(lcs),
                    "with_touch": sum(1 for x in lcs if x.touch_ts),
                    "absorbed": n_cls("ABSORBED"),
                    "pulled": n_cls("PULLED"),
                    "broken_no_reclaim": n_cls("BROKEN_NO_RECLAIM"),
                    "broken_reclaimed": n_cls("BROKEN_RECLAIMED"),
                    "complete_primary_class": n_cls("COMPLETE_PRIMARY"),
                    "primary_chains": len(prim),
                    "complete_primary_chains": sum(
                        1 for c in prim if c.completion_class == "COMPLETE_PRIMARY"
                    ),
                    "touch_only": n_cls("TOUCH_ONLY"),
                    "partial": n_cls("PARTIAL"),
                }
            )
    return rows


def lifecycle_to_row(lc: WallLifecycle) -> dict[str, Any]:
    return asdict(lc)


def chain_v2_to_row(c: ChainV2) -> dict[str, Any]:
    return asdict(c)


def overlap_to_row(r: OverlapAuditRow) -> dict[str, Any]:
    return asdict(r)

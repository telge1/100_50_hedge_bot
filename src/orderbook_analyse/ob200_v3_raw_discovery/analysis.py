"""Event chains, matched controls, and causal outcomes (discovery-scale)."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow
from orderbook_analyse.ob200_v3_raw_discovery.walls import WallEvent

HORIZONS_S = (1, 3, 5, 10, 30, 60, 180, 300, 900, 1800, 3600)


@dataclass
class EventChain:
    chain_id: str
    symbol: str
    direction: str
    complete: bool
    touch_ts: int | None
    absorption_ts: int | None
    pull_ts: int | None
    break_ts: int | None
    reclaim_ts: int | None
    recovery_ts: int | None
    wall_price: float | None
    stages: str


@dataclass
class OutcomeRow:
    event_id: str
    symbol: str
    direction: str
    event_type: str
    ts_ms: int
    is_control: bool
    horizon_s: int
    forward_return_bps: float | None
    mfe_bps: float | None
    mae_bps: float | None
    mid0: float


def build_chains(events: Sequence[WallEvent], *, seed: int = 0) -> list[EventChain]:
    """Link touch → absorption/pull/break → reclaim within a causal window."""
    by_sym_dir: dict[tuple[str, str], list[WallEvent]] = defaultdict(list)
    for ev in events:
        by_sym_dir[(ev.symbol, ev.direction)].append(ev)
    chains: list[EventChain] = []
    n = 0
    for (symbol, direction), group in by_sym_dir.items():
        group = sorted(group, key=lambda e: e.ts_ms)
        touches = [e for e in group if e.event_type == "WALL_TOUCH"]
        for touch in touches:
            window_end = touch.ts_ms + 300_000  # 5 min causal forward
            later = [e for e in group if touch.ts_ms <= e.ts_ms <= window_end]
            abs_e = next((e for e in later if e.event_type == "WALL_ABSORPTION_PROXY"), None)
            pull_e = next((e for e in later if e.event_type == "WALL_PULL"), None)
            brk = next((e for e in later if e.event_type == "WALL_BREAK"), None)
            reclaim = None
            recovery = None
            if brk is not None:
                after = [e for e in group if brk.ts_ms <= e.ts_ms <= brk.ts_ms + 600_000]
                reclaim = next((e for e in after if e.event_type == "WALL_RECLAIM"), None)
                recovery = next((e for e in after if e.event_type == "DEPTH_RECOVERY"), None)
            elif abs_e is not None:
                after = [e for e in group if abs_e.ts_ms <= e.ts_ms <= abs_e.ts_ms + 600_000]
                reclaim = next((e for e in after if e.event_type == "WALL_RECLAIM"), None)
            stages = []
            if touch:
                stages.append("TOUCH")
            if abs_e:
                stages.append("ABSORPTION")
            if pull_e:
                stages.append("PULL")
            if brk:
                stages.append("BREAK")
            if reclaim:
                stages.append("RECLAIM")
            if recovery:
                stages.append("RECOVERY")
            complete = "TOUCH" in stages and ("ABSORPTION" in stages or "PULL" in stages) and "RECLAIM" in stages
            n += 1
            chains.append(
                EventChain(
                    chain_id=f"chain_{seed}_{n}",
                    symbol=symbol,
                    direction=direction,
                    complete=complete,
                    touch_ts=touch.ts_ms,
                    absorption_ts=None if abs_e is None else abs_e.ts_ms,
                    pull_ts=None if pull_e is None else pull_e.ts_ms,
                    break_ts=None if brk is None else brk.ts_ms,
                    reclaim_ts=None if reclaim is None else reclaim.ts_ms,
                    recovery_ts=None if recovery is None else recovery.ts_ms,
                    wall_price=touch.wall_price,
                    stages=">".join(stages),
                )
            )
    return chains


def _mid_at(samples: list[SampleRow], ts_ms: int) -> float | None:
    # samples sorted; last sample at or before ts
    lo, hi = 0, len(samples) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms <= ts_ms:
            best = samples[mid].mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _path_mids(samples: list[SampleRow], t0: int, t1: int) -> list[float]:
    if not samples:
        return []
    # binary search left bound
    lo, hi = 0, len(samples)
    while lo < hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms < t0:
            lo = mid + 1
        else:
            hi = mid
    left = lo
    lo, hi = left, len(samples)
    while lo < hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms <= t1:
            lo = mid + 1
        else:
            hi = mid
    return [samples[i].mid for i in range(left, lo)]


def compute_outcomes(
    events: Sequence[WallEvent],
    samples_by_symbol: dict[str, list[SampleRow]],
    *,
    is_control: bool = False,
) -> list[OutcomeRow]:
    primary = {
        "WALL_TOUCH",
        "WALL_ABSORPTION_PROXY",
        "WALL_PULL",
        "WALL_BREAK",
        "WALL_RECLAIM",
        "CONTROL_WALL_TOUCH",
        "CONTROL_WALL_ABSORPTION_PROXY",
        "CONTROL_WALL_RECLAIM",
    }
    out: list[OutcomeRow] = []
    for ev in events:
        if ev.event_type not in primary:
            continue
        samples = samples_by_symbol.get(ev.symbol) or []
        if not samples:
            continue
        mid0 = _mid_at(samples, ev.ts_ms)
        if mid0 is None or mid0 <= 0:
            continue
        for h in HORIZONS_S:
            t1 = ev.ts_ms + h * 1000
            path = _path_mids(samples, ev.ts_ms, t1)
            if len(path) < 2:
                out.append(
                    OutcomeRow(
                        event_id=ev.event_id,
                        symbol=ev.symbol,
                        direction=ev.direction,
                        event_type=ev.event_type,
                        ts_ms=ev.ts_ms,
                        is_control=is_control,
                        horizon_s=h,
                        forward_return_bps=None,
                        mfe_bps=None,
                        mae_bps=None,
                        mid0=mid0,
                    )
                )
                continue
            end = path[-1]
            if ev.direction == "LONG":
                rets = [(m - mid0) / mid0 * 10000 for m in path]
            else:
                rets = [(mid0 - m) / mid0 * 10000 for m in path]
            out.append(
                OutcomeRow(
                    event_id=ev.event_id,
                    symbol=ev.symbol,
                    direction=ev.direction,
                    event_type=ev.event_type,
                    ts_ms=ev.ts_ms,
                    is_control=is_control,
                    horizon_s=h,
                    forward_return_bps=(end - mid0) / mid0 * 10000
                    if ev.direction == "LONG"
                    else (mid0 - end) / mid0 * 10000,
                    mfe_bps=max(rets),
                    mae_bps=min(rets),
                    mid0=mid0,
                )
            )
    return out


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    out = [ordered[0]]
    for a, b in ordered[1:]:
        la, lb = out[-1]
        if a <= lb:
            out[-1] = (la, max(lb, b))
        else:
            out.append((a, b))
    return out


def _in_merged(ts: int, merged: list[tuple[int, int]]) -> bool:
    lo, hi = 0, len(merged) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        a, b = merged[mid]
        if ts < a:
            hi = mid - 1
        elif ts > b:
            lo = mid + 1
        else:
            return True
    return False


def matched_controls(
    events: Sequence[WallEvent],
    samples_by_symbol: dict[str, list[SampleRow]],
    *,
    controls_per_event: int = 3,
    seed: int = 42,
    max_events_per_group: int = 80,
) -> list[WallEvent]:
    """Sample control timestamps matched on symbol/hour/spread; exclude event windows.

    Forbidden windows use only primary event types (touch/absorption/reclaim),
    otherwise APPEAR-dense hours leave zero eligible control times.
    """
    rng = random.Random(seed)
    primary = {"WALL_TOUCH", "WALL_ABSORPTION_PROXY", "WALL_RECLAIM"}
    forbidden_raw: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for ev in events:
        if ev.event_type in primary:
            forbidden_raw[ev.symbol].append((ev.ts_ms - 30_000, ev.ts_ms + 120_000))
    forbidden = {sym: _merge_intervals(ivs) for sym, ivs in forbidden_raw.items()}

    by_group: dict[tuple[str, str, str], list[WallEvent]] = defaultdict(list)
    for ev in events:
        if ev.event_type in primary:
            by_group[(ev.symbol, ev.direction, ev.event_type)].append(ev)

    controls: list[WallEvent] = []
    cid = 0
    for key, group in sorted(by_group.items()):
        group = sorted(group, key=lambda e: e.ts_ms)
        if len(group) > max_events_per_group:
            step = max(1, len(group) // max_events_per_group)
            group = group[::step][:max_events_per_group]
        samples_all = [s for s in samples_by_symbol.get(key[0], []) if not s.warmup]
        if len(samples_all) < 100:
            continue
        merged = forbidden.get(key[0], [])
        free = [s for s in samples_all if not _in_merged(s.ts_ms, merged)]
        if len(free) < 20:
            # If primary windows cover most of the hour, shrink exclusion to ±10s.
            tight = _merge_intervals(
                [(e.ts_ms - 10_000, e.ts_ms + 10_000) for e in group]
            )
            free = [s for s in samples_all if not _in_merged(s.ts_ms, tight)]
        if not free:
            continue
        for ev in group:
            hour = (ev.ts_ms // 3_600_000) % 24
            spread_bucket = int(ev.spread_bps)
            candidates = [
                s
                for s in free
                if (s.ts_ms // 3_600_000) % 24 == hour
                and abs(int(s.spread_bps) - spread_bucket) <= 5
            ]
            if not candidates:
                candidates = [s for s in free if (s.ts_ms // 3_600_000) % 24 == hour]
            if not candidates:
                candidates = free
            picks = rng.sample(candidates, k=min(controls_per_event, len(candidates)))
            for s in picks:
                cid += 1
                controls.append(
                    WallEvent(
                        event_id=f"ctrl_{seed}_{cid}",
                        symbol=s.symbol,
                        side=ev.side,
                        direction=ev.direction,
                        event_type=f"CONTROL_{ev.event_type}",
                        ts_ms=s.ts_ms,
                        wall_price=s.bid_wall_price or s.ask_wall_price or s.mid,
                        wall_qty=s.bid_wall_qty or s.ask_wall_qty or 0.0,
                        wall_dist_bps=0.0,
                        mid=s.mid,
                        best_bid=s.best_bid,
                        best_ask=s.best_ask,
                        spread_bps=s.spread_bps,
                        imbalance_l10=s.imbalance_l10,
                        qty_vs_median=0.0,
                        persistence_s=0.0,
                        source_file=s.source_file,
                        threshold_qty_median_mult=ev.threshold_qty_median_mult,
                        notes=f"matched_to={ev.event_id}",
                    )
                )
    return controls


def summarize_outcomes(rows: Sequence[OutcomeRow]) -> list[dict[str, Any]]:
    groups: dict[tuple, list[OutcomeRow]] = defaultdict(list)
    for r in rows:
        if r.forward_return_bps is None:
            continue
        key = (r.symbol, r.direction, r.event_type, r.horizon_s, r.is_control)
        groups[key].append(r)
    out = []
    for key, grp in sorted(groups.items()):
        vals = [g.forward_return_bps for g in grp if g.forward_return_bps is not None]
        if not vals:
            continue
        vals_sorted = sorted(vals)
        mid = len(vals_sorted) // 2
        median = vals_sorted[mid] if len(vals_sorted) % 2 else 0.5 * (vals_sorted[mid - 1] + vals_sorted[mid])
        mean = sum(vals) / len(vals)
        # simple bootstrap CI
        rng = random.Random(0)
        boots = []
        for _ in range(200):
            sample = [vals[rng.randrange(len(vals))] for _ in range(len(vals))]
            boots.append(sum(sample) / len(sample))
        boots.sort()
        out.append(
            {
                "symbol": key[0],
                "direction": key[1],
                "event_type": key[2],
                "horizon_s": key[3],
                "is_control": key[4],
                "n": len(vals),
                "mean_fwd_bps": mean,
                "median_fwd_bps": median,
                "ci80_low": boots[int(0.1 * len(boots))],
                "ci80_high": boots[int(0.9 * len(boots))],
            }
        )
    return out


def funnel_counts(events: Sequence[WallEvent], chains: Sequence[EventChain]) -> list[dict[str, Any]]:
    rows = []
    for symbol in sorted({e.symbol for e in events}):
        for direction in ("LONG", "SHORT"):
            sub = [e for e in events if e.symbol == symbol and e.direction == direction]
            ch = [c for c in chains if c.symbol == symbol and c.direction == direction]
            def n(t: str) -> int:
                return sum(1 for e in sub if e.event_type == t)
            rows.append(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "appear": n("WALL_APPEAR"),
                    "approach": n("WALL_APPROACH"),
                    "touch": n("WALL_TOUCH"),
                    "absorption": n("WALL_ABSORPTION_PROXY"),
                    "pull": n("WALL_PULL"),
                    "break": n("WALL_BREAK"),
                    "reclaim": n("WALL_RECLAIM"),
                    "depth_recovery": n("DEPTH_RECOVERY"),
                    "chains_total": len(ch),
                    "chains_complete": sum(1 for c in ch if c.complete),
                }
            )
    return rows

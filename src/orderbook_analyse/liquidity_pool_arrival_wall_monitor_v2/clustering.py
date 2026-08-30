"""Interval connected components + market arrival clustering."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class PoolInterval:
    pool_id: str
    side: str
    lower: float
    upper: float
    available_at_ms: int
    invalidated_at_ms: int | None


@dataclass
class Component:
    component_id: str
    side: str
    member_pool_ids: list[str]
    lower: float
    upper: float


class UnionFind:
    def __init__(self, ids: Iterable[str]):
        id_list = list(ids)
        self.parent = {i: i for i in id_list}
        self.rank = {i: 0 for i in id_list}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1


def intervals_connected(a: PoolInterval, b: PoolInterval) -> bool:
    """Overlap or touch (inclusive edges)."""
    return max(a.lower, b.lower) <= min(a.upper, b.upper)


def build_components(side: str, pools: list[PoolInterval], *, as_of_ms: int) -> list[Component]:
    active = [
        p
        for p in pools
        if p.side == side
        and p.available_at_ms <= as_of_ms
        and (p.invalidated_at_ms is None or as_of_ms < p.invalidated_at_ms)
    ]
    if not active:
        return []
    uf = UnionFind(p.pool_id for p in active)
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            if intervals_connected(active[i], active[j]):
                uf.union(active[i].pool_id, active[j].pool_id)
    groups: dict[str, list[PoolInterval]] = {}
    for p in active:
        groups.setdefault(uf.find(p.pool_id), []).append(p)
    comps: list[Component] = []
    for root, members in groups.items():
        lo = min(m.lower for m in members)
        hi = max(m.upper for m in members)
        mids = sorted(m.pool_id for m in members)
        cid = f"{side}|{root}|{lo:.6g}|{hi:.6g}|n={len(mids)}"
        comps.append(
            Component(component_id=cid, side=side, member_pool_ids=mids, lower=lo, upper=hi)
        )
    return comps


def component_for_pool(comps: list[Component], pool_id: str) -> Component | None:
    for c in comps:
        if pool_id in c.member_pool_ids:
            return c
    return None


def component_key_from_members(member_pool_ids: list[str]) -> str:
    return "|".join(sorted(member_pool_ids))


def stable_cluster_id(
    *,
    symbol: str,
    side: str,
    approach: str,
    start_ts_ms: int,
    first_pool_id: str,
    component_key: str,
) -> str:
    raw = f"{symbol}|{side}|{approach}|{start_ts_ms}|{first_pool_id}|{component_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


@dataclass
class ActiveCluster:
    cluster_id: str
    side: str
    approach: str
    start_ts_ms: int
    first_pool_id: str
    component_key_at_start: str
    union_lower: float
    union_upper: float
    member_pool_ids: set[str] = field(default_factory=set)
    pool_arrival_ids: list[str] = field(default_factory=list)
    membership_events: list[dict] = field(default_factory=list)
    ended: bool = False
    end_ts_ms: int | None = None
    end_reason: str | None = None


def assign_market_clusters(
    *,
    symbol: str,
    pool_arrivals: list[dict],
    pools: list[PoolInterval],
    mids: list[tuple[int, float, bool]],
) -> tuple[list[dict], list[ActiveCluster], list[dict]]:
    """Chronological mid walk: component unions + attach pool arrivals.

    Merge rule: same side+approach, same connected component, still inside union
    since cluster start. Time proximity alone is insufficient.
    """
    arrivals_by_ts: dict[int, list[dict]] = {}
    for a in pool_arrivals:
        arrivals_by_ts.setdefault(int(a["arrival_ts_ms"]), []).append(a)

    open_lists: dict[tuple[str, str], list[ActiveCluster]] = {
        ("ASK", "FROM_BELOW"): [],
        ("BID", "FROM_ABOVE"): [],
    }
    finished: list[ActiveCluster] = []
    membership_rows: list[dict] = []
    enriched = {a["pool_arrival_id"]: dict(a) for a in pool_arrivals}

    prev_mid: float | None = None
    prev_genuine = False

    def end_cluster(cl: ActiveCluster, ts_ms: int, reason: str) -> None:
        if cl.ended:
            return
        cl.ended = True
        cl.end_ts_ms = ts_ms
        cl.end_reason = reason
        finished.append(cl)
        key = (cl.side, cl.approach)
        open_lists[key] = [c for c in open_lists[key] if c is not cl]

    def find_open_for_host(key: tuple[str, str], host_members: set[str]) -> ActiveCluster | None:
        for cl in open_lists[key]:
            if not cl.ended and (cl.member_pool_ids & host_members):
                return cl
        return None

    for ts_ms, mid, genuine in mids:
        if not genuine:
            continue

        for side in ("ASK", "BID"):
            comps = build_components(side, pools, as_of_ms=ts_ms)
            approach = "FROM_BELOW" if side == "ASK" else "FROM_ABOVE"
            key = (side, approach)

            # Update / exit each open cluster
            for cl in list(open_lists[key]):
                host = None
                for c in comps:
                    if set(c.member_pool_ids) & cl.member_pool_ids:
                        host = c
                        break
                if host is None:
                    end_cluster(cl, ts_ms, "ALL_MEMBERS_INVALIDATED")
                    continue
                new_members = set(host.member_pool_ids)
                added = new_members - cl.member_pool_ids
                removed = cl.member_pool_ids - new_members
                for pid in sorted(added):
                    # non-retroactive: only if available
                    p = next(x for x in pools if x.pool_id == pid)
                    if p.available_at_ms > ts_ms:
                        continue
                    cl.member_pool_ids.add(pid)
                    ev = {
                        "market_arrival_cluster_id": cl.cluster_id,
                        "pool_id": pid,
                        "membership_valid_from": ts_ms,
                        "membership_valid_to": None,
                        "membership_change_reason": "COMPONENT_EXPANDED",
                    }
                    cl.membership_events.append(ev)
                    membership_rows.append(ev)
                for pid in sorted(removed):
                    cl.member_pool_ids.discard(pid)
                    membership_rows.append(
                        {
                            "market_arrival_cluster_id": cl.cluster_id,
                            "pool_id": pid,
                            "membership_valid_from": None,
                            "membership_valid_to": ts_ms,
                            "membership_change_reason": "POOL_LEFT_COMPONENT",
                        }
                    )
                cl.union_lower = host.lower
                cl.union_upper = host.upper
                if prev_mid is not None and prev_genuine:
                    if side == "ASK" and mid < cl.union_lower:
                        end_cluster(cl, ts_ms, "EXITED_ENTRY_SIDE")
                    elif side == "BID" and mid > cl.union_upper:
                        end_cluster(cl, ts_ms, "EXITED_ENTRY_SIDE")
                    elif side == "ASK" and mid > cl.union_upper:
                        end_cluster(cl, ts_ms, "EXITED_OPPOSITE_SIDE")
                    elif side == "BID" and mid < cl.union_lower:
                        end_cluster(cl, ts_ms, "EXITED_OPPOSITE_SIDE")

            # Component entry → start cluster if no open cluster for that component
            if prev_mid is not None and prev_genuine:
                for c in comps:
                    entered = False
                    if side == "ASK" and prev_mid < c.lower <= mid:
                        entered = True
                    if side == "BID" and prev_mid > c.upper >= mid:
                        entered = True
                    if not entered:
                        continue
                    host_set = set(c.member_pool_ids)
                    if find_open_for_host(key, host_set) is not None:
                        continue
                    ckey = component_key_from_members(c.member_pool_ids)
                    first_pool = c.member_pool_ids[0]
                    for pid in c.member_pool_ids:
                        p = next(x for x in pools if x.pool_id == pid)
                        if side == "ASK" and prev_mid < p.lower <= mid:
                            first_pool = pid
                            break
                        if side == "BID" and prev_mid > p.upper >= mid:
                            first_pool = pid
                            break
                    cid = stable_cluster_id(
                        symbol=symbol,
                        side=side,
                        approach=approach,
                        start_ts_ms=ts_ms,
                        first_pool_id=first_pool,
                        component_key=ckey,
                    )
                    new_cl = ActiveCluster(
                        cluster_id=cid,
                        side=side,
                        approach=approach,
                        start_ts_ms=ts_ms,
                        first_pool_id=first_pool,
                        component_key_at_start=ckey,
                        union_lower=c.lower,
                        union_upper=c.upper,
                        member_pool_ids=set(c.member_pool_ids),
                    )
                    for pid in c.member_pool_ids:
                        membership_rows.append(
                            {
                                "market_arrival_cluster_id": cid,
                                "pool_id": pid,
                                "membership_valid_from": max(
                                    ts_ms,
                                    next(x for x in pools if x.pool_id == pid).available_at_ms,
                                ),
                                "membership_valid_to": None,
                                "membership_change_reason": "CLUSTER_START",
                            }
                        )
                    open_lists[key].append(new_cl)

        # Attach pool arrivals at this timestamp
        for a in arrivals_by_ts.get(ts_ms, []):
            side = a["side"]
            approach = a["approach_direction"]
            key = (side, approach)
            comps = build_components(side, pools, as_of_ms=ts_ms)
            host = component_for_pool(comps, a["pool_id"])
            host_set = set(host.member_pool_ids) if host else {a["pool_id"]}
            cl = find_open_for_host(key, host_set)
            if (
                cl is not None
                and host is not None
                and a["pool_id"] in host.member_pool_ids
                and mid_inside_union(mid, cl.union_lower, cl.union_upper)
            ):
                cl.pool_arrival_ids.append(a["pool_arrival_id"])
                cl.member_pool_ids.add(a["pool_id"])
                enriched[a["pool_arrival_id"]]["market_arrival_cluster_id"] = cl.cluster_id
                enriched[a["pool_arrival_id"]]["attach_reason"] = "MERGED_INTO_ACTIVE_CLUSTER"
            else:
                members = host.member_pool_ids if host else [a["pool_id"]]
                lo = host.lower if host else float(a["lower_edge"])
                hi = host.upper if host else float(a["upper_edge"])
                ckey = component_key_from_members(members)
                cid = stable_cluster_id(
                    symbol=symbol,
                    side=side,
                    approach=approach,
                    start_ts_ms=ts_ms,
                    first_pool_id=a["pool_id"],
                    component_key=ckey,
                )
                # If cluster already started this tick via component entry, reuse it
                existing = find_open_for_host(key, set(members))
                if existing is not None and existing.start_ts_ms == ts_ms:
                    existing.pool_arrival_ids.append(a["pool_arrival_id"])
                    existing.member_pool_ids.add(a["pool_id"])
                    enriched[a["pool_arrival_id"]]["market_arrival_cluster_id"] = existing.cluster_id
                    enriched[a["pool_arrival_id"]]["attach_reason"] = "ATTACH_SAME_TICK_START"
                else:
                    cl = ActiveCluster(
                        cluster_id=cid,
                        side=side,
                        approach=approach,
                        start_ts_ms=ts_ms,
                        first_pool_id=a["pool_id"],
                        component_key_at_start=ckey,
                        union_lower=lo,
                        union_upper=hi,
                        member_pool_ids=set(members),
                        pool_arrival_ids=[a["pool_arrival_id"]],
                    )
                    open_lists[key].append(cl)
                    enriched[a["pool_arrival_id"]]["market_arrival_cluster_id"] = cid
                    enriched[a["pool_arrival_id"]]["attach_reason"] = (
                        "CLUSTER_START" if cl is None else "NEW_CLUSTER_DISJOINT"
                    )

        prev_mid = mid
        prev_genuine = True

    for key, lst in open_lists.items():
        for cl in list(lst):
            if not cl.ended:
                end_cluster(cl, mids[-1][0] if mids else cl.start_ts_ms, "WINDOW_END")

    return list(enriched.values()), finished, membership_rows


def mid_inside_union(mid: float, lo: float, hi: float) -> bool:
    return lo <= mid <= hi

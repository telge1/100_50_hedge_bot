"""Pool / cluster lifecycle scanning on closed candles (causal)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .causality import engine_side_to_bid_ask
from .constants import (
    ACCEPTANCE_BARS,
    APPROACH_ATR_MULT,
    DESTINATION_HORIZONS_MIN,
    REACTION_ATR_MULTS,
    RECLAIM_HORIZON_BARS,
)
from .ema_context import ema_snapshot


def _utc(ts: Any) -> datetime:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def _to_naive(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


@dataclass
class ZoneGeom:
    entity_id: str
    entity_kind: str  # pool | cluster
    symbol: str
    timeframe: str
    side: str  # BID | ASK
    lower: float
    upper: float
    strength: float | None
    known_at: datetime
    component_ids: tuple[str, ...]
    pool_count: int
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def near_edge(self) -> float:
        # approach from market: BID from above → top; ASK from below → bottom
        return self.upper if self.side == "BID" else self.lower

    @property
    def far_edge(self) -> float:
        return self.lower if self.side == "BID" else self.upper

    @property
    def center(self) -> float:
        return (self.lower + self.upper) / 2.0


def pool_to_zone(pool: Any) -> ZoneGeom:
    return ZoneGeom(
        entity_id=pool.pool_id,
        entity_kind="pool",
        symbol=pool.symbol,
        timeframe=pool.timeframe,
        side=engine_side_to_bid_ask(pool.side),
        lower=float(pool.bottom_price),
        upper=float(pool.top_price),
        strength=None if pool.strength is None else float(pool.strength),
        known_at=_utc(pool.created_timestamp),
        component_ids=(pool.pool_id,),
        pool_count=1,
        meta={
            "source_at": _utc(pool.source_timestamp).isoformat(),
            "invalidated_at": (
                None
                if pool.invalidated_timestamp is None
                else _utc(pool.invalidated_timestamp).isoformat()
            ),
        },
    )


def cluster_to_zone(cluster: Any, *, symbol: str, timeframe: str) -> ZoneGeom:
    """Accept TRP PoolCluster or cluster_sweep ClusterSnapshot."""
    side = engine_side_to_bid_ask(cluster.side)
    if hasattr(cluster, "cluster_low"):
        lower = float(cluster.cluster_low)
        upper = float(cluster.cluster_high)
        known = _utc(cluster.newest_created_timestamp)
        oldest = _utc(cluster.oldest_created_timestamp)
        width_abs = float(cluster.cluster_width_abs)
        width_pct = cluster.cluster_width_pct
    else:
        lower = float(cluster.low)
        upper = float(cluster.high)
        known = _utc(cluster.newest_created)
        oldest = _utc(cluster.oldest_created)
        width_abs = float(cluster.width_abs)
        width_pct = cluster.width_pct
    return ZoneGeom(
        entity_id=cluster.cluster_id,
        entity_kind="cluster",
        symbol=symbol,
        timeframe=timeframe,
        side=side,
        lower=lower,
        upper=upper,
        strength=None if cluster.strength_sum is None else float(cluster.strength_sum),
        known_at=known,
        component_ids=tuple(cluster.pool_ids),
        pool_count=int(cluster.pool_count),
        meta={
            "strength_mean": cluster.strength_mean,
            "strength_max": cluster.strength_max,
            "oldest_created": oldest.isoformat(),
            "newest_created": known.isoformat(),
            "width_abs": width_abs,
            "width_pct": width_pct,
        },
    )


def analysis_start_index(df: pd.DataFrame, known_at: datetime) -> int:
    """First closed bar strictly after confirmation bar open (post known_at)."""
    ka = _to_naive(known_at)
    times = pd.to_datetime(df["open_time"])
    hits = np.where(times.values > np.datetime64(ka.to_datetime64()))[0]
    if len(hits) == 0:
        return len(df)
    return int(hits[0])


def _intersects(lo: float, hi: float, z: ZoneGeom) -> bool:
    return lo <= z.upper and hi >= z.lower


def _approaching(lo: float, hi: float, z: ZoneGeom, atr: float) -> bool:
    if atr <= 0 or np.isnan(atr):
        return False
    band = APPROACH_ATR_MULT * atr
    if z.side == "BID":
        # price above pool, within band of top
        return hi >= z.upper - band and lo > z.upper
    return lo <= z.lower + band and hi < z.lower


def _swept(lo: float, hi: float, z: ZoneGeom) -> bool:
    if z.side == "BID":
        return lo <= z.far_edge
    return hi >= z.far_edge


def _penetrated(lo: float, hi: float, z: ZoneGeom) -> bool:
    if not _intersects(lo, hi, z):
        return False
    if z.side == "BID":
        return lo < z.near_edge
    return hi > z.near_edge


def _away_reaction(lo: float, hi: float, z: ZoneGeom, atr: float, mult: float) -> bool:
    if atr <= 0 or np.isnan(atr):
        return False
    dist = mult * atr
    if z.side == "BID":
        return lo >= z.near_edge + dist
    return hi <= z.near_edge - dist


def _reclaimed_close(close: float, z: ZoneGeom) -> bool:
    if z.side == "BID":
        return close > z.near_edge
    return close < z.near_edge


def _accepted_close(close: float, z: ZoneGeom) -> bool:
    """Close beyond far edge (consumed side)."""
    if z.side == "BID":
        return close < z.far_edge
    return close > z.far_edge


def scan_zone_lifecycle(
    df: pd.DataFrame,
    zone: ZoneGeom,
    *,
    other_zones: list[ZoneGeom] | None = None,
) -> dict[str, Any]:
    """Scan one zone after known_at; return events, outcomes, ema, destinations."""
    n = len(df)
    start_i = analysis_start_index(df, zone.known_at)
    events: list[dict[str, Any]] = []
    ema_rows: list[dict[str, Any]] = []

    def emit(state: str, i: int, **extra: Any) -> None:
        row = df.iloc[i]
        events.append(
            {
                "entity_id": zone.entity_id,
                "entity_kind": zone.entity_kind,
                "symbol": zone.symbol,
                "timeframe": zone.timeframe,
                "side": zone.side,
                "state": state,
                "bar_index": i,
                "bar_open_time": str(_to_naive(row["open_time"])),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                **extra,
            }
        )

    # CREATED / ACTIVE at known_at bar if present
    ka = _to_naive(zone.known_at)
    times = pd.to_datetime(df["open_time"])
    created_hits = np.where(times.values == np.datetime64(ka.to_datetime64()))[0]
    if len(created_hits):
        ci = int(created_hits[0])
        emit("CREATED", ci)
        emit("ACTIVE", ci)
        ema_rows.append(
            {
                "entity_id": zone.entity_id,
                "entity_kind": zone.entity_kind,
                **ema_snapshot(
                    df.iloc[ci],
                    pool_lower=zone.lower,
                    pool_upper=zone.upper,
                    label="CREATED",
                ),
            }
        )

    if start_i >= n:
        return {
            "events": events,
            "outcomes": [],
            "ema": ema_rows,
            "destinations": [],
            "summary": _empty_summary(zone, start_i),
        }

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    atrs = df["atr_14"].to_numpy(dtype=float)

    first_approach_i = None
    first_touch_i = None
    first_pen_i = None
    far_edge_i = None
    sweep_i = None
    invalidated_i = None

    for i in range(start_i, n):
        lo, hi = float(lows[i]), float(highs[i])
        atr = float(atrs[i])

        if zone.meta.get("invalidated_at") and invalidated_i is None:
            inv = zone.meta["invalidated_at"]
            if inv and _to_naive(inv) <= _to_naive(df.iloc[i]["open_time"]):
                invalidated_i = i
                emit("INVALIDATED", i)

        if first_approach_i is None and _approaching(lo, hi, zone, atr):
            first_approach_i = i
            emit("APPROACHED", i)
            ema_rows.append(
                {
                    "entity_id": zone.entity_id,
                    "entity_kind": zone.entity_kind,
                    **ema_snapshot(
                        df.iloc[i],
                        pool_lower=zone.lower,
                        pool_upper=zone.upper,
                        label="APPROACHED",
                    ),
                }
            )

        if first_touch_i is None and _intersects(lo, hi, zone):
            first_touch_i = i
            emit("FIRST_TOUCH", i)
            ema_rows.append(
                {
                    "entity_id": zone.entity_id,
                    "entity_kind": zone.entity_kind,
                    **ema_snapshot(
                        df.iloc[i],
                        pool_lower=zone.lower,
                        pool_upper=zone.upper,
                        label="FIRST_TOUCH",
                    ),
                }
            )

        if first_touch_i is not None and first_pen_i is None and _penetrated(lo, hi, zone):
            first_pen_i = i
            emit("PENETRATED", i)

        if first_touch_i is not None and far_edge_i is None and _swept(lo, hi, zone):
            far_edge_i = i
            sweep_i = i
            emit("FAR_EDGE_REACHED", i)
            emit("SWEPT", i)
            ema_rows.append(
                {
                    "entity_id": zone.entity_id,
                    "entity_kind": zone.entity_kind,
                    **ema_snapshot(
                        df.iloc[i],
                        pool_lower=zone.lower,
                        pool_upper=zone.upper,
                        label="SWEPT",
                    ),
                }
            )
            break

    # post-sweep reclaim / accept scan (sensitivity)
    outcomes: list[dict[str, Any]] = []
    reclaim_times: dict[tuple[int, float], int | None] = {}

    if sweep_i is not None:
        for h_rec in RECLAIM_HORIZON_BARS:
            reclaim_i = None
            end = min(n, sweep_i + 1 + h_rec)
            for j in range(sweep_i + 1, end):
                if _reclaimed_close(float(df.iloc[j]["close"]), zone):
                    reclaim_i = j
                    break
            reclaim_times[(h_rec, -1.0)] = reclaim_i
            if reclaim_i is not None:
                emit("RECLAIMED", reclaim_i, reclaim_horizon_bars=h_rec)
                ema_rows.append(
                    {
                        "entity_id": zone.entity_id,
                        "entity_kind": zone.entity_kind,
                        **ema_snapshot(
                            df.iloc[reclaim_i],
                            pool_lower=zone.lower,
                            pool_upper=zone.upper,
                            label=f"RECLAIMED_h{h_rec}",
                        ),
                    }
                )

        for k_acc in ACCEPTANCE_BARS:
            for h_rec in RECLAIM_HORIZON_BARS:
                reclaim_i = reclaim_times.get((h_rec, -1.0))
                # acceptance exclusive with reclaim for same variant
                accepted = False
                accept_end_i = None
                if reclaim_i is None or reclaim_i > sweep_i + k_acc:
                    # need K consecutive accepted closes starting at sweep_i+1
                    if sweep_i + k_acc < n:
                        ok = True
                        for j in range(sweep_i + 1, sweep_i + 1 + k_acc):
                            if not _accepted_close(float(df.iloc[j]["close"]), zone):
                                ok = False
                                break
                            if reclaim_i is not None and j >= reclaim_i:
                                ok = False
                                break
                        if ok:
                            accepted = True
                            accept_end_i = sweep_i + k_acc
                if accepted and accept_end_i is not None:
                    emit(
                        "ACCEPTED_BEYOND",
                        accept_end_i,
                        acceptance_bars=k_acc,
                        reclaim_horizon_bars=h_rec,
                    )
                    ema_rows.append(
                        {
                            "entity_id": zone.entity_id,
                            "entity_kind": zone.entity_kind,
                            **ema_snapshot(
                                df.iloc[accept_end_i],
                                pool_lower=zone.lower,
                                pool_upper=zone.upper,
                                label=f"ACCEPTED_k{k_acc}_h{h_rec}",
                            ),
                        }
                    )

                outcomes.append(
                    _outcome_row(
                        zone,
                        first_touch_i=first_touch_i,
                        sweep_i=sweep_i,
                        reclaim_i=reclaim_i,
                        accepted=accepted,
                        acceptance_bars=k_acc,
                        reclaim_horizon_bars=h_rec,
                        reaction_atr=None,
                        defended=False,
                        df=df,
                        start_i=start_i,
                    )
                )

    # defended variants (no sweep)
    if first_touch_i is not None and sweep_i is None:
        for mult in REACTION_ATR_MULTS:
            defended = False
            defend_i = None
            for j in range(first_touch_i + 1, n):
                row = df.iloc[j]
                atr = float(row["atr_14"]) if pd.notna(row["atr_14"]) else float("nan")
                if _swept(float(row["low"]), float(row["high"]), zone):
                    break
                if _away_reaction(float(row["low"]), float(row["high"]), zone, atr, mult):
                    defended = True
                    defend_i = j
                    break
            for k_acc in ACCEPTANCE_BARS:
                for h_rec in RECLAIM_HORIZON_BARS:
                    outcomes.append(
                        _outcome_row(
                            zone,
                            first_touch_i=first_touch_i,
                            sweep_i=None,
                            reclaim_i=None,
                            accepted=False,
                            acceptance_bars=k_acc,
                            reclaim_horizon_bars=h_rec,
                            reaction_atr=mult,
                            defended=defended,
                            defend_i=defend_i,
                            df=df,
                            start_i=start_i,
                        )
                    )
    elif first_touch_i is None:
        for mult in REACTION_ATR_MULTS:
            for k_acc in ACCEPTANCE_BARS:
                for h_rec in RECLAIM_HORIZON_BARS:
                    outcomes.append(
                        _outcome_row(
                            zone,
                            first_touch_i=None,
                            sweep_i=None,
                            reclaim_i=None,
                            accepted=False,
                            acceptance_bars=k_acc,
                            reclaim_horizon_bars=h_rec,
                            reaction_atr=mult,
                            defended=False,
                            df=df,
                            start_i=start_i,
                        )
                    )

    # If swept, also add reaction variants for completeness (defended=False)
    if sweep_i is not None:
        # already added acceptance×reclaim outcomes; add reaction labels as NA
        pass

    destinations = _destinations(
        df,
        zone,
        first_touch_i=first_touch_i,
        sweep_i=sweep_i,
        other_zones=other_zones or [],
    )

    summary = {
        "entity_id": zone.entity_id,
        "entity_kind": zone.entity_kind,
        "symbol": zone.symbol,
        "timeframe": zone.timeframe,
        "side": zone.side,
        "known_at": zone.known_at.isoformat(),
        "analysis_start_index": start_i,
        "first_approach_index": first_approach_i,
        "first_touch_index": first_touch_i,
        "first_penetrated_index": first_pen_i,
        "sweep_index": sweep_i,
        "invalidated_index": invalidated_i,
        "touched": first_touch_i is not None,
        "swept": sweep_i is not None,
        "bars_to_touch": None if first_touch_i is None else first_touch_i - start_i,
        "bars_to_sweep": None if sweep_i is None else sweep_i - start_i,
        "minutes_to_touch": _bars_to_minutes(df, start_i, first_touch_i),
        "minutes_to_sweep": _bars_to_minutes(df, start_i, sweep_i),
    }
    return {
        "events": events,
        "outcomes": outcomes,
        "ema": ema_rows,
        "destinations": destinations,
        "summary": summary,
    }


def _bars_to_minutes(df: pd.DataFrame, start_i: int, end_i: int | None) -> float | None:
    if end_i is None or start_i >= len(df):
        return None
    t0 = _to_naive(df.iloc[start_i]["open_time"])
    t1 = _to_naive(df.iloc[end_i]["open_time"])
    return float((t1 - t0).total_seconds() / 60.0)


def _empty_summary(zone: ZoneGeom, start_i: int) -> dict[str, Any]:
    return {
        "entity_id": zone.entity_id,
        "entity_kind": zone.entity_kind,
        "symbol": zone.symbol,
        "timeframe": zone.timeframe,
        "side": zone.side,
        "known_at": zone.known_at.isoformat(),
        "analysis_start_index": start_i,
        "first_approach_index": None,
        "first_touch_index": None,
        "first_penetrated_index": None,
        "sweep_index": None,
        "invalidated_index": None,
        "touched": False,
        "swept": False,
        "bars_to_touch": None,
        "bars_to_sweep": None,
        "minutes_to_touch": None,
        "minutes_to_sweep": None,
    }


def _outcome_row(
    zone: ZoneGeom,
    *,
    first_touch_i: int | None,
    sweep_i: int | None,
    reclaim_i: int | None,
    accepted: bool,
    acceptance_bars: int,
    reclaim_horizon_bars: int,
    reaction_atr: float | None,
    defended: bool,
    df: pd.DataFrame,
    start_i: int,
    defend_i: int | None = None,
) -> dict[str, Any]:
    touched = first_touch_i is not None
    swept = sweep_i is not None
    swept_reclaimed = bool(swept and reclaim_i is not None)
    # mutual exclusion for same variant
    if swept_reclaimed and accepted:
        accepted = False
    consumed_accepted = bool(swept and accepted and not swept_reclaimed)

    primary = "NONE"
    if not touched:
        primary = "NO_TOUCH"
    elif swept_reclaimed:
        primary = "SWEPT_RECLAIMED"
    elif consumed_accepted:
        primary = "CONSUMED_ACCEPTED"
    elif swept:
        primary = "SWEPT"
    elif defended:
        primary = "DEFENDED"
    elif touched:
        primary = "TOUCHED"

    mfe = mae = None
    if first_touch_i is not None:
        entry = float(df.iloc[first_touch_i]["close"])
        end = min(len(df), first_touch_i + 1 + max(DESTINATION_HORIZONS_MIN) // _tf_minutes(zone.timeframe))
        chunk = df.iloc[first_touch_i + 1 : end]
        if not chunk.empty and entry > 0:
            if zone.side == "BID":
                # bounce up is favorable after bid touch
                mfe = float(chunk["high"].max() / entry - 1.0)
                mae = float(1.0 - chunk["low"].min() / entry)
            else:
                mfe = float(1.0 - chunk["low"].min() / entry)
                mae = float(chunk["high"].max() / entry - 1.0)

    return {
        "entity_id": zone.entity_id,
        "entity_kind": zone.entity_kind,
        "symbol": zone.symbol,
        "timeframe": zone.timeframe,
        "side": zone.side,
        "pool_count": zone.pool_count,
        "acceptance_bars": acceptance_bars,
        "reclaim_horizon_bars": reclaim_horizon_bars,
        "reaction_atr_mult": reaction_atr,
        "touched": touched,
        "defended": defended and not swept,
        "swept": swept,
        "swept_reclaimed": swept_reclaimed,
        "consumed_accepted": consumed_accepted,
        "primary_outcome": primary,
        "first_touch_time": None
        if first_touch_i is None
        else str(_to_naive(df.iloc[first_touch_i]["open_time"])),
        "sweep_time": None if sweep_i is None else str(_to_naive(df.iloc[sweep_i]["open_time"])),
        "reclaim_time": None
        if reclaim_i is None
        else str(_to_naive(df.iloc[reclaim_i]["open_time"])),
        "defend_time": None
        if defend_i is None
        else str(_to_naive(df.iloc[defend_i]["open_time"])),
        "minutes_to_touch": _bars_to_minutes(df, start_i, first_touch_i),
        "minutes_to_sweep": _bars_to_minutes(df, start_i, sweep_i),
        "minutes_to_reclaim": _bars_to_minutes(df, sweep_i if sweep_i is not None else start_i, reclaim_i)
        if sweep_i is not None
        else None,
        "mfe_frac": mfe,
        "mae_frac": mae,
        "strength": zone.strength,
        "lower_price": zone.lower,
        "upper_price": zone.upper,
    }


def _tf_minutes(tf: str) -> int:
    t = str(tf).lower()
    if t.endswith("m"):
        return int(t[:-1])
    if t.endswith("h"):
        return int(t[:-1]) * 60
    return 15


def _destinations(
    df: pd.DataFrame,
    zone: ZoneGeom,
    *,
    first_touch_i: int | None,
    sweep_i: int | None,
    other_zones: list[ZoneGeom],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trigger, idx in (("FIRST_TOUCH", first_touch_i), ("SWEPT", sweep_i)):
        if idx is None:
            continue
        origin_px = float(df.iloc[idx]["close"])
        t0 = _to_naive(df.iloc[idx]["open_time"])
        for hmin in DESTINATION_HORIZONS_MIN:
            end_t = t0 + pd.Timedelta(minutes=hmin)
            path = df.iloc[idx + 1 :]
            path = path[pd.to_datetime(path["open_time"]) <= end_t]
            hit = _first_destination(path, zone, origin_px, other_zones, as_of_i=idx, df=df)
            mfe = mae = None
            if not path.empty and origin_px > 0:
                if zone.side == "BID":
                    mfe = float(path["high"].max() / origin_px - 1.0)
                    mae = float(1.0 - path["low"].min() / origin_px)
                else:
                    mfe = float(1.0 - path["low"].min() / origin_px)
                    mae = float(path["high"].max() / origin_px - 1.0)
            rows.append(
                {
                    "entity_id": zone.entity_id,
                    "entity_kind": zone.entity_kind,
                    "symbol": zone.symbol,
                    "timeframe": zone.timeframe,
                    "side": zone.side,
                    "trigger": trigger,
                    "trigger_time": str(t0),
                    "horizon_minutes": hmin,
                    "first_destination": hit["name"],
                    "destination_time": hit.get("time"),
                    "destination_price": hit.get("price"),
                    "minutes_to_destination": hit.get("minutes"),
                    "mfe_frac": mfe,
                    "mae_frac": mae,
                    "origin_price": origin_px,
                }
            )
    return rows


def _first_destination(
    path: pd.DataFrame,
    zone: ZoneGeom,
    origin_px: float,
    other_zones: list[ZoneGeom],
    *,
    as_of_i: int,
    df: pd.DataFrame,
) -> dict[str, Any]:
    if path.empty:
        return {"name": "NO_TARGET_IN_HORIZON"}

    as_of_t = _to_naive(df.iloc[as_of_i]["open_time"])
    # targets known at trigger
    bids = [
        z
        for z in other_zones
        if z.side == "BID"
        and z.entity_id != zone.entity_id
        and _to_naive(z.known_at) <= as_of_t
        and z.upper < zone.lower
    ]
    asks = [
        z
        for z in other_zones
        if z.side == "ASK"
        and z.entity_id != zone.entity_id
        and _to_naive(z.known_at) <= as_of_t
        and z.lower > zone.upper
    ]
    next_bid = max(bids, key=lambda z: z.upper) if bids else None
    next_ask = min(asks, key=lambda z: z.lower) if asks else None
    stronger = None
    cands = [z for z in other_zones if z.entity_id != zone.entity_id and _to_naive(z.known_at) <= as_of_t]
    if zone.strength is not None:
        stronger_cands = [z for z in cands if z.strength is not None and z.strength > zone.strength]
        if stronger_cands:
            stronger = min(stronger_cands, key=lambda z: abs(z.center - zone.center))

    row0 = df.iloc[as_of_i]
    targets: list[tuple[str, float, str]] = []  # name, price, mode high|low|both

    def add_ema(name: str, col: str) -> None:
        v = row0.get(col)
        if v is not None and pd.notna(v):
            targets.append((name, float(v), "both"))

    add_ema("EMA9", "ema_9")
    add_ema("EMA20", "ema_20")
    add_ema("EMA59", "ema_59")
    add_ema("EMA200", "ema_200")
    if pd.notna(row0.get("prior_swing_high")):
        targets.append(("SWING_HIGH", float(row0["prior_swing_high"]), "high"))
    if pd.notna(row0.get("prior_swing_low")):
        targets.append(("SWING_LOW", float(row0["prior_swing_low"]), "low"))
    targets.append(("RETURN_ORIGIN", origin_px, "both"))
    if next_bid is not None:
        targets.append(("NEXT_BID_POOL", next_bid.upper, "low"))
    if next_ask is not None:
        targets.append(("NEXT_ASK_POOL", next_ask.lower, "high"))
    if stronger is not None:
        targets.append(("NEXT_STRONGER_POOL", stronger.center, "both"))

    t0 = _to_naive(df.iloc[as_of_i]["open_time"])
    best: dict[str, Any] | None = None
    for _, r in path.iterrows():
        lo, hi = float(r["low"]), float(r["high"])
        t = _to_naive(r["open_time"])
        mins = (t - t0).total_seconds() / 60.0
        for name, px, mode in targets:
            hit = False
            if mode == "high":
                hit = hi >= px
            elif mode == "low":
                hit = lo <= px
            else:
                hit = lo <= px <= hi
            if hit:
                cand = {"name": name, "time": str(t), "price": px, "minutes": mins}
                if best is None or mins < best["minutes"]:
                    best = cand
        if best is not None:
            # first bar may hit multiple; pick earliest minute (same bar: prefer order)
            return best
    return best or {"name": "NO_TARGET_IN_HORIZON"}


def pool_count_bucket(n: int) -> str:
    if n <= 1:
        return "1"
    if n == 2:
        return "2"
    if n == 3:
        return "3"
    if n <= 5:
        return "4-5"
    return "6+"

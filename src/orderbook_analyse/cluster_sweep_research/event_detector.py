"""Causal candidate detection for bullish/bearish cluster sweeps (no look-ahead entries)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

from .cluster_adapter import active_clusters_as_of, run_lld_pools
from .ema_features import attach_emas, required_warmup_bars
from .models import (
    ClusterSnapshot,
    ConfirmationVariant,
    EventState,
    SetupDirection,
    SweepEvent,
)


def _utc(ts: Any) -> datetime:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def make_event_id(
    symbol: str,
    timeframe: str,
    direction: SetupDirection,
    cluster_id: str,
    t_entry: datetime,
) -> str:
    raw = f"{symbol}|{timeframe}|{direction.value}|{cluster_id}|{_utc(t_entry).isoformat()}"
    return "csw:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def _price_in_cluster(low: float, high: float, c: ClusterSnapshot) -> bool:
    return low <= c.high and high >= c.low


def _sweep_depth(direction: SetupDirection, extreme: float, c: ClusterSnapshot) -> float:
    if direction == SetupDirection.BULLISH:
        return max(0.0, c.high - extreme) / c.mid if c.mid else 0.0
    return max(0.0, extreme - c.low) / c.mid if c.mid else 0.0


def _structure_ok(direction: SetupDirection, e9: float, e20: float, e59: float) -> bool:
    if direction == SetupDirection.BULLISH:
        return e9 > e59 and e20 > e59
    return e9 < e59 and e20 < e59


def _ema_snapshot(row: pd.Series, direction: SetupDirection, *, label: str) -> dict[str, Any]:
    e9 = float(row["ema_9"]) if pd.notna(row["ema_9"]) else None
    e20 = float(row["ema_20"]) if pd.notna(row["ema_20"]) else None
    e59 = float(row["ema_59"]) if pd.notna(row["ema_59"]) else None
    px = float(row["close"]) if pd.notna(row["close"]) else None
    snap: dict[str, Any] = {
        "label": label,
        "bar_open_time": str(_utc(row["open_time"])),
        "ema_9": e9,
        "ema_20": e20,
        "ema_59": e59,
        "close": px,
        "high": float(row["high"]) if pd.notna(row["high"]) else None,
        "low": float(row["low"]) if pd.notna(row["low"]) else None,
    }
    if None not in (e9, e20, e59):
        snap["ema9_gt_ema59"] = bool(e9 > e59)
        snap["ema20_gt_ema59"] = bool(e20 > e59)
        snap["ema9_lt_ema59"] = bool(e9 < e59)
        snap["ema20_lt_ema59"] = bool(e20 < e59)
        snap["structure_ok"] = _structure_ok(direction, e9, e20, e59)
    else:
        snap["structure_ok"] = False
        snap["ema9_gt_ema59"] = None
        snap["ema20_gt_ema59"] = None
        snap["ema9_lt_ema59"] = None
        snap["ema20_lt_ema59"] = None
    if px is not None and e59 is not None:
        snap["price_below_ema59"] = bool(px < e59)
        snap["price_above_ema59"] = bool(px > e59)
        snap["price_dist_ema59_bps"] = abs(px - e59) / e59 * 10_000.0 if e59 else None
    for name in ("ema_9_slope_1", "ema_20_slope_1", "ema_59_slope_1"):
        snap[name] = float(row[name]) if name in row and pd.notna(row[name]) else None
    return snap


def detect_candidates(
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str = "15m",
    pools: list[Any] | None = None,
    approach_bps: float = 25.0,
    expire_bars: int = 24,
    minimum_cluster_pools: int = 3,
    require_cluster_entry: bool = True,
) -> list[SweepEvent]:
    """Scan closed bars for structural candidates; confirmations require intact EMA stack.

    Bullish candidate (all required):
      - EMA9 > EMA59 and EMA20 > EMA59 on the candidate bar close
      - low pierces below EMA59
      - low/high intersects a lower cluster known as_of previous bar
      - by default require actual cluster entry (not approach-only)

    Bearish is the mirror. Confirmation bars re-check the stack; structure break
    invalidates and blocks later confirmation of the same event.
    """
    if candles.empty:
        return []
    df = candles.sort_values("open_time").reset_index(drop=True).copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    df = attach_emas(df)
    df["atr_14"] = _atr(df)

    if pools is None:
        lld = run_lld_pools(df, symbol=symbol, timeframe=timeframe)
        if lld.verdict.value != "CAUSAL_REUSABLE":
            return []
        pools = lld.pools

    events: list[SweepEvent] = []
    warm = required_warmup_bars()
    seen: set[str] = set()
    # prior touches per cluster_id (causal order)
    touch_counts: dict[str, int] = {}

    for i in range(warm, len(df)):
        row = df.iloc[i]
        if row["ema_9"] is None or row["ema_59"] is None or pd.isna(row["ema_9"]) or pd.isna(row["ema_59"]):
            continue
        t = _utc(row["open_time"])
        as_of = _utc(df.iloc[i - 1]["open_time"])
        clusters = active_clusters_as_of(pools, as_of, minimum_pools=minimum_cluster_pools)

        for direction, need_side in (
            (SetupDirection.BULLISH, "lower"),
            (SetupDirection.BEARISH, "upper"),
        ):
            e9, e20, e59 = float(row["ema_9"]), float(row["ema_20"]), float(row["ema_59"])
            if not _structure_ok(direction, e9, e20, e59):
                continue
            px = float(row["close"])
            hi = float(row["high"])
            lo = float(row["low"])

            if direction == SetupDirection.BULLISH:
                crossed = lo < e59
            else:
                crossed = hi > e59
            if not crossed:
                continue

            for c in clusters:
                if c.side != need_side:
                    continue
                # Cluster must already exist before this bar (as_of = prior bar).
                if _utc(c.oldest_created) > as_of:
                    continue
                mid_dist_bps = abs(px - c.mid) / c.mid * 10_000.0 if c.mid else 1e9
                entered = _price_in_cluster(lo, hi, c)
                approached = mid_dist_bps <= approach_bps or entered
                if not approached:
                    continue
                if require_cluster_entry and not entered:
                    continue

                eid = make_event_id(symbol, timeframe, direction, c.cluster_id, t)
                if eid in seen:
                    continue
                seen.add(eid)

                prior = touch_counts.get(c.cluster_id, 0)
                touch_counts[c.cluster_id] = prior + 1

                states = [EventState.EMA_STRUCTURE_INTACT]
                if approached:
                    states.append(EventState.CLUSTER_APPROACH)
                if entered:
                    states.append(EventState.CLUSTER_ENTRY)
                if crossed:
                    states.append(EventState.PRICE_CROSSED_EMA59)

                extreme = lo if direction == SetupDirection.BULLISH else hi
                depth = _sweep_depth(direction, extreme, c) if entered else 0.0
                cand_snap = _ema_snapshot(row, direction, label="candidate")
                sweep_snap = dict(cand_snap)
                sweep_snap["label"] = "sweep"

                conf, outcome_meta, final_states, t_entry, t_inv = _resolve_forward(
                    df, i, direction, c, expire_bars
                )
                states.extend(final_states)

                feat = {
                    "ema_9": e9,
                    "ema_20": e20,
                    "ema_59": e59,
                    "ema_bull_stack": bool(row["ema_bull_stack"]),
                    "ema_bear_stack": bool(row["ema_bear_stack"]),
                    "ema_9_20_gap": float(row["ema_9_20_gap"]) if pd.notna(row["ema_9_20_gap"]) else None,
                    "ema_9_59_gap": float(row["ema_9_59_gap"]) if pd.notna(row["ema_9_59_gap"]) else None,
                    "ema_20_59_gap": float(row["ema_20_59_gap"]) if pd.notna(row["ema_20_59_gap"]) else None,
                    "ema_9_slope_1": (
                        float(row["ema_9_slope_1"])
                        if "ema_9_slope_1" in row and pd.notna(row["ema_9_slope_1"])
                        else None
                    ),
                    "ema_20_slope_1": (
                        float(row["ema_20_slope_1"])
                        if "ema_20_slope_1" in row and pd.notna(row["ema_20_slope_1"])
                        else None
                    ),
                    "ema_59_slope_1": float(row["ema_59_slope_1"]) if pd.notna(row["ema_59_slope_1"]) else None,
                    "ema_59_slope_3": float(row["ema_59_slope_3"]) if pd.notna(row["ema_59_slope_3"]) else None,
                    "ema_59_slope_5": float(row["ema_59_slope_5"]) if pd.notna(row["ema_59_slope_5"]) else None,
                    "ema_band_width": float(row["ema_band_width"]) if pd.notna(row["ema_band_width"]) else None,
                    "close": px,
                    "high": hi,
                    "low": lo,
                    "atr_14": float(row["atr_14"]) if pd.notna(row["atr_14"]) else None,
                    "candle_range": (hi - lo) / px if px else None,
                    "volume": float(row["volume"]) if "volume" in row and pd.notna(row["volume"]) else None,
                    "sweep_depth_pct": depth,
                    "approach_dist_bps": mid_dist_bps,
                    "cluster_age_bars_proxy_minutes": (
                        (t - _utc(c.oldest_created)).total_seconds() / 60.0
                    ),
                    "n_clusters_same_side": sum(1 for x in clusters if x.side == need_side),
                    "cluster_as_of": as_of.isoformat(),
                    "prior_touch_count": prior,
                    "invalidation_reason": outcome_meta.get("invalidation_reason"),
                    "ema_audit": {
                        "candidate": cand_snap,
                        "sweep": sweep_snap,
                        "confirmation": outcome_meta.get("confirmation_ema"),
                    },
                }

                ev = SweepEvent(
                    event_id=eid,
                    setup_direction=direction,
                    symbol=symbol,
                    timeframe=timeframe,
                    cluster=c,
                    states=_unique_states(states),
                    t_approach=t if EventState.CLUSTER_APPROACH in states else None,
                    t_first_touch=t if entered else None,
                    t_entry=t if entered else None,
                    t_max_sweep=t if entered else None,
                    t_price_cross_ema59=t if crossed else None,
                    t_reclaim_or_reject=outcome_meta.get("t_reclaim_or_reject"),
                    t_earliest_entry=t_entry,
                    t_invalidated=t_inv,
                    features=feat,
                    confirmations=conf,
                    outcomes={},
                    coverage={},
                )
                events.append(ev)
    return events


def dedupe_related_events(
    events: list[SweepEvent],
    *,
    merge_within_bars_minutes: float | None = None,
) -> list[SweepEvent]:
    """Deterministic grouping of near-duplicate contacts without changing TRP geometry.

    Keeps the event with highest pool_count, then earliest touch, within the same
    direction and overlapping cluster bounds in a short time window.
    Suppressed events keep features['dedupe_group'] pointing at the survivor.
    """
    if not events:
        return []
    # default: 15 minutes for 5m, else 3 bars of timeframe if encoded
    window_min = merge_within_bars_minutes
    if window_min is None:
        tf = events[0].timeframe or "5m"
        try:
            bar_m = int(str(tf).replace("m", ""))
        except ValueError:
            bar_m = 5
        window_min = float(bar_m * 3)

    ordered = sorted(
        events,
        key=lambda e: (
            e.setup_direction.value,
            _utc(e.t_first_touch or e.t_entry or datetime.min.replace(tzinfo=timezone.utc)),
            -int(e.cluster.pool_count),
            e.event_id,
        ),
    )
    keep: list[SweepEvent] = []
    suppressed: list[SweepEvent] = []

    def overlaps(a: ClusterSnapshot, b: ClusterSnapshot) -> bool:
        return a.low <= b.high and b.low <= a.high

    for ev in ordered:
        merged = False
        t_ev = _utc(ev.t_first_touch or ev.t_entry)
        for survivor in keep:
            if survivor.setup_direction != ev.setup_direction:
                continue
            t_s = _utc(survivor.t_first_touch or survivor.t_entry)
            if abs((t_ev - t_s).total_seconds()) > window_min * 60:
                continue
            if not overlaps(survivor.cluster, ev.cluster) and survivor.cluster.cluster_id != ev.cluster.cluster_id:
                continue
            # prefer higher pool_count; if equal, keep earlier (already in keep)
            if ev.cluster.pool_count > survivor.cluster.pool_count:
                keep.remove(survivor)
                survivor.features["dedupe_group"] = ev.event_id
                suppressed.append(survivor)
                keep.append(ev)
                ev.features["dedupe_group"] = ev.event_id
            else:
                ev.features["dedupe_group"] = survivor.event_id
                suppressed.append(ev)
            merged = True
            break
        if not merged:
            ev.features.setdefault("dedupe_group", ev.event_id)
            keep.append(ev)

    # stable chronological
    keep.sort(
        key=lambda e: _utc(e.t_first_touch or e.t_entry or datetime.min.replace(tzinfo=timezone.utc))
    )
    return keep


def _unique_states(states: Iterable[EventState]) -> list[EventState]:
    out: list[EventState] = []
    for s in states:
        if s not in out:
            out.append(s)
    return out


def _resolve_forward(
    df: pd.DataFrame,
    i: int,
    direction: SetupDirection,
    c: ClusterSnapshot,
    expire_bars: int,
) -> tuple[dict[str, Any], dict[str, Any], list[EventState], datetime | None, datetime | None]:
    """Causal confirmations after bar i; entry = next bar open after confirm bar.

    Structure must remain intact on the confirmation bar. After INVALIDATED,
    no confirmation is recorded for this event.
    """
    conf: dict[str, Any] = {v.value: {"fired": False, "bar_time": None} for v in ConfirmationVariant}
    extra_states: list[EventState] = []
    t_entry = None
    t_inv = None
    meta: dict[str, Any] = {}
    invalidated = False

    end = min(len(df) - 1, i + expire_bars)
    for j in range(i + 1, end + 1):
        r = df.iloc[j]
        if r["ema_9"] is None or pd.isna(r["ema_9"]) or pd.isna(r["ema_59"]):
            continue
        e9, e20, e59 = float(r["ema_9"]), float(r["ema_20"]), float(r["ema_59"])
        cl, hi, lo = float(r["close"]), float(r["high"]), float(r["low"])
        bt = _utc(r["open_time"])

        if not _structure_ok(direction, e9, e20, e59):
            extra_states.append(EventState.INVALIDATED)
            t_inv = bt
            meta["invalidation_reason"] = "EMA_STRUCTURE_BREAK"
            meta["confirmation_ema"] = _ema_snapshot(r, direction, label="invalidation")
            invalidated = True
            break

        if direction == SetupDirection.BULLISH and cl < c.low:
            extra_states.append(EventState.CLUSTER_BREAK)
        if direction == SetupDirection.BEARISH and cl > c.high:
            extra_states.append(EventState.CLUSTER_BREAK)

        def _mark(variant: ConfirmationVariant) -> None:
            nonlocal t_entry
            if invalidated or conf[variant.value]["fired"]:
                return
            # Re-verify structure at confirmation close (same bar already checked above).
            if not _structure_ok(direction, e9, e20, e59):
                return
            conf[variant.value] = {
                "fired": True,
                "bar_time": bt.isoformat(),
                "structure_ok_at_confirm": True,
                "ema_9": e9,
                "ema_20": e20,
                "ema_59": e59,
                "close": cl,
            }
            if j + 1 < len(df) and t_entry is None:
                t_entry = _utc(df.iloc[j + 1]["open_time"])
            meta["t_reclaim_or_reject"] = bt
            meta["confirmation_ema"] = _ema_snapshot(r, direction, label="confirmation")
            if direction == SetupDirection.BULLISH:
                extra_states.append(EventState.RECLAIM_CONFIRMED)
            else:
                extra_states.append(EventState.REJECTION_CONFIRMED)
            extra_states.append(EventState.CLUSTER_HOLD)

        if direction == SetupDirection.BULLISH:
            if c.low <= cl <= c.high:
                _mark(ConfirmationVariant.CLOSE_BACK_IN_CLUSTER)
            if cl > c.high:
                _mark(ConfirmationVariant.CLOSE_BEYOND_CLUSTER_EDGE)
            if cl > e59:
                _mark(ConfirmationVariant.CLOSE_RECLAIM_EMA59)
            if cl > c.high and cl > e59:
                _mark(ConfirmationVariant.CLUSTER_AND_EMA_RECLAIM)
        else:
            if c.low <= cl <= c.high:
                _mark(ConfirmationVariant.CLOSE_BACK_IN_CLUSTER)
            if cl < c.low:
                _mark(ConfirmationVariant.CLOSE_BEYOND_CLUSTER_EDGE)
            if cl < e59:
                _mark(ConfirmationVariant.CLOSE_RECLAIM_EMA59)
            if cl < c.low and cl < e59:
                _mark(ConfirmationVariant.CLUSTER_AND_EMA_RECLAIM)

        if any(
            conf[v.value]["fired"]
            for v in ConfirmationVariant
            if v != ConfirmationVariant.ORDERFLOW_REVERSAL
        ):
            break
    else:
        if t_entry is None and t_inv is None:
            extra_states.append(EventState.EXPIRED)

    # Hard guarantee: never leave fired confirms if invalidated won the race
    if invalidated:
        for k in conf:
            if conf[k].get("fired"):
                conf[k] = {"fired": False, "bar_time": None, "cleared_by": "INVALIDATED"}
        t_entry = None
        meta.pop("t_reclaim_or_reject", None)
        # drop reclaim states if any slipped in
        extra_states = [
            s
            for s in extra_states
            if s
            not in (
                EventState.RECLAIM_CONFIRMED,
                EventState.REJECTION_CONFIRMED,
                EventState.CLUSTER_HOLD,
            )
        ]

    return conf, meta, _unique_states(extra_states), t_entry, t_inv

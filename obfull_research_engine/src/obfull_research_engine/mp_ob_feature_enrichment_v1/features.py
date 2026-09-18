"""Causal feature computation for one MP event."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from obfull_research_engine.breakout_xray_v1.ports import LevelChangeEvent
from obfull_research_engine.breakout_xray_v1.trades import XRayTrade

from .lc_book import (
    AttributedChange,
    replay_and_attribute,
    snapshot_at_or_before,
    trade_ts_ns,
)
from .loaders import MetricTick
from .params import (
    APPROACH_BEFORE_S,
    BASELINE_BEFORE_S,
    BASELINE_END_BEFORE_S,
    LEVEL_CHANGES_TABLE,
    NS,
    SHORT_WINDOWS_S,
    WALL_PRESENT_FRAC,
)
from .zones import ZoneBands, build_zone_bands, sum_depth


@dataclass
class FeatureValue:
    name: str
    value: Any
    available: bool
    missing_reason: str | None
    source_table: str
    source_window_start_ns: int | None
    source_window_end_ns: int | None
    max_source_ts_ns: int | None
    causal_valid: bool

    def to_row(self, event_id: str) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "feature": self.name,
            "value": self.value,
            "available": self.available,
            "missing_reason": self.missing_reason,
            "source_table": self.source_table,
            "source_window_start_ns": self.source_window_start_ns,
            "source_window_end_ns": self.source_window_end_ns,
            "max_source_ts_ns": self.max_source_ts_ns,
            "causal_valid": self.causal_valid,
        }


def _fv(
    name: str,
    value: Any,
    *,
    available: bool = True,
    missing_reason: str | None = None,
    source_table: str = "derived",
    start_ns: int | None = None,
    end_ns: int | None = None,
    max_ts: int | None = None,
    causal_valid: bool = True,
) -> FeatureValue:
    if value is None and available:
        available = False
        missing_reason = missing_reason or "NULL"
    return FeatureValue(
        name=name,
        value=value,
        available=available,
        missing_reason=None if available else (missing_reason or "NULL"),
        source_table=source_table,
        source_window_start_ns=start_ns,
        source_window_end_ns=end_ns,
        max_source_ts_ns=max_ts,
        causal_valid=causal_valid,
    )


def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or abs(float(b)) < 1e-15:
        return None
    return float(a) / float(b)


def _phase_bounds(touch_ns: int, trigger_ns: int | None) -> dict[str, tuple[int, int]]:
    baseline = (
        touch_ns - int(BASELINE_BEFORE_S * NS),
        touch_ns - int(BASELINE_END_BEFORE_S * NS),
    )
    approach = (touch_ns - int(APPROACH_BEFORE_S * NS), touch_ns)
    contact_end = trigger_ns if trigger_ns is not None else touch_ns
    contact = (touch_ns, contact_end)
    return {"baseline": baseline, "approach": approach, "contact": contact}


def _filter_attr(
    attrs: Sequence[AttributedChange],
    *,
    start_ns: int,
    end_ns: int,
    side: str | None = None,
) -> list[AttributedChange]:
    out = []
    for a in attrs:
        if a.event_time_ns < start_ns or a.event_time_ns > end_ns:
            continue
        if side is not None and a.side != side:
            continue
        out.append(a)
    return out


def _sum_kind(attrs: Sequence[AttributedChange], kind: str) -> tuple[float, float]:
    qty = 0.0
    notional_v = 0.0
    for a in attrs:
        if a.kind == kind:
            qty += a.qty
            notional_v += a.notional
    return qty, notional_v


def _refill_stats(attrs: Sequence[AttributedChange], *, side: str) -> dict[str, float | None]:
    """Refill = ADD after HIT on same side within contact/approach attrs chronological."""
    cycles = 0
    add_after_hit = 0.0
    last_hit_ts: int | None = None
    first_refill_latency_ms: float | None = None
    awaiting = False
    for a in sorted(attrs, key=lambda x: x.event_time_ns):
        if a.side != side:
            continue
        if a.kind == "HIT":
            awaiting = True
            last_hit_ts = a.event_time_ns
        elif a.kind == "ADD" and awaiting:
            cycles += 1
            add_after_hit += a.qty
            if first_refill_latency_ms is None and last_hit_ts is not None:
                first_refill_latency_ms = (a.event_time_ns - last_hit_ts) / 1e6
            awaiting = False
    hit_qty, _ = _sum_kind([a for a in attrs if a.side == side], "HIT")
    refill_ratio = _safe_div(add_after_hit, hit_qty) if hit_qty else None
    return {
        "add_after_hit_qty": add_after_hit if hit_qty else None,
        "refill_ratio": refill_ratio,
        "refill_latency_ms": first_refill_latency_ms,
        "number_of_refill_cycles": float(cycles) if hit_qty else None,
    }


def _wall_present_fraction(
    path: Sequence[tuple[int, dict]],
    *,
    side: str,
    interval: tuple[float, float],
    start_ns: int,
    end_ns: int,
    baseline_depth: float,
) -> float | None:
    if end_ns < start_ns:
        return None
    thr = max(float(baseline_depth) * WALL_PRESENT_FRAC, 1e-9)
    samples = [(t, snap) for t, snap in path if start_ns <= t <= end_ns]
    if not samples:
        # no updates: use last snap before start if any
        snap = snapshot_at_or_before(path, start_ns)
        d = sum_depth(snap, side=side, interval=interval)
        return 1.0 if d >= thr else 0.0
    present = 0
    for _, snap in samples:
        d = sum_depth(snap, side=side, interval=interval)
        if d >= thr:
            present += 1
    return present / len(samples)


def _com_price(sizes: dict, *, side: str, interval: tuple[float, float]) -> float | None:
    num = 0.0
    den = 0.0
    a, b = interval
    lo, hi = (a, b) if a <= b else (b, a)
    for (s, px), sz in sizes.items():
        if s != side or sz <= 0:
            continue
        if lo - 1e-12 <= px <= hi + 1e-12 or (abs(hi - lo) < 1e-12 and abs(px - lo) <= 1e-9):
            num += px * sz
            den += sz
    if den <= 0:
        return None
    return num / den


@dataclass
class EventFeatureBundle:
    event_id: str
    features: dict[str, FeatureValue] = field(default_factory=dict)
    max_feature_ts_ns: int | None = None
    causal_ok: bool = True
    leakage_flags: list[str] = field(default_factory=list)
    trades_available: bool = False
    baseline_warmup_ok: bool = True
    level_changes_available: bool = True

    def values_dict(self) -> dict[str, Any]:
        return {k: v.value for k, v in self.features.items()}


def compute_event_features(
    *,
    event: dict[str, Any],
    level_changes: Sequence[LevelChangeEvent],
    metrics: Sequence[MetricTick],
    trades: Sequence[XRayTrade] | None,
    trades_available: bool,
    window_start_ns: int,
    window_end_ns: int,
) -> EventFeatureBundle:
    eid = str(event["event_id"])
    touch_ns = int(event["first_touch_ts_ns"])
    trigger_raw = event.get("trigger_ts_ns")
    trigger_ns = int(trigger_raw) if trigger_raw not in (None, "", "None") else None
    cutoff_ns = trigger_ns if trigger_ns is not None else touch_ns
    role = str(event["event_role"]).upper()
    bands = build_zone_bands(
        role=role,
        low=float(event["confluence_low"]),
        high=float(event["confluence_high"]),
    )
    phases = _phase_bounds(touch_ns, trigger_ns)
    feature_start = phases["baseline"][0]
    if feature_start < window_start_ns:
        baseline_warmup_ok = False
        feature_start = window_start_ns
    else:
        baseline_warmup_ok = True

    # Hard causal clamp
    if cutoff_ns > window_end_ns:
        cutoff_ns = window_end_ns

    bundle = EventFeatureBundle(
        event_id=eid,
        trades_available=trades_available,
        baseline_warmup_ok=baseline_warmup_ok,
        level_changes_available=len(level_changes) > 0,
    )

    trade_list = list(trades or [])
    state, attrs, path = replay_and_attribute(
        level_changes,
        trades=trade_list,
        trades_available=trades_available,
        cutoff_ns=cutoff_ns,
        start_ns=feature_start,
    )
    max_ts = state.max_feature_ts_ns or feature_start
    metric_ts = [m.bucket_start_ns for m in metrics if m.bucket_start_ns <= cutoff_ns]
    if metric_ts:
        max_ts = max(max_ts, max(metric_ts))
    if trade_list and trades_available:
        for t in trade_list:
            tns = trade_ts_ns(t)
            if feature_start <= tns <= cutoff_ns:
                max_ts = max(max_ts, tns)
    bundle.max_feature_ts_ns = max_ts

    leakage: list[str] = []
    if max_ts > cutoff_ns:
        leakage.append("max_feature_ts_gt_cutoff")
    if trigger_ns is not None:
        # outcome starts at trigger; features must not exceed trigger
        if max_ts > trigger_ns:
            leakage.append("max_feature_ts_gt_trigger")
    bundle.leakage_flags = leakage
    bundle.causal_ok = len(leakage) == 0

    def put(fv: FeatureValue) -> None:
        if not bundle.causal_ok:
            fv = FeatureValue(
                name=fv.name,
                value=None,
                available=False,
                missing_reason="CAUSAL_INVALID",
                source_table=fv.source_table,
                source_window_start_ns=fv.source_window_start_ns,
                source_window_end_ns=fv.source_window_end_ns,
                max_source_ts_ns=fv.max_source_ts_ns,
                causal_valid=False,
            )
        bundle.features[fv.name] = fv

    # Depth snapshots
    snap_base_end = snapshot_at_or_before(path, phases["baseline"][1])
    snap_approach_end = snapshot_at_or_before(path, touch_ns)
    snap_touch = snap_approach_end
    snap_trigger = snapshot_at_or_before(path, cutoff_ns)

    def depth_at(snap: dict, interval: tuple[float, float], side: str | None = None) -> float:
        return sum_depth(snap, side=side or bands.defense_side, interval=interval)

    d_base = depth_at(snap_base_end, bands.inside)
    d_appr = depth_at(snap_approach_end, bands.inside)
    d_touch = depth_at(snap_touch, bands.inside)
    d_trig = depth_at(snap_trigger, bands.inside)

    src_lc = f"silver.{LEVEL_CHANGES_TABLE}"
    put(_fv("depth_at_zone_baseline", d_base, source_table=src_lc, start_ns=phases["baseline"][0], end_ns=phases["baseline"][1], max_ts=max_ts, available=baseline_warmup_ok or d_base > 0, missing_reason=None if (baseline_warmup_ok or d_base > 0) else "INSUFFICIENT_BASELINE"))
    put(_fv("depth_at_zone_approach", d_appr, source_table=src_lc, start_ns=phases["approach"][0], end_ns=phases["approach"][1], max_ts=max_ts))
    put(_fv("depth_at_zone_touch", d_touch, source_table=src_lc, start_ns=touch_ns, end_ns=touch_ns, max_ts=max_ts))
    if trigger_ns is None:
        put(_fv("depth_at_zone_trigger", None, available=False, missing_reason="NO_TRIGGER", source_table=src_lc))
    else:
        put(_fv("depth_at_zone_trigger", d_trig, source_table=src_lc, start_ns=touch_ns, end_ns=trigger_ns, max_ts=max_ts))

    # inside bands = before (approach side of zone)
    put(_fv("depth_inside_1bp", depth_at(snap_touch, bands.before_1bp), source_table=src_lc, max_ts=max_ts))
    put(_fv("depth_inside_2bps", depth_at(snap_touch, bands.before_2bps), source_table=src_lc, max_ts=max_ts))
    put(_fv("depth_inside_5bps", depth_at(snap_touch, bands.before_5bps), source_table=src_lc, max_ts=max_ts))
    put(_fv("depth_beyond_1bp", depth_at(snap_touch, bands.beyond_1bp), source_table=src_lc, max_ts=max_ts))
    put(_fv("depth_beyond_2bps", depth_at(snap_touch, bands.beyond_2bps), source_table=src_lc, max_ts=max_ts))
    put(_fv("depth_beyond_5bps", depth_at(snap_touch, bands.beyond_5bps), source_table=src_lc, max_ts=max_ts))
    put(_fv("opposing_depth_1bp", depth_at(snap_touch, bands.opposing_1bp, side=bands.attack_side), source_table=src_lc, max_ts=max_ts))
    put(_fv("opposing_depth_2bps", depth_at(snap_touch, bands.opposing_2bps, side=bands.attack_side), source_table=src_lc, max_ts=max_ts))
    put(_fv("opposing_depth_5bps", depth_at(snap_touch, bands.opposing_5bps, side=bands.attack_side), source_table=src_lc, max_ts=max_ts))

    local = d_touch + depth_at(snap_touch, bands.before_5bps) + depth_at(snap_touch, bands.beyond_5bps)
    put(_fv("normalized_depth_ratio", _safe_div(d_touch, local), source_table=src_lc, max_ts=max_ts))
    put(_fv("local_depth_percentile", None, available=False, missing_reason="REQUIRES_CROSS_SECTION", source_table=src_lc))
    put(_fv("wall_strength_vs_local_book", _safe_div(d_touch, local), source_table=src_lc, max_ts=max_ts))

    # Level-change aggregates over contact (or approach+contact if no trigger span)
    contact_attrs = _filter_attr(attrs, start_ns=phases["contact"][0], end_ns=phases["contact"][1], side=bands.defense_side)
    all_def = _filter_attr(attrs, start_ns=feature_start, end_ns=cutoff_ns, side=bands.defense_side)

    def put_hit_pull(prefix_attrs: list[AttributedChange], window_name: str, start_ns: int, end_ns: int) -> None:
        if not trades_available:
            for nm in (
                "hit_qty",
                "hit_notional",
                "pull_qty",
                "pull_notional",
                "hit_to_depth_ratio",
                "pull_to_depth_ratio",
                "add_after_hit_qty",
                "refill_ratio",
                "refill_latency_ms",
                "number_of_refill_cycles",
                "max_single_hit",
                "max_single_pull",
            ):
                if window_name != "contact":
                    continue
                put(
                    _fv(
                        nm,
                        None,
                        available=False,
                        missing_reason="NOT_AVAILABLE",
                        source_table="public_trades+lc",
                        start_ns=start_ns,
                        end_ns=end_ns,
                    )
                )
            # ADD still from LC
            add_q, add_n = _sum_kind(prefix_attrs, "ADD")
            put(_fv("add_qty", add_q, source_table=src_lc, start_ns=start_ns, end_ns=end_ns, max_ts=max_ts))
            put(_fv("add_notional", add_n, source_table=src_lc, start_ns=start_ns, end_ns=end_ns, max_ts=max_ts))
            dec_q, _ = _sum_kind(prefix_attrs, "DECREASE_UNCLASSIFIED")
            put(_fv("net_added_qty", add_q - dec_q, source_table=src_lc, start_ns=start_ns, end_ns=end_ns, max_ts=max_ts))
            put(_fv("net_depth_change", d_trig - d_touch if trigger_ns else d_touch - d_appr, source_table=src_lc, max_ts=max_ts))
            put(_fv("add_to_depth_ratio", _safe_div(add_q, d_touch or d_base or None), source_table=src_lc, max_ts=max_ts))
            max_add = max((a.qty for a in prefix_attrs if a.kind == "ADD"), default=None)
            put(_fv("max_single_add", max_add, source_table=src_lc, max_ts=max_ts, available=max_add is not None, missing_reason=None if max_add is not None else "NO_ADDS"))
            return

        hit_q, hit_n = _sum_kind(prefix_attrs, "HIT")
        pull_q, pull_n = _sum_kind(prefix_attrs, "PULL")
        add_q, add_n = _sum_kind(prefix_attrs, "ADD")
        put(_fv("hit_qty", hit_q, source_table="public_trades+lc", start_ns=start_ns, end_ns=end_ns, max_ts=max_ts))
        put(_fv("hit_notional", hit_n, source_table="public_trades+lc", start_ns=start_ns, end_ns=end_ns, max_ts=max_ts))
        put(_fv("pull_qty", pull_q, source_table="public_trades+lc", start_ns=start_ns, end_ns=end_ns, max_ts=max_ts))
        put(_fv("pull_notional", pull_n, source_table="public_trades+lc", start_ns=start_ns, end_ns=end_ns, max_ts=max_ts))
        put(_fv("add_qty", add_q, source_table=src_lc, start_ns=start_ns, end_ns=end_ns, max_ts=max_ts))
        put(_fv("add_notional", add_n, source_table=src_lc, start_ns=start_ns, end_ns=end_ns, max_ts=max_ts))
        put(_fv("net_added_qty", add_q - pull_q - hit_q, source_table=src_lc, max_ts=max_ts))
        put(_fv("net_depth_change", d_trig - d_touch if trigger_ns else d_touch - d_appr, source_table=src_lc, max_ts=max_ts))
        base_d = d_touch if d_touch > 0 else (d_base if d_base > 0 else None)
        put(_fv("hit_to_depth_ratio", _safe_div(hit_q, base_d), source_table="public_trades+lc", max_ts=max_ts))
        put(_fv("pull_to_depth_ratio", _safe_div(pull_q, base_d), source_table="public_trades+lc", max_ts=max_ts))
        put(_fv("add_to_depth_ratio", _safe_div(add_q, base_d), source_table=src_lc, max_ts=max_ts))
        rs = _refill_stats(prefix_attrs, side=bands.defense_side)
        for k, v in rs.items():
            put(_fv(k, v, source_table="public_trades+lc", start_ns=start_ns, end_ns=end_ns, max_ts=max_ts, available=v is not None, missing_reason=None if v is not None else "NO_HIT_REFILL"))
        hits = [a.qty for a in prefix_attrs if a.kind == "HIT"]
        pulls = [a.qty for a in prefix_attrs if a.kind == "PULL"]
        adds = [a.qty for a in prefix_attrs if a.kind == "ADD"]
        put(_fv("max_single_hit", max(hits) if hits else None, source_table="public_trades+lc", max_ts=max_ts, available=bool(hits), missing_reason=None if hits else "NO_HITS"))
        put(_fv("max_single_pull", max(pulls) if pulls else None, source_table="public_trades+lc", max_ts=max_ts, available=bool(pulls), missing_reason=None if pulls else "NO_PULLS"))
        put(_fv("max_single_add", max(adds) if adds else None, source_table=src_lc, max_ts=max_ts, available=bool(adds), missing_reason=None if adds else "NO_ADDS"))

    put_hit_pull(contact_attrs if trigger_ns else all_def, "contact", phases["contact"][0], phases["contact"][1])

    # Persistence
    put(
        _fv(
            "wall_present_baseline_fraction",
            _wall_present_fraction(path, side=bands.defense_side, interval=bands.inside, start_ns=phases["baseline"][0], end_ns=phases["baseline"][1], baseline_depth=d_base or d_touch or 1.0)
            if baseline_warmup_ok
            else None,
            available=baseline_warmup_ok,
            missing_reason=None if baseline_warmup_ok else "INSUFFICIENT_BASELINE",
            source_table=src_lc,
            max_ts=max_ts,
        )
    )
    put(_fv("wall_present_approach_fraction", _wall_present_fraction(path, side=bands.defense_side, interval=bands.inside, start_ns=phases["approach"][0], end_ns=phases["approach"][1], baseline_depth=d_base or d_touch or 1.0), source_table=src_lc, max_ts=max_ts))
    put(_fv("wall_present_contact_fraction", _wall_present_fraction(path, side=bands.defense_side, interval=bands.inside, start_ns=phases["contact"][0], end_ns=phases["contact"][1], baseline_depth=d_base or d_touch or 1.0), source_table=src_lc, max_ts=max_ts))

    # survival after hits: remaining / baseline after contact
    put(_fv("wall_survival_after_hits", _safe_div(d_trig if trigger_ns else d_touch, d_base or d_appr or None), source_table=src_lc, max_ts=max_ts))
    # min remaining along path in contact
    min_rem = None
    base_for_min = d_touch if d_touch > 0 else d_base
    if base_for_min and base_for_min > 0:
        mins = []
        for t, snap in path:
            if phases["contact"][0] <= t <= phases["contact"][1]:
                mins.append(depth_at(snap, bands.inside) / base_for_min)
        min_rem = min(mins) if mins else _safe_div(d_touch, base_for_min)
    put(_fv("wall_min_remaining_fraction", min_rem, source_table=src_lc, max_ts=max_ts))
    put(_fv("wall_recovery_fraction", _safe_div(d_trig if trigger_ns else d_touch, d_base or d_appr or None), source_table=src_lc, max_ts=max_ts))

    # migration band: expand inside ±5bps
    mig_lo = min(bands.inside[0], bands.before_5bps[0], bands.beyond_5bps[0], bands.before_5bps[1], bands.beyond_5bps[1])
    mig_hi = max(bands.inside[1], bands.before_5bps[0], bands.beyond_5bps[0], bands.before_5bps[1], bands.beyond_5bps[1])
    com_base = _com_price(snap_base_end, side=bands.defense_side, interval=(mig_lo, mig_hi))
    com_trig = _com_price(snap_trigger, side=bands.defense_side, interval=(mig_lo, mig_hi))
    migr_bps = None
    if com_base and com_trig and bands.center:
        migr_bps = abs(com_trig - com_base) / bands.center * 1e4
    put(_fv("wall_price_migration_bps", migr_bps, source_table=src_lc, max_ts=max_ts, available=migr_bps is not None, missing_reason=None if migr_bps is not None else "NO_COM"))

    # mid move vs wall move
    mids = [m for m in metrics if feature_start <= m.bucket_start_ns <= cutoff_ns and m.mid]
    mid0 = mids[0].mid if mids else None
    mid1 = mids[-1].mid if mids else None
    moved_with = None
    if migr_bps is not None and mid0 and mid1 and com_base and com_trig:
        mid_dir = mid1 - mid0
        wall_dir = com_trig - com_base
        moved_with = 1.0 if mid_dir * wall_dir > 0 else 0.0
    put(_fv("wall_moved_with_price", moved_with, source_table=src_lc, max_ts=max_ts, available=moved_with is not None, missing_reason=None if moved_with is not None else "NO_MID_OR_COM"))

    disappeared_before = 1.0 if (d_appr <= 1e-9 and (d_base or 0) > 1e-9) else 0.0
    disappeared_contact = 1.0 if ((d_trig if trigger_ns else d_touch) <= 1e-9 and (d_touch or d_appr or 0) > 1e-9) else 0.0
    put(_fv("wall_disappeared_before_touch", disappeared_before, source_table=src_lc, max_ts=max_ts))
    put(_fv("wall_disappeared_on_contact", disappeared_contact, source_table=src_lc, max_ts=max_ts))

    # Book state from metrics
    src_m = "silver.ob_metrics_100ms_v1_3"
    m_win = [m for m in metrics if feature_start <= m.bucket_start_ns <= cutoff_ns]
    spreads = []
    imbs = []
    for m in m_win:
        if m.spread is not None and m.mid:
            spreads.append(m.spread / m.mid * 1e4)
        if m.imbalance is not None:
            # normalize so positive = defense-favoring
            # UPPER defense ask → want negative raw imbalance (more asks); flip
            if bands.role == "UPPER":
                imbs.append(-m.imbalance)
            else:
                imbs.append(m.imbalance)
    put(_fv("spread_mean_bps", sum(spreads) / len(spreads) if spreads else None, source_table=src_m, max_ts=max_ts, available=bool(spreads), missing_reason=None if spreads else "NO_METRICS"))
    put(_fv("spread_max_bps", max(spreads) if spreads else None, source_table=src_m, max_ts=max_ts, available=bool(spreads), missing_reason=None if spreads else "NO_METRICS"))
    put(_fv("imbalance_mean", sum(imbs) / len(imbs) if imbs else None, source_table=src_m, max_ts=max_ts, available=bool(imbs), missing_reason=None if imbs else "NO_METRICS"))

    def imb_at(ts: int) -> float | None:
        cand = [m for m in metrics if m.bucket_start_ns <= ts]
        if not cand or cand[-1].imbalance is None:
            return None
        raw = cand[-1].imbalance
        return -raw if bands.role == "UPPER" else raw

    put(_fv("imbalance_at_touch", imb_at(touch_ns), source_table=src_m, max_ts=max_ts))
    if trigger_ns is None:
        put(_fv("imbalance_at_trigger", None, available=False, missing_reason="NO_TRIGGER", source_table=src_m))
    else:
        put(_fv("imbalance_at_trigger", imb_at(trigger_ns), source_table=src_m, max_ts=max_ts))

    # microprice approx: mid vs zone center
    mid_touch = None
    for m in metrics:
        if m.bucket_start_ns <= touch_ns and m.mid:
            mid_touch = m.mid
    micro_bps = None
    if mid_touch and bands.center:
        # distance into attack direction (positive = still approaching / not broken)
        if bands.role == "UPPER":
            micro_bps = (bands.center - mid_touch) / bands.center * 1e4
        else:
            micro_bps = (mid_touch - bands.center) / bands.center * 1e4
    put(_fv("microprice_distance_bps", micro_bps, source_table=src_m, max_ts=max_ts, available=micro_bps is not None, missing_reason=None if micro_bps is not None else "NO_MID"))

    # mid velocity over approach
    ap_mids = [m for m in metrics if phases["approach"][0] <= m.bucket_start_ns <= touch_ns and m.mid]
    vel = None
    acc = None
    if len(ap_mids) >= 2 and bands.center:
        dt = (ap_mids[-1].bucket_start_ns - ap_mids[0].bucket_start_ns) / NS
        if dt > 0:
            move_bps = (ap_mids[-1].mid - ap_mids[0].mid) / bands.center * 1e4
            # normalize: positive velocity = toward zone
            if bands.role == "UPPER":
                vel = move_bps / dt  # up toward upper zone is positive
            else:
                vel = (-move_bps) / dt
            if len(ap_mids) >= 3:
                mid_i = ap_mids[len(ap_mids) // 2]
                dt1 = (mid_i.bucket_start_ns - ap_mids[0].bucket_start_ns) / NS
                dt2 = (ap_mids[-1].bucket_start_ns - mid_i.bucket_start_ns) / NS
                if dt1 > 0 and dt2 > 0:
                    m1 = (mid_i.mid - ap_mids[0].mid) / bands.center * 1e4 / dt1
                    m2 = (ap_mids[-1].mid - mid_i.mid) / bands.center * 1e4 / dt2
                    if bands.role == "LOWER":
                        m1, m2 = -m1, -m2
                    acc = m2 - m1
    put(_fv("mid_velocity_bps_s", vel, source_table=src_m, max_ts=max_ts, available=vel is not None, missing_reason=None if vel is not None else "NO_MID_PATH"))
    put(_fv("mid_acceleration", acc, source_table=src_m, max_ts=max_ts, available=acc is not None, missing_reason=None if acc is not None else "NO_MID_PATH"))

    dur_s = max((cutoff_ns - feature_start) / NS, 1e-9)
    put(_fv("book_update_rate", len(m_win) / dur_s, source_table=src_m, max_ts=max_ts))
    put(_fv("level_change_rate", len(attrs) / dur_s, source_table=src_lc, max_ts=max_ts))

    # Short windows relative to touch (capped at cutoff)
    for w in SHORT_WINDOWS_S:
        w_end = min(touch_ns + int(w * NS), cutoff_ns)
        if w_end < touch_ns:
            continue
        w_attrs = _filter_attr(attrs, start_ns=touch_ns, end_ns=w_end, side=bands.defense_side)
        if trades_available:
            hq, _ = _sum_kind(w_attrs, "HIT")
            pq, _ = _sum_kind(w_attrs, "PULL")
            put(_fv(f"hit_qty_{int(w)}s", hq, source_table="public_trades+lc", start_ns=touch_ns, end_ns=w_end, max_ts=max_ts))
            put(_fv(f"pull_qty_{int(w)}s", pq, source_table="public_trades+lc", start_ns=touch_ns, end_ns=w_end, max_ts=max_ts))
        else:
            put(_fv(f"hit_qty_{int(w)}s", None, available=False, missing_reason="NOT_AVAILABLE", source_table="public_trades+lc"))
            put(_fv(f"pull_qty_{int(w)}s", None, available=False, missing_reason="NOT_AVAILABLE", source_table="public_trades+lc"))
        aq, _ = _sum_kind(w_attrs, "ADD")
        put(_fv(f"add_qty_{int(w)}s", aq, source_table=src_lc, start_ns=touch_ns, end_ns=w_end, max_ts=max_ts))

    # Public trade features
    src_t = "orderbook_analysis.public_trades_canonical"
    if not trades_available:
        for nm in (
            "aggressive_buy_notional",
            "aggressive_sell_notional",
            "net_aggressive_notional",
            "buy_share",
            "sell_share",
            "trade_count",
            "trade_notional",
            "trade_pace_notional_s",
            "max_trade_notional",
            "aggression_against_zone",
            "aggression_with_break",
            "aggression_with_fade",
            "price_move_bps_per_1m_aggressive_notional",
            "aggression_without_progress",
            "aggressive_notional_per_penetration_bp",
            "aggressive_notional_before_reclaim",
            "flow_flip_before_trigger",
        ):
            put(_fv(nm, None, available=False, missing_reason="NOT_AVAILABLE", source_table=src_t))
    else:
        t_win = []
        for t in trade_list:
            tns = trade_ts_ns(t)
            if feature_start <= tns <= cutoff_ns:
                t_win.append((tns, t))
        buy_n = sum(float(t.notional) for _, t in t_win if t.side == "Buy")
        sell_n = sum(float(t.notional) for _, t in t_win if t.side == "Sell")
        tot = buy_n + sell_n
        put(_fv("aggressive_buy_notional", buy_n, source_table=src_t, start_ns=feature_start, end_ns=cutoff_ns, max_ts=max_ts))
        put(_fv("aggressive_sell_notional", sell_n, source_table=src_t, start_ns=feature_start, end_ns=cutoff_ns, max_ts=max_ts))
        put(_fv("net_aggressive_notional", buy_n - sell_n, source_table=src_t, max_ts=max_ts))
        put(_fv("buy_share", _safe_div(buy_n, tot), source_table=src_t, max_ts=max_ts))
        put(_fv("sell_share", _safe_div(sell_n, tot), source_table=src_t, max_ts=max_ts))
        put(_fv("trade_count", float(len(t_win)), source_table=src_t, max_ts=max_ts))
        put(_fv("trade_notional", tot, source_table=src_t, max_ts=max_ts))
        put(_fv("trade_pace_notional_s", tot / dur_s, source_table=src_t, max_ts=max_ts))
        put(_fv("max_trade_notional", max((float(t.notional) for _, t in t_win), default=None), source_table=src_t, max_ts=max_ts, available=bool(t_win), missing_reason=None if t_win else "NO_TRADES_IN_WINDOW"))

        # against zone: Buy into UPPER ask / Sell into LOWER bid
        if bands.role == "UPPER":
            against = buy_n
            with_break = buy_n
            with_fade = sell_n
        else:
            against = sell_n
            with_break = sell_n
            with_fade = buy_n
        put(_fv("aggression_against_zone", against, source_table=src_t, max_ts=max_ts))
        put(_fv("aggression_with_break", with_break, source_table=src_t, max_ts=max_ts))
        put(_fv("aggression_with_fade", with_fade, source_table=src_t, max_ts=max_ts))

        # price efficiency vs penetration / mid move in contact
        pen = event.get("max_penetration_bps")
        try:
            pen_f = float(pen) if pen not in (None, "", "None") else None
        except (TypeError, ValueError):
            pen_f = None
        mid_move = None
        if mid0 and mid1 and bands.center:
            raw = (mid1 - mid0) / bands.center * 1e4
            mid_move = abs(raw)
        eff = None
        if against and against > 0 and mid_move is not None:
            eff = mid_move / (against / 1_000_000.0)
        put(_fv("price_move_bps_per_1m_aggressive_notional", eff, source_table=src_t, max_ts=max_ts, available=eff is not None, missing_reason=None if eff is not None else "NO_MOVE_OR_FLOW"))
        no_prog = None
        if against and against > 0:
            no_prog = 1.0 if (mid_move is not None and mid_move < 1.0) else 0.0
        put(_fv("aggression_without_progress", no_prog, source_table=src_t, max_ts=max_ts, available=no_prog is not None, missing_reason=None if no_prog is not None else "NO_FLOW"))
        put(_fv("aggressive_notional_per_penetration_bp", _safe_div(against, pen_f) if pen_f and pen_f > 0 else None, source_table=src_t, max_ts=max_ts, available=bool(pen_f and pen_f > 0), missing_reason=None if (pen_f and pen_f > 0) else "NO_PENETRATION"))

        reclaim_ts = event.get("reclaim_ts_ns") or event.get("confirmed_reclaim_ts_ns")
        before_reclaim = None
        if reclaim_ts not in (None, "", "None"):
            rns = int(reclaim_ts)
            if rns <= cutoff_ns:
                before_reclaim = sum(
                    float(t.notional)
                    for tns, t in t_win
                    if tns <= rns and ((bands.role == "UPPER" and t.side == "Buy") or (bands.role == "LOWER" and t.side == "Sell"))
                )
        put(_fv("aggressive_notional_before_reclaim", before_reclaim, source_table=src_t, max_ts=max_ts, available=before_reclaim is not None, missing_reason=None if before_reclaim is not None else "NO_RECLAIM_OR_AFTER_CUTOFF"))

        # flow flip: net sign changes in contact
        flip = None
        if trigger_ns and len(t_win) >= 4:
            contact_trades = [(tns, t) for tns, t in t_win if touch_ns <= tns <= cutoff_ns]
            if len(contact_trades) >= 4:
                mid_i = len(contact_trades) // 2
                n1 = sum(float(t.notional) * (1 if t.side == "Buy" else -1) for _, t in contact_trades[:mid_i])
                n2 = sum(float(t.notional) * (1 if t.side == "Buy" else -1) for _, t in contact_trades[mid_i:])
                flip = 1.0 if n1 * n2 < 0 else 0.0
        put(_fv("flow_flip_before_trigger", flip, source_table=src_t, max_ts=max_ts, available=flip is not None, missing_reason=None if flip is not None else "INSUFFICIENT_CONTACT_TRADES"))

    return bundle

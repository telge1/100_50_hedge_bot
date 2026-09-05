"""Episode discovery state machine (diagnostic only)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Optional  # noqa: F401 — Optional used in helpers

from orderbook_analyse.aggressor_efficiency_flip.acceptance import find_acceptance
from orderbook_analyse.aggressor_efficiency_flip.buckets import aggregate_window, side_vwap
from orderbook_analyse.aggressor_efficiency_flip.compression import (
    compression_notional,
    evaluate_compression,
)
from orderbook_analyse.aggressor_efficiency_flip.contracts import (
    AEFConfig,
    aggressor_side,
    counter_side,
)
from orderbook_analyse.aggressor_efficiency_flip.impact import measure_dual_impact
from orderbook_analyse.aggressor_efficiency_flip.initiative import (
    evaluate_initiative,
    initiative_notional,
)
from orderbook_analyse.aggressor_efficiency_flip.models import (
    Episode,
    SecondBucket,
    StateTransition,
    Trade,
)
from orderbook_analyse.aggressor_efficiency_flip.structure import (
    find_structure_break,
    frozen_structure_level,
)
from orderbook_analyse.aggressor_efficiency_flip.timeutil import (
    align_floor,
    floor_second,
    iso_z,
)


STATES = (
    "NEUTRAL",
    "AGGRESSOR_BURST",
    "IMPACT_COMPRESSION_PENDING",
    "IMPACT_COMPRESSION_CONFIRMED",
    "COUNTER_SIDE_WATCH",
    "COUNTER_INITIATIVE_PENDING",
    "EFFICIENCY_FLIP",
    "STRUCTURE_CONFIRM_PENDING",
    "ACCEPTANCE_PENDING",
    "DIAGNOSTIC_CANDIDATE",
    "INVALIDATED",
    "TIMEOUT",
)


def make_episode_id(
    symbol: str,
    direction: str,
    compression_start: datetime,
    feature_version: str,
) -> str:
    raw = f"{symbol}|{direction}|{iso_z(compression_start)}|{feature_version}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def discover_episodes(
    *,
    symbol: str,
    trades: list[Trade],
    buckets: dict[datetime, SecondBucket],
    start: datetime,
    end: datetime,
    cfg: AEFConfig,
    as_of: Optional[datetime] = None,
    oi_labels: Optional[dict[datetime, str]] = None,
) -> dict[str, Any]:
    """Run dual-impact discovery up to as_of (default=end).

    Prefix-parity: as_of truncates which windows may close.
    """
    symbol = str(symbol).upper()
    start = floor_second(start)
    end = floor_second(end)
    horizon = as_of if as_of is not None else end
    horizon = floor_second(horizon)

    bursts: list[dict[str, Any]] = []
    compressions: list[dict[str, Any]] = []
    counters: list[dict[str, Any]] = []
    transitions: list[StateTransition] = []
    candidates: list[Episode] = []
    timeline: list[dict[str, Any]] = []

    past_n: dict[str, list[float]] = {"Sell": [], "Buy": []}
    past_s: dict[str, list[float]] = {"Sell": [], "Buy": []}
    past_c: dict[str, list[float]] = {"Sell": [], "Buy": []}
    past_p: dict[str, list[float]] = {"Sell": [], "Buy": []}

    cooldown_until: dict[str, datetime] = {"LONG": start, "SHORT": start}
    active_starts: set[tuple[str, datetime]] = set()

    step = timedelta(seconds=cfg.burst_step_seconds)
    flow_s = cfg.flow_seconds
    post_s = cfg.post_flow_seconds
    search_s = cfg.counter_search_seconds

    t0 = align_floor(start, cfg.burst_step_seconds)
    while t0 + timedelta(seconds=flow_s + post_s) <= end:
        t1 = t0 + timedelta(seconds=flow_s)
        t2 = t1 + timedelta(seconds=post_s)
        # Prefix: need t2 closed
        if t2 > horizon:
            break

        for direction in ("LONG", "SHORT"):
            side = aggressor_side(direction)
            try:
                dual = measure_dual_impact(
                    buckets,
                    t0=t0,
                    t1=t1,
                    t2=t2,
                    side=side,
                    reclaim_bps=cfg.reclaim_bps,
                    strong_post_bps=cfg.strong_post_followthrough_bps,
                )
            except ValueError:
                continue

            notion = compression_notional(dual, side)
            bursts.append(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "t0": iso_z(t0),
                    "t1": iso_z(t1),
                    "t2": iso_z(t2),
                    "side": side,
                    "buy_notional": dual.flow.buy_notional,
                    "sell_notional": dual.flow.sell_notional,
                    "dominant_side": dual.flow.dominant_side(),
                    "dominant_share": dual.flow.dominant_share(),
                    "same_side_contemporaneous_bps": dual.same_side_contemporaneous_bps,
                    "post_same_side_followthrough_bps": dual.post_same_side_followthrough_bps,
                }
            )

            dec = evaluate_compression(
                dual,
                direction=direction,
                cfg=cfg,
                past_notionals=past_n[side][-cfg.rank_lookback_bursts :],
                past_shares=past_s[side][-cfg.rank_lookback_bursts :],
                past_contemp_impacts=past_c[side][-cfg.rank_lookback_bursts :],
                past_post_follows=past_p[side][-cfg.rank_lookback_bursts :],
            )
            # update history after evaluation (past-only)
            past_n[side].append(notion)
            past_s[side].append(dual.flow.dominant_share())
            past_c[side].append(dual.same_side_contemporaneous_bps)
            past_p[side].append(dual.post_same_side_followthrough_bps)

            key = (direction, t0)
            if key in active_starts:
                continue
            if t0 < cooldown_until[direction]:
                continue

            ep_id = make_episode_id(symbol, direction, t0, cfg.feature_version)
            compressions.append(
                {
                    "episode_id": ep_id,
                    "symbol": symbol,
                    "direction": direction,
                    "allowed": dec.allowed,
                    "reason_code": dec.reason_code,
                    "semantic_case": dec.semantic_case,
                    "strong_same_side_impact_veto": dec.strong_same_side_impact_veto,
                    "delayed_continuation_veto": dec.delayed_continuation_veto,
                    "t0": iso_z(t0),
                    "t1": iso_z(t1),
                    "t2": iso_z(t2),
                    "notional": dec.notional,
                    "dominant_share": dec.dominant_share,
                    "notional_rank": dec.notional_rank,
                    "ordinal_compression_score": dec.ordinal_compression_score,
                    "contemporaneous_return_bps": dual.contemporaneous_return_bps,
                    "same_side_contemporaneous_bps": dual.same_side_contemporaneous_bps,
                    "post_flow_return_bps": dual.post_flow_return_bps,
                    "post_same_side_followthrough_bps": dual.post_same_side_followthrough_bps,
                }
            )

            def _tr(frm: str, to: str, reason: str, event_ts: datetime, decision_ts: datetime) -> None:
                transitions.append(
                    StateTransition(
                        episode_id=ep_id,
                        symbol=symbol,
                        direction=direction,
                        event_ts=event_ts,
                        decision_ts=decision_ts,
                        from_state=frm,
                        to_state=to,
                        reason_code=reason,
                        closed_windows=f"[{iso_z(t0)},{iso_z(t1)})+[{iso_z(t1)},{iso_z(t2)})",
                        data_quality="OK" if not dual.post_empty else "DEGRADED_EMPTY_POST",
                    )
                )
                timeline.append(
                    {
                        "episode_id": ep_id,
                        "ts": iso_z(decision_ts),
                        "direction": direction,
                        "state": to,
                        "reason": reason,
                    }
                )

            if not dec.allowed:
                if dec.strong_same_side_impact_veto or dec.delayed_continuation_veto:
                    _tr("AGGRESSOR_BURST", "INVALIDATED", dec.reason_code, t1, t2)
                continue

            active_starts.add(key)
            _tr("NEUTRAL", "AGGRESSOR_BURST", "burst_closed", t1, t1)
            _tr("AGGRESSOR_BURST", "IMPACT_COMPRESSION_PENDING", "await_post", t1, t1)
            _tr(
                "IMPACT_COMPRESSION_PENDING",
                "IMPACT_COMPRESSION_CONFIRMED",
                "compression_confirmed",
                t2,
                t2,
            )
            _tr(
                "IMPACT_COMPRESSION_CONFIRMED",
                "COUNTER_SIDE_WATCH",
                "search_counter",
                t2,
                t2,
            )

            vwap = side_vwap(trades, t0, t1, side)
            cside = counter_side(direction)
            search_end = t2 + timedelta(seconds=search_s)
            found_counter = False
            u0 = t2
            while u0 + timedelta(seconds=flow_s + post_s) <= search_end:
                u1 = u0 + timedelta(seconds=flow_s)
                u2 = u1 + timedelta(seconds=post_s)
                if u2 > horizon:
                    break
                try:
                    cdual = measure_dual_impact(
                        buckets,
                        t0=u0,
                        t1=u1,
                        t2=u2,
                        side=cside,
                        reclaim_bps=cfg.reclaim_bps,
                        strong_post_bps=cfg.strong_post_followthrough_bps,
                    )
                except ValueError:
                    u0 += step
                    continue
                ide = evaluate_initiative(
                    cdual,
                    direction=direction,
                    cfg=cfg,
                    past_notionals=past_n[cside][-cfg.rank_lookback_bursts :],
                    past_shares=past_s[cside][-cfg.rank_lookback_bursts :],
                    past_impacts=past_c[cside][-cfg.rank_lookback_bursts :],
                    past_posts=past_p[cside][-cfg.rank_lookback_bursts :],
                )
                past_n[cside].append(initiative_notional(cdual, cside))
                past_s[cside].append(cdual.flow.dominant_share())
                past_c[cside].append(cdual.same_side_contemporaneous_bps)
                past_p[cside].append(cdual.post_same_side_followthrough_bps)

                counters.append(
                    {
                        "episode_id": ep_id,
                        "direction": direction,
                        "u0": iso_z(u0),
                        "u1": iso_z(u1),
                        "u2": iso_z(u2),
                        "confirmed": ide.confirmed,
                        "label": ide.label,
                        "reason_code": ide.reason_code,
                        "notional": ide.notional,
                        "directional_impact_bps": ide.directional_impact_bps,
                        "ordinal_initiative_score": ide.ordinal_initiative_score,
                    }
                )
                if not ide.confirmed:
                    u0 += step
                    continue

                found_counter = True
                _tr("COUNTER_SIDE_WATCH", "COUNTER_INITIATIVE_PENDING", "counter_flow", u1, u1)
                _tr(
                    "COUNTER_INITIATIVE_PENDING",
                    "EFFICIENCY_FLIP",
                    "initiative_confirmed",
                    u2,
                    u2,
                )
                flip_decision = u2
                level = frozen_structure_level(
                    buckets,
                    as_of_exclusive=flip_decision,
                    direction=direction,
                    lookback_s=cfg.structure_lookback_seconds,
                )
                struct_ok = True
                br_ts = None
                br_conf = None
                if cfg.require_structure:
                    if level is None:
                        _tr("EFFICIENCY_FLIP", "TIMEOUT", "no_structure_level", flip_decision, flip_decision)
                        struct_ok = False
                    else:
                        _tr(
                            "EFFICIENCY_FLIP",
                            "STRUCTURE_CONFIRM_PENDING",
                            "await_break",
                            flip_decision,
                            flip_decision,
                        )
                        br = find_structure_break(
                            buckets,
                            search_start=flip_decision,
                            search_end=min(search_end + timedelta(seconds=120), end),
                            direction=direction,
                            level=level,
                            cfg=cfg,
                        )
                        if not br.found or br.confirmed_ts is None or br.confirmed_ts > horizon:
                            _tr(
                                "STRUCTURE_CONFIRM_PENDING",
                                "TIMEOUT",
                                "no_structure_break",
                                flip_decision,
                                min(horizon, search_end),
                            )
                            struct_ok = False
                        else:
                            br_ts = br.break_ts
                            br_conf = br.confirmed_ts
                            _tr(
                                "STRUCTURE_CONFIRM_PENDING",
                                "ACCEPTANCE_PENDING",
                                "structure_break",
                                br_ts,
                                br_conf,
                            )
                acc_ts = None
                if struct_ok and cfg.require_acceptance and br_conf is not None and level is not None:
                    acc = find_acceptance(
                        buckets,
                        break_confirmed_ts=br_conf,
                        search_end=min(br_conf + timedelta(seconds=120), end),
                        direction=direction,
                        level=level,
                        cfg=cfg,
                    )
                    if not acc.found or acc.confirmed_ts is None or acc.confirmed_ts > horizon:
                        _tr(
                            "ACCEPTANCE_PENDING",
                            "TIMEOUT",
                            "acceptance_timeout",
                            br_conf,
                            min(horizon, br_conf + timedelta(seconds=120)),
                        )
                        struct_ok = False
                    else:
                        acc_ts = acc.confirmed_ts
                        _tr(
                            "ACCEPTANCE_PENDING",
                            "DIAGNOSTIC_CANDIDATE",
                            "acceptance_confirmed",
                            acc_ts,
                            acc_ts,
                        )

                if struct_ok and (not cfg.require_acceptance or acc_ts is not None):
                    final_ts = acc_ts or br_conf or flip_decision
                    # Next fully closed 1s after final_decision_ts (may be beyond as_of;
                    # candidate is still decided at final_ts for prefix parity).
                    entry_close = floor_second(final_ts) + timedelta(seconds=1)
                    entry_bucket_start = entry_close - timedelta(seconds=1)
                    if not (entry_close > final_ts):
                        raise AssertionError("diagnostic_earliest_entry_ts must be > final_decision_ts")

                    entry_px = None
                    eb = buckets.get(entry_bucket_start)
                    if eb is not None and eb.last_price is not None:
                        entry_px = eb.last_price
                    else:
                        # Causal carry-forward: last closed 1s price at/before entry bucket.
                        cur = entry_bucket_start
                        for _ in range(120):
                            bb = buckets.get(cur)
                            if bb is not None and bb.last_price is not None:
                                entry_px = bb.last_price
                                break
                            cur -= timedelta(seconds=1)
                            if cur < start:
                                break

                    flip_score = (
                        dec.ordinal_compression_score
                        + ide.ordinal_initiative_score
                        + 1.0
                        + (1.0 if br_conf else 0.0)
                        + (1.0 if acc_ts else 0.0)
                    )
                    oi_class = "MISSING"
                    if oi_labels:
                        # label at final minute floor
                        key_oi = floor_second(final_ts).replace(second=0, microsecond=0)
                        # try 5s align
                        oi_class = oi_labels.get(floor_second(final_ts), oi_labels.get(key_oi, "MISSING"))

                    fields = {
                        "episode_id": ep_id,
                        "symbol": symbol,
                        "direction": direction,
                        "profile_name": cfg.profile_name,
                        "status": cfg.status_label,
                        "source": "public_trades_canonical",
                        "data_quality": "OK",
                        "compression_start": iso_z(t0),
                        "compression_end": iso_z(t1),
                        "compression_flow_close_ts": iso_z(t1),
                        "compression_confirmed_ts": iso_z(t2),
                        "compression_side": side,
                        "compression_buy_notional": dual.flow.buy_notional,
                        "compression_sell_notional": dual.flow.sell_notional,
                        "compression_dominant_share": dual.flow.dominant_share(),
                        "compression_notional_rank": dec.notional_rank,
                        "compression_start_price": dual.flow.first_price,
                        "compression_end_price": dual.flow.last_price,
                        "compression_contemporaneous_return_bps": dual.contemporaneous_return_bps,
                        "compression_same_side_impact_bps": dual.same_side_contemporaneous_bps,
                        "compression_post_flow_return_bps": dual.post_flow_return_bps,
                        "compression_post_same_side_followthrough_bps": dual.post_same_side_followthrough_bps,
                        "compression_counter_move_bps": dual.post_counter_move_bps,
                        "strong_same_side_impact_veto": False,
                        "compression_aggressor_vwap": vwap,
                        "counter_search_start": iso_z(t2),
                        "counter_search_end": iso_z(search_end),
                        "counter_flow_start": iso_z(u0),
                        "counter_flow_end": iso_z(u1),
                        "counter_flow_close_ts": iso_z(u1),
                        "counter_confirmed_ts": iso_z(u2),
                        "counter_side": cside,
                        "counter_buy_notional": cdual.flow.buy_notional,
                        "counter_sell_notional": cdual.flow.sell_notional,
                        "counter_dominant_share": cdual.flow.dominant_share(),
                        "counter_notional_rank": ide.notional_rank,
                        "counter_contemporaneous_return_bps": cdual.contemporaneous_return_bps,
                        "counter_directional_impact_bps": ide.directional_impact_bps,
                        "counter_post_flow_return_bps": cdual.post_flow_return_bps,
                        "flip_delay_seconds": (u0 - t2).total_seconds(),
                        "ordinal_compression_score": dec.ordinal_compression_score,
                        "ordinal_initiative_score": ide.ordinal_initiative_score,
                        "ordinal_flip_score": flip_score,
                        "trapped_aggressor_vwap_label": _trapped_label(
                            direction, vwap, dual.flow.last_price, cdual.flow.last_price
                        ),
                        "structure_level": level,
                        "structure_break_ts": iso_z(br_ts),
                        "structure_break_confirmed_ts": iso_z(br_conf),
                        "acceptance_confirmed_ts": iso_z(acc_ts),
                        "final_decision_ts": iso_z(final_ts),
                        "diagnostic_earliest_entry_ts": iso_z(entry_close),
                        "diagnostic_earliest_entry_price": entry_px,
                        "invalidation_ts": None,
                        "invalidation_reason": None,
                        "timeout_reason": None,
                        "oi_class": oi_class,
                        "oi_available": oi_class not in {None, "MISSING"},
                        "ob_available": False,
                        "bbo_available": False,
                        "pool_context_available": False,
                        "feature_version": cfg.feature_version,
                        "causal_contract_version": cfg.causal_contract_version,
                    }
                    candidates.append(Episode(fields=fields))
                    cooldown_until[direction] = final_ts + timedelta(seconds=cfg.cooldown_seconds)
                break  # one counter per compression episode

            if not found_counter and t2 + timedelta(seconds=search_s) <= horizon:
                _tr(
                    "COUNTER_SIDE_WATCH",
                    "TIMEOUT",
                    "no_counter_within_search",
                    t2,
                    t2 + timedelta(seconds=search_s),
                )
            elif not found_counter and search_end > horizon:
                # still watching under prefix — no terminal yet
                pass

        t0 += step

    return {
        "bursts": bursts,
        "compressions": compressions,
        "counters": counters,
        "transitions": [t.to_dict() for t in transitions],
        "candidates": [c.to_dict() for c in candidates],
        "timeline": timeline,
        "horizon": iso_z(horizon),
    }


def _trapped_label(
    direction: str,
    vwap: Optional[float],
    compress_end: Optional[float],
    counter_end: Optional[float],
) -> str:
    if vwap is None or counter_end is None:
        return "UNAVAILABLE"
    if direction == "LONG":
        return "RECLAIM_ABOVE_SELL_VWAP" if counter_end > vwap else "BELOW_SELL_VWAP"
    return "RECLAIM_BELOW_BUY_VWAP" if counter_end < vwap else "ABOVE_BUY_VWAP"

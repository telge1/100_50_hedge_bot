"""Multi-price ask wall generations with mass attribution (epoch-scoped)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt, _apply_change, _apply_reset
from ..level_first_episode1_wall_flow_qdh_base_v1.mass_balance import decompose_mass_balance
from ..timeparse import format_utc_z
from . import (
    ASK_WALL_BREACH,
    EPSILON,
    LAYER_POST_BREACH,
    LAYER_POST_EPOCH,
    LAYER_PRE_EXISTING,
    MASS_TOL,
    MIN_RELEVANT_ASK_QTY,
    ORIGINAL_WALL_GENERATION_ID,
    ORIGINAL_WALL_PRICE,
    TICK_SIZE,
    analysis_band_bounds,
)
from .segments import EpochSegment, iter_merged_events


def _gid(*, side: str, price: float, epoch: int, gen_index: int, start: datetime) -> str:
    raw = f"{side}|{price:.10f}|epoch={epoch}|gen={gen_index}|start={format_utc_z(start)}"
    return "wg_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _price_key(px: float) -> float:
    return round(float(px) / TICK_SIZE) * TICK_SIZE


@dataclass
class OpenGen:
    price: float
    epoch: int
    generation_index: int
    start: datetime
    initial_qty: float
    peak_qty: float
    qty: float
    source_event_id: str | None
    cum_fills: float = 0.0
    cum_pulls: float = 0.0
    cum_refills: float = 0.0
    first_trade_touch: datetime | None = None
    last_trade_touch: datetime | None = None
    queue_at_last_node: float = 0.0
    last_node_time: datetime | None = None


@dataclass
class AskWallGeneration:
    wall_generation_id: str
    replay_epoch: int
    side: str
    price: float
    start_time: str
    end_time: str | None
    initial_qty: float
    peak_qty: float
    final_qty: float
    cumulative_attributed_fills: float
    cumulative_residual_pulls: float
    cumulative_refills: float
    termination_reason: str
    first_trade_touch: str | None
    last_trade_touch: str | None
    distance_from_original_wall_ticks: float
    distance_from_mid_ticks: float | None
    attribution_confidence: str
    coverage_ok: bool
    layer_class: str
    generation_index: int
    mid_at_start: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_generation_id": self.wall_generation_id,
            "replay_epoch": self.replay_epoch,
            "side": self.side,
            "price": self.price,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "initial_qty": self.initial_qty,
            "peak_qty": self.peak_qty,
            "final_qty": self.final_qty,
            "cumulative_attributed_fills": self.cumulative_attributed_fills,
            "cumulative_residual_pulls": self.cumulative_residual_pulls,
            "cumulative_refills": self.cumulative_refills,
            "termination_reason": self.termination_reason,
            "first_trade_touch": self.first_trade_touch,
            "last_trade_touch": self.last_trade_touch,
            "distance_from_original_wall_ticks": self.distance_from_original_wall_ticks,
            "distance_from_mid_ticks": self.distance_from_mid_ticks,
            "attribution_confidence": self.attribution_confidence,
            "coverage_ok": self.coverage_ok,
            "layer_class": self.layer_class,
            "generation_index": self.generation_index,
            "mid_at_start": self.mid_at_start,
        }


def _termination_reason(*, fills: float, pulls: float, refills: float, final_qty: float, reason_hint: str) -> str:
    if reason_hint in ("epoch_boundary", "segment_end", "checkpoint_reanchor", "reset"):
        return reason_hint
    total = fills + pulls
    if total <= EPSILON:
        return "UNKNOWN_END" if final_qty <= EPSILON else "STILL_ACTIVE_OR_UNKNOWN"
    fill_share = fills / total
    pull_share = pulls / total
    if fill_share >= 0.7:
        return "MOSTLY_FILLED"
    if pull_share >= 0.7:
        return "MOSTLY_PULLED"
    if fills > EPSILON and pulls > EPSILON:
        return "MIXED_FILL_PULL"
    return "UNKNOWN_END"


def _layer_class(*, start: datetime, breach: datetime | None, segment: EpochSegment) -> str:
    if segment.replay_epoch >= 5 or segment.anchor_mode == "checkpoint_reanchor":
        return LAYER_POST_EPOCH
    if breach is None:
        return LAYER_PRE_EXISTING
    if start < breach:
        return LAYER_PRE_EXISTING
    return LAYER_POST_BREACH


def build_ask_generations_for_segment(
    *,
    payload: dict[str, Any],
    segment: EpochSegment,
    trades: list[dict[str, Any]],
    wall_price: float = ORIGINAL_WALL_PRICE,
    min_qty: float = MIN_RELEVANT_ASK_QTY,
    breach_iso: str | None = ASK_WALL_BREACH,
) -> list[AskWallGeneration]:
    """Build ask-wall generations inside one epoch segment only."""
    band_low, band_high = analysis_band_bounds(wall_price=wall_price)
    breach = _as_dt(breach_iso) if breach_iso and segment.replay_epoch == 4 else None

    # Index buy-taker trades by rounded price for attribution
    trades_by_price: dict[float, list[tuple[datetime, float, str]]] = {}
    for tr in trades:
        side = str(tr.get("taker_side") or tr.get("side") or "").lower()
        if side not in ("buy", "b"):
            continue
        px = _price_key(float(tr["price"]))
        if px < band_low - 1e-9 or px > band_high + 1e-9:
            continue
        ts = _as_dt(tr.get("trade_ts") or tr.get("exchange_event_time") or tr.get("event_time"))
        if ts < segment.coverage_start or ts > segment.coverage_end:
            continue
        tid = str(tr.get("trade_id") or "")
        trades_by_price.setdefault(px, []).append((ts, float(tr.get("size") or tr.get("qty") or 0.0), tid))
    for px in trades_by_price:
        trades_by_price[px].sort(key=lambda x: x[0])

    consumed_trade_ids: set[str] = set()
    bids = dict(payload["initial_bids"])
    asks = dict(payload["initial_asks"])
    epoch = int(payload.get("initial_replay_epoch") or 0)
    open_gens: dict[float, OpenGen] = {}
    closed: list[AskWallGeneration] = []
    gen_counter = 0
    mid_at_start_cache: dict[float, float | None] = {}

    def mid_now() -> float | None:
        if not bids or not asks:
            return None
        return 0.5 * (max(bids) + min(asks))

    def in_band(px: float) -> bool:
        return band_low - 1e-9 <= px <= band_high + 1e-9

    def attribute_step(og: OpenGen, new_qty: float, ts: datetime) -> None:
        q0 = float(og.queue_at_last_node if og.last_node_time is not None else og.qty)
        # trades in (last_node_time, ts] at this price
        fills = 0.0
        t0 = og.last_node_time or og.start
        for tts, sz, tid in trades_by_price.get(_price_key(og.price), []):
            if tts <= t0:
                continue
            if tts > ts:
                break
            if tid and tid in consumed_trade_ids:
                continue
            fills += sz
            if tid:
                consumed_trade_ids.add(tid)
            if og.first_trade_touch is None:
                og.first_trade_touch = tts
            og.last_trade_touch = tts
        mb = decompose_mass_balance(queue_before=q0, queue_after=new_qty, attributed_hit_qty=fills, tol=MASS_TOL)
        og.cum_fills += mb.attributed_hit_qty
        og.cum_pulls += mb.residual_pull_qty
        og.cum_refills += mb.net_refill_qty
        og.queue_at_last_node = new_qty
        og.last_node_time = ts
        og.qty = new_qty
        og.peak_qty = max(og.peak_qty, new_qty)

    def close_price(px: float, ts: datetime, reason_hint: str) -> None:
        nonlocal open_gens
        og = open_gens.pop(px, None)
        if og is None:
            return
        attribute_step(og, 0.0 if reason_hint != "segment_end" else og.qty, ts)
        final_qty = 0.0 if reason_hint not in ("segment_end",) else og.qty
        if reason_hint == "segment_end":
            final_qty = og.qty
        term = _termination_reason(
            fills=og.cum_fills, pulls=og.cum_pulls, refills=og.cum_refills, final_qty=final_qty, reason_hint=reason_hint
        )
        mid0 = mid_at_start_cache.get(px)
        dist_mid = None
        if mid0 is not None:
            dist_mid = (og.price - mid0) / TICK_SIZE
        conf = "HIGH"
        if og.cum_fills + og.cum_pulls + og.cum_refills <= EPSILON and term == "UNKNOWN_END":
            conf = "MEDIUM"
        layer = _layer_class(start=og.start, breach=breach, segment=segment)
        # Never classify epoch5 as migration of epoch4
        if segment.replay_epoch != 4:
            layer = LAYER_POST_EPOCH
        closed.append(
            AskWallGeneration(
                wall_generation_id=_gid(
                    side="ask", price=og.price, epoch=og.epoch, gen_index=og.generation_index, start=og.start
                ),
                replay_epoch=og.epoch,
                side="ask",
                price=og.price,
                start_time=format_utc_z(og.start),
                end_time=None if reason_hint == "segment_end" and final_qty > EPSILON else format_utc_z(ts),
                initial_qty=og.initial_qty,
                peak_qty=og.peak_qty,
                final_qty=final_qty,
                cumulative_attributed_fills=og.cum_fills,
                cumulative_residual_pulls=og.cum_pulls,
                cumulative_refills=og.cum_refills,
                termination_reason=term if final_qty <= EPSILON or reason_hint != "segment_end" else "STILL_ACTIVE_AT_SEGMENT_END",
                first_trade_touch=format_utc_z(og.first_trade_touch) if og.first_trade_touch else None,
                last_trade_touch=format_utc_z(og.last_trade_touch) if og.last_trade_touch else None,
                distance_from_original_wall_ticks=(og.price - wall_price) / TICK_SIZE,
                distance_from_mid_ticks=dist_mid,
                attribution_confidence=conf,
                coverage_ok=True,
                layer_class=layer,
                generation_index=og.generation_index,
                mid_at_start=mid0,
            )
        )

    def open_price(px: float, qty: float, ts: datetime, ep: int, source_id: str | None) -> None:
        nonlocal gen_counter
        if px in open_gens:
            return
        if qty < min_qty:
            return
        gen_counter += 1
        mid_at_start_cache[px] = mid_now()
        open_gens[px] = OpenGen(
            price=px,
            epoch=ep,
            generation_index=gen_counter,
            start=ts,
            initial_qty=qty,
            peak_qty=qty,
            qty=qty,
            source_event_id=source_id,
            queue_at_last_node=qty,
            last_node_time=ts,
        )

    def sync_opens(ts: datetime, ep: int, source_id: str | None, *, only_price: float | None = None) -> None:
        # Close wiped; open new relevant asks; update peaks on existing
        if only_price is not None:
            px = _price_key(only_price)
            q = 0.0
            for ap, aq in asks.items():
                if abs(float(ap) - px) <= 1e-9:
                    q = float(aq)
                    break
            if px in open_gens:
                if q <= EPSILON:
                    close_price(px, ts, "size_wipe")
                elif abs(q - open_gens[px].qty) > EPSILON:
                    attribute_step(open_gens[px], q, ts)
                else:
                    open_gens[px].peak_qty = max(open_gens[px].peak_qty, q)
            elif q >= min_qty and in_band(px):
                open_price(px, q, ts, ep, source_id)
            return
        alive = {_price_key(px): float(q) for px, q in asks.items() if q and in_band(float(px)) and float(q) > 0}
        for px in list(open_gens.keys()):
            if px not in alive or alive[px] <= EPSILON:
                close_price(px, ts, "size_wipe")
        for px, q in alive.items():
            if px in open_gens:
                og = open_gens[px]
                if abs(q - og.qty) > EPSILON:
                    attribute_step(og, q, ts)
                else:
                    og.peak_qty = max(og.peak_qty, q)
            else:
                if q >= min_qty:
                    open_price(px, q, ts, ep, source_id)

    # Warmup to segment start
    entered = False
    for et, _o, _k, _i, kind, pev in iter_merged_events(payload):
        if et > segment.coverage_end:
            break
        if kind == "reset":
            _apply_reset(bids, asks, pev)
            new_ep = int(pev.get("replay_epoch") or epoch)
            # On reset: close all opens at reset time if inside segment
            if entered and et >= segment.coverage_start and et <= segment.coverage_end and int(epoch) == segment.replay_epoch:
                for px in list(open_gens.keys()):
                    close_price(px, et, "reset")
            epoch = new_ep
            if et >= segment.coverage_start and epoch == segment.replay_epoch:
                if not entered:
                    entered = True
                sync_opens(max(et, segment.coverage_start), epoch, str(pev.get("checkpoint_or_snapshot_id") or "reset"))
            continue

        _apply_change(bids, asks, pev)
        new_ep = int(pev.get("replay_epoch") or epoch)

        # Before coverage: just maintain book
        if et < segment.coverage_start:
            epoch = new_ep
            continue

        # First entry into coverage: open gens from book as-of coverage_start
        if not entered:
            epoch = new_ep
            if epoch == segment.replay_epoch:
                entered = True
                sync_opens(segment.coverage_start, epoch, "coverage_start_snapshot")
            else:
                continue

        # Epoch isolation: ignore other epochs entirely inside this builder
        if new_ep != segment.replay_epoch and epoch != segment.replay_epoch:
            epoch = new_ep
            continue

        # Crossing into foreign epoch inside coverage → close segment gens, stop
        if epoch == segment.replay_epoch and new_ep != segment.replay_epoch:
            for px in list(open_gens.keys()):
                close_price(px, et, "epoch_boundary")
            break

        epoch = new_ep
        if epoch != segment.replay_epoch:
            continue

        # Only process ask-side band updates tightly, but sync from full asks map
        side = str(pev.get("side") or "")
        try:
            px = float(pev.get("price")) if pev.get("price") is not None else None
        except (TypeError, ValueError):
            px = None
        if side == "ask" and px is not None and in_band(px):
            sync_opens(et, epoch, str(pev.get("source_event_id") or pev.get("u") or "lc"), only_price=px)
        elif side != "ask":
            # bid updates can move mid but not ask gens; skip full sync
            pass
        else:
            # ask outside band — ignore for gens
            pass

    # Segment end: close or mark still active
    for px in list(open_gens.keys()):
        close_price(px, segment.coverage_end, "segment_end")

    # Tag original wall generation id match if same price/start semantics
    for g in closed:
        if (
            abs(g.price - ORIGINAL_WALL_PRICE) <= 1e-9
            and g.replay_epoch == 4
            and g.wall_generation_id != ORIGINAL_WALL_GENERATION_ID
        ):
            # Keep computed id; record note via attribution if start aligns with known gen
            pass
    return sorted(closed, key=lambda g: (g.start_time, g.price, g.generation_index))

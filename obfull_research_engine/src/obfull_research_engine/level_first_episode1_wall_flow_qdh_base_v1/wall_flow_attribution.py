"""Causal wall-flow attribution for exact-price and defended-band queues.

Attribution interval rule (documented, Bybit L2 research):
  - Book queue observations at the wall (exact or band aggregate) define nodes.
  - Interval i is exchange-time half-open (t_i, t_{i+1}] where t are book
    event_times (ties broken by apply_order, then source_event_id).
  - Buy-taker trades against an ask wall with price in the view's price rule
    and exchange_event_time in (t_i, t_{i+1}] are attributed to the interval
    that ends at book state i+1.
  - Same-millisecond trades: sorted by (exchange_event_time, trade_id).
  - A trade is attributed to at most one interval (enforced by consumption set).
  - attribution_available_at = max(trade.event_available_at..., next_book.available_at)
  - Future book information is never treated as known at the trade instant;
    the feature becomes available only at attribution_available_at.

L2 limit: gross add vs cancel within one interval is not identifiable; only
net_refill / residual_pull are emitted.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from ..drilldown.aggregation_100ms import _as_dt
from ..timeparse import format_utc_z
from . import (
    ATTRIBUTION_RULE_VERSION,
    BAND_TICKS_DEFAULT,
    BUCKET_MS,
    EPISODE_ID,
    MASS_BALANCE_ABS_TOL,
    SYMBOL,
    TICK_SIZE,
    WALL_ID,
    WALL_PRICE,
    WALL_SIDE,
    ZONE_ID,
)
from ..level_first_episode1_corrected_sms1_persist_v1.persist import event_available_at
from .canonical_trades import CanonicalTrade, trades_hash
from .mass_balance import decompose_mass_balance

ViewKind = Literal["exact_price", "defended_band"]
Confidence = Literal["HIGH", "MEDIUM", "LOW", "INVALID"]


@dataclass
class BookNode:
    exchange_event_time: datetime
    available_at: datetime
    collector_received_at: datetime | None
    queue: float
    replay_epoch: int | None
    sequence: int | None
    update_id: int | None
    apply_order: int | None
    source_event_id: str | None
    source_record_id: str


@dataclass
class WallFlowEvent:
    episode_id: str
    symbol: str
    zone_id: str
    wall_id: str
    wall_side: str
    wall_price: float
    band_low: float
    band_high: float
    view: str
    event_type: str
    interval_start_exchange_time: str
    interval_end_exchange_time: str
    interval_start_available_at: str
    interval_end_available_at: str
    max_input_available_at: str
    computed_at: str
    attribution_available_at: str
    replay_epoch: int | None
    book_sequence_start: int | None
    book_sequence_end: int | None
    book_update_id_start: int | None
    book_update_id_end: int | None
    queue_before: float
    queue_after: float
    delta_queue: float
    attributed_trade_count: int
    attributed_trade_ids: list[str]
    attributed_trade_ids_hash: str
    attributed_hit_qty: float
    attributed_hit_notional: float
    net_refill_qty: float
    residual_pull_qty: float
    net_depletion_qty: float
    aggregate_queue_survival_proxy: float
    aggressor_side: str
    attribution_rule_version: str
    attribution_confidence: str
    mass_balance_error: float
    coverage_ok: bool
    look_ahead: bool
    source_record_ids: list[str]


def band_bounds(
    wall_price: float = WALL_PRICE,
    *,
    tick_size: float = TICK_SIZE,
    band_ticks: int = BAND_TICKS_DEFAULT,
) -> tuple[float, float]:
    half = float(band_ticks) * float(tick_size)
    return wall_price - half, wall_price + half


def _price_in_view(price: float, *, view: ViewKind, wall_price: float, band_low: float, band_high: float) -> bool:
    if view == "exact_price":
        return abs(float(price) - float(wall_price)) <= 1e-9
    return float(band_low) - 1e-12 <= float(price) <= float(band_high) + 1e-12


def is_ask_wall_attack_trade(
    trade: CanonicalTrade,
    *,
    view: ViewKind,
    wall_price: float,
    band_low: float,
    band_high: float,
    wall_side: str = WALL_SIDE,
) -> bool:
    if wall_side != "ask":
        raise NotImplementedError("Episode-1 implementation covers ask wall only")
    if trade.taker_side != "Buy":
        return False
    return _price_in_view(trade.price, view=view, wall_price=wall_price, band_low=band_low, band_high=band_high)


def _recv_dt(row: dict[str, Any]) -> datetime | None:
    for key in ("collector_received_at", "received_at", "receive_time_ns", "ingest_timestamp"):
        v = row.get(key)
        if v in (None, ""):
            continue
        if isinstance(v, (int, float)) and key == "receive_time_ns":
            # ns epoch
            return datetime.fromtimestamp(float(v) / 1e9, tz=timezone.utc)
        return _as_dt(v)
    return None


def _book_available(row: dict[str, Any], exch: datetime) -> tuple[datetime, datetime | None, bool]:
    """Return (available_at, collector_received_at, receive_used)."""
    recv = _recv_dt(row)
    if recv is not None:
        return recv, recv, True
    proxy = row.get("event_available_at") or row.get("available_at")
    if proxy:
        return _as_dt(proxy), None, False
    return event_available_at(exch, BUCKET_MS), None, False


def build_exact_price_nodes(
    *,
    initial_queue: float,
    initial_event_time: datetime,
    initial_available_at: datetime,
    initial_replay_epoch: int | None,
    level_changes: list[dict[str, Any]],
    wall_price: float = WALL_PRICE,
    wall_side: str = WALL_SIDE,
) -> tuple[list[BookNode], dict[str, Any]]:
    nodes: list[BookNode] = [
        BookNode(
            exchange_event_time=initial_event_time,
            available_at=initial_available_at,
            collector_received_at=None,
            queue=float(initial_queue),
            replay_epoch=initial_replay_epoch,
            sequence=None,
            update_id=None,
            apply_order=-1,
            source_event_id="initial_book",
            source_record_id="initial_book",
        )
    ]
    receive_present = 0
    receive_missing = 0
    filtered = [
        r
        for r in level_changes
        if str(r.get("side")) == wall_side and abs(float(r["price"]) - float(wall_price)) <= 1e-9
    ]
    filtered.sort(
        key=lambda r: (
            _as_dt(r["event_time"]).timestamp(),
            int(r.get("apply_order") or 0),
            str(r.get("source_event_id") or ""),
        )
    )
    q = float(initial_queue)
    for r in filtered:
        exch = _as_dt(r["event_time"])
        avail, recv, used = _book_available(r, exch)
        if used:
            receive_present += 1
        else:
            receive_missing += 1
        old_s = float(r.get("old_size") if r.get("old_size") is not None else q)
        new_s = float(r["new_size"])
        # Consistency soft-check: prefer new_size as authoritative after update.
        q = new_s
        nodes.append(
            BookNode(
                exchange_event_time=exch,
                available_at=avail,
                collector_received_at=recv,
                queue=q,
                replay_epoch=None if r.get("replay_epoch") is None else int(r["replay_epoch"]),
                sequence=None if r.get("seq") is None else int(r["seq"]),
                update_id=None if r.get("u") is None else int(r["u"]),
                apply_order=None if r.get("apply_order") is None else int(r["apply_order"]),
                source_event_id=None if r.get("source_event_id") is None else str(r["source_event_id"]),
                source_record_id=str(r.get("source_event_id") or f"lc:{exch.isoformat()}:{old_s}->{new_s}"),
            )
        )
    meta = {
        "view": "exact_price",
        "n_nodes": len(nodes),
        "n_level_changes": len(filtered),
        "book_receive_present": receive_present,
        "book_receive_missing": receive_missing,
        "old_size_ignored_note": "queue_after uses new_size; old_size not required for mass balance",
    }
    return nodes, meta


def build_band_nodes(
    *,
    initial_asks: dict[float, float],
    initial_event_time: datetime,
    initial_available_at: datetime,
    initial_replay_epoch: int | None,
    level_changes: list[dict[str, Any]],
    band_low: float,
    band_high: float,
    wall_side: str = WALL_SIDE,
) -> tuple[list[BookNode], dict[str, Any]]:
    """Band queue = sum of ask sizes in [band_low, band_high]. Internal shifts net out."""
    book: dict[float, float] = {
        float(px): float(qty)
        for px, qty in initial_asks.items()
        if qty and float(qty) > 0 and band_low - 1e-12 <= float(px) <= band_high + 1e-12
    }

    def band_sum() -> float:
        return float(sum(book.values()))

    nodes = [
        BookNode(
            exchange_event_time=initial_event_time,
            available_at=initial_available_at,
            collector_received_at=None,
            queue=band_sum(),
            replay_epoch=initial_replay_epoch,
            sequence=None,
            update_id=None,
            apply_order=-1,
            source_event_id="initial_book_band",
            source_record_id="initial_book_band",
        )
    ]
    filtered = [
        r
        for r in level_changes
        if str(r.get("side")) == wall_side
        and band_low - 1e-12 <= float(r["price"]) <= band_high + 1e-12
    ]
    filtered.sort(
        key=lambda r: (
            _as_dt(r["event_time"]).timestamp(),
            int(r.get("apply_order") or 0),
            str(r.get("source_event_id") or ""),
        )
    )
    # Collapse simultaneous updates into one node so intra-band reshuffles are netted.
    receive_present = 0
    receive_missing = 0
    i = 0
    while i < len(filtered):
        r0 = filtered[i]
        exch = _as_dt(r0["event_time"])
        apply0 = int(r0.get("apply_order") or 0)
        group = [r0]
        j = i + 1
        while j < len(filtered):
            rj = filtered[j]
            if _as_dt(rj["event_time"]) != exch:
                break
            # Same exchange ms: include contiguous apply_order cluster from same update burst.
            group.append(rj)
            j += 1
        for r in group:
            px = float(r["price"])
            new_s = float(r["new_size"])
            if new_s <= 0:
                book.pop(px, None)
            else:
                book[px] = new_s
            avail, recv, used = _book_available(r, exch)
            if used:
                receive_present += 1
            else:
                receive_missing += 1
        last = group[-1]
        nodes.append(
            BookNode(
                exchange_event_time=exch,
                available_at=_book_available(last, exch)[0],
                collector_received_at=_book_available(last, exch)[1],
                queue=band_sum(),
                replay_epoch=None if last.get("replay_epoch") is None else int(last["replay_epoch"]),
                sequence=None if last.get("seq") is None else int(last["seq"]),
                update_id=None if last.get("u") is None else int(last["u"]),
                apply_order=None if last.get("apply_order") is None else int(last["apply_order"]),
                source_event_id=None if last.get("source_event_id") is None else str(last["source_event_id"]),
                source_record_id="|".join(str(g.get("source_event_id") or "") for g in group),
            )
        )
        i = j
    meta = {
        "view": "defended_band",
        "n_nodes": len(nodes),
        "n_level_changes_in_band": len(filtered),
        "book_receive_present": receive_present,
        "book_receive_missing": receive_missing,
        "note": "Same-ms band updates collapsed so internal reshuffles are not full pull+refill",
    }
    return nodes, meta


def _confidence(
    *,
    n0: BookNode,
    n1: BookNode,
    trades: list[CanonicalTrade],
    mass_ok: bool,
    seq_gap: bool,
    epoch_change: bool,
) -> Confidence:
    if not mass_ok or seq_gap or epoch_change:
        return "INVALID"
    if any(t.collector_received_at is None for t in trades) or n1.collector_received_at is None:
        # Deterministic but missing receive → LOW for causal claim (still usable event-time).
        low = True
    else:
        low = False
    same_ts = n0.exchange_event_time == n1.exchange_event_time
    amb_trades = len({t.exchange_event_time for t in trades}) < len(trades) if trades else False
    if same_ts or amb_trades:
        return "LOW" if low else "MEDIUM"
    if low:
        return "LOW"
    return "HIGH"


def _seq_gap(n0: BookNode, n1: BookNode) -> bool:
    """Only treat sequence *decreases* as gaps.

    Bybit linear seq can advance by thousands within 100ms on BTCUSDT; a large
    positive jump is not evidence of a missing wall-level update.
    """
    if n0.sequence is None or n1.sequence is None:
        return False
    return n1.sequence < n0.sequence


def attribute_intervals(
    *,
    nodes: list[BookNode],
    trades: list[CanonicalTrade],
    view: ViewKind,
    wall_price: float,
    band_low: float,
    band_high: float,
    wall_visible_at: datetime,
    analysis_end_exclusive: datetime,
    episode_id: str = EPISODE_ID,
    zone_id: str = ZONE_ID,
    wall_id: str = WALL_ID,
    wall_side: str = WALL_SIDE,
    consumed_trade_ids: set[str] | None = None,
    computed_at: datetime | None = None,
) -> tuple[list[WallFlowEvent], dict[str, Any]]:
    """Attribute trades to book intervals; each trade id at most once per view's consumed set."""
    consumed = consumed_trade_ids if consumed_trade_ids is not None else set()
    now = computed_at or datetime.now(timezone.utc)
    events: list[WallFlowEvent] = []
    stats = {
        "intervals": 0,
        "invalid_intervals": 0,
        "mass_balance_violations": 0,
        "trades_attributed": 0,
        "trades_skipped_before_visible": 0,
        "trades_skipped_after_end": 0,
        "trades_skipped_wrong_side_or_price": 0,
        "trades_skipped_already_consumed": 0,
        "cross_epoch": 0,
        "seq_gaps": 0,
    }

    # Candidate trades sorted; two-pointer scan across intervals (monotonic ends).
    sorted_trades = sorted(trades, key=lambda t: (t.exchange_dt().timestamp(), t.trade_id))
    trade_i = 0
    n_trades = len(sorted_trades)

    for i in range(len(nodes) - 1):
        n0 = nodes[i]
        n1 = nodes[i + 1]
        t0 = n0.exchange_event_time
        t1 = n1.exchange_event_time
        # Only intervals that overlap analysis after wall visibility
        if t1 <= wall_visible_at or t0 >= analysis_end_exclusive:
            continue
        stats["intervals"] += 1

        while trade_i < n_trades and sorted_trades[trade_i].exchange_dt() <= t0:
            trade_i += 1

        attributed: list[CanonicalTrade] = []
        j = trade_i
        while j < n_trades:
            tr = sorted_trades[j]
            ts = tr.exchange_dt()
            if ts > t1:
                break
            j += 1
            if ts < wall_visible_at:
                continue
            if ts >= analysis_end_exclusive:
                continue
            if not is_ask_wall_attack_trade(
                tr, view=view, wall_price=wall_price, band_low=band_low, band_high=band_high, wall_side=wall_side
            ):
                continue
            if tr.trade_id in consumed:
                stats["trades_skipped_already_consumed"] += 1
                continue
            attributed.append(tr)
            consumed.add(tr.trade_id)

        x = sum(t.size_base for t in attributed)
        x_notional = sum(t.notional_usdt for t in attributed)
        mb = decompose_mass_balance(queue_before=n0.queue, queue_after=n1.queue, attributed_hit_qty=x)
        if not mb.identity_ok:
            stats["mass_balance_violations"] += 1

        epoch_change = (
            n0.replay_epoch is not None
            and n1.replay_epoch is not None
            and n0.replay_epoch != n1.replay_epoch
        )
        if epoch_change:
            stats["cross_epoch"] += 1
        gap = _seq_gap(n0, n1)
        if gap:
            stats["seq_gaps"] += 1

        conf = _confidence(
            n0=n0, n1=n1, trades=attributed, mass_ok=mb.identity_ok, seq_gap=gap, epoch_change=epoch_change
        )
        if conf == "INVALID":
            stats["invalid_intervals"] += 1

        # Event type (descriptive; not a wall-state label)
        if conf == "INVALID":
            et = "ATTRIBUTION_INVALID"
        elif conf in ("LOW",) and not attributed and abs(mb.delta_queue) < MASS_BALANCE_ABS_TOL:
            et = "ATTRIBUTION_AMBIGUOUS" if n0.exchange_event_time == n1.exchange_event_time else "QUEUE_UNCHANGED_UNDER_HIT"
            if not attributed:
                et = "QUEUE_UNCHANGED_UNDER_HIT" if abs(mb.delta_queue) < MASS_BALANCE_ABS_TOL else "QUEUE_DEPLETION"
        elif attributed and abs(mb.net_refill_qty) < MASS_BALANCE_ABS_TOL and abs(mb.residual_pull_qty) < MASS_BALANCE_ABS_TOL:
            et = "WALL_HIT" if abs(mb.delta_queue + x) < MASS_BALANCE_ABS_TOL else "QUEUE_DEPLETION"
            if abs(mb.delta_queue) < MASS_BALANCE_ABS_TOL and x > 0:
                et = "QUEUE_UNCHANGED_UNDER_HIT"
            elif x > 0:
                et = "WALL_HIT"
        elif mb.net_refill_qty > MASS_BALANCE_ABS_TOL and x > 0:
            et = "NET_REFILL"
        elif mb.residual_pull_qty > MASS_BALANCE_ABS_TOL:
            et = "RESIDUAL_PULL"
        elif x > 0:
            et = "WALL_HIT"
        elif mb.net_depletion_qty > MASS_BALANCE_ABS_TOL:
            et = "QUEUE_DEPLETION"
        else:
            et = "QUEUE_UNCHANGED_UNDER_HIT"

        trade_ids = [t.trade_id for t in attributed]
        max_in = n1.available_at
        for t in attributed:
            ta = t.available_dt()
            if ta > max_in:
                max_in = ta
        # Attribution needs the closing book state
        attr_avail = max_in
        look_ahead = attr_avail < n1.available_at  # should never happen
        if any(t.available_dt() > attr_avail for t in attributed):
            look_ahead = True

        stats["trades_attributed"] += len(attributed)
        events.append(
            WallFlowEvent(
                episode_id=episode_id,
                symbol=SYMBOL,
                zone_id=zone_id,
                wall_id=wall_id,
                wall_side=wall_side,
                wall_price=wall_price,
                band_low=band_low,
                band_high=band_high,
                view=view,
                event_type=et if conf != "INVALID" else "ATTRIBUTION_INVALID",
                interval_start_exchange_time=format_utc_z(t0),
                interval_end_exchange_time=format_utc_z(t1),
                interval_start_available_at=format_utc_z(n0.available_at),
                interval_end_available_at=format_utc_z(n1.available_at),
                max_input_available_at=format_utc_z(attr_avail),
                computed_at=format_utc_z(attr_avail),
                attribution_available_at=format_utc_z(attr_avail),
                replay_epoch=n1.replay_epoch,
                book_sequence_start=n0.sequence,
                book_sequence_end=n1.sequence,
                book_update_id_start=n0.update_id,
                book_update_id_end=n1.update_id,
                queue_before=mb.queue_before,
                queue_after=mb.queue_after,
                delta_queue=mb.delta_queue,
                attributed_trade_count=len(trade_ids),
                attributed_trade_ids=trade_ids,
                attributed_trade_ids_hash=trades_hash(trade_ids),
                attributed_hit_qty=mb.attributed_hit_qty,
                attributed_hit_notional=x_notional,
                net_refill_qty=mb.net_refill_qty,
                residual_pull_qty=mb.residual_pull_qty,
                net_depletion_qty=mb.net_depletion_qty,
                aggregate_queue_survival_proxy=mb.aggregate_queue_survival_proxy,
                aggressor_side="Buy",
                attribution_rule_version=ATTRIBUTION_RULE_VERSION,
                attribution_confidence=conf,
                mass_balance_error=mb.mass_balance_error,
                coverage_ok=True,
                look_ahead=bool(look_ahead),
                source_record_ids=[n0.source_record_id, n1.source_record_id]
                + [t.source_record_id for t in attributed],
            )
        )

    stats["consumed_trade_ids"] = len(consumed)
    return events, stats


def wall_flow_event_to_row(ev: WallFlowEvent, *, persist_full_ids: bool = False) -> dict[str, Any]:
    row = asdict(ev)
    if not persist_full_ids and len(row["attributed_trade_ids"]) > 32:
        row["attributed_trade_ids_full_ref"] = row["attributed_trade_ids_hash"]
        row["attributed_trade_ids"] = row["attributed_trade_ids"][:8] + ["…truncated…"]
    return row

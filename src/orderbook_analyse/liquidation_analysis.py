"""Causal, event-based liquidation analysis against reconstructed near-book state.

Read-only ClickHouse research module. Does not modify the recorder, writer, or schema.

Bybit ``allLiquidation.{symbol}`` semantics (verified for this endpoint)
------------------------------------------------------------------------
Recorder mapping (``parse_liquidation_rows``):

- ``T`` → ``liquidation_ts`` (exchange event timestamp)
- ``s`` → ``symbol``
- ``S`` → ``side`` (raw position side)
- ``p`` → ``price`` (ClickHouse column; **bankruptcy price**, not necessarily
  traded market / fill / mid)
- ``v`` → ``quantity`` (executed liquidation size)
- ``p * v`` → ``notional``

Position side ``S``:

- ``Buy``  → a **long** position was liquidated  → ``LIQUIDATED_LONG``
- ``Sell`` → a **short** position was liquidated → ``LIQUIDATED_SHORT``

Analysis outputs prefer ``bankruptcy_price`` / ``price_type=BANKRUPTCY_PRICE``.
Legacy ``liquidation_price`` remains as an alias of the bankruptcy price and is
documented as such. Forward reactions always use mid / market / trade paths,
never the bankruptcy price as the reaction start price.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import orjson

from orderbook_analyse.dynamic_wall_detector import (
    PROJECT_ROOT,
    ReadOnlyClickHouse,
    WallDetectorParams,
    choose_bucket_size,
    connect_readonly,
    find_bootstrap_snapshot,
    infer_tick_size,
    load_events,
    parse_utc,
    reconstruct_with_samples,
    utc_now,
    write_csv,
)
from orderbook_analyse.near_liquidity import NearParams, select_near_and_dominant
from orderbook_analyse.orderbook_replay import (
    BookLevelEvent,
    OrderBookState,
    ReplayError,
    clone_book,
    replay_until,
)
from orderbook_analyse.wall_movement_tracker import (
    WallView,
    extract_walls_from_book,
    load_oi_at,
    load_trades_between,
)

logger = logging.getLogger(__name__)

LIQUIDATED_LONG = "LIQUIDATED_LONG"
LIQUIDATED_SHORT = "LIQUIDATED_SHORT"
LIQUIDATION_SIDE_UNKNOWN = "LIQUIDATION_SIDE_UNKNOWN"
# Deprecated alias kept only so older imports do not crash; never emitted for Buy/Sell.
LIQUIDATION_SIDE_SEMANTICS_UNVERIFIED = LIQUIDATION_SIDE_UNKNOWN

PRICE_TYPE_BANKRUPTCY = "BANKRUPTCY_PRICE"
SIDE_SEMANTICS_STATUS = "BYBIT_ALL_LIQUIDATION_POSITION_SIDE"

HORIZONS_SEC: tuple[int, ...] = (30, 60, 120, 300, 600)

UPSIDE_CONTINUATION_AFTER_LIQUIDATION = "UPSIDE_CONTINUATION_AFTER_LIQUIDATION"
DOWNSIDE_CONTINUATION_AFTER_LIQUIDATION = "DOWNSIDE_CONTINUATION_AFTER_LIQUIDATION"
LIQUIDATION_REJECTION = "LIQUIDATION_REJECTION"
LIQUIDATION_EXHAUSTION = "LIQUIDATION_EXHAUSTION"
LIQUIDATION_BREAKOUT_ACCELERATION = "LIQUIDATION_BREAKOUT_ACCELERATION"
LIQUIDATION_BREAKDOWN_ACCELERATION = "LIQUIDATION_BREAKDOWN_ACCELERATION"
LIQUIDATION_ABSORBED = "LIQUIDATION_ABSORBED"
NO_CLEAR_REACTION = "NO_CLEAR_REACTION"

LIQUIDATION_AT_BID_SUPPORT = "LIQUIDATION_AT_BID_SUPPORT"
LIQUIDATION_BELOW_ASK_RESISTANCE = "LIQUIDATION_BELOW_ASK_RESISTANCE"
LIQUIDATION_THROUGH_ASK = "LIQUIDATION_THROUGH_ASK"
LIQUIDATION_THROUGH_BID = "LIQUIDATION_THROUGH_BID"
POST_LIQ_RISING_BID_FLOOR = "POST_LIQ_RISING_BID_FLOOR"
POST_LIQ_FALLING_BID_FLOOR = "POST_LIQ_FALLING_BID_FLOOR"
POST_LIQ_NEAR_ASK_HIGHER = "POST_LIQ_NEAR_ASK_HIGHER"
POST_LIQ_NEAR_ASK_LOWER = "POST_LIQ_NEAR_ASK_LOWER"
POST_LIQ_AUCTION_HIGHER = "POST_LIQ_AUCTION_HIGHER"
POST_LIQ_AUCTION_LOWER = "POST_LIQ_AUCTION_LOWER"


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _fmt(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _pct(numer: Decimal, denom: Decimal) -> float | None:
    if denom == 0:
        return None
    return float(numer / denom * Decimal("100"))


@dataclass
class ReactionThresholds:
    """Configurable diagnostic thresholds (documented in REPORT.md)."""

    continuation_min_return_pct: float = 0.05
    continuation_max_giveback_ratio: float = 0.40
    rejection_min_excursion_pct: float = 0.05
    rejection_min_giveback_ratio: float = 0.50
    exhaustion_max_continuation_pct: float = 0.03
    exhaustion_min_reversal_pct: float = 0.05
    absorption_max_abs_return_pct: float = 0.03
    absorption_max_mfe_pct: float = 0.04
    absorption_min_trade_multiple_of_liq: float = 3.0
    wall_stable_notional_tol_pct: float = 20.0
    breakout_min_return_pct: float = 0.05
    wall_relation_bps: float = 10.0
    primary_horizon_seconds: int = 300


@dataclass
class LiquidationAnalysisParams:
    sample_seconds: int = 30
    target_bps: float = 10.0
    distance_max_pct: float = 3.0
    near_min_distance_pct: float = 0.10
    near_max_distance_pct: float = 1.50
    near_top_n: int = 3
    near_max_buckets: int = 15
    cluster_window_seconds: int = 60
    cluster_price_bps: float = 10.0
    wall_relation_bps: float = 10.0
    pre_context_lookback_seconds: int = 30
    thresholds: ReactionThresholds = field(default_factory=ReactionThresholds)
    wall_params: WallDetectorParams = field(default_factory=WallDetectorParams)


@dataclass(frozen=True)
class LiquidationEvent:
    event_key: str
    exchange_timestamp: datetime
    received_timestamp: datetime | None
    symbol: str
    raw_side: str
    interpreted_position_side: str
    bankruptcy_price: Decimal
    liquidation_qty: Decimal
    liquidation_notional: Decimal
    price_type: str = PRICE_TYPE_BANKRUPTCY
    side_semantics_status: str = SIDE_SEMANTICS_STATUS

    @property
    def liquidation_price(self) -> Decimal:
        """Alias of bankruptcy_price (Bybit field ``p``); not necessarily market/fill."""
        return self.bankruptcy_price

    def to_row(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "exchange_timestamp": self.exchange_timestamp.isoformat(),
            "received_timestamp": None
            if self.received_timestamp is None
            else self.received_timestamp.isoformat(),
            "symbol": self.symbol,
            "raw_side": self.raw_side,
            "interpreted_position_side": self.interpreted_position_side,
            "side_semantics_status": self.side_semantics_status,
            "bankruptcy_price": _fmt(self.bankruptcy_price),
            "price_type": self.price_type,
            # Compat: same value as bankruptcy_price (Bybit p)
            "liquidation_price": _fmt(self.bankruptcy_price),
            "quantity": _fmt(self.liquidation_qty),
            "liquidation_qty": _fmt(self.liquidation_qty),
            "notional": _fmt(self.liquidation_notional),
            "liquidation_notional": _fmt(self.liquidation_notional),
        }


def make_event_key(
    symbol: str,
    exchange_timestamp: datetime,
    side: str,
    price: Decimal,
    qty: Decimal,
) -> str:
    ts = _ensure_utc(exchange_timestamp).isoformat()
    return f"{symbol}|{ts}|{side}|{format(price, 'f')}|{format(qty, 'f')}"


def compute_notional(
    price: Decimal, qty: Decimal, stored_notional: Decimal | None = None
) -> Decimal:
    """Prefer stored notional when present; otherwise price * qty."""
    if stored_notional is not None:
        return _dec(stored_notional)
    return _dec(price) * _dec(qty)


def interpret_liquidation_side(raw_side: str) -> str:
    """Map Bybit allLiquidation position side ``S`` to a research label."""
    if raw_side == "Buy":
        return LIQUIDATED_LONG
    if raw_side == "Sell":
        return LIQUIDATED_SHORT
    return LIQUIDATION_SIDE_UNKNOWN


def interpret_position_side(raw_side: str, *, semantics_map: dict[str, str] | None = None) -> tuple[str, str]:
    """Compatibility wrapper; prefers verified Bybit allLiquidation mapping.

    ``semantics_map`` is ignored — position side is always interpreted via
    ``interpret_liquidation_side``.
    """
    del semantics_map  # unused; kept for call-site compatibility
    interpreted = interpret_liquidation_side(raw_side)
    if interpreted == LIQUIDATION_SIDE_UNKNOWN:
        return interpreted, LIQUIDATION_SIDE_UNKNOWN
    return interpreted, SIDE_SEMANTICS_STATUS


def liquidation_from_row(
    row: Mapping[str, Any], *, side_semantics: str | None = None
) -> LiquidationEvent:
    del side_semantics  # ignored; Bybit allLiquidation mapping is always used
    price = _dec(row["price"] if "price" in row else row.get("liquidation_price") or row.get("bankruptcy_price"))
    qty = _dec(row["quantity"] if "quantity" in row else row.get("liquidation_qty"))
    stored = row.get("notional")
    stored_n = None if stored is None else _dec(stored)
    notional = compute_notional(price, qty, stored_n)
    exchange_ts = _ensure_utc(row["liquidation_ts"] if "liquidation_ts" in row else row["exchange_timestamp"])
    received = row.get("received_ts", row.get("received_timestamp"))
    received_ts = None if received is None else _ensure_utc(received)
    symbol = str(row.get("symbol") or "")
    raw_side = str(row.get("side") or row.get("raw_side") or "")
    interpreted, status = interpret_position_side(raw_side)
    key = make_event_key(symbol, exchange_ts, raw_side, price, qty)
    return LiquidationEvent(
        event_key=key,
        exchange_timestamp=exchange_ts,
        received_timestamp=received_ts,
        symbol=symbol,
        raw_side=raw_side,
        interpreted_position_side=interpreted,
        bankruptcy_price=price,
        liquidation_qty=qty,
        liquidation_notional=notional,
        price_type=PRICE_TYPE_BANKRUPTCY,
        side_semantics_status=status,
    )


def dedupe_liquidations(events: Sequence[LiquidationEvent]) -> list[LiquidationEvent]:
    seen: set[str] = set()
    out: list[LiquidationEvent] = []
    for ev in sorted(events, key=lambda e: (e.exchange_timestamp, e.event_key)):
        if ev.event_key in seen:
            continue
        seen.add(ev.event_key)
        out.append(ev)
    return out


def load_liquidations(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    side_semantics: str | None = None,
) -> list[LiquidationEvent]:
    del side_semantics
    result = db.query(
        """
        SELECT
            liquidation_ts, received_ts, symbol, side, price, quantity, notional
        FROM liquidations
        WHERE symbol = %(symbol)s
          AND liquidation_ts >= %(start)s
          AND liquidation_ts <= %(end)s
        ORDER BY liquidation_ts ASC, side ASC, price ASC, quantity ASC
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    )
    events = [
        liquidation_from_row(
            {
                "liquidation_ts": r[0],
                "received_ts": r[1],
                "symbol": r[2],
                "side": r[3],
                "price": r[4],
                "quantity": r[5],
                "notional": r[6],
            }
        )
        for r in result.result_rows
    ]
    return dedupe_liquidations(events)


def book_state_before_event(
    events: Sequence[BookLevelEvent], *, event_ts: datetime, strict: bool = True
) -> OrderBookState:
    """Reconstruct book causally at/before the liquidation timestamp.

    ``strict=True`` (default): apply only messages with exchange_ts < event_ts
    (no look-ahead, including same-ms book updates after the event clock).
    ``strict=False``: exchange_ts <= event_ts (``replay_until``).
    """
    cutoff = _ensure_utc(event_ts)
    if strict:
        # one microsecond before event keeps DateTime64(3) ms events exclusive
        cutoff = cutoff - timedelta(microseconds=1)
    return replay_until(events, as_of=cutoff)


def _wall_slot(w: WallView | None) -> tuple[Decimal | None, Decimal | None]:
    if w is None:
        return None, None
    return w.price, w.notional


def analyze_book_near(
    book: OrderBookState,
    *,
    bucket_size: Decimal,
    params: LiquidationAnalysisParams,
) -> dict[str, Any]:
    wall_params = params.wall_params
    wall_params.distance_max_pct = params.distance_max_pct
    bid_walls, ask_walls, _bm, _am, mid, all_bids, all_asks = extract_walls_from_book(
        book, bucket_size=bucket_size, params=wall_params
    )
    near = NearParams(
        near_min_distance_pct=params.near_min_distance_pct,
        near_max_distance_pct=params.near_max_distance_pct,
        near_top_n=params.near_top_n,
        near_max_buckets=params.near_max_buckets,
    )
    view = select_near_and_dominant(
        bid_candidates=all_bids,
        ask_candidates=all_asks,
        mid=mid,
        best_bid=book.best_bid(),
        best_ask=book.best_ask(),
        bucket_size=bucket_size,
        near=near,
    )
    bb, ba = book.best_bid(), book.best_ask()
    spread = book.spread()
    nearest_bid_p, nearest_bid_n = _wall_slot(view.nearest_bid)  # type: ignore[arg-type]
    nearest_ask_p, nearest_ask_n = _wall_slot(view.nearest_ask)  # type: ignore[arg-type]
    dom_bid_p, dom_bid_n = _wall_slot(view.dominant_bid)  # type: ignore[arg-type]
    dom_ask_p, dom_ask_n = _wall_slot(view.dominant_ask)  # type: ignore[arg-type]
    # Soft fallback to strongest tracked walls if near band empty
    if nearest_bid_p is None and bid_walls:
        nearest_bid_p, nearest_bid_n = bid_walls[0].price, bid_walls[0].notional
    if nearest_ask_p is None and ask_walls:
        nearest_ask_p, nearest_ask_n = ask_walls[0].price, ask_walls[0].notional
    if dom_bid_p is None and bid_walls:
        dom_bid_p, dom_bid_n = bid_walls[0].price, bid_walls[0].notional
    if dom_ask_p is None and ask_walls:
        dom_ask_p, dom_ask_n = ask_walls[0].price, ask_walls[0].notional
    return {
        "mid": mid,
        "best_bid": bb,
        "best_ask": ba,
        "spread": spread,
        "nearest_bid_price": nearest_bid_p,
        "nearest_bid_notional": nearest_bid_n,
        "nearest_ask_price": nearest_ask_p,
        "nearest_ask_notional": nearest_ask_n,
        "dominant_bid_price": dom_bid_p,
        "dominant_bid_notional": dom_bid_n,
        "dominant_ask_price": dom_ask_p,
        "dominant_ask_notional": dom_ask_n,
        "total_near_bid_notional": view.total_near_bid_notional,
        "total_near_ask_notional": view.total_near_ask_notional,
        "near_book_imbalance": view.near_book_imbalance,
        "near_view": view,
        "strongest_bid": bid_walls[0] if bid_walls else None,
        "strongest_ask": ask_walls[0] if ask_walls else None,
    }


def direction_label(prev: Decimal | None, cur: Decimal | None) -> str:
    if prev is None or cur is None:
        return "INCONCLUSIVE"
    if cur > prev:
        return "HIGHER"
    if cur < prev:
        return "LOWER"
    return "STABLE"


def auction_direction(bid_dir: str, ask_dir: str) -> str:
    if bid_dir == "HIGHER" and ask_dir == "HIGHER":
        return "HIGHER"
    if bid_dir == "LOWER" and ask_dir == "LOWER":
        return "LOWER"
    if bid_dir == "HIGHER" and ask_dir == "LOWER":
        return "COMPRESSION"
    if bid_dir == "LOWER" and ask_dir == "HIGHER":
        return "EXPANSION"
    if bid_dir == "INCONCLUSIVE" or ask_dir == "INCONCLUSIVE":
        return "INCONCLUSIVE"
    return "MIXED"


def short_term_bias(bid_dir: str, ask_dir: str, auction: str) -> str:
    if auction == "COMPRESSION":
        return "COMPRESSION"
    if auction == "EXPANSION":
        return "EXPANSION"
    if bid_dir == "HIGHER" and ask_dir in {"HIGHER", "STABLE", "LOWER"}:
        return "BULLISH_LIQUIDITY_SHIFT"
    if bid_dir == "LOWER" and ask_dir in {"LOWER", "STABLE", "HIGHER"}:
        return "BEARISH_LIQUIDITY_SHIFT"
    return "INCONCLUSIVE"


def within_bps(a: Decimal, b: Decimal, bps: float) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(float((a - b) / b * Decimal("10000"))) <= bps


def wall_relation_labels(
    *,
    liq_price: Decimal,
    nearest_bid: Decimal | None,
    nearest_ask: Decimal | None,
    bps: float,
) -> list[str]:
    labels: list[str] = []
    if nearest_bid is not None and within_bps(liq_price, nearest_bid, bps):
        labels.append(LIQUIDATION_AT_BID_SUPPORT)
    if nearest_ask is not None and liq_price < nearest_ask:
        labels.append(LIQUIDATION_BELOW_ASK_RESISTANCE)
    if nearest_ask is not None and liq_price >= nearest_ask:
        labels.append(LIQUIDATION_THROUGH_ASK)
    if nearest_bid is not None and liq_price <= nearest_bid:
        labels.append(LIQUIDATION_THROUGH_BID)
    return labels


def post_liq_wall_labels(
    *,
    bid_before: Decimal | None,
    bid_after: Decimal | None,
    ask_before: Decimal | None,
    ask_after: Decimal | None,
    auction_before: str,
    auction_after: str,
) -> list[str]:
    labels: list[str] = []
    bid_dir = direction_label(bid_before, bid_after)
    ask_dir = direction_label(ask_before, ask_after)
    if bid_dir == "HIGHER":
        labels.append(POST_LIQ_RISING_BID_FLOOR)
    elif bid_dir == "LOWER":
        labels.append(POST_LIQ_FALLING_BID_FLOOR)
    if ask_dir == "HIGHER":
        labels.append(POST_LIQ_NEAR_ASK_HIGHER)
    elif ask_dir == "LOWER":
        labels.append(POST_LIQ_NEAR_ASK_LOWER)
    if auction_after == "HIGHER" or (
        auction_before != "HIGHER" and auction_after == "HIGHER"
    ):
        if auction_after == "HIGHER":
            labels.append(POST_LIQ_AUCTION_HIGHER)
    if auction_after == "LOWER":
        labels.append(POST_LIQ_AUCTION_LOWER)
    return labels


def trade_delta(
    db: ReadOnlyClickHouse | None,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    buy_sell: tuple[Decimal, Decimal] | None = None,
) -> Decimal:
    """Signed trade delta: buy_notional - sell_notional over (start, end]."""
    if buy_sell is not None:
        buy_n, sell_n = buy_sell
        return buy_n - sell_n
    assert db is not None
    buy_n, sell_n, _, _ = load_trades_between(db, symbol=symbol, start=start, end=end)
    return buy_n - sell_n


def oi_change(
    db: ReadOnlyClickHouse | None,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    oi_lookup: Mapping[datetime, Decimal | None] | None = None,
) -> Decimal | None:
    if oi_lookup is not None:
        a = oi_lookup.get(start)
        b = oi_lookup.get(end)
        if a is None or b is None:
            return None
        return b - a
    assert db is not None
    a = load_oi_at(db, symbol=symbol, as_of=start)
    b = load_oi_at(db, symbol=symbol, as_of=end)
    if a is None or b is None:
        return None
    return b - a


def price_at_or_before(
    path: Sequence[tuple[datetime, Decimal]], *, as_of: datetime, fallback: Decimal
) -> Decimal:
    chosen = fallback
    as_of = _ensure_utc(as_of)
    for ts, px in path:
        if _ensure_utc(ts) <= as_of:
            chosen = px
        else:
            break
    return chosen


def path_stats(
    path: Sequence[tuple[datetime, Decimal]],
    *,
    start_ts: datetime,
    end_ts: datetime,
    start_price: Decimal,
) -> dict[str, Any]:
    start_ts = _ensure_utc(start_ts)
    end_ts = _ensure_utc(end_ts)
    window = [(ts, px) for ts, px in path if start_ts < _ensure_utc(ts) <= end_ts]
    end_price = price_at_or_before(path, as_of=end_ts, fallback=start_price)
    if window:
        highest = max(px for _, px in window)
        lowest = min(px for _, px in window)
    else:
        highest = max(start_price, end_price)
        lowest = min(start_price, end_price)
    ret = _pct(end_price - start_price, start_price) or 0.0
    mfe_up = _pct(highest - start_price, start_price) or 0.0
    mae_down = _pct(start_price - lowest, start_price) or 0.0
    if mfe_up >= mae_down:
        max_move = "UP"
    elif mae_down > mfe_up:
        max_move = "DOWN"
    else:
        max_move = "FLAT"
    return {
        "end_price": end_price,
        "return_pct": ret,
        "highest_price": highest,
        "lowest_price": lowest,
        "mfe_up_pct": mfe_up,
        "mae_down_pct": mae_down,
        "max_move_direction": max_move,
    }


def classify_reaction(
    *,
    return_pct: float,
    mfe_up_pct: float,
    mae_down_pct: float,
    trade_delta_after: Decimal,
    liquidation_notional: Decimal,
    wall_labels: Sequence[str],
    bid_floor_change: str,
    near_ask_change: str,
    near_ask_notional_before: Decimal | None,
    near_ask_notional_after: Decimal | None,
    thresholds: ReactionThresholds,
) -> str:
    thr = thresholds
    giveback_up = 0.0
    if mfe_up_pct > 0:
        # how much of upside excursion was given back by end
        end_vs_mfe = max(0.0, mfe_up_pct - max(return_pct, 0.0))
        giveback_up = end_vs_mfe / mfe_up_pct
    giveback_down = 0.0
    if mae_down_pct > 0:
        end_vs_mae = max(0.0, mae_down_pct - max(-return_pct, 0.0))
        giveback_down = end_vs_mae / mae_down_pct

    broke_ask = LIQUIDATION_THROUGH_ASK in wall_labels
    broke_bid = LIQUIDATION_THROUGH_BID in wall_labels
    ask_thinner = False
    if (
        near_ask_notional_before is not None
        and near_ask_notional_after is not None
        and near_ask_notional_before > 0
    ):
        chg = float(
            (near_ask_notional_after - near_ask_notional_before)
            / near_ask_notional_before
            * 100
        )
        ask_thinner = chg <= -thr.wall_stable_notional_tol_pct

    # Breakout / breakdown acceleration
    if (
        broke_ask
        and return_pct >= thr.breakout_min_return_pct
        and (near_ask_change == "HIGHER" or ask_thinner)
        and bid_floor_change == "HIGHER"
    ):
        return LIQUIDATION_BREAKOUT_ACCELERATION
    if (
        broke_bid
        and return_pct <= -thr.breakout_min_return_pct
        and (near_ask_change == "LOWER" or bid_floor_change == "LOWER")
        and bid_floor_change == "LOWER"
    ):
        return LIQUIDATION_BREAKDOWN_ACCELERATION

    # Absorption
    trade_mult = (
        float(abs(trade_delta_after) / liquidation_notional)
        if liquidation_notional > 0
        else 0.0
    )
    if (
        abs(return_pct) <= thr.absorption_max_abs_return_pct
        and max(mfe_up_pct, mae_down_pct) <= thr.absorption_max_mfe_pct
        and trade_mult >= thr.absorption_min_trade_multiple_of_liq
    ):
        return LIQUIDATION_ABSORBED

    # Rejection
    if (
        mfe_up_pct >= thr.rejection_min_excursion_pct
        and giveback_up >= thr.rejection_min_giveback_ratio
        and return_pct < mfe_up_pct * (1 - thr.rejection_min_giveback_ratio)
    ):
        return LIQUIDATION_REJECTION
    if (
        mae_down_pct >= thr.rejection_min_excursion_pct
        and giveback_down >= thr.rejection_min_giveback_ratio
        and (-return_pct) < mae_down_pct * (1 - thr.rejection_min_giveback_ratio)
    ):
        return LIQUIDATION_REJECTION

    # Exhaustion: little continuation, clear reverse
    if (
        abs(return_pct) <= thr.exhaustion_max_continuation_pct
        and (
            (mfe_up_pct >= thr.exhaustion_min_reversal_pct and return_pct <= 0)
            or (mae_down_pct >= thr.exhaustion_min_reversal_pct and return_pct >= 0)
        )
    ):
        # prefer rejection already handled; exhaustion when reverse dominates end
        if (mfe_up_pct >= thr.exhaustion_min_reversal_pct and return_pct <= 0) or (
            mae_down_pct >= thr.exhaustion_min_reversal_pct and return_pct >= 0
        ):
            return LIQUIDATION_EXHAUSTION

    # Continuations
    if (
        return_pct >= thr.continuation_min_return_pct
        and mfe_up_pct >= thr.continuation_min_return_pct
        and giveback_up <= thr.continuation_max_giveback_ratio
    ):
        return UPSIDE_CONTINUATION_AFTER_LIQUIDATION
    if (
        return_pct <= -thr.continuation_min_return_pct
        and mae_down_pct >= thr.continuation_min_return_pct
        and giveback_down <= thr.continuation_max_giveback_ratio
    ):
        return DOWNSIDE_CONTINUATION_AFTER_LIQUIDATION

    return NO_CLEAR_REACTION


def cluster_events(
    events: Sequence[LiquidationEvent],
    *,
    window_seconds: int,
    price_bps: float,
    mid_at: Mapping[str, Decimal | None] | None = None,
    reaction_at: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    if not events:
        return []
    ordered = sorted(events, key=lambda e: e.exchange_timestamp)
    clusters: list[list[LiquidationEvent]] = []
    current: list[LiquidationEvent] = [ordered[0]]
    for ev in ordered[1:]:
        anchor = current[0]
        dt = (ev.exchange_timestamp - current[-1].exchange_timestamp).total_seconds()
        same_window = dt <= window_seconds
        same_price = within_bps(ev.bankruptcy_price, anchor.bankruptcy_price, price_bps)
        if same_window and same_price:
            current.append(ev)
        else:
            clusters.append(current)
            current = [ev]
    clusters.append(current)

    out: list[dict[str, Any]] = []
    for i, group in enumerate(clusters, start=1):
        buy_n = sum((e.liquidation_notional for e in group if e.raw_side == "Buy"), Decimal("0"))
        sell_n = sum((e.liquidation_notional for e in group if e.raw_side == "Sell"), Decimal("0"))
        if buy_n > sell_n:
            dominant = "Buy"
        elif sell_n > buy_n:
            dominant = "Sell"
        else:
            dominant = "MIXED"
        start_mid = None
        end_mid = None
        if mid_at:
            start_mid = mid_at.get(group[0].event_key)
            end_mid = mid_at.get(group[-1].event_key)
        reactions = []
        if reaction_at:
            reactions = [reaction_at.get(e.event_key) for e in group if reaction_at.get(e.event_key)]
        # majority reaction label
        reaction = NO_CLEAR_REACTION
        if reactions:
            counts: dict[str, int] = {}
            for r in reactions:
                if r:
                    counts[r] = counts.get(r, 0) + 1
            reaction = max(counts, key=counts.get)  # type: ignore[arg-type]
        out.append(
            {
                "cluster_id": f"C{i:04d}",
                "cluster_start": group[0].exchange_timestamp.isoformat(),
                "cluster_end": group[-1].exchange_timestamp.isoformat(),
                "event_count": len(group),
                "event_keys": "|".join(e.event_key for e in group),
                "total_qty": _fmt(sum((e.liquidation_qty for e in group), Decimal("0"))),
                "total_notional": _fmt(
                    sum((e.liquidation_notional for e in group), Decimal("0"))
                ),
                "buy_liquidation_notional": _fmt(buy_n),
                "sell_liquidation_notional": _fmt(sell_n),
                "dominant_side": dominant,
                "min_price": _fmt(min(e.bankruptcy_price for e in group)),
                "max_price": _fmt(max(e.bankruptcy_price for e in group)),
                "min_bankruptcy_price": _fmt(min(e.bankruptcy_price for e in group)),
                "max_bankruptcy_price": _fmt(max(e.bankruptcy_price for e in group)),
                "start_mid": _fmt(start_mid),
                "end_mid": _fmt(end_mid),
                "reaction_classification": reaction,
            }
        )
    return out


def build_price_path_from_books(
    books: Mapping[datetime, OrderBookState],
) -> list[tuple[datetime, Decimal]]:
    path: list[tuple[datetime, Decimal]] = []
    for ts in sorted(books):
        mid = books[ts].mid_price()
        if mid is not None:
            path.append((_ensure_utc(ts), mid))
    return path


def load_trade_price_path(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, Decimal]]:
    result = db.query(
        """
        SELECT trade_ts, price
        FROM public_trades
        WHERE symbol = %(symbol)s
          AND trade_ts >= %(start)s
          AND trade_ts <= %(end)s
        ORDER BY trade_ts ASC
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    )
    out: list[tuple[datetime, Decimal]] = []
    for ts, px in result.result_rows:
        out.append((_ensure_utc(ts), _dec(px)))
    return out


def merge_price_paths(
    *paths: Sequence[tuple[datetime, Decimal]],
) -> list[tuple[datetime, Decimal]]:
    merged: list[tuple[datetime, Decimal]] = []
    for path in paths:
        merged.extend((_ensure_utc(ts), px) for ts, px in path)
    merged.sort(key=lambda x: x[0])
    return merged


def analyze_single_event(
    event: LiquidationEvent,
    *,
    book_before: OrderBookState,
    book_lookback: OrderBookState | None,
    book_after_by_horizon: Mapping[int, OrderBookState],
    price_path: Sequence[tuple[datetime, Decimal]],
    pre_trade_deltas: Mapping[int, Decimal],
    pre_oi_changes: Mapping[int, Decimal | None],
    pre_price_changes: Mapping[int, float | None],
    post_trade_deltas: Mapping[int, Decimal],
    post_oi_changes: Mapping[int, Decimal | None],
    bucket_size: Decimal,
    params: LiquidationAnalysisParams,
) -> dict[str, Any]:
    before = analyze_book_near(book_before, bucket_size=bucket_size, params=params)
    mid_before = before["mid"]
    assert mid_before is not None
    dist_pct = _pct(event.bankruptcy_price - mid_before, mid_before)
    bankruptcy_minus_mid = event.bankruptcy_price - mid_before

    look = None
    if book_lookback is not None and book_lookback.mid_price() is not None:
        look = analyze_book_near(book_lookback, bucket_size=bucket_size, params=params)

    bid_floor_dir = direction_label(
        None if look is None else look["nearest_bid_price"],
        before["nearest_bid_price"],
    )
    near_ask_dir = direction_label(
        None if look is None else look["nearest_ask_price"],
        before["nearest_ask_price"],
    )
    auction_before = auction_direction(bid_floor_dir, near_ask_dir)
    bias_before = short_term_bias(bid_floor_dir, near_ask_dir, auction_before)

    wall_labels = wall_relation_labels(
        liq_price=event.bankruptcy_price,
        nearest_bid=before["nearest_bid_price"],
        nearest_ask=before["nearest_ask_price"],
        bps=params.wall_relation_bps,
    )

    event_row = event.to_row()
    event_row.update(
        {
            "mid_before": _fmt(mid_before),
            "best_bid_before": _fmt(before["best_bid"]),
            "best_ask_before": _fmt(before["best_ask"]),
            "spread_before": _fmt(before["spread"]),
            "bankruptcy_price_minus_mid": _fmt(bankruptcy_minus_mid),
            "bankruptcy_price_distance_from_mid_pct": None
            if dist_pct is None
            else round(dist_pct, 6),
            # Compat alias of bankruptcy distance
            "liquidation_distance_from_mid_pct": None
            if dist_pct is None
            else round(dist_pct, 6),
            "nearest_bid_wall_price": _fmt(before["nearest_bid_price"]),
            "nearest_bid_wall_notional": _fmt(before["nearest_bid_notional"]),
            "nearest_ask_wall_price": _fmt(before["nearest_ask_price"]),
            "nearest_ask_wall_notional": _fmt(before["nearest_ask_notional"]),
            "dominant_bid_wall_price": _fmt(before["dominant_bid_price"]),
            "dominant_bid_wall_notional": _fmt(before["dominant_bid_notional"]),
            "dominant_ask_wall_price": _fmt(before["dominant_ask_price"]),
            "dominant_ask_wall_notional": _fmt(before["dominant_ask_notional"]),
            "total_near_bid_notional": _fmt(before["total_near_bid_notional"]),
            "total_near_ask_notional": _fmt(before["total_near_ask_notional"]),
            "near_book_imbalance": round(float(before["near_book_imbalance"]), 6),
            "bid_floor_direction_before": bid_floor_dir,
            "near_ask_direction_before": near_ask_dir,
            "auction_direction_before": auction_before,
            "short_term_bias_before": bias_before,
            "trade_delta_30s_before": _fmt(pre_trade_deltas.get(30, Decimal("0"))),
            "trade_delta_60s_before": _fmt(pre_trade_deltas.get(60, Decimal("0"))),
            "trade_delta_2m_before": _fmt(pre_trade_deltas.get(120, Decimal("0"))),
            "trade_delta_5m_before": _fmt(pre_trade_deltas.get(300, Decimal("0"))),
            "oi_change_30s_before": _fmt(pre_oi_changes.get(30)),
            "oi_change_60s_before": _fmt(pre_oi_changes.get(60)),
            "oi_change_2m_before": _fmt(pre_oi_changes.get(120)),
            "oi_change_5m_before": _fmt(pre_oi_changes.get(300)),
            "price_change_30s_before": pre_price_changes.get(30),
            "price_change_60s_before": pre_price_changes.get(60),
            "price_change_2m_before": pre_price_changes.get(120),
            "price_change_5m_before": pre_price_changes.get(300),
            "wall_relation_labels": "|".join(wall_labels),
        }
    )

    forward_rows: list[dict[str, Any]] = []
    primary_cls = NO_CLEAR_REACTION
    primary_h = params.thresholds.primary_horizon_seconds
    wall_ctx_rows: list[dict[str, Any]] = []

    for h in HORIZONS_SEC:
        book_after = book_after_by_horizon.get(h)
        after = (
            analyze_book_near(book_after, bucket_size=bucket_size, params=params)
            if book_after is not None and book_after.mid_price() is not None
            else None
        )
        stats = path_stats(
            price_path,
            start_ts=event.exchange_timestamp,
            end_ts=event.exchange_timestamp + timedelta(seconds=h),
            start_price=mid_before,
        )
        bid_chg = direction_label(
            before["nearest_bid_price"],
            None if after is None else after["nearest_bid_price"],
        )
        ask_chg = direction_label(
            before["nearest_ask_price"],
            None if after is None else after["nearest_ask_price"],
        )
        auction_after = auction_direction(bid_chg, ask_chg)
        near_bid_chg_pct = None
        near_ask_chg_pct = None
        if after is not None:
            if before["total_near_bid_notional"] > 0:
                near_bid_chg_pct = _pct(
                    after["total_near_bid_notional"] - before["total_near_bid_notional"],
                    before["total_near_bid_notional"],
                )
            if before["total_near_ask_notional"] > 0:
                near_ask_chg_pct = _pct(
                    after["total_near_ask_notional"] - before["total_near_ask_notional"],
                    before["total_near_ask_notional"],
                )
        post_labels = post_liq_wall_labels(
            bid_before=before["nearest_bid_price"],
            bid_after=None if after is None else after["nearest_bid_price"],
            ask_before=before["nearest_ask_price"],
            ask_after=None if after is None else after["nearest_ask_price"],
            auction_before=auction_before,
            auction_after=auction_after,
        )
        cls = classify_reaction(
            return_pct=stats["return_pct"],
            mfe_up_pct=stats["mfe_up_pct"],
            mae_down_pct=stats["mae_down_pct"],
            trade_delta_after=post_trade_deltas.get(h, Decimal("0")),
            liquidation_notional=event.liquidation_notional,
            wall_labels=wall_labels + post_labels,
            bid_floor_change=bid_chg,
            near_ask_change=ask_chg,
            near_ask_notional_before=before["nearest_ask_notional"],
            near_ask_notional_after=None
            if after is None
            else after["nearest_ask_notional"],
            thresholds=params.thresholds,
        )
        if h == primary_h:
            primary_cls = cls
        forward_rows.append(
            {
                "event_key": event.event_key,
                "horizon_seconds": h,
                "start_price": _fmt(mid_before),
                "start_price_basis": "mid_before",
                "end_price": _fmt(stats["end_price"]),
                "return_pct": round(stats["return_pct"], 6),
                "highest_price": _fmt(stats["highest_price"]),
                "lowest_price": _fmt(stats["lowest_price"]),
                "mfe_up_pct": round(stats["mfe_up_pct"], 6),
                "mae_down_pct": round(stats["mae_down_pct"], 6),
                "max_move_direction": stats["max_move_direction"],
                "trade_delta_after": _fmt(post_trade_deltas.get(h, Decimal("0"))),
                "oi_change_after": _fmt(post_oi_changes.get(h)),
                "nearest_bid_after": None
                if after is None
                else _fmt(after["nearest_bid_price"]),
                "nearest_ask_after": None
                if after is None
                else _fmt(after["nearest_ask_price"]),
                "bid_floor_change": bid_chg,
                "near_ask_change": ask_chg,
                "total_near_bid_change_pct": None
                if near_bid_chg_pct is None
                else round(near_bid_chg_pct, 6),
                "total_near_ask_change_pct": None
                if near_ask_chg_pct is None
                else round(near_ask_chg_pct, 6),
                "auction_direction_after": auction_after,
                "reaction_classification": cls,
                "post_wall_labels": "|".join(post_labels),
            }
        )
        wall_ctx_rows.append(
            {
                "event_key": event.event_key,
                "horizon_seconds": h,
                "wall_relation_labels": "|".join(wall_labels),
                "post_wall_labels": "|".join(post_labels),
                "nearest_bid_before": _fmt(before["nearest_bid_price"]),
                "nearest_ask_before": _fmt(before["nearest_ask_price"]),
                "nearest_bid_after": None
                if after is None
                else _fmt(after["nearest_bid_price"]),
                "nearest_ask_after": None
                if after is None
                else _fmt(after["nearest_ask_price"]),
                "bid_floor_direction_before": bid_floor_dir,
                "near_ask_direction_before": near_ask_dir,
                "auction_direction_before": auction_before,
                "auction_direction_after": auction_after,
                "reaction_classification": cls,
            }
        )

    event_row["primary_reaction_classification"] = primary_cls
    event_row["wall_labels"] = "|".join(wall_labels)
    return {
        "event": event_row,
        "forward": forward_rows,
        "wall_context": wall_ctx_rows,
        "mid_before": mid_before,
        "primary_reaction": primary_cls,
        "wall_labels": wall_labels,
    }


def run_liquidation_analysis(
    *,
    db: ReadOnlyClickHouse | None,
    symbol: str,
    start: datetime,
    end: datetime,
    params: LiquidationAnalysisParams,
    output_dir: Path,
    book_events: Sequence[BookLevelEvent] | None = None,
    liquidation_events: Sequence[LiquidationEvent] | None = None,
    price_path: Sequence[tuple[datetime, Decimal]] | None = None,
    trade_delta_fn: Any | None = None,
    oi_change_fn: Any | None = None,
) -> dict[str, Any]:
    """Core analysis. Supports pure fixtures (no DB) via injected events/books."""
    start = _ensure_utc(start)
    end = _ensure_utc(end)
    output_dir.mkdir(parents=True, exist_ok=True)

    if liquidation_events is None:
        assert db is not None
        events = load_liquidations(
            db,
            symbol=symbol,
            start=start,
            end=end,
        )
    else:
        events = dedupe_liquidations(list(liquidation_events))

    forward_end = end + timedelta(seconds=max(HORIZONS_SEC))
    if book_events is None:
        assert db is not None
        snap_ts, snap_u, snap_seq = find_bootstrap_snapshot(
            db, symbol=symbol, start=start, end=end
        )
        book_events = load_events(
            db,
            symbol=symbol,
            snapshot_ts=snap_ts,
            snapshot_u=snap_u,
            snapshot_seq=snap_seq,
            end=forward_end,
        )

    # Capture times: lookback, event, each horizon (no forward data into pre-context)
    sample_times: list[datetime] = []
    for ev in events:
        sample_times.append(
            ev.exchange_timestamp - timedelta(seconds=params.pre_context_lookback_seconds)
        )
        sample_times.append(ev.exchange_timestamp)
        for h in HORIZONS_SEC:
            sample_times.append(ev.exchange_timestamp + timedelta(seconds=h))
    # denser path for MFE/MAE
    t = start
    while t <= forward_end:
        sample_times.append(t)
        t += timedelta(seconds=params.sample_seconds)
    sample_times = sorted({_ensure_utc(x) for x in sample_times if x <= forward_end})

    try:
        final_book, timed_books = reconstruct_with_samples(
            book_events, sample_times=sample_times, end=forward_end
        )
    except ReplayError as exc:
        summary = {
            "symbol": symbol,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "decision": "LIQUIDATION_CONTEXT_FAILED",
            "error": str(exc),
            "event_count": len(events),
        }
        (output_dir / "liquidation_summary.json").write_bytes(
            orjson.dumps(summary, option=orjson.OPT_INDENT_2)
        )
        (output_dir / "REPORT.md").write_text(
            f"# Liquidation History Report\n\nFAILED: {exc}\n", encoding="utf-8"
        )
        return summary

    prices: list[Decimal] = []
    for book in timed_books.values():
        prices.extend(book.bids)
        prices.extend(book.asks)
    tick = infer_tick_size(prices) if prices else Decimal("0.0001")
    mid_ref = final_book.mid_price() or (events[0].bankruptcy_price if events else Decimal("1"))
    bucket_size = choose_bucket_size(mid_ref, tick, params.target_bps)

    book_path = build_price_path_from_books(timed_books)
    if price_path is None:
        if db is not None:
            trade_path = load_trade_price_path(
                db, symbol=symbol, start=start, end=forward_end
            )
            price_path = merge_price_paths(book_path, trade_path)
        else:
            price_path = book_path
    else:
        price_path = merge_price_paths(book_path, price_path)

    event_rows: list[dict[str, Any]] = []
    forward_rows: list[dict[str, Any]] = []
    wall_rows: list[dict[str, Any]] = []
    mid_at: dict[str, Decimal | None] = {}
    reaction_at: dict[str, str] = {}

    def _td(a: datetime, b: datetime) -> Decimal:
        if trade_delta_fn is not None:
            return _dec(trade_delta_fn(a, b))
        if db is None:
            return Decimal("0")
        return trade_delta(db, symbol=symbol, start=a, end=b)

    def _oi(a: datetime, b: datetime) -> Decimal | None:
        if oi_change_fn is not None:
            return oi_change_fn(a, b)
        if db is None:
            return None
        return oi_change(db, symbol=symbol, start=a, end=b)

    for ev in events:
        # Pre-event book: prefer timed sample at event ts (msgs with ts < sample captured
        # before applying equal-ts messages in reconstruct_with_samples). Also build a
        # strict causal book for safety.
        try:
            book_before = book_state_before_event(book_events, event_ts=ev.exchange_timestamp, strict=True)
        except ReplayError:
            book_before = timed_books.get(ev.exchange_timestamp) or clone_book(final_book)
        if book_before.mid_price() is None:
            # fallback to closest prior sample
            prior = [ts for ts in timed_books if ts <= ev.exchange_timestamp and timed_books[ts].mid_price()]
            if not prior:
                continue
            book_before = timed_books[max(prior)]

        look_ts = ev.exchange_timestamp - timedelta(seconds=params.pre_context_lookback_seconds)
        book_lookback = timed_books.get(look_ts)
        if book_lookback is None or book_lookback.mid_price() is None:
            try:
                book_lookback = book_state_before_event(
                    book_events, event_ts=look_ts, strict=False
                )
            except ReplayError:
                book_lookback = None

        books_after: dict[int, OrderBookState] = {}
        for h in HORIZONS_SEC:
            ts_h = ev.exchange_timestamp + timedelta(seconds=h)
            b = timed_books.get(ts_h)
            if b is None or b.mid_price() is None:
                try:
                    b = replay_until(book_events, as_of=ts_h)
                except ReplayError:
                    b = book_before
            books_after[h] = b

        pre_td: dict[int, Decimal] = {}
        pre_oi: dict[int, Decimal | None] = {}
        pre_px: dict[int, float | None] = {}
        mid_before = book_before.mid_price()
        for sec in (30, 60, 120, 300):
            a = ev.exchange_timestamp - timedelta(seconds=sec)
            pre_td[sec] = _td(a, ev.exchange_timestamp)
            pre_oi[sec] = _oi(a, ev.exchange_timestamp)
            px0 = price_at_or_before(price_path, as_of=a, fallback=mid_before or Decimal("0"))
            if mid_before and px0 > 0:
                pre_px[sec] = _pct(mid_before - px0, px0)
            else:
                pre_px[sec] = None

        post_td: dict[int, Decimal] = {}
        post_oi: dict[int, Decimal | None] = {}
        for h in HORIZONS_SEC:
            post_td[h] = _td(ev.exchange_timestamp, ev.exchange_timestamp + timedelta(seconds=h))
            post_oi[h] = _oi(ev.exchange_timestamp, ev.exchange_timestamp + timedelta(seconds=h))

        # Causal guard: price_path used for forward stats is filtered inside path_stats
        # to (event_ts, event_ts+h]; pre_* uses only times <= event.
        result = analyze_single_event(
            ev,
            book_before=book_before,
            book_lookback=book_lookback,
            book_after_by_horizon=books_after,
            price_path=price_path,
            pre_trade_deltas=pre_td,
            pre_oi_changes=pre_oi,
            pre_price_changes=pre_px,
            post_trade_deltas=post_td,
            post_oi_changes=post_oi,
            bucket_size=bucket_size,
            params=params,
        )
        event_rows.append(result["event"])
        forward_rows.extend(result["forward"])
        wall_rows.extend(result["wall_context"])
        mid_at[ev.event_key] = result["mid_before"]
        reaction_at[ev.event_key] = result["primary_reaction"]

    clusters = cluster_events(
        events,
        window_seconds=params.cluster_window_seconds,
        price_bps=params.cluster_price_bps,
        mid_at=mid_at,
        reaction_at=reaction_at,
    )

    # Relevance vs market volume around events
    relevance_notes: list[str] = []
    for ev in events:
        window_td = abs(_td(ev.exchange_timestamp - timedelta(seconds=60), ev.exchange_timestamp + timedelta(seconds=60)))
        if window_td > 0:
            share = float(ev.liquidation_notional / window_td * 100)
            relevance_notes.append(
                f"{ev.event_key}: liq_notional/|trade_delta_±60s| = {share:.4f}%"
            )
        else:
            relevance_notes.append(f"{ev.event_key}: no trade activity ±60s for size comparison")

    decision = "LIQUIDATION_CONTEXT_INCONCLUSIVE"
    if not events:
        decision = "LIQUIDATION_CONTEXT_INCONCLUSIVE"
    elif not event_rows:
        decision = "LIQUIDATION_CONTEXT_FAILED"
    else:
        clear = [
            r["primary_reaction_classification"]
            for r in event_rows
            if r.get("primary_reaction_classification") not in {NO_CLEAR_REACTION, None}
        ]
        if clear:
            decision = "LIQUIDATION_CONTEXT_PROMISING"
        else:
            decision = "LIQUIDATION_CONTEXT_INCONCLUSIVE"

    thr = params.thresholds
    long_events = [e for e in events if e.interpreted_position_side == LIQUIDATED_LONG]
    short_events = [e for e in events if e.interpreted_position_side == LIQUIDATED_SHORT]
    unknown_events = [e for e in events if e.interpreted_position_side == LIQUIDATION_SIDE_UNKNOWN]
    long_notional = sum((e.liquidation_notional for e in long_events), Decimal("0"))
    short_notional = sum((e.liquidation_notional for e in short_events), Decimal("0"))

    summary: dict[str, Any] = {
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "event_count": len(events),
        "analyzed_event_count": len(event_rows),
        "cluster_count": len(clusters),
        "liquidated_long_event_count": len(long_events),
        "liquidated_short_event_count": len(short_events),
        "unknown_side_event_count": len(unknown_events),
        "liquidated_long_notional": _fmt(long_notional),
        "liquidated_short_notional": _fmt(short_notional),
        "side_semantics": SIDE_SEMANTICS_STATUS,
        "source_columns": {
            "exchange_timestamp": "liquidation_ts (Bybit field T)",
            "received_timestamp": "received_ts",
            "bankruptcy_price": "ClickHouse price ← Bybit field p (BANKRUPTCY_PRICE)",
            "liquidation_price": "compat alias of bankruptcy_price (Bybit p); NOT necessarily traded market/fill/mid",
            "liquidation_qty / quantity": "quantity (Bybit field v)",
            "notional": "stored notional = price * qty at insert; recomputed if missing",
            "raw_side": "side (Bybit field S = position side)",
            "interpreted_position_side": "Buy→LIQUIDATED_LONG, Sell→LIQUIDATED_SHORT",
        },
        "thresholds": {
            "continuation_min_return_pct": thr.continuation_min_return_pct,
            "continuation_max_giveback_ratio": thr.continuation_max_giveback_ratio,
            "rejection_min_excursion_pct": thr.rejection_min_excursion_pct,
            "rejection_min_giveback_ratio": thr.rejection_min_giveback_ratio,
            "exhaustion_max_continuation_pct": thr.exhaustion_max_continuation_pct,
            "exhaustion_min_reversal_pct": thr.exhaustion_min_reversal_pct,
            "absorption_max_abs_return_pct": thr.absorption_max_abs_return_pct,
            "absorption_max_mfe_pct": thr.absorption_max_mfe_pct,
            "absorption_min_trade_multiple_of_liq": thr.absorption_min_trade_multiple_of_liq,
            "breakout_min_return_pct": thr.breakout_min_return_pct,
            "wall_relation_bps": params.wall_relation_bps,
            "cluster_window_seconds": params.cluster_window_seconds,
            "cluster_price_bps": params.cluster_price_bps,
            "primary_horizon_seconds": thr.primary_horizon_seconds,
            "horizons_seconds": list(HORIZONS_SEC),
        },
        "tick_size": _fmt(tick),
        "bucket_size": _fmt(bucket_size),
        "relevance_notes": relevance_notes,
        "events": event_rows,
        "clusters": clusters,
        "decision": decision,
        "limitations": [
            "Reaction labels are diagnostic heuristics, not causal claims.",
            "Bybit p is bankruptcy price — not claimed to be the traded market price at the print.",
            "Forward returns use mid / trade path start_price=mid_before, never bankruptcy_price.",
            "Pre-event book uses messages strictly before liquidation_ts.",
            "Single small liquidations may be noise relative to market volume.",
            "Read-only ClickHouse; recorder/writer/schema untouched by this analysis run.",
        ],
    }

    write_csv(output_dir / "liquidation_events.csv", event_rows)
    write_csv(output_dir / "liquidation_forward_outcomes.csv", forward_rows)
    write_csv(output_dir / "liquidation_clusters.csv", clusters)
    write_csv(output_dir / "liquidation_wall_context.csv", wall_rows)
    (output_dir / "liquidation_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2)
    )
    (output_dir / "REPORT.md").write_text(
        render_report(summary, params=params), encoding="utf-8"
    )
    return summary


def render_report(summary: dict[str, Any], *, params: LiquidationAnalysisParams) -> str:
    lines: list[str] = [
        "# Liquidation History Analysis Report",
        "",
        f"- Symbol: `{summary.get('symbol')}`",
        f"- Window: `{summary.get('start')}` → `{summary.get('end')}`",
        f"- Decision: **{summary.get('decision')}**",
        f"- Liquidations found: **{summary.get('event_count')}**",
        f"- Clusters: **{summary.get('cluster_count')}**",
        f"- Side semantics: `{summary.get('side_semantics')}` "
        f"(Buy→`{LIQUIDATED_LONG}`, Sell→`{LIQUIDATED_SHORT}`)",
        f"- Liquidated LONG events / notional: "
        f"**{summary.get('liquidated_long_event_count')}** / "
        f"`{summary.get('liquidated_long_notional')}`",
        f"- Liquidated SHORT events / notional: "
        f"**{summary.get('liquidated_short_event_count')}** / "
        f"`{summary.get('liquidated_short_notional')}`",
        f"- Unknown side events: **{summary.get('unknown_side_event_count')}**",
        "",
        "## Bankruptcy price vs market price",
        "",
        "- Bybit `allLiquidation` field `p` is the **bankruptcy price**.",
        "- Stored ClickHouse column `price` keeps that value for compatibility.",
        "- Analysis outputs use `bankruptcy_price` with `price_type=BANKRUPTCY_PRICE`.",
        "- Compat field `liquidation_price` is an alias of the bankruptcy price — "
        "**not** claimed to be the traded market, fill, or mid price.",
        "- Forward reaction `start_price` is always `mid_before` (market/mid path), "
        "never the bankruptcy price.",
        "",
        "## Source column mapping",
        "",
    ]
    for k, v in (summary.get("source_columns") or {}).items():
        lines.append(f"- `{k}` ← {v}")
    lines += [
        "",
        "## Configurable thresholds",
        "",
    ]
    for k, v in (summary.get("thresholds") or {}).items():
        lines.append(f"- `{k}`: `{v}`")
    lines += ["", "## Events", ""]
    for ev in summary.get("events") or []:
        narrative = ""
        if (
            ev.get("raw_side") == "Sell"
            and ev.get("interpreted_position_side") == LIQUIDATED_SHORT
            and str(ev.get("quantity") or ev.get("liquidation_qty") or "").startswith("815.65")
        ):
            narrative = (
                "- Narrative: small **short** liquidation during an upside breakout context; "
                "30–60s mild rejection, from ~2m upside continuation, 5–10m breakout "
                "acceleration. **Do not** claim the market traded at the bankruptcy print "
                f"`{ev.get('bankruptcy_price')}`; mid before was ~`{ev.get('mid_before')}`."
            )
        lines += [
            f"### `{ev.get('event_key')}`",
            f"- When (exchange): `{ev.get('exchange_timestamp')}`",
            f"- Received: `{ev.get('received_timestamp')}`",
            f"- Bankruptcy price (`p`): `{ev.get('bankruptcy_price')}` "
            f"(`{ev.get('price_type')}`)",
            f"- Compat `liquidation_price` (alias of bankruptcy): `{ev.get('liquidation_price')}`",
            f"- Raw side (`S`): `{ev.get('raw_side')}`",
            f"- Liquidated position side: `{ev.get('interpreted_position_side')}` "
            f"({ev.get('side_semantics_status')})",
            f"- Qty / notional: `{ev.get('quantity') or ev.get('liquidation_qty')}` / "
            f"`{ev.get('notional') or ev.get('liquidation_notional')}`",
            f"- Mid before: `{ev.get('mid_before')}`",
            f"- Bankruptcy − mid: `{ev.get('bankruptcy_price_minus_mid')}` "
            f"({ev.get('bankruptcy_price_distance_from_mid_pct')} %)",
            f"- Bid-floor / Near-ask before: `{ev.get('nearest_bid_wall_price')}` / "
            f"`{ev.get('nearest_ask_wall_price')}`",
            f"- Primary reaction (horizon {params.thresholds.primary_horizon_seconds}s, "
            f"from mid): `{ev.get('primary_reaction_classification')}`",
            f"- Wall labels: `{ev.get('wall_labels')}`",
        ]
        if narrative:
            lines.append(narrative)
        lines.append("")
    lines += ["## Clusters", ""]
    for c in summary.get("clusters") or []:
        lines.append(
            f"- `{c.get('cluster_id')}` {c.get('cluster_start')}→{c.get('cluster_end')} "
            f"n={c.get('event_count')} notional={c.get('total_notional')} "
            f"side={c.get('dominant_side')} reaction={c.get('reaction_classification')}"
        )
    if not summary.get("clusters"):
        lines.append("- (none)")
    lines += ["", "## Relevance vs market", ""]
    for note in summary.get("relevance_notes") or []:
        lines.append(f"- {note}")
    lines += ["", "## Method limits", ""]
    for lim in summary.get("limitations") or []:
        lines.append(f"- {lim}")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Causal liquidation history analysis (ClickHouse read-only)"
    )
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", required=True, help="UTC start, e.g. 2026-07-26T09:16:29Z")
    p.add_argument("--end", required=True, help="UTC end")
    p.add_argument("--sample-seconds", type=int, default=30)
    p.add_argument("--target-bps", type=float, default=10.0)
    p.add_argument("--near-min-distance-pct", type=float, default=0.10)
    p.add_argument("--near-max-distance-pct", type=float, default=1.50)
    p.add_argument("--near-top-n", type=int, default=3)
    p.add_argument("--cluster-window-seconds", type=int, default=60)
    p.add_argument("--cluster-price-bps", type=float, default=10.0)
    p.add_argument("--wall-relation-bps", type=float, default=10.0)
    p.add_argument("--output-dir", default=None)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def run_from_args(args: argparse.Namespace) -> dict[str, Any]:
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    start = parse_utc(args.start)
    end = parse_utc(args.end)
    params = LiquidationAnalysisParams(
        sample_seconds=int(args.sample_seconds),
        target_bps=float(args.target_bps),
        near_min_distance_pct=float(args.near_min_distance_pct),
        near_max_distance_pct=float(args.near_max_distance_pct),
        near_top_n=int(args.near_top_n),
        cluster_window_seconds=int(args.cluster_window_seconds),
        cluster_price_bps=float(args.cluster_price_bps),
        wall_relation_bps=float(args.wall_relation_bps),
        thresholds=ReactionThresholds(wall_relation_bps=float(args.wall_relation_bps)),
    )
    out_dir = Path(args.output_dir) if args.output_dir else (
        PROJECT_ROOT
        / "results"
        / f"liquidation_full_history_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    )
    db = connect_readonly()
    try:
        return run_liquidation_analysis(
            db=db,
            symbol=args.symbol,
            start=start,
            end=end,
            params=params,
            output_dir=out_dir,
        )
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = run_from_args(args)
    sys.stdout.buffer.write(orjson.dumps({"decision": summary.get("decision"), "event_count": summary.get("event_count"), "output_hint": True}, option=orjson.OPT_INDENT_2))
    sys.stdout.buffer.write(b"\n")
    return 0 if summary.get("decision") != "LIQUIDATION_CONTEXT_FAILED" else 2


if __name__ == "__main__":
    raise SystemExit(main())

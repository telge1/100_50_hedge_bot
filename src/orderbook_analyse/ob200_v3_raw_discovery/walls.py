"""Causal wall event extraction from 1s L2 samples (no look-ahead)."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


@dataclass
class WallEvent:
    event_id: str
    symbol: str
    side: str  # BID or ASK
    direction: str  # LONG (bid wall attack) / SHORT (ask wall attack)
    event_type: str
    ts_ms: int
    wall_price: float
    wall_qty: float
    wall_dist_bps: float
    mid: float
    best_bid: float
    best_ask: float
    spread_bps: float
    imbalance_l10: float
    qty_vs_median: float
    persistence_s: float
    source_file: str
    threshold_qty_median_mult: float
    notes: str = ""


def _rolling_median(window: deque[float]) -> float:
    vals = sorted(window)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])


def extract_wall_events(
    samples: Iterable[SampleRow],
    *,
    median_window: int = 120,
    qty_median_mult: float = 3.0,
    approach_bps: float = 5.0,
    touch_bps: float = 1.5,
    pull_frac: float = 0.5,
    break_hold_s: int = 3,
    reclaim_hold_s: int = 3,
    seed: int = 0,
) -> list[WallEvent]:
    """Causal event detector. Uses only past samples in rolling windows."""
    events: list[WallEvent] = []
    bid_qty_hist: deque[float] = deque(maxlen=median_window)
    ask_qty_hist: deque[float] = deque(maxlen=median_window)
    # track active walls
    bid_wall: dict[str, Any] | None = None
    ask_wall: dict[str, Any] | None = None
    broken_bid: dict[str, Any] | None = None
    broken_ask: dict[str, Any] | None = None
    pulled_bid_prices: list[tuple[float, int]] = []  # (price, ts)
    pulled_ask_prices: list[tuple[float, int]] = []
    seq = 0

    def _eid(prefix: str) -> str:
        nonlocal seq
        seq += 1
        return f"{prefix}_{seed}_{seq}"

    prev: SampleRow | None = None
    for s in samples:
        if s.warmup:
            if s.bid_wall_qty is not None:
                bid_qty_hist.append(s.bid_wall_qty)
            if s.ask_wall_qty is not None:
                ask_qty_hist.append(s.ask_wall_qty)
            prev = s
            continue

        bid_med = _rolling_median(bid_qty_hist) or 1e-12
        ask_med = _rolling_median(ask_qty_hist) or 1e-12

        # --- BID wall (LONG attack direction: price falling into bids) ---
        if s.bid_wall_price is not None and s.bid_wall_qty is not None and s.mid > 0:
            dist = abs(s.mid - s.bid_wall_price) / s.mid * 10000
            qvsm = s.bid_wall_qty / bid_med
            share_l10 = s.bid_wall_qty / max(s.bid_qty_l10, 1e-12)
            # Primary: rolling median×mult. Secondary (thin/uniform books e.g. DOGE):
            # large share of local L10 depth AND still above median.
            dominant = qvsm >= qty_median_mult or (share_l10 >= 0.55 and qvsm >= 1.5)
            if dominant:
                if bid_wall is None or abs(bid_wall["price"] - s.bid_wall_price) / s.mid * 10000 > 2:
                    bid_wall = {
                        "price": s.bid_wall_price,
                        "qty": s.bid_wall_qty,
                        "first_ts": s.ts_ms,
                        "peak_qty": s.bid_wall_qty,
                        "approached": False,
                        "touched": False,
                    }
                    events.append(
                        WallEvent(
                            event_id=_eid(s.symbol),
                            symbol=s.symbol,
                            side="BID",
                            direction="LONG",
                            event_type="WALL_APPEAR",
                            ts_ms=s.ts_ms,
                            wall_price=s.bid_wall_price,
                            wall_qty=s.bid_wall_qty,
                            wall_dist_bps=dist,
                            mid=s.mid,
                            best_bid=s.best_bid,
                            best_ask=s.best_ask,
                            spread_bps=s.spread_bps,
                            imbalance_l10=s.imbalance_l10,
                            qty_vs_median=qvsm,
                            persistence_s=0.0,
                            source_file=s.source_file,
                            threshold_qty_median_mult=qty_median_mult,
                        )
                    )
                    for pp, pts in list(pulled_bid_prices):
                        if s.ts_ms - pts > 300_000:
                            continue
                        if abs(pp - s.bid_wall_price) / s.mid * 10000 <= 2.0:
                            events.append(
                                WallEvent(
                                    event_id=_eid(s.symbol),
                                    symbol=s.symbol,
                                    side="BID",
                                    direction="LONG",
                                    event_type="WALL_REAPPEARANCE",
                                    ts_ms=s.ts_ms,
                                    wall_price=s.bid_wall_price,
                                    wall_qty=s.bid_wall_qty,
                                    wall_dist_bps=dist,
                                    mid=s.mid,
                                    best_bid=s.best_bid,
                                    best_ask=s.best_ask,
                                    spread_bps=s.spread_bps,
                                    imbalance_l10=s.imbalance_l10,
                                    qty_vs_median=qvsm,
                                    persistence_s=0.0,
                                    source_file=s.source_file,
                                    threshold_qty_median_mult=qty_median_mult,
                                    notes=f"near_pulled={pp}",
                                )
                            )
                            pulled_bid_prices = [
                                x for x in pulled_bid_prices if abs(x[0] - pp) / s.mid * 10000 > 2
                            ]
                            break
                else:
                    bid_wall["qty"] = s.bid_wall_qty
                    bid_wall["peak_qty"] = max(bid_wall["peak_qty"], s.bid_wall_qty)
                    age = (s.ts_ms - bid_wall["first_ts"]) / 1000.0
                    if dist <= approach_bps and not bid_wall["approached"]:
                        bid_wall["approached"] = True
                        events.append(
                            WallEvent(
                                event_id=_eid(s.symbol),
                                symbol=s.symbol,
                                side="BID",
                                direction="LONG",
                                event_type="WALL_APPROACH",
                                ts_ms=s.ts_ms,
                                wall_price=bid_wall["price"],
                                wall_qty=s.bid_wall_qty,
                                wall_dist_bps=dist,
                                mid=s.mid,
                                best_bid=s.best_bid,
                                best_ask=s.best_ask,
                                spread_bps=s.spread_bps,
                                imbalance_l10=s.imbalance_l10,
                                qty_vs_median=qvsm,
                                persistence_s=age,
                                source_file=s.source_file,
                                threshold_qty_median_mult=qty_median_mult,
                            )
                        )
                    if dist <= touch_bps and not bid_wall["touched"]:
                        bid_wall["touched"] = True
                        events.append(
                            WallEvent(
                                event_id=_eid(s.symbol),
                                symbol=s.symbol,
                                side="BID",
                                direction="LONG",
                                event_type="WALL_TOUCH",
                                ts_ms=s.ts_ms,
                                wall_price=bid_wall["price"],
                                wall_qty=s.bid_wall_qty,
                                wall_dist_bps=dist,
                                mid=s.mid,
                                best_bid=s.best_bid,
                                best_ask=s.best_ask,
                                spread_bps=s.spread_bps,
                                imbalance_l10=s.imbalance_l10,
                                qty_vs_median=qvsm,
                                persistence_s=age,
                                source_file=s.source_file,
                                threshold_qty_median_mult=qty_median_mult,
                            )
                        )
                    # pull: once per wall
                    if (
                        not bid_wall.get("pulled")
                        and prev is not None
                        and prev.bid_wall_qty
                        and s.bid_wall_qty < prev.bid_wall_qty * (1 - pull_frac)
                        and s.best_bid > bid_wall["price"] * 0.999
                    ):
                        bid_wall["pulled"] = True
                        pulled_bid_prices.append((bid_wall["price"], s.ts_ms))
                        events.append(
                            WallEvent(
                                event_id=_eid(s.symbol),
                                symbol=s.symbol,
                                side="BID",
                                direction="LONG",
                                event_type="WALL_PULL",
                                ts_ms=s.ts_ms,
                                wall_price=bid_wall["price"],
                                wall_qty=s.bid_wall_qty,
                                wall_dist_bps=dist,
                                mid=s.mid,
                                best_bid=s.best_bid,
                                best_ask=s.best_ask,
                                spread_bps=s.spread_bps,
                                imbalance_l10=s.imbalance_l10,
                                qty_vs_median=qvsm,
                                persistence_s=age,
                                source_file=s.source_file,
                                threshold_qty_median_mult=qty_median_mult,
                                notes=f"qty {prev.bid_wall_qty:.4g}->{s.bid_wall_qty:.4g}",
                            )
                        )
                    # absorption proxy: once per wall after touch
                    if (
                        bid_wall["touched"]
                        and not bid_wall.get("absorbed")
                        and qvsm >= qty_median_mult
                        and s.best_bid >= bid_wall["price"]
                    ):
                        if prev and prev.mid >= s.mid and (prev.mid - s.mid) / s.mid * 10000 < 3:
                            bid_wall["absorbed"] = True
                            events.append(
                                WallEvent(
                                    event_id=_eid(s.symbol),
                                    symbol=s.symbol,
                                    side="BID",
                                    direction="LONG",
                                    event_type="WALL_ABSORPTION_PROXY",
                                    ts_ms=s.ts_ms,
                                    wall_price=bid_wall["price"],
                                    wall_qty=s.bid_wall_qty,
                                    wall_dist_bps=dist,
                                    mid=s.mid,
                                    best_bid=s.best_bid,
                                    best_ask=s.best_ask,
                                    spread_bps=s.spread_bps,
                                    imbalance_l10=s.imbalance_l10,
                                    qty_vs_median=qvsm,
                                    persistence_s=age,
                                    source_file=s.source_file,
                                    threshold_qty_median_mult=qty_median_mult,
                                )
                            )
                # break: mid/best_bid below wall
                if bid_wall is not None and s.best_bid < bid_wall["price"]:
                    broken_bid = {
                        "price": bid_wall["price"],
                        "ts": s.ts_ms,
                        "hold": 1,
                    }
                    events.append(
                        WallEvent(
                            event_id=_eid(s.symbol),
                            symbol=s.symbol,
                            side="BID",
                            direction="LONG",
                            event_type="WALL_BREAK",
                            ts_ms=s.ts_ms,
                            wall_price=bid_wall["price"],
                            wall_qty=s.bid_wall_qty,
                            wall_dist_bps=dist,
                            mid=s.mid,
                            best_bid=s.best_bid,
                            best_ask=s.best_ask,
                            spread_bps=s.spread_bps,
                            imbalance_l10=s.imbalance_l10,
                            qty_vs_median=qvsm,
                            persistence_s=(s.ts_ms - bid_wall["first_ts"]) / 1000.0,
                            source_file=s.source_file,
                            threshold_qty_median_mult=qty_median_mult,
                        )
                    )
                    bid_wall = None
            bid_qty_hist.append(s.bid_wall_qty)

        # reclaim after break
        if broken_bid is not None:
            if s.best_bid >= broken_bid["price"]:
                broken_bid["hold"] += 1
                if broken_bid["hold"] >= reclaim_hold_s:
                    events.append(
                        WallEvent(
                            event_id=_eid(s.symbol),
                            symbol=s.symbol,
                            side="BID",
                            direction="LONG",
                            event_type="WALL_RECLAIM",
                            ts_ms=s.ts_ms,
                            wall_price=broken_bid["price"],
                            wall_qty=s.bid_wall_qty or 0.0,
                            wall_dist_bps=0.0,
                            mid=s.mid,
                            best_bid=s.best_bid,
                            best_ask=s.best_ask,
                            spread_bps=s.spread_bps,
                            imbalance_l10=s.imbalance_l10,
                            qty_vs_median=0.0,
                            persistence_s=(s.ts_ms - broken_bid["ts"]) / 1000.0,
                            source_file=s.source_file,
                            threshold_qty_median_mult=qty_median_mult,
                        )
                    )
                    # depth recovery if bid depth restored vs prior
                    if prev and s.bid_qty_l10 >= (prev.bid_qty_l10 * 0.9):
                        events.append(
                            WallEvent(
                                event_id=_eid(s.symbol),
                                symbol=s.symbol,
                                side="BID",
                                direction="LONG",
                                event_type="DEPTH_RECOVERY",
                                ts_ms=s.ts_ms,
                                wall_price=broken_bid["price"],
                                wall_qty=s.bid_qty_l10,
                                wall_dist_bps=0.0,
                                mid=s.mid,
                                best_bid=s.best_bid,
                                best_ask=s.best_ask,
                                spread_bps=s.spread_bps,
                                imbalance_l10=s.imbalance_l10,
                                qty_vs_median=0.0,
                                persistence_s=0.0,
                                source_file=s.source_file,
                                threshold_qty_median_mult=qty_median_mult,
                            )
                        )
                    broken_bid = None
            else:
                broken_bid["hold"] = 0

        # --- ASK wall (SHORT) mirrored ---
        if s.ask_wall_price is not None and s.ask_wall_qty is not None and s.mid > 0:
            dist = abs(s.ask_wall_price - s.mid) / s.mid * 10000
            qvsm = s.ask_wall_qty / ask_med
            share_l10 = s.ask_wall_qty / max(s.ask_qty_l10, 1e-12)
            dominant = qvsm >= qty_median_mult or (share_l10 >= 0.55 and qvsm >= 1.5)
            if dominant:
                if ask_wall is None or abs(ask_wall["price"] - s.ask_wall_price) / s.mid * 10000 > 2:
                    ask_wall = {
                        "price": s.ask_wall_price,
                        "qty": s.ask_wall_qty,
                        "first_ts": s.ts_ms,
                        "peak_qty": s.ask_wall_qty,
                        "approached": False,
                        "touched": False,
                    }
                    events.append(
                        WallEvent(
                            event_id=_eid(s.symbol),
                            symbol=s.symbol,
                            side="ASK",
                            direction="SHORT",
                            event_type="WALL_APPEAR",
                            ts_ms=s.ts_ms,
                            wall_price=s.ask_wall_price,
                            wall_qty=s.ask_wall_qty,
                            wall_dist_bps=dist,
                            mid=s.mid,
                            best_bid=s.best_bid,
                            best_ask=s.best_ask,
                            spread_bps=s.spread_bps,
                            imbalance_l10=s.imbalance_l10,
                            qty_vs_median=qvsm,
                            persistence_s=0.0,
                            source_file=s.source_file,
                            threshold_qty_median_mult=qty_median_mult,
                        )
                    )
                    for pp, pts in list(pulled_ask_prices):
                        if s.ts_ms - pts > 300_000:
                            continue
                        if abs(pp - s.ask_wall_price) / s.mid * 10000 <= 2.0:
                            events.append(
                                WallEvent(
                                    event_id=_eid(s.symbol),
                                    symbol=s.symbol,
                                    side="ASK",
                                    direction="SHORT",
                                    event_type="WALL_REAPPEARANCE",
                                    ts_ms=s.ts_ms,
                                    wall_price=s.ask_wall_price,
                                    wall_qty=s.ask_wall_qty,
                                    wall_dist_bps=dist,
                                    mid=s.mid,
                                    best_bid=s.best_bid,
                                    best_ask=s.best_ask,
                                    spread_bps=s.spread_bps,
                                    imbalance_l10=s.imbalance_l10,
                                    qty_vs_median=qvsm,
                                    persistence_s=0.0,
                                    source_file=s.source_file,
                                    threshold_qty_median_mult=qty_median_mult,
                                    notes=f"near_pulled={pp}",
                                )
                            )
                            pulled_ask_prices = [
                                x for x in pulled_ask_prices if abs(x[0] - pp) / s.mid * 10000 > 2
                            ]
                            break
                else:
                    ask_wall["qty"] = s.ask_wall_qty
                    ask_wall["peak_qty"] = max(ask_wall["peak_qty"], s.ask_wall_qty)
                    age = (s.ts_ms - ask_wall["first_ts"]) / 1000.0
                    if dist <= approach_bps and not ask_wall["approached"]:
                        ask_wall["approached"] = True
                        events.append(
                            WallEvent(
                                event_id=_eid(s.symbol),
                                symbol=s.symbol,
                                side="ASK",
                                direction="SHORT",
                                event_type="WALL_APPROACH",
                                ts_ms=s.ts_ms,
                                wall_price=ask_wall["price"],
                                wall_qty=s.ask_wall_qty,
                                wall_dist_bps=dist,
                                mid=s.mid,
                                best_bid=s.best_bid,
                                best_ask=s.best_ask,
                                spread_bps=s.spread_bps,
                                imbalance_l10=s.imbalance_l10,
                                qty_vs_median=qvsm,
                                persistence_s=age,
                                source_file=s.source_file,
                                threshold_qty_median_mult=qty_median_mult,
                            )
                        )
                    if dist <= touch_bps and not ask_wall["touched"]:
                        ask_wall["touched"] = True
                        events.append(
                            WallEvent(
                                event_id=_eid(s.symbol),
                                symbol=s.symbol,
                                side="ASK",
                                direction="SHORT",
                                event_type="WALL_TOUCH",
                                ts_ms=s.ts_ms,
                                wall_price=ask_wall["price"],
                                wall_qty=s.ask_wall_qty,
                                wall_dist_bps=dist,
                                mid=s.mid,
                                best_bid=s.best_bid,
                                best_ask=s.best_ask,
                                spread_bps=s.spread_bps,
                                imbalance_l10=s.imbalance_l10,
                                qty_vs_median=qvsm,
                                persistence_s=age,
                                source_file=s.source_file,
                                threshold_qty_median_mult=qty_median_mult,
                            )
                        )
                    if (
                        not ask_wall.get("pulled")
                        and prev is not None
                        and prev.ask_wall_qty
                        and s.ask_wall_qty < prev.ask_wall_qty * (1 - pull_frac)
                        and s.best_ask < ask_wall["price"] * 1.001
                    ):
                        ask_wall["pulled"] = True
                        pulled_ask_prices.append((ask_wall["price"], s.ts_ms))
                        events.append(
                            WallEvent(
                                event_id=_eid(s.symbol),
                                symbol=s.symbol,
                                side="ASK",
                                direction="SHORT",
                                event_type="WALL_PULL",
                                ts_ms=s.ts_ms,
                                wall_price=ask_wall["price"],
                                wall_qty=s.ask_wall_qty,
                                wall_dist_bps=dist,
                                mid=s.mid,
                                best_bid=s.best_bid,
                                best_ask=s.best_ask,
                                spread_bps=s.spread_bps,
                                imbalance_l10=s.imbalance_l10,
                                qty_vs_median=qvsm,
                                persistence_s=age,
                                source_file=s.source_file,
                                threshold_qty_median_mult=qty_median_mult,
                            )
                        )
                    if (
                        ask_wall["touched"]
                        and not ask_wall.get("absorbed")
                        and qvsm >= qty_median_mult
                        and s.best_ask <= ask_wall["price"]
                    ):
                        if prev and prev.mid <= s.mid and (s.mid - prev.mid) / s.mid * 10000 < 3:
                            ask_wall["absorbed"] = True
                            events.append(
                                WallEvent(
                                    event_id=_eid(s.symbol),
                                    symbol=s.symbol,
                                    side="ASK",
                                    direction="SHORT",
                                    event_type="WALL_ABSORPTION_PROXY",
                                    ts_ms=s.ts_ms,
                                    wall_price=ask_wall["price"],
                                    wall_qty=s.ask_wall_qty,
                                    wall_dist_bps=dist,
                                    mid=s.mid,
                                    best_bid=s.best_bid,
                                    best_ask=s.best_ask,
                                    spread_bps=s.spread_bps,
                                    imbalance_l10=s.imbalance_l10,
                                    qty_vs_median=qvsm,
                                    persistence_s=age,
                                    source_file=s.source_file,
                                    threshold_qty_median_mult=qty_median_mult,
                                )
                            )
                if ask_wall is not None and s.best_ask > ask_wall["price"]:
                    broken_ask = {"price": ask_wall["price"], "ts": s.ts_ms, "hold": 1}
                    events.append(
                        WallEvent(
                            event_id=_eid(s.symbol),
                            symbol=s.symbol,
                            side="ASK",
                            direction="SHORT",
                            event_type="WALL_BREAK",
                            ts_ms=s.ts_ms,
                            wall_price=ask_wall["price"],
                            wall_qty=s.ask_wall_qty,
                            wall_dist_bps=dist,
                            mid=s.mid,
                            best_bid=s.best_bid,
                            best_ask=s.best_ask,
                            spread_bps=s.spread_bps,
                            imbalance_l10=s.imbalance_l10,
                            qty_vs_median=qvsm,
                            persistence_s=(s.ts_ms - ask_wall["first_ts"]) / 1000.0,
                            source_file=s.source_file,
                            threshold_qty_median_mult=qty_median_mult,
                        )
                    )
                    ask_wall = None
            ask_qty_hist.append(s.ask_wall_qty)

        if broken_ask is not None:
            if s.best_ask <= broken_ask["price"]:
                broken_ask["hold"] += 1
                if broken_ask["hold"] >= reclaim_hold_s:
                    events.append(
                        WallEvent(
                            event_id=_eid(s.symbol),
                            symbol=s.symbol,
                            side="ASK",
                            direction="SHORT",
                            event_type="WALL_RECLAIM",
                            ts_ms=s.ts_ms,
                            wall_price=broken_ask["price"],
                            wall_qty=s.ask_wall_qty or 0.0,
                            wall_dist_bps=0.0,
                            mid=s.mid,
                            best_bid=s.best_bid,
                            best_ask=s.best_ask,
                            spread_bps=s.spread_bps,
                            imbalance_l10=s.imbalance_l10,
                            qty_vs_median=0.0,
                            persistence_s=(s.ts_ms - broken_ask["ts"]) / 1000.0,
                            source_file=s.source_file,
                            threshold_qty_median_mult=qty_median_mult,
                        )
                    )
                    broken_ask = None
            else:
                broken_ask["hold"] = 0

        # reappearance: new dominant wall near previously pulled price
        prev = s

    # de-dup absorption spam: keep first per 30s per side
    filtered: list[WallEvent] = []
    last_abs: dict[str, int] = {}
    for ev in events:
        if ev.event_type == "WALL_ABSORPTION_PROXY":
            key = f"{ev.symbol}:{ev.side}"
            last = last_abs.get(key, -10**12)
            if ev.ts_ms - last < 30_000:
                continue
            last_abs[key] = ev.ts_ms
        filtered.append(ev)
    return filtered


def wall_event_to_row(ev: WallEvent) -> dict[str, Any]:
    return asdict(ev)

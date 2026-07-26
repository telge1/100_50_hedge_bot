"""Tick-level trade features for absorption / exhaustion research (read-only).

Window semantics (used everywhere):
  trades in (t - window_seconds, t]  — exclusive left, inclusive right.
  Only events with event_time <= decision_time ``t`` may enter features.

Level join:
  Buy aggressor → ask walls; Sell aggressor → bid walls.
  Match if abs(trade_price - wall_price) / wall_price * 10_000 <= level_join_bps.
  One trade → at most one wall (nearest distance; ties: higher notional, then
  lower price for asks / higher price for bids).

Refill / Iceberg labels are proxies only — never claim true iceberg fills.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

EPSILON = 1e-12

REFILL_QUALITY_HIGH = "HIGH"
REFILL_QUALITY_MEDIUM = "MEDIUM"
REFILL_QUALITY_LOW = "LOW"
REFILL_QUALITY_INSUFFICIENT = "INSUFFICIENT"

JOIN_QUALITY_HIGH = "HIGH"
JOIN_QUALITY_MEDIUM = "MEDIUM"
JOIN_QUALITY_LOW = "LOW"
JOIN_QUALITY_INSUFFICIENT = "INSUFFICIENT"


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _f(x: Any) -> float:
    if x is None:
        return 0.0
    if isinstance(x, Decimal):
        return float(x)
    return float(x)


def _bps_distance(a: float, b: float) -> float:
    if b == 0.0:
        return float("inf")
    return abs(a - b) / abs(b) * 10_000.0


@dataclass(frozen=True)
class TradeTick:
    trade_ts: datetime
    side: str
    price: float
    quantity: float
    notional: float
    trade_id: str


@dataclass(frozen=True)
class WallLevel:
    side: str  # "Bid" | "Ask"
    price: float
    notional: float
    label: str = ""  # nearest / dominant / top


@dataclass
class TradeLoaderDiagnostics:
    trade_tick_count: int = 0
    duplicate_trade_count: int = 0
    invalid_trade_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    total_buy_notional: float = 0.0
    total_sell_notional: float = 0.0
    first_trade_ts: str | None = None
    last_trade_ts: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_tick_count": self.trade_tick_count,
            "duplicate_trade_count": self.duplicate_trade_count,
            "invalid_trade_count": self.invalid_trade_count,
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
            "total_buy_notional": self.total_buy_notional,
            "total_sell_notional": self.total_sell_notional,
            "first_trade_ts": self.first_trade_ts,
            "last_trade_ts": self.last_trade_ts,
            "warnings": list(self.warnings),
        }


@dataclass
class LevelJoinResult:
    wall_side: str
    wall_price: float
    wall_notional: float
    matched_trade_notional: float = 0.0
    unmatched_trade_notional: float = 0.0
    matched_trade_count: int = 0
    unmatched_trade_count: int = 0
    ambiguous_trade_count: int = 0
    level_join_distance_bps_mean: float | None = None
    level_join_distance_bps_max: float | None = None
    level_join_quality: str = JOIN_QUALITY_INSUFFICIENT
    ambiguity_rate: float = 0.0


def sort_trade_ticks(ticks: Iterable[TradeTick]) -> list[TradeTick]:
    return sorted(
        ticks,
        key=lambda t: (ensure_utc(t.trade_ts), str(t.trade_id)),
    )


def normalize_trade_ticks(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[TradeTick], TradeLoaderDiagnostics]:
    """Parse, validate, dedupe by (trade_ts, trade_id), sort deterministically."""
    diag = TradeLoaderDiagnostics()
    seen: set[tuple[str, str]] = set()
    out: list[TradeTick] = []
    for row in rows:
        try:
            ts = row["trade_ts"]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts = ensure_utc(ts)
            side = str(row["side"]).strip()
            price = float(row["price"])
            qty = float(row["quantity"])
            notional = float(row["notional"])
            trade_id = str(row["trade_id"])
        except Exception:
            diag.invalid_trade_count += 1
            continue
        if side not in {"Buy", "Sell"} or price <= 0 or qty <= 0 or notional < 0:
            diag.invalid_trade_count += 1
            continue
        key = (ts.isoformat(), trade_id)
        if key in seen:
            diag.duplicate_trade_count += 1
            continue
        seen.add(key)
        tick = TradeTick(
            trade_ts=ts,
            side=side,
            price=price,
            quantity=qty,
            notional=notional,
            trade_id=trade_id,
        )
        out.append(tick)
        if side == "Buy":
            diag.buy_count += 1
            diag.total_buy_notional += notional
        else:
            diag.sell_count += 1
            diag.total_sell_notional += notional
    out = sort_trade_ticks(out)
    diag.trade_tick_count = len(out)
    if out:
        diag.first_trade_ts = out[0].trade_ts.isoformat()
        diag.last_trade_ts = out[-1].trade_ts.isoformat()
    return out, diag


def load_trade_ticks(
    db: Any,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[list[TradeTick], TradeLoaderDiagnostics]:
    """Load individual trades in [start, end] (inclusive both ends), read-only."""
    start = ensure_utc(start)
    end = ensure_utc(end)
    rows = db.query(
        """
        SELECT trade_ts, side, price, quantity, notional, trade_id
        FROM public_trades
        WHERE symbol = %(symbol)s
          AND trade_ts >= %(start)s
          AND trade_ts <= %(end)s
        ORDER BY trade_ts ASC, trade_id ASC
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    ).result_rows
    mapped = [
        {
            "trade_ts": r[0],
            "side": r[1],
            "price": r[2],
            "quantity": r[3],
            "notional": r[4],
            "trade_id": r[5],
        }
        for r in rows
    ]
    return normalize_trade_ticks(mapped)


def trades_in_window(
    ticks: Sequence[TradeTick],
    *,
    decision_time: datetime,
    window_seconds: float,
) -> list[TradeTick]:
    """Return trades in (decision_time - window, decision_time]."""
    t = ensure_utc(decision_time)
    left = t - timedelta(seconds=window_seconds)
    return [x for x in ticks if left < ensure_utc(x.trade_ts) <= t]


def wall_levels_from_snapshot(
    snap: Any,
    *,
    min_wall_notional: float = 0.0,
    include_touch: bool = True,
    include_near_buckets: bool = True,
    near_book_bps: float = 50.0,
) -> list[WallLevel]:
    """Extract candidate join levels from a SnapshotRecord-like object.

    Always prefers touch/near liquidity for trade matching. ``min_wall_notional``
    filters *wall-labelled* candidates but not best bid/ask touch levels.
    """
    levels: list[WallLevel] = []

    def add(side: str, wall: Any, label: str, *, enforce_min: bool) -> None:
        if wall is None:
            return
        price = _f(getattr(wall, "price", None))
        notional = _f(getattr(wall, "notional", None))
        if price <= 0:
            return
        if enforce_min and notional < min_wall_notional:
            return
        levels.append(WallLevel(side=side, price=price, notional=notional, label=label))

    def add_price(side: str, price: Any, notional: Any, label: str, *, enforce_min: bool) -> None:
        p = _f(price)
        n = _f(notional)
        if p <= 0:
            return
        if enforce_min and n < min_wall_notional:
            return
        levels.append(WallLevel(side=side, price=p, notional=n, label=label))

    # Touch levels — required for meaningful aggressor-to-book joins
    if include_touch:
        bb = getattr(snap, "best_bid", None)
        ba = getattr(snap, "best_ask", None)
        if bb is not None:
            # notional unknown at bare best — use nearest/bid bucket if available
            n = 0.0
            buckets = getattr(snap, "all_bid_buckets", None) or {}
            n = _f(buckets.get(bb, 0))
            add_price("Bid", bb, n, "best", enforce_min=False)
        if ba is not None:
            buckets = getattr(snap, "all_ask_buckets", None) or {}
            n = _f(buckets.get(ba, 0))
            add_price("Ask", ba, n, "best", enforce_min=False)

    add("Ask", getattr(snap, "nearest_ask", None), "nearest", enforce_min=False)
    add("Ask", getattr(snap, "dominant_ask", None), "dominant", enforce_min=True)
    add("Bid", getattr(snap, "nearest_bid", None), "nearest", enforce_min=False)
    add("Bid", getattr(snap, "dominant_bid", None), "dominant", enforce_min=True)
    for w in getattr(snap, "top_ask_walls", None) or []:
        add("Ask", w, "top", enforce_min=True)
    for w in getattr(snap, "top_bid_walls", None) or []:
        add("Bid", w, "top", enforce_min=True)
    for w in getattr(snap, "near_asks", None) or []:
        add("Ask", w, "near", enforce_min=False)
    for w in getattr(snap, "near_bids", None) or []:
        add("Bid", w, "near", enforce_min=False)

    mid = _f(getattr(snap, "mid_price", None) or 0)
    if include_near_buckets and mid > 0:
        for px, n in (getattr(snap, "all_ask_buckets", None) or {}).items():
            if _bps_distance(_f(px), mid) <= near_book_bps:
                add_price("Ask", px, n, "bucket", enforce_min=False)
        for px, n in (getattr(snap, "all_bid_buckets", None) or {}).items():
            if _bps_distance(_f(px), mid) <= near_book_bps:
                add_price("Bid", px, n, "bucket", enforce_min=False)

    # Deduplicate by (side, price) keeping max notional
    best: dict[tuple[str, float], WallLevel] = {}
    for lv in levels:
        key = (lv.side, round(lv.price, 10))
        prev = best.get(key)
        if prev is None or lv.notional > prev.notional:
            best[key] = lv
    return list(best.values())


def match_trade_to_wall(
    tick: TradeTick,
    walls: Sequence[WallLevel],
    *,
    level_join_bps: float,
) -> tuple[WallLevel | None, float | None, bool]:
    """Return (wall, distance_bps, ambiguous).

    Buy → Ask only; Sell → Bid only.
    """
    want = "Ask" if tick.side == "Buy" else "Bid"
    candidates: list[tuple[float, float, WallLevel]] = []
    for w in walls:
        if w.side != want:
            continue
        d = _bps_distance(tick.price, w.price)
        if d <= level_join_bps:
            # sort key: distance, -notional, price preference
            price_tie = w.price if want == "Ask" else -w.price
            candidates.append((d, -w.notional, w))
    if not candidates:
        return None, None, False
    candidates.sort(key=lambda x: (x[0], x[1], x[2].price if want == "Ask" else -x[2].price))
    best_d, _, best_w = candidates[0]
    ambiguous = False
    if len(candidates) > 1:
        d2 = candidates[1][0]
        if abs(d2 - best_d) < 1e-9:
            ambiguous = True
    return best_w, best_d, ambiguous


def join_trades_to_levels(
    ticks: Sequence[TradeTick],
    walls: Sequence[WallLevel],
    *,
    level_join_bps: float,
) -> tuple[dict[tuple[str, float], LevelJoinResult], dict[str, Any]]:
    """Join each trade to at most one wall. Returns per-wall joins + summary."""
    per_wall: dict[tuple[str, float], LevelJoinResult] = {}
    for w in walls:
        key = (w.side, round(w.price, 10))
        per_wall[key] = LevelJoinResult(
            wall_side=w.side,
            wall_price=w.price,
            wall_notional=w.notional,
        )

    matched = 0
    unmatched = 0
    ambiguous = 0
    distances: list[float] = []
    matched_buy_at_ask = 0.0
    matched_sell_at_bid = 0.0
    total_buy = 0.0
    total_sell = 0.0

    for tick in ticks:
        if tick.side == "Buy":
            total_buy += tick.notional
        else:
            total_sell += tick.notional
        wall, dist, amb = match_trade_to_wall(
            tick, walls, level_join_bps=level_join_bps
        )
        if wall is None:
            unmatched += 1
            continue
        key = (wall.side, round(wall.price, 10))
        if key not in per_wall:
            per_wall[key] = LevelJoinResult(
                wall_side=wall.side,
                wall_price=wall.price,
                wall_notional=wall.notional,
            )
        jr = per_wall[key]
        jr.matched_trade_notional += tick.notional
        jr.matched_trade_count += 1
        if amb:
            jr.ambiguous_trade_count += 1
            ambiguous += 1
        if dist is not None:
            distances.append(dist)
        matched += 1
        if tick.side == "Buy":
            matched_buy_at_ask += tick.notional
        else:
            matched_sell_at_bid += tick.notional

    mean_d = sum(distances) / len(distances) if distances else None
    max_d = max(distances) if distances else None
    for jr in per_wall.values():
        jr.ambiguity_rate = (
            jr.ambiguous_trade_count / jr.matched_trade_count
            if jr.matched_trade_count
            else 0.0
        )
        jr.level_join_quality = classify_join_quality(
            matched_count=jr.matched_trade_count,
            ambiguity_rate=jr.ambiguity_rate,
            wall_notional=jr.wall_notional,
            matched_notional=jr.matched_trade_notional,
        )
        if jr.matched_trade_count:
            jr.level_join_distance_bps_mean = mean_d
            jr.level_join_distance_bps_max = max_d

    total = matched + unmatched
    match_rate = matched / total if total else 0.0
    amb_rate = ambiguous / matched if matched else 0.0
    summary = {
        "matched_trade_count": matched,
        "unmatched_trade_count": unmatched,
        "ambiguous_trade_match_count": ambiguous,
        "match_rate": match_rate,
        "ambiguity_rate": amb_rate,
        "level_join_distance_bps_mean": mean_d,
        "level_join_distance_bps_max": max_d,
        "aggressive_buy_at_wall_notional": matched_buy_at_ask,
        "aggressive_sell_at_wall_notional": matched_sell_at_bid,
        "aggressive_buy_total_notional": total_buy,
        "aggressive_sell_total_notional": total_sell,
        "level_join_quality": classify_join_quality(
            matched_count=matched,
            ambiguity_rate=amb_rate,
            wall_notional=sum(w.notional for w in walls),
            matched_notional=matched_buy_at_ask + matched_sell_at_bid,
        ),
    }
    return per_wall, summary


def classify_join_quality(
    *,
    matched_count: int,
    ambiguity_rate: float,
    wall_notional: float,
    matched_notional: float,
) -> str:
    if matched_count < 2 or wall_notional <= 0:
        return JOIN_QUALITY_INSUFFICIENT
    coverage = matched_notional / max(wall_notional, EPSILON)
    if ambiguity_rate > 0.35:
        return JOIN_QUALITY_LOW
    if matched_count >= 5 and ambiguity_rate <= 0.15 and coverage >= 0.05:
        return JOIN_QUALITY_HIGH
    if matched_count >= 3 and ambiguity_rate <= 0.25:
        return JOIN_QUALITY_MEDIUM
    return JOIN_QUALITY_LOW


def near_notional(
    ticks: Sequence[TradeTick],
    *,
    side: str,
    ref_price: float | None,
    near_bps: float,
) -> float:
    if ref_price is None or ref_price <= 0:
        return 0.0
    total = 0.0
    for t in ticks:
        if t.side != side:
            continue
        if _bps_distance(t.price, ref_price) <= near_bps:
            total += t.notional
    return total


def compute_orderflow_window(
    ticks: Sequence[TradeTick],
    *,
    decision_time: datetime,
    window_seconds: float,
    walls: Sequence[WallLevel],
    nearest_ask: float | None,
    nearest_bid: float | None,
    level_join_bps: float,
    near_level_bps: float,
) -> dict[str, Any]:
    win = trades_in_window(
        ticks, decision_time=decision_time, window_seconds=window_seconds
    )
    buy_n = sum(t.notional for t in win if t.side == "Buy")
    sell_n = sum(t.notional for t in win if t.side == "Sell")
    buy_c = sum(1 for t in win if t.side == "Buy")
    sell_c = sum(1 for t in win if t.side == "Sell")
    delta = buy_n - sell_n
    total = buy_n + sell_n
    delta_ratio = delta / total if total > EPSILON else 0.0
    _, join_sum = join_trades_to_levels(win, walls, level_join_bps=level_join_bps)
    buy_near_ask = near_notional(
        win, side="Buy", ref_price=nearest_ask, near_bps=near_level_bps
    )
    sell_near_bid = near_notional(
        win, side="Sell", ref_price=nearest_bid, near_bps=near_level_bps
    )
    return {
        "window_seconds": window_seconds,
        "decision_time": ensure_utc(decision_time).isoformat(),
        "trade_count": len(win),
        "buy_trade_count": buy_c,
        "sell_trade_count": sell_c,
        "aggressive_buy_total_notional": buy_n,
        "aggressive_sell_total_notional": sell_n,
        "aggressive_buy_near_ask_notional": buy_near_ask,
        "aggressive_sell_near_bid_notional": sell_near_bid,
        "aggressive_buy_at_wall_notional": join_sum["aggressive_buy_at_wall_notional"],
        "aggressive_sell_at_wall_notional": join_sum["aggressive_sell_at_wall_notional"],
        "delta_notional": delta,
        "delta_ratio": delta_ratio,
        "matched_trade_count": join_sum["matched_trade_count"],
        "unmatched_trade_count": join_sum["unmatched_trade_count"],
        "ambiguous_trade_match_count": join_sum["ambiguous_trade_match_count"],
        "match_rate": join_sum["match_rate"],
        "ambiguity_rate": join_sum["ambiguity_rate"],
        "level_join_distance_bps_mean": join_sum["level_join_distance_bps_mean"],
        "level_join_distance_bps_max": join_sum["level_join_distance_bps_max"],
        "level_join_quality": join_sum["level_join_quality"],
    }


def price_path_progress(
    mids: Sequence[tuple[datetime, float]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, float | None]:
    """Progress over [start, end] using mid path points with ts in (start, end]."""
    start = ensure_utc(start)
    end = ensure_utc(end)
    pts = [(ensure_utc(ts), float(px)) for ts, px in mids if start < ensure_utc(ts) <= end]
    # also include last mid at/before start as baseline
    before = [(ensure_utc(ts), float(px)) for ts, px in mids if ensure_utc(ts) <= start]
    if not before:
        return {
            "price_progress_bps": None,
            "upside_progress_bps": None,
            "downside_progress_bps": None,
            "mid_return_bps": None,
            "high_extension_bps": None,
            "low_extension_bps": None,
        }
    base = before[-1][1]
    if base <= 0:
        return {
            "price_progress_bps": None,
            "upside_progress_bps": None,
            "downside_progress_bps": None,
            "mid_return_bps": None,
            "high_extension_bps": None,
            "low_extension_bps": None,
        }
    series = [base] + [p for _, p in pts]
    end_px = series[-1]
    hi = max(series)
    lo = min(series)
    mid_ret = (end_px - base) / base * 10_000.0
    up = (hi - base) / base * 10_000.0
    down = (base - lo) / base * 10_000.0
    return {
        "price_progress_bps": mid_ret,
        "upside_progress_bps": up,
        "downside_progress_bps": down,
        "mid_return_bps": mid_ret,
        "high_extension_bps": up,
        "low_extension_bps": down,
    }


def buy_efficiency_bps_per_1k(
    upside_progress_bps: float | None,
    buy_notional: float,
) -> float | None:
    if upside_progress_bps is None or buy_notional < EPSILON:
        return None
    return upside_progress_bps / max(buy_notional / 1000.0, EPSILON)


def sell_efficiency_bps_per_1k(
    downside_progress_bps: float | None,
    sell_notional: float,
) -> float | None:
    if downside_progress_bps is None or sell_notional < EPSILON:
        return None
    return downside_progress_bps / max(sell_notional / 1000.0, EPSILON)


def observed_depletion(prev_notional: float, curr_notional: float) -> float:
    return max(prev_notional - curr_notional, 0.0)


def wall_trade_coverage_ratio(
    matched_aggressive_notional: float,
    observed_depletion: float,
) -> tuple[float, float]:
    """Return (raw_ratio, capped_ratio at 5.0)."""
    raw = matched_aggressive_notional / max(observed_depletion, EPSILON)
    return raw, min(raw, 5.0)


def estimated_cancel_or_pull(
    observed_depletion: float,
    matched_aggressive_notional: float,
) -> float:
    """Proxy only — not proven cancel."""
    return max(observed_depletion - matched_aggressive_notional, 0.0)


def estimated_refill_notional(
    *,
    wall_notional_before: float,
    wall_notional_after: float,
    aggressive_buy_at_level: float,
) -> float:
    """Proxy refill: after - max(before - buys, 0)."""
    expected_min = max(wall_notional_before - aggressive_buy_at_level, 0.0)
    return max(wall_notional_after - expected_min, 0.0)


def classify_refill_quality(
    *,
    join_quality: str,
    snapshot_gap_seconds: float,
    reappear_distance_bps: float | None,
    coverage_ratio: float,
    ambiguity_rate: float,
    same_level: bool,
) -> str:
    if join_quality == JOIN_QUALITY_INSUFFICIENT:
        return REFILL_QUALITY_INSUFFICIENT
    if snapshot_gap_seconds > 90 or ambiguity_rate > 0.4:
        return REFILL_QUALITY_LOW
    if reappear_distance_bps is None:
        return REFILL_QUALITY_INSUFFICIENT
    if not same_level and reappear_distance_bps > 5:
        return REFILL_QUALITY_LOW
    if (
        join_quality == JOIN_QUALITY_HIGH
        and same_level
        and coverage_ratio >= 0.2
        and ambiguity_rate <= 0.15
        and snapshot_gap_seconds <= 45
    ):
        return REFILL_QUALITY_HIGH
    if join_quality in {JOIN_QUALITY_HIGH, JOIN_QUALITY_MEDIUM} and reappear_distance_bps <= 8:
        return REFILL_QUALITY_MEDIUM
    return REFILL_QUALITY_LOW


def find_level_notional(
    snap: Any,
    *,
    side: str,
    level_price: float,
    near_bps: float,
) -> tuple[float, float | None]:
    """Return (notional_at_or_near_level, matched_price)."""
    buckets = (
        getattr(snap, "all_ask_buckets", None)
        if side == "Ask"
        else getattr(snap, "all_bid_buckets", None)
    ) or {}
    best_price: float | None = None
    best_n = 0.0
    best_d = float("inf")
    for px, n in buckets.items():
        p = _f(px)
        d = _bps_distance(p, level_price)
        if d <= near_bps and d < best_d:
            best_d = d
            best_price = p
            best_n = _f(n)
    if best_price is not None:
        return best_n, best_price
    # fallback walls
    walls = wall_levels_from_snapshot(snap, min_wall_notional=0.0)
    for w in walls:
        if w.side != side:
            continue
        d = _bps_distance(w.price, level_price)
        if d <= near_bps and d < best_d:
            best_d = d
            best_price = w.price
            best_n = w.notional
    return best_n, best_price


def compute_ask_depletion_refill(
    prev_snap: Any,
    curr_snap: Any,
    *,
    ticks: Sequence[TradeTick],
    level_join_bps: float,
    near_level_bps: float,
    min_wall_notional: float,
) -> dict[str, Any]:
    """Causal ask-wall depletion / refill proxies between two consecutive snapshots."""
    prev_t = ensure_utc(prev_snap.timestamp)
    curr_t = ensure_utc(curr_snap.timestamp)
    gap = (curr_t - prev_t).total_seconds()
    ask_walls = [w for w in wall_levels_from_snapshot(prev_snap, min_wall_notional=min_wall_notional) if w.side == "Ask"]
    if not ask_walls:
        return {
            "absorption_level": None,
            "ask_wall_notional_before": None,
            "ask_wall_notional_after": None,
            "observed_wall_depletion_notional": 0.0,
            "aggressive_buy_at_level": 0.0,
            "wall_trade_coverage_ratio_raw": None,
            "wall_trade_coverage_ratio_capped": None,
            "estimated_cancel_or_pull_notional": 0.0,
            "estimated_refill_notional": 0.0,
            "refill_ratio": None,
            "same_level_reappear": False,
            "near_level_reappear": False,
            "reappear_distance_bps": None,
            "refill_estimate_quality": REFILL_QUALITY_INSUFFICIENT,
            "level_join_quality": JOIN_QUALITY_INSUFFICIENT,
            "snapshot_gap_seconds": gap,
        }

    # Focus on nearest ask at prev (primary absorption level)
    primary = None
    if getattr(prev_snap, "nearest_ask", None) is not None:
        primary = WallLevel(
            side="Ask",
            price=_f(prev_snap.nearest_ask.price),
            notional=_f(prev_snap.nearest_ask.notional),
            label="nearest",
        )
    if primary is None or primary.notional < min_wall_notional:
        primary = max(ask_walls, key=lambda w: w.notional)

    interval_ticks = trades_in_window(
        ticks, decision_time=curr_t, window_seconds=max(gap, 1.0)
    )
    # Restrict to trades after prev_t
    interval_ticks = [t for t in interval_ticks if ensure_utc(t.trade_ts) > prev_t]
    single_wall = [primary]
    _, join_sum = join_trades_to_levels(
        interval_ticks, single_wall, level_join_bps=level_join_bps
    )
    buy_at = join_sum["aggressive_buy_at_wall_notional"]
    after_n, after_px = find_level_notional(
        curr_snap, side="Ask", level_price=primary.price, near_bps=near_level_bps
    )
    deplete = observed_depletion(primary.notional, after_n if after_px is not None and _bps_distance(after_px, primary.price) < 1e-6 else after_n)
    # same-level: exact or within 0.5 bps
    reappear_d = None if after_px is None else _bps_distance(after_px, primary.price)
    same = reappear_d is not None and reappear_d <= 0.5
    near = reappear_d is not None and reappear_d <= near_level_bps
    # If wall vanished then reappeared near: use curr nearest ask
    if after_px is None and getattr(curr_snap, "nearest_ask", None) is not None:
        after_px = _f(curr_snap.nearest_ask.price)
        after_n = _f(curr_snap.nearest_ask.notional)
        reappear_d = _bps_distance(after_px, primary.price)
        same = reappear_d <= 0.5
        near = reappear_d <= near_level_bps
        deplete = observed_depletion(primary.notional, 0.0 if not near else after_n)

    raw_cov, cap_cov = wall_trade_coverage_ratio(buy_at, deplete if deplete > 0 else EPSILON)
    cancel_proxy = estimated_cancel_or_pull(deplete, buy_at)
    refill = estimated_refill_notional(
        wall_notional_before=primary.notional,
        wall_notional_after=after_n,
        aggressive_buy_at_level=buy_at,
    )
    refill_ratio = refill / max(buy_at, EPSILON) if buy_at > 0 else None
    quality = classify_refill_quality(
        join_quality=str(join_sum["level_join_quality"]),
        snapshot_gap_seconds=gap,
        reappear_distance_bps=reappear_d,
        coverage_ratio=cap_cov if deplete > 0 else 0.0,
        ambiguity_rate=float(join_sum["ambiguity_rate"]),
        same_level=same,
    )
    return {
        "absorption_level": primary.price,
        "ask_wall_notional_before": primary.notional,
        "ask_wall_notional_after": after_n,
        "observed_wall_depletion_notional": deplete,
        "aggressive_buy_at_level": buy_at,
        "wall_trade_coverage_ratio_raw": raw_cov if deplete > 0 else None,
        "wall_trade_coverage_ratio_capped": cap_cov if deplete > 0 else None,
        "estimated_cancel_or_pull_notional": cancel_proxy,
        "estimated_refill_notional": refill,
        "refill_ratio": refill_ratio,
        "same_level_reappear": same,
        "near_level_reappear": near and not same,
        "reappear_distance_bps": reappear_d,
        "refill_estimate_quality": quality,
        "level_join_quality": join_sum["level_join_quality"],
        "matched_trade_count": join_sum["matched_trade_count"],
        "ambiguous_trade_match_count": join_sum["ambiguous_trade_match_count"],
        "snapshot_gap_seconds": gap,
        "proxy_labels": [
            "PASSIVE_SELL_REFILL_PROXY" if refill > 0 and (same or near) else None,
            "REFILL_AT_ASK_PROXY" if refill > 0 else None,
        ],
    }

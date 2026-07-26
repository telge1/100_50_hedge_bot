"""Dynamic orderbook bucket aggregation and relative wall detection (research).

Bucket assignment semantics (Bybit-UI-oriented):
- Bids: floor(price / bucket_size) * bucket_size
- Asks: ceil(price / bucket_size) * bucket_size

Rationale: aggregation pulls liquidity toward the mid from each side, matching
typical exchange UI compressed books. Documented and unit-tested.

This module is read-only w.r.t. ClickHouse and does not touch the live recorder.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence

import orjson

from orderbook_analyse.config import load_settings
from orderbook_analyse.orderbook_replay import (
    BookLevelEvent,
    OrderBookReplayer,
    OrderBookState,
    ReplayError,
    clone_book,
    event_from_row,
    group_messages,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_LEVELS_EVAL = (Decimal("0.617"), Decimal("0.628"))  # evaluation only


@dataclass
class WallDetectorParams:
    wall_multiple_min: float = 3.0
    percentile_min: float = 90.0
    depth_share_min: float = 0.01  # >= 1% of visible side depth
    local_radius: int = 5
    distance_max_pct: float = 3.0
    cluster_max_gap_buckets: int = 1


@dataclass
class BucketStat:
    side: str
    bucket_price: Decimal
    bucket_size: Decimal
    qty: Decimal
    notional: Decimal
    level_count: int
    distance_pct: float
    local_median_notional: float = 0.0
    wall_multiple: float = 0.0
    percentile: float = 0.0
    z_or_mad_score: float = 0.0
    depth_share: float = 0.0
    strong_neighbor_count: int = 0
    is_wall: bool = False
    resolution: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution,
            "side": self.side,
            "bucket_price": format(self.bucket_price, "f"),
            "bucket_size": format(self.bucket_size, "f"),
            "bucket_qty": format(self.qty, "f"),
            "bucket_notional": format(self.notional, "f"),
            "exact_level_count": self.level_count,
            "distance_pct": round(self.distance_pct, 6),
            "local_median_notional": round(self.local_median_notional, 6),
            "wall_multiple": round(self.wall_multiple, 6),
            "percentile": round(self.percentile, 6),
            "mad_score": round(self.z_or_mad_score, 6),
            "depth_share": round(self.depth_share, 6),
            "strong_neighbor_count": self.strong_neighbor_count,
            "is_wall": self.is_wall,
        }


@dataclass
class WallCluster:
    side: str
    start_price: Decimal
    end_price: Decimal
    total_qty: Decimal
    total_notional: Decimal
    strongest_bucket: Decimal
    strongest_bucket_notional: Decimal
    bucket_count: int
    distance_min_pct: float
    distance_max_pct: float
    resolution: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution,
            "side": self.side,
            "start_price": format(self.start_price, "f"),
            "end_price": format(self.end_price, "f"),
            "total_qty": format(self.total_qty, "f"),
            "total_notional": format(self.total_notional, "f"),
            "strongest_bucket": format(self.strongest_bucket, "f"),
            "strongest_bucket_notional": format(self.strongest_bucket_notional, "f"),
            "bucket_count": self.bucket_count,
            "distance_min_pct": round(self.distance_min_pct, 6),
            "distance_max_pct": round(self.distance_max_pct, 6),
        }


def parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def choose_bucket_size(
    mid_price: Decimal | float,
    tick_size: Decimal | float,
    target_bps: float,
) -> Decimal:
    """Pick a nice bucket >= tick_size near mid * target_bps / 10000."""
    mid = Decimal(str(mid_price))
    tick = Decimal(str(tick_size))
    if mid <= 0:
        raise ValueError("mid_price must be > 0")
    if tick <= 0:
        raise ValueError("tick_size must be > 0")
    if target_bps <= 0:
        raise ValueError("target_bps must be > 0")

    raw = mid * Decimal(str(target_bps)) / Decimal("10000")
    if raw <= 0:
        return tick

    # Round onto the nice ladder without undershooting the target spacing.
    # Example: mid≈0.62, 10 bps → raw≈0.00062 → ceil-nice → 0.001
    # (nearest-nice alone would pick 0.0005).
    nice = _ceil_nice_step(raw)
    if nice < tick:
        nice = _ceil_nice_step(tick)
    multiples = (nice / tick).to_integral_value(rounding=ROUND_HALF_UP)
    if multiples < 1:
        multiples = Decimal("1")
    candidate = multiples * tick
    return _normalize_decimal(max(candidate, tick))


def _normalize_decimal(value: Decimal) -> Decimal:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return Decimal(text) if text else Decimal("0")


def _nice_mantissa_exponent(value: Decimal) -> tuple[Decimal, int]:
    if value <= 0:
        return Decimal("1"), 0
    exp = int(math.floor(math.log10(float(value))))
    scale = Decimal(10) ** exp
    mantissa = value / scale
    return mantissa, exp


def _nearest_nice_step(value: Decimal) -> Decimal:
    mantissa, exp = _nice_mantissa_exponent(value)
    choices = (Decimal("1"), Decimal("2"), Decimal("5"), Decimal("10"))
    best = choices[0]
    best_err = abs(mantissa - best)
    for c in choices[1:]:
        err = abs(mantissa - c)
        if err < best_err:
            best, best_err = c, err
    if best == Decimal("10"):
        return _normalize_decimal(Decimal("1") * (Decimal(10) ** (exp + 1)))
    return _normalize_decimal(best * (Decimal(10) ** exp))


def _ceil_nice_step(value: Decimal) -> Decimal:
    """Smallest nice step >= value."""
    mantissa, exp = _nice_mantissa_exponent(value)
    for c in (Decimal("1"), Decimal("2"), Decimal("5")):
        if mantissa <= c:
            return _normalize_decimal(c * (Decimal(10) ** exp))
    return _normalize_decimal(Decimal("1") * (Decimal(10) ** (exp + 1)))


def infer_tick_size(prices: Iterable[Decimal], *, fallback: Decimal | None = None) -> Decimal:
    """Robust tick inference from observed exact price differences."""
    uniq = sorted({Decimal(str(p)) for p in prices})
    if len(uniq) < 2:
        if fallback is not None:
            return fallback
        raise ValueError("need at least 2 distinct prices to infer tick size")
    diffs = [uniq[i] - uniq[i - 1] for i in range(1, len(uniq)) if uniq[i] > uniq[i - 1]]
    if not diffs:
        if fallback is not None:
            return fallback
        raise ValueError("no positive price diffs")
    # Mode of rounded diffs (most common spacing)
    counter = Counter(diffs)
    # Prefer smallest among top modes to avoid multiples of tick
    top_count = max(counter.values())
    candidates = [d for d, n in counter.items() if n == top_count]
    tick = min(candidates)
    # Also check GCD-like reduction via repeated min positive
    min_diff = min(diffs)
    if min_diff < tick and counter[min_diff] >= max(3, len(diffs) // 20):
        tick = min_diff
    return _normalize_decimal(tick)


def assign_bucket_price(price: Decimal, bucket_size: Decimal, side: str) -> Decimal:
    """Bid=floor, Ask=ceil multiples of bucket_size."""
    if bucket_size <= 0:
        raise ValueError("bucket_size must be > 0")
    ratio = price / bucket_size
    if side == "bid":
        mult = ratio.to_integral_value(rounding=ROUND_FLOOR)
    elif side == "ask":
        mult = ratio.to_integral_value(rounding=ROUND_CEILING)
    else:
        raise ValueError(f"invalid side={side}")
    return _normalize_decimal(mult * bucket_size)


def aggregate_book(
    book: OrderBookState,
    *,
    bucket_size: Decimal,
    mid: Decimal,
    distance_max_pct: float,
) -> dict[str, list[BucketStat]]:
    out: dict[str, list[BucketStat]] = {"bid": [], "ask": []}
    for side, levels in (("bid", book.bids), ("ask", book.asks)):
        buckets: dict[Decimal, list[tuple[Decimal, Decimal]]] = defaultdict(list)
        for price, qty in levels.items():
            if qty <= 0:
                continue
            dist = float(abs(price - mid) / mid * Decimal("100"))
            if dist > distance_max_pct:
                continue
            bprice = assign_bucket_price(price, bucket_size, side)
            buckets[bprice].append((price, qty))

        stats: list[BucketStat] = []
        for bprice, items in buckets.items():
            qty = sum((q for _, q in items), Decimal("0"))
            notional = sum((p * q for p, q in items), Decimal("0"))
            dist = float(abs(bprice - mid) / mid * Decimal("100"))
            stats.append(
                BucketStat(
                    side=side,
                    bucket_price=bprice,
                    bucket_size=bucket_size,
                    qty=qty,
                    notional=notional,
                    level_count=len(items),
                    distance_pct=dist,
                )
            )
        stats.sort(key=lambda s: s.bucket_price, reverse=(side == "bid"))
        out[side] = stats
    return out


def _percentile_rank(values: Sequence[float], value: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    # percent of values <= value
    le = sum(1 for v in sorted_vals if v <= value)
    return 100.0 * le / len(sorted_vals)


def _mad_score(values: Sequence[float], value: float) -> float:
    if len(values) < 2:
        return 0.0
    med = median(values)
    abs_dev = [abs(v - med) for v in values]
    mad = median(abs_dev)
    if mad == 0:
        return 0.0 if value == med else (999.0 if value > med else -999.0)
    return (value - med) / (1.4826 * mad)


def score_buckets(
    side_buckets: Sequence[BucketStat],
    *,
    params: WallDetectorParams,
    resolution: str,
) -> list[BucketStat]:
    if not side_buckets:
        return []
    ordered = sorted(side_buckets, key=lambda b: b.bucket_price)
    notionals = [float(b.notional) for b in ordered]
    total_notional = sum(notionals) or 1.0

    scored: list[BucketStat] = []
    for i, bucket in enumerate(ordered):
        left = max(0, i - params.local_radius)
        right = min(len(ordered), i + params.local_radius + 1)
        local = [notionals[j] for j in range(left, right) if j != i]
        local_med = float(median(local)) if local else 0.0
        wall_multiple = (
            float(bucket.notional) / local_med if local_med > 0 else float("inf")
            if float(bucket.notional) > 0
            else 0.0
        )
        pct = _percentile_rank(notionals, float(bucket.notional))
        mad = _mad_score(notionals, float(bucket.notional))
        depth_share = float(bucket.notional) / total_notional

        # Strong neighbors: adjacent buckets with wall_multiple proxy via share
        strong_neighbors = 0
        for j in (i - 1, i + 1):
            if 0 <= j < len(ordered):
                if float(ordered[j].notional) >= local_med * params.wall_multiple_min and local_med > 0:
                    strong_neighbors += 1

        is_wall = (
            wall_multiple >= params.wall_multiple_min
            and pct >= params.percentile_min
            and depth_share >= params.depth_share_min
            and math.isfinite(wall_multiple)
        )
        scored.append(
            BucketStat(
                side=bucket.side,
                bucket_price=bucket.bucket_price,
                bucket_size=bucket.bucket_size,
                qty=bucket.qty,
                notional=bucket.notional,
                level_count=bucket.level_count,
                distance_pct=bucket.distance_pct,
                local_median_notional=local_med,
                wall_multiple=wall_multiple if math.isfinite(wall_multiple) else 999.0,
                percentile=pct,
                z_or_mad_score=mad,
                depth_share=depth_share,
                strong_neighbor_count=strong_neighbors,
                is_wall=is_wall,
                resolution=resolution,
            )
        )
    return scored


def build_clusters(
    walls: Sequence[BucketStat],
    *,
    bucket_size: Decimal,
    max_gap_buckets: int = 1,
) -> list[WallCluster]:
    """Group wall candidates separated by at most ``max_gap_buckets`` empty buckets."""
    if not walls:
        return []
    by_side: dict[str, list[BucketStat]] = defaultdict(list)
    for w in walls:
        if w.is_wall:
            by_side[w.side].append(w)

    clusters: list[WallCluster] = []
    max_step = Decimal(max_gap_buckets + 1)  # adjacent=1, one hole=2 when max_gap=1
    for side, items in by_side.items():
        items = sorted(items, key=lambda x: x.bucket_price)
        if not items:
            continue
        group = [items[0]]
        for cur in items[1:]:
            prev = group[-1]
            gap_buckets = abs(cur.bucket_price - prev.bucket_price) / bucket_size
            if gap_buckets <= max_step:
                group.append(cur)
            else:
                clusters.append(_cluster_from_group(side, group))
                group = [cur]
        clusters.append(_cluster_from_group(side, group))
    return clusters


def _cluster_from_group(side: str, group: Sequence[BucketStat]) -> WallCluster:
    strongest = max(group, key=lambda g: g.notional)
    return WallCluster(
        side=side,
        start_price=min(g.bucket_price for g in group),
        end_price=max(g.bucket_price for g in group),
        total_qty=sum((g.qty for g in group), Decimal("0")),
        total_notional=sum((g.notional for g in group), Decimal("0")),
        strongest_bucket=strongest.bucket_price,
        strongest_bucket_notional=strongest.notional,
        bucket_count=len(group),
        distance_min_pct=min(g.distance_pct for g in group),
        distance_max_pct=max(g.distance_pct for g in group),
        resolution=group[0].resolution,
    )


def analyze_resolution(
    book: OrderBookState,
    *,
    bucket_size: Decimal,
    resolution: str,
    mid: Decimal,
    params: WallDetectorParams,
) -> dict[str, Any]:
    raw = aggregate_book(
        book, bucket_size=bucket_size, mid=mid, distance_max_pct=params.distance_max_pct
    )
    scored_bid = score_buckets(raw["bid"], params=params, resolution=resolution)
    scored_ask = score_buckets(raw["ask"], params=params, resolution=resolution)
    all_scored = scored_bid + scored_ask
    walls = [b for b in all_scored if b.is_wall]
    clusters = build_clusters(walls, bucket_size=bucket_size, max_gap_buckets=params.cluster_max_gap_buckets)
    for c in clusters:
        c.resolution = resolution

    total_notional = sum((float(b.notional) for b in all_scored), 0.0) or 1.0
    top_wall_notional = sum(float(w.notional) for w in walls)
    concentration = top_wall_notional / total_notional

    def top_walls(side: str, n: int = 5) -> list[BucketStat]:
        return sorted(
            [w for w in walls if w.side == side],
            key=lambda w: w.notional,
            reverse=True,
        )[:n]

    def strongest_cluster(side: str) -> WallCluster | None:
        side_clusters = [c for c in clusters if c.side == side]
        if not side_clusters:
            return None
        return max(side_clusters, key=lambda c: c.total_notional)

    return {
        "resolution": resolution,
        "bucket_size": format(bucket_size, "f"),
        "bucket_count": len(all_scored),
        "wall_candidate_count": len(walls),
        "candidates": all_scored,
        "walls": walls,
        "clusters": clusters,
        "top_bid_walls": top_walls("bid"),
        "top_ask_walls": top_walls("ask"),
        "strongest_bid_cluster": strongest_cluster("bid"),
        "strongest_ask_cluster": strongest_cluster("ask"),
        "liquidity_concentration_in_walls": concentration,
    }


def match_reference_level(
    level: Decimal,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    candidates: list[BucketStat] = analysis["candidates"]
    if not candidates:
        return {
            "reference_level": format(level, "f"),
            "resolution": analysis["resolution"],
            "nearest_bucket": None,
            "price_distance": None,
            "rank": None,
            "wall_multiple": None,
            "notional": None,
            "is_wall": False,
            "in_cluster": False,
        }
    nearest = min(candidates, key=lambda c: abs(c.bucket_price - level))
    side_sorted = sorted(
        [c for c in candidates if c.side == nearest.side],
        key=lambda c: c.notional,
        reverse=True,
    )
    rank = next((i + 1 for i, c in enumerate(side_sorted) if c.bucket_price == nearest.bucket_price), None)
    in_cluster = False
    for cluster in analysis["clusters"]:
        if cluster.side != nearest.side:
            continue
        if cluster.start_price <= nearest.bucket_price <= cluster.end_price:
            in_cluster = True
            break
    return {
        "reference_level": format(level, "f"),
        "resolution": analysis["resolution"],
        "nearest_bucket": format(nearest.bucket_price, "f"),
        "price_distance": format(abs(nearest.bucket_price - level), "f"),
        "rank": rank,
        "wall_multiple": round(nearest.wall_multiple, 6),
        "notional": format(nearest.notional, "f"),
        "is_wall": nearest.is_wall,
        "in_cluster": in_cluster,
        "side": nearest.side,
    }


@dataclass
class StabilitySample:
    as_of: datetime
    notional: Decimal | None  # None = absent


def compute_wall_stability(
    samples: dict[tuple[str, Decimal], list[StabilitySample]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for (side, bucket_price), series in samples.items():
        present = [s for s in series if s.notional is not None and s.notional > 0]
        notionals = [float(s.notional) for s in present if s.notional is not None]
        absent = len(series) - len(present)
        peak = max(notionals) if notionals else 0.0
        final = float(present[-1].notional) if present else 0.0
        drawdown = ((peak - final) / peak * 100.0) if peak > 0 else 0.0
        rows.append(
            {
                "side": side,
                "bucket_price": format(bucket_price, "f"),
                "first_observed": present[0].as_of.isoformat() if present else None,
                "samples_present": len(present),
                "samples_total": len(series),
                "presence_ratio": round(len(present) / len(series), 6) if series else 0.0,
                "min_notional": min(notionals) if notionals else None,
                "max_notional": max(notionals) if notionals else None,
                "median_notional": float(median(notionals)) if notionals else None,
                "final_notional": final if present else None,
                "max_drawdown_from_peak_pct": round(drawdown, 6),
                "number_of_zero_or_absent_samples": absent,
            }
        )
    return rows


class ReadOnlyClickHouse:
    """Thin read-only wrapper — only SELECT queries are exposed."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        upper = sql.lstrip().upper()
        if not upper.startswith("SELECT") and not upper.startswith("WITH"):
            raise RuntimeError("only SELECT/WITH queries are allowed in research mode")
        forbidden = (" INSERT ", " ALTER ", " DROP ", " DELETE ", " OPTIMIZE ", " TRUNCATE ", " CREATE ", " RENAME ")
        padded = f" {upper} "
        for token in forbidden:
            if token in padded:
                raise RuntimeError(f"forbidden statement token detected: {token.strip()}")
        return self._client.query(sql, parameters=parameters)

    def close(self) -> None:
        self._client.close()


def connect_readonly() -> ReadOnlyClickHouse:
    import clickhouse_connect

    settings = load_settings()
    client = clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_http_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
    )
    return ReadOnlyClickHouse(client)


def find_bootstrap_snapshot(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[datetime, int, int]:
    """Return (exchange_ts, update_id, cross_sequence) for bootstrap snapshot.

    Preference:
    1) first snapshot inside [start, end]
    2) else last snapshot strictly before start
    """
    rows = db.query(
        """
        SELECT exchange_ts, update_id, cross_sequence
        FROM orderbook_deltas
        WHERE symbol = %(symbol)s
          AND message_type = 'snapshot'
          AND exchange_ts >= %(start)s
          AND exchange_ts <= %(end)s
        ORDER BY exchange_ts ASC, cross_sequence ASC, update_id ASC
        LIMIT 1
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    ).result_rows
    if rows:
        ts = rows[0][0]
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        return ts, int(rows[0][1]), int(rows[0][2])

    rows = db.query(
        """
        SELECT exchange_ts, update_id, cross_sequence
        FROM orderbook_deltas
        WHERE symbol = %(symbol)s
          AND message_type = 'snapshot'
          AND exchange_ts < %(start)s
        ORDER BY exchange_ts DESC, cross_sequence DESC, update_id DESC
        LIMIT 1
        """,
        parameters={"symbol": symbol, "start": start},
    ).result_rows
    if not rows:
        raise ReplayError(
            f"no snapshot found for {symbol} in/before window "
            f"{start.isoformat()} .. {end.isoformat()}"
        )
    ts = rows[0][0]
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts, int(rows[0][1]), int(rows[0][2])


def load_events(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    snapshot_ts: datetime,
    snapshot_u: int,
    snapshot_seq: int,
    end: datetime,
) -> list[BookLevelEvent]:
    result = db.query(
        """
        SELECT
            exchange_ts, side, price, quantity, message_type,
            update_id, cross_sequence, level_index
        FROM orderbook_deltas
        WHERE symbol = %(symbol)s
          AND exchange_ts <= %(end)s
          AND (
                (message_type = 'snapshot'
                 AND update_id = %(snap_u)s
                 AND cross_sequence = %(snap_seq)s)
             OR (
                    cross_sequence > %(snap_seq)s
                    OR (cross_sequence = %(snap_seq)s AND update_id > %(snap_u)s)
                )
          )
          AND (
                exchange_ts >= %(snap_ts)s
                OR (message_type = 'snapshot' AND update_id = %(snap_u)s)
          )
        ORDER BY exchange_ts, cross_sequence, update_id, side, level_index
        """,
        parameters={
            "symbol": symbol,
            "snap_ts": snapshot_ts,
            "snap_u": snapshot_u,
            "snap_seq": snapshot_seq,
            "end": end,
        },
    )
    cols = list(result.column_names)
    events = [event_from_row(dict(zip(cols, row, strict=True))) for row in result.result_rows]
    # Keep only bootstrap snapshot + later messages (filter stray older rows)
    filtered: list[BookLevelEvent] = []
    for event in events:
        if event.message_type == "snapshot":
            if event.update_id == snapshot_u and event.cross_sequence == snapshot_seq:
                filtered.append(event)
            continue
        if (event.cross_sequence > snapshot_seq) or (
            event.cross_sequence == snapshot_seq and event.update_id > snapshot_u
        ):
            filtered.append(event)
    return filtered


def load_trade_context(
    db: ReadOnlyClickHouse, *, symbol: str, start: datetime, end: datetime
) -> dict[str, Any]:
    row = db.query(
        """
        SELECT
            count() AS trades,
            sumIf(quantity, side = 'Buy') AS buy_qty,
            sumIf(notional, side = 'Buy') AS buy_notional,
            sumIf(quantity, side = 'Sell') AS sell_qty,
            sumIf(notional, side = 'Sell') AS sell_notional,
            min(trade_ts) AS first_ts,
            max(trade_ts) AS last_ts,
            argMin(price, trade_ts) AS start_price,
            argMax(price, trade_ts) AS end_price
        FROM public_trades
        WHERE symbol = %(symbol)s
          AND trade_ts >= %(start)s
          AND trade_ts <= %(end)s
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    ).first_item
    buy_n = Decimal(str(row["buy_notional"] or 0))
    sell_n = Decimal(str(row["sell_notional"] or 0))
    start_px = row["start_price"]
    end_px = row["end_price"]
    change_pct = None
    if start_px is not None and end_px is not None and Decimal(str(start_px)) != 0:
        change_pct = float(
            (Decimal(str(end_px)) - Decimal(str(start_px))) / Decimal(str(start_px)) * 100
        )
    return {
        "trades": int(row["trades"] or 0),
        "buy_qty": format(Decimal(str(row["buy_qty"] or 0)), "f"),
        "buy_notional": format(buy_n, "f"),
        "sell_qty": format(Decimal(str(row["sell_qty"] or 0)), "f"),
        "sell_notional": format(sell_n, "f"),
        "trade_delta_notional_buy_minus_sell": format(buy_n - sell_n, "f"),
        "start_price": None if start_px is None else format(Decimal(str(start_px)), "f"),
        "end_price": None if end_px is None else format(Decimal(str(end_px)), "f"),
        "price_change_pct": change_pct,
    }


def load_oi_context(
    db: ReadOnlyClickHouse, *, symbol: str, start: datetime, end: datetime
) -> dict[str, Any]:
    row = db.query(
        """
        SELECT
            argMin(open_interest, exchange_ts) AS start_oi,
            argMax(open_interest, exchange_ts) AS end_oi,
            min(exchange_ts) AS min_ts,
            max(exchange_ts) AS max_ts,
            count() AS samples
        FROM ticker_samples
        WHERE symbol = %(symbol)s
          AND exchange_ts >= %(start)s
          AND exchange_ts <= %(end)s
          AND open_interest IS NOT NULL
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    ).first_item
    start_oi = row["start_oi"]
    end_oi = row["end_oi"]
    change = None
    change_pct = None
    if start_oi is not None and end_oi is not None:
        start_d = Decimal(str(start_oi))
        end_d = Decimal(str(end_oi))
        change = end_d - start_d
        if start_d != 0:
            change_pct = float(change / start_d * 100)
    return {
        "ticker_samples": int(row["samples"] or 0),
        "start_oi": None if start_oi is None else format(Decimal(str(start_oi)), "f"),
        "end_oi": None if end_oi is None else format(Decimal(str(end_oi)), "f"),
        "oi_change_abs": None if change is None else format(change, "f"),
        "oi_change_pct": change_pct,
    }


def load_liquidation_context(
    db: ReadOnlyClickHouse, *, symbol: str, start: datetime, end: datetime
) -> dict[str, Any]:
    row = db.query(
        """
        SELECT
            count() AS events,
            sumIf(quantity, side = 'Buy') AS buy_qty,
            sumIf(notional, side = 'Buy') AS buy_notional,
            sumIf(quantity, side = 'Sell') AS sell_qty,
            sumIf(notional, side = 'Sell') AS sell_notional
        FROM liquidations
        WHERE symbol = %(symbol)s
          AND liquidation_ts >= %(start)s
          AND liquidation_ts <= %(end)s
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    ).first_item
    return {
        "events": int(row["events"] or 0),
        "buy_qty": format(Decimal(str(row["buy_qty"] or 0)), "f"),
        "buy_notional": format(Decimal(str(row["buy_notional"] or 0)), "f"),
        "sell_qty": format(Decimal(str(row["sell_qty"] or 0)), "f"),
        "sell_notional": format(Decimal(str(row["sell_notional"] or 0)), "f"),
    }


def reconstruct_with_samples(
    events: Sequence[BookLevelEvent],
    *,
    sample_times: Sequence[datetime],
    end: datetime,
) -> tuple[OrderBookState, dict[datetime, OrderBookState]]:
    """Single-pass replay; capture clones at sample_times and final at end."""
    replayer = OrderBookReplayer()
    samples: dict[datetime, OrderBookState] = {}
    remaining = sorted(_ensure_aware(t) for t in sample_times)
    end_aware = _ensure_aware(end)

    for message_type, update_id, seq, ts, levels in group_messages(events):
        if ts > end_aware:
            break
        # Capture states for sample times that are <= current message ts,
        # using book state BEFORE applying this message if sample < ts.
        while remaining and remaining[0] < ts:
            # state as of remaining[0] is current book (all msgs with msg_ts <= sample)
            samples[remaining.pop(0)] = clone_book(replayer.book)
        replayer.apply_message(message_type, update_id, seq, ts, levels)
        while remaining and remaining[0] == ts:
            samples[remaining.pop(0)] = clone_book(replayer.book)

    while remaining:
        # leftover samples after last message but <= end
        t = remaining.pop(0)
        if t <= end_aware:
            samples[t] = clone_book(replayer.book)

    if not replayer.book.has_snapshot:
        raise ReplayError("no snapshot applied during reconstruction")
    return replayer.book, samples


def _ensure_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _bucket_short(b: BucketStat) -> dict[str, Any]:
    return {
        "side": b.side,
        "bucket_price": format(b.bucket_price, "f"),
        "notional": format(b.notional, "f"),
        "wall_multiple": round(b.wall_multiple, 4),
        "percentile": round(b.percentile, 2),
        "distance_pct": round(b.distance_pct, 4),
        "is_wall": b.is_wall,
    }


def run_detector(args: argparse.Namespace) -> dict[str, Any]:
    symbol = args.symbol
    start = parse_utc(args.start)
    if args.end:
        end = parse_utc(args.end)
    else:
        end = start + timedelta(minutes=float(args.lookback_minutes))
    if args.snapshot_at:
        snapshot_at = parse_utc(args.snapshot_at)
        if snapshot_at < start or snapshot_at > end:
            raise ValueError("--snapshot-at must lie within [start, end]")
        as_of = snapshot_at
    else:
        as_of = end

    params = WallDetectorParams(
        wall_multiple_min=float(args.wall_multiple_min),
        percentile_min=float(args.percentile_min),
        depth_share_min=float(args.depth_share_min),
        local_radius=int(args.local_radius),
        distance_max_pct=float(args.distance_max_pct),
    )

    multi_bps = [float(x.strip()) for x in str(args.multi_bps).split(",") if x.strip()]
    if args.bucket_mode == "multi" and not multi_bps:
        raise ValueError("--multi-bps empty")

    out_dir = Path(args.output_dir) if args.output_dir else (
        PROJECT_ROOT / "results" / f"dynamic_wall_detector_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    db = connect_readonly()
    try:
        snap_ts, snap_u, snap_seq = find_bootstrap_snapshot(
            db, symbol=symbol, start=start, end=as_of
        )
        events = load_events(
            db,
            symbol=symbol,
            snapshot_ts=snap_ts,
            snapshot_u=snap_u,
            snapshot_seq=snap_seq,
            end=as_of,
        )
        if not events:
            raise ReplayError("no orderbook events loaded for reconstruction")

        # Stability sample times: last 10 minutes before as_of, every 30s
        stability_start = max(start, as_of - timedelta(minutes=10))
        sample_times: list[datetime] = []
        t = stability_start
        while t <= as_of:
            sample_times.append(t)
            t += timedelta(seconds=30)

        book, timed_books = reconstruct_with_samples(
            events, sample_times=sample_times, end=as_of
        )
        mid = book.mid_price()
        if mid is None:
            raise ReplayError("cannot compute mid_price from reconstructed book")

        tick = infer_tick_size(list(book.bids) + list(book.asks))
        book_summary = book.summary()

        resolutions: list[tuple[str, Decimal]] = []
        if args.bucket_mode == "fixed":
            if args.fixed_bucket_size is None:
                raise ValueError("--fixed-bucket-size required for bucket-mode=fixed")
            resolutions.append(("fixed", Decimal(str(args.fixed_bucket_size))))
        elif args.bucket_mode == "auto":
            size = choose_bucket_size(mid, tick, float(args.target_bps))
            resolutions.append((f"auto_{int(args.target_bps)}bps", size))
        else:  # multi
            for bps in multi_bps:
                size = choose_bucket_size(mid, tick, bps)
                resolutions.append((f"auto_{int(bps) if bps == int(bps) else bps}bps", size))
            # Evaluation-only fixed 0.001 comparison (not a coin-specific detection rule)
            resolutions.append(("fixed_0.001_eval", Decimal("0.001")))

        analyses: dict[str, dict[str, Any]] = {}
        all_candidates: list[dict[str, Any]] = []
        all_clusters: list[dict[str, Any]] = []
        comparison_rows: list[dict[str, Any]] = []
        reference_rows: list[dict[str, Any]] = []

        for name, bucket_size in resolutions:
            analysis = analyze_resolution(
                book, bucket_size=bucket_size, resolution=name, mid=mid, params=params
            )
            analyses[name] = analysis
            for c in analysis["candidates"]:
                all_candidates.append(c.to_row())
            for cl in analysis["clusters"]:
                all_clusters.append(cl.to_row())

            top_bid = analysis["top_bid_walls"][:5]
            top_ask = analysis["top_ask_walls"][:5]
            sbc = analysis["strongest_bid_cluster"]
            sac = analysis["strongest_ask_cluster"]
            comparison_rows.append(
                {
                    "resolution": name,
                    "bucket_size": format(bucket_size, "f"),
                    "wall_candidates": analysis["wall_candidate_count"],
                    "top5_bid": "; ".join(
                        f"{format(w.bucket_price, 'f')}@{format(w.notional, 'f')}" for w in top_bid
                    ),
                    "top5_ask": "; ".join(
                        f"{format(w.bucket_price, 'f')}@{format(w.notional, 'f')}" for w in top_ask
                    ),
                    "strongest_bid_zone": None
                    if sbc is None
                    else f"{format(sbc.start_price, 'f')}-{format(sbc.end_price, 'f')} "
                    f"notional={format(sbc.total_notional, 'f')}",
                    "strongest_ask_zone": None
                    if sac is None
                    else f"{format(sac.start_price, 'f')}-{format(sac.end_price, 'f')} "
                    f"notional={format(sac.total_notional, 'f')}",
                    "liquidity_concentration_in_walls": round(
                        analysis["liquidity_concentration_in_walls"], 6
                    ),
                }
            )
            for ref in REFERENCE_LEVELS_EVAL:
                reference_rows.append(match_reference_level(ref, analysis))

        # Stability for top walls of preferred resolution (10bps or first auto / fixed)
        preferred_name = None
        for candidate in (f"auto_{int(args.target_bps)}bps", "auto_10bps", resolutions[0][0]):
            if candidate in analyses:
                preferred_name = candidate
                break
        assert preferred_name is not None
        preferred = analyses[preferred_name]
        preferred_bucket = Decimal(preferred["bucket_size"])
        top_for_stability = sorted(
            preferred["walls"], key=lambda w: w.notional, reverse=True
        )[: max(1, int(args.top))]

        stability_map: dict[tuple[str, Decimal], list[StabilitySample]] = {
            (w.side, w.bucket_price): [] for w in top_for_stability
        }
        for sample_ts in sample_times:
            state = timed_books.get(sample_ts)
            if state is None or state.mid_price() is None:
                for key in stability_map:
                    stability_map[key].append(StabilitySample(sample_ts, None))
                continue
            agg = aggregate_book(
                state,
                bucket_size=preferred_bucket,
                mid=state.mid_price(),  # type: ignore[arg-type]
                distance_max_pct=params.distance_max_pct,
            )
            lookup = {
                (b.side, b.bucket_price): b.notional
                for side in ("bid", "ask")
                for b in agg[side]
            }
            for key in stability_map:
                stability_map[key].append(
                    StabilitySample(sample_ts, lookup.get(key))
                )

        stability_rows = compute_wall_stability(stability_map)

        trade_ctx = load_trade_context(db, symbol=symbol, start=start, end=as_of)
        oi_ctx = load_oi_context(db, symbol=symbol, start=start, end=as_of)
        liq_ctx = load_liquidation_context(db, symbol=symbol, start=start, end=as_of)

        # Decision heuristic
        ref_hits = [
            r
            for r in reference_rows
            if r["resolution"] in {preferred_name, "fixed_0.001_eval", "fixed"}
            and r["is_wall"]
        ]
        decision = "DYNAMIC_BUCKET_PROMISING"
        limitations = [
            "Wall rules are diagnostic heuristics, not trading signals.",
            "No cancel-vs-execution classification is claimed from stability stats.",
            "Tick size inferred from observed prices; REST instrument info not required.",
            "fixed_0.001_eval is evaluation-only and not a coin-hardcoded detection rule.",
            "Reconstruction depends on recorded deltas; missing snapshot/gap aborts.",
        ]

        summary = {
            "symbol": symbol,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "as_of": as_of.isoformat(),
            "bootstrap_snapshot": {
                "exchange_ts": snap_ts.isoformat(),
                "update_id": snap_u,
                "cross_sequence": snap_seq,
            },
            "events_loaded": len(events),
            "book": book_summary,
            "tick_size": format(tick, "f"),
            "bucket_mode": args.bucket_mode,
            "resolutions": {
                name: {"bucket_size": format(size, "f")} for name, size in resolutions
            },
            "preferred_resolution": preferred_name,
            "params": asdict(params),
            "comparison": comparison_rows,
            "reference_levels_eval_only": [format(x, "f") for x in REFERENCE_LEVELS_EVAL],
            "reference_matches": reference_rows,
            "trade_context": trade_ctx,
            "oi_context": oi_ctx,
            "liquidation_context": liq_ctx,
            "top_walls_preferred": [_bucket_short(w) for w in preferred["walls"][: int(args.top)]],
            "clusters_preferred": [c.to_row() for c in preferred["clusters"]],
            "decision": decision,
            "limitations": limitations,
            "output_dir": str(out_dir),
        }

        # Refine decision based on reference proximity
        apt_eval = [
            r
            for r in reference_rows
            if r["resolution"] in {preferred_name, "fixed_0.001_eval"}
        ]
        close_hits = [
            r
            for r in apt_eval
            if r["nearest_bucket"] is not None
            and Decimal(r["price_distance"]) <= Decimal("0.002")
        ]
        if not close_hits:
            summary["decision"] = "INCONCLUSIVE"
            decision = "INCONCLUSIVE"

        write_csv(out_dir / "wall_candidates.csv", all_candidates)
        write_csv(out_dir / "wall_clusters.csv", all_clusters)
        write_csv(out_dir / "bucket_comparison.csv", comparison_rows)
        write_csv(out_dir / "reference_level_comparison.csv", reference_rows)
        write_csv(out_dir / "wall_stability.csv", stability_rows)
        (out_dir / "summary.json").write_bytes(
            orjson.dumps(summary, option=orjson.OPT_INDENT_2)
        )
        (out_dir / "REPORT.md").write_text(
            render_report(summary, analyses, preferred_name), encoding="utf-8"
        )
        return summary
    finally:
        db.close()


def render_report(
    summary: dict[str, Any],
    analyses: dict[str, dict[str, Any]],
    preferred_name: str,
) -> str:
    preferred = analyses[preferred_name]
    lines = [
        "# Dynamic Wall Detector Report",
        "",
        f"- Symbol: `{summary['symbol']}`",
        f"- Window: `{summary['start']}` → `{summary['end']}` (as_of `{summary['as_of']}`)",
        f"- Tick size (inferred): `{summary['tick_size']}`",
        f"- Mid: `{summary['book']['mid_price']}`  best bid/ask: "
        f"`{summary['book']['best_bid']}` / `{summary['book']['best_ask']}`",
        f"- Active levels: {summary['book']['active_levels']}",
        f"- Preferred resolution: `{preferred_name}` "
        f"(bucket `{analyses[preferred_name]['bucket_size']}`)",
        f"- Decision: **{summary['decision']}**",
        "",
        "## Dynamic bucket sizes",
        "",
    ]
    for name, meta in summary["resolutions"].items():
        lines.append(f"- `{name}`: bucket_size=`{meta['bucket_size']}`")

    lines += ["", "## Strongest walls (preferred)", ""]
    for w in preferred["walls"][:10]:
        lines.append(
            f"- {w.side} `{format(w.bucket_price, 'f')}` notional=`{format(w.notional, 'f')}` "
            f"mult=`{w.wall_multiple:.2f}` pct=`{w.percentile:.1f}` dist=`{w.distance_pct:.3f}%`"
        )

    lines += ["", "## Clusters (preferred)", ""]
    if not preferred["clusters"]:
        lines.append("- none")
    for c in preferred["clusters"]:
        lines.append(
            f"- {c.side} `{format(c.start_price, 'f')}`–`{format(c.end_price, 'f')}` "
            f"buckets={c.bucket_count} notional=`{format(c.total_notional, 'f')}` "
            f"strongest=`{format(c.strongest_bucket, 'f')}`"
        )

    lines += ["", "## Resolution comparison", ""]
    for row in summary["comparison"]:
        lines.append(
            f"- `{row['resolution']}` walls={row['wall_candidates']} "
            f"conc={row['liquidity_concentration_in_walls']:.3f} "
            f"bid_zone={row['strongest_bid_zone']} ask_zone={row['strongest_ask_zone']}"
        )

    lines += ["", "## Reference levels (evaluation only: 0.617 / 0.628)", ""]
    for row in summary["reference_matches"]:
        if row["resolution"] not in {preferred_name, "fixed_0.001_eval", "fixed"}:
            continue
        lines.append(
            f"- ref `{row['reference_level']}` @ `{row['resolution']}` → "
            f"nearest `{row['nearest_bucket']}` dist=`{row['price_distance']}` "
            f"rank={row['rank']} wall={row['is_wall']} cluster={row['in_cluster']} "
            f"mult={row['wall_multiple']}"
        )

    lines += [
        "",
        "## Market context",
        "",
        f"- Trades: {summary['trade_context']}",
        f"- OI: {summary['oi_context']}",
        f"- Liquidations: {summary['liquidation_context']}",
        "",
        "## Which resolution looks most Bybit-like?",
        "",
        "For APT near 0.62, `auto_10bps` typically yields bucket≈0.001 and should align "
        "with Bybit UI precision 0.001. Compare against `fixed_0.001_eval` for confirmation. "
        "`auto_5bps` is finer; `auto_25bps` merges zones more aggressively.",
        "",
        "## Limitations",
        "",
    ]
    for lim in summary["limitations"]:
        lines.append(f"- {lim}")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dynamic orderbook wall detector (research, read-only)")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", required=True, help="UTC start, e.g. 2026-07-26T09:16:29Z")
    p.add_argument("--end", default=None)
    p.add_argument("--lookback-minutes", type=float, default=30.0)
    p.add_argument("--bucket-mode", choices=["auto", "fixed", "multi"], default="multi")
    p.add_argument("--fixed-bucket-size", type=float, default=None)
    p.add_argument("--target-bps", type=float, default=10.0)
    p.add_argument("--multi-bps", default="5,10,25")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--distance-max-pct", type=float, default=3.0)
    p.add_argument("--snapshot-at", default=None)
    p.add_argument("--output-dir", default=None)
    p.add_argument("--wall-multiple-min", type=float, default=3.0)
    p.add_argument("--percentile-min", type=float, default=90.0)
    p.add_argument("--depth-share-min", type=float, default=0.01)
    p.add_argument("--local-radius", type=int, default=5)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        summary = run_detector(args)
        sys.stdout.buffer.write(orjson.dumps(summary, option=orjson.OPT_INDENT_2))
        sys.stdout.buffer.write(b"\n")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error("ERROR: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Pure causal microstructure feature helpers (no ClickHouse)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

DEPTH_BANDS_BPS: tuple[tuple[str, float, float], ...] = (
    ("0_5", 0.0, 5.0),
    ("5_10", 5.0, 10.0),
    ("10_25", 10.0, 25.0),
    ("25_50", 25.0, 50.0),
)

FLOW_WINDOWS_S = (5, 10, 30, 60)
PULL_LAGS_S = (5, 10, 30)
PERSIST_WINDOWS_S = (10, 30, 60)

PRE_TOUCH_OFFSETS_S = (-300, -120, -60, -30, -10)
POST_BREAK_OFFSETS_S = (5, 10, 20, 30, 60, 120)

EARLIEST_TIME_ORDER = (
    "PRE_TOUCH_5M",
    "PRE_TOUCH_2M",
    "PRE_TOUCH_1M",
    "PRE_TOUCH_30S",
    "PRE_TOUCH_10S",
    "FIRST_TOUCH",
    "FIRST_BREAK",
    "BREAK_PLUS_5S",
    "BREAK_PLUS_10S",
    "BREAK_PLUS_20S",
    "BREAK_PLUS_30S",
    "BREAK_PLUS_60S",
    "BREAK_PLUS_120S",
    "POSTMORTEM_PLUS_5M",
)


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def iso_z(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ensure_utc(ts).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def bps_distance(price: float, level: float) -> float | None:
    if level <= 0 or price is None:
        return None
    return (price - level) / level * 10_000.0


def assert_causal_cutoff(records: Sequence[Mapping[str, Any]], *, cutoff: datetime, ts_key: str = "ts") -> None:
    """Raise if any record timestamp is after cutoff."""
    cutoff = ensure_utc(cutoff)
    for r in records:
        ts = r.get(ts_key)
        if ts is None:
            continue
        if ensure_utc(ts) > cutoff:
            raise AssertionError(f"lookahead: {ts_key}={ts} > cutoff={cutoff}")


@dataclass(frozen=True)
class DirectionContext:
    level_type: str
    break_direction: str  # bearish | bullish
    support_side: str  # bid | ask (holds against break)
    break_aggressor: str  # Sell | Buy
    # signed flow: + = pressure in break direction


def direction_context(level_type: str) -> DirectionContext:
    lt = level_type.strip().lower()
    if lt in {"protected_low", "pl", "low"}:
        return DirectionContext("protected_low", "bearish", "bid", "Sell")
    if lt in {"protected_high", "ph", "high"}:
        return DirectionContext("protected_high", "bullish", "ask", "Buy")
    raise ValueError(f"unknown level_type={level_type}")


def signed_break_flow(*, buy_notional: float, sell_notional: float, break_direction: str) -> float:
    """Positive = pressure in break direction."""
    net = buy_notional - sell_notional  # buy-positive
    if break_direction == "bearish":
        return -net  # sell pressure positive
    if break_direction == "bullish":
        return net
    raise ValueError(break_direction)


def depth_in_level_band(
    levels: Mapping[Any, Any],
    *,
    side: str,
    level: float,
    lo_bps: float,
    hi_bps: float,
) -> float:
    """Notional in [lo_bps, hi_bps) from level, side-aware.

    bid: levels at or below level for support under PL; include slightly above.
    Distance measured as abs(price-level)/level in bps.
    """
    if level <= 0:
        return 0.0
    total = 0.0
    for px, qty in levels.items():
        p = float(px)
        q = float(qty)
        if q <= 0:
            continue
        dist = abs(p - level) / level * 10_000.0
        if dist < lo_bps or dist >= hi_bps:
            continue
        if side == "bid" and p > level * (1 + 25 / 10_000):
            continue
        if side == "ask" and p < level * (1 - 25 / 10_000):
            continue
        total += p * q
    return total


def depth_bands_near_level(book: Any, *, level: float) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, lo, hi in DEPTH_BANDS_BPS:
        out[f"bid_depth_bps_{name}"] = depth_in_level_band(book.bids, side="bid", level=level, lo_bps=lo, hi_bps=hi)
        out[f"ask_depth_bps_{name}"] = depth_in_level_band(book.asks, side="ask", level=level, lo_bps=lo, hi_bps=hi)
    return out


def imbalance(bid_depth: float, ask_depth: float) -> float | None:
    s = bid_depth + ask_depth
    if s <= 0:
        return None
    return bid_depth / s


def strongest_wall(levels: Mapping[Any, Any], *, level: float, max_bps: float = 50.0) -> dict[str, Any]:
    best_px, best_n = None, 0.0
    for px, qty in levels.items():
        p, q = float(px), float(qty)
        if q <= 0 or level <= 0:
            continue
        dist = abs(p - level) / level * 10_000.0
        if dist > max_bps:
            continue
        n = p * q
        if n > best_n:
            best_n, best_px = n, p
    return {
        "price": best_px,
        "notional": best_n if best_px is not None else 0.0,
        "distance_bps": None if best_px is None else (best_px - level) / level * 10_000.0,
    }


def wall_relative_size(wall_notional: float, local_notionals: Sequence[float]) -> float | None:
    vals = [float(x) for x in local_notionals if x is not None and float(x) > 0]
    if not vals or wall_notional <= 0:
        return None
    med = sorted(vals)[len(vals) // 2]
    if med <= 0:
        return None
    return wall_notional / med


def depth_change(current: float | None, lagged: float | None) -> float | None:
    if current is None or lagged is None:
        return None
    return current - lagged


def persistence_ratio(present_flags: Sequence[bool]) -> float | None:
    if not present_flags:
        return None
    return sum(1 for x in present_flags if x) / len(present_flags)


def book_snapshot_features(book: Any, *, level: float, ts: datetime, ctx: DirectionContext) -> dict[str, Any]:
    mid = book.mid_price()
    mid_f = float(mid) if mid is not None else None
    bb, ba = book.best_bid(), book.best_ask()
    bb_f = float(bb) if bb is not None else None
    ba_f = float(ba) if ba is not None else None
    spread_bps = None
    if mid_f and bb_f is not None and ba_f is not None and mid_f > 0:
        spread_bps = (ba_f - bb_f) / mid_f * 10_000.0

    bands = depth_bands_near_level(book, level=level)
    bid_near = sum(bands[k] for k in bands if k.startswith("bid_depth"))
    ask_near = sum(bands[k] for k in bands if k.startswith("ask_depth"))
    # tighter near depth 0-10 / 0-25
    bid_0_10 = bands["bid_depth_bps_0_5"] + bands["bid_depth_bps_5_10"]
    ask_0_10 = bands["ask_depth_bps_0_5"] + bands["ask_depth_bps_5_10"]
    bid_0_25 = bid_0_10 + bands["bid_depth_bps_10_25"]
    ask_0_25 = ask_0_10 + bands["ask_depth_bps_10_25"]

    bid_wall = strongest_wall(book.bids, level=level)
    ask_wall = strongest_wall(book.asks, level=level)
    support_near = bid_near if ctx.support_side == "bid" else ask_near
    break_near = ask_near if ctx.support_side == "bid" else bid_near

    dist = bps_distance(mid_f, level) if mid_f is not None else None
    # signed distance: positive = already beyond level in break direction
    if dist is None:
        signed_dist = None
    elif ctx.break_direction == "bearish":
        signed_dist = -dist  # below level → positive
    else:
        signed_dist = dist  # above level → positive

    beyond = False
    if bb_f is not None and ba_f is not None:
        if ctx.break_direction == "bearish":
            beyond = bb_f < level
        else:
            beyond = ba_f > level

    return {
        "ts": iso_z(ts),
        "mid": mid_f,
        "best_bid": bb_f,
        "best_ask": ba_f,
        "spread_bps": spread_bps,
        "distance_to_level_bps": dist,
        "signed_distance_beyond_bps": signed_dist,
        "bbo_beyond_level": int(beyond),
        **bands,
        "bid_depth_0_10": bid_0_10,
        "ask_depth_0_10": ask_0_10,
        "bid_depth_0_25": bid_0_25,
        "ask_depth_0_25": ask_0_25,
        "imbalance_0_10": imbalance(bid_0_10, ask_0_10),
        "imbalance_0_25": imbalance(bid_0_25, ask_0_25),
        "imbalance_near_all": imbalance(bid_near, ask_near),
        "support_near_depth": support_near,
        "break_side_near_depth": break_near,
        "support_minus_break_depth": support_near - break_near,
        "bid_wall_notional": bid_wall["notional"],
        "bid_wall_distance_bps": bid_wall["distance_bps"],
        "ask_wall_notional": ask_wall["notional"],
        "ask_wall_distance_bps": ask_wall["distance_bps"],
        "support_wall_notional": bid_wall["notional"] if ctx.support_side == "bid" else ask_wall["notional"],
        "break_wall_notional": ask_wall["notional"] if ctx.support_side == "bid" else bid_wall["notional"],
    }


@dataclass(frozen=True)
class SimpleTrade:
    trade_ts: datetime
    side: str
    price: float
    quantity: float
    notional: float


def filter_trades_causal(
    trades: Sequence[SimpleTrade],
    *,
    cutoff: datetime,
    start: datetime | None = None,
) -> list[SimpleTrade]:
    cutoff = ensure_utc(cutoff)
    start_u = ensure_utc(start) if start is not None else None
    out: list[SimpleTrade] = []
    for t in trades:
        ts = ensure_utc(t.trade_ts)
        if ts > cutoff:
            continue
        if start_u is not None and ts <= start_u:
            continue
        out.append(t)
    assert_causal_cutoff([{"ts": ensure_utc(t.trade_ts)} for t in out], cutoff=cutoff)
    return out


def aggregate_trade_flow(
    trades: Sequence[SimpleTrade],
    *,
    cutoff: datetime,
    window_s: float,
    break_direction: str,
) -> dict[str, Any]:
    """Trades in (cutoff - window, cutoff] — causal."""
    cutoff = ensure_utc(cutoff)
    start = cutoff - timedelta(seconds=window_s)
    wins = filter_trades_causal(trades, cutoff=cutoff, start=start)
    buys = [t for t in wins if str(t.side).lower() == "buy"]
    sells = [t for t in wins if str(t.side).lower() == "sell"]
    buy_n = sum(t.notional for t in buys)
    sell_n = sum(t.notional for t in sells)
    signed = signed_break_flow(buy_notional=buy_n, sell_notional=sell_n, break_direction=break_direction)
    ratio = (buy_n / sell_n) if sell_n > 0 else (math.inf if buy_n > 0 else None)
    prices = [t.price for t in wins]
    move_bps = None
    if len(prices) >= 2 and prices[0] > 0:
        move_bps = (prices[-1] - prices[0]) / prices[0] * 10_000.0
    # signed move in break direction
    if move_bps is None:
        signed_move = None
    elif break_direction == "bearish":
        signed_move = -move_bps
    else:
        signed_move = move_bps
    largest = max((t.notional for t in wins), default=0.0)
    return {
        f"flow_{int(window_s)}s_buy_notional": buy_n,
        f"flow_{int(window_s)}s_sell_notional": sell_n,
        f"flow_{int(window_s)}s_signed_break": signed,
        f"flow_{int(window_s)}s_buy_sell_ratio": None if ratio is None or math.isinf(ratio) else ratio,
        f"flow_{int(window_s)}s_n_trades": len(wins),
        f"flow_{int(window_s)}s_largest": largest,
        f"flow_{int(window_s)}s_price_move_bps": move_bps,
        f"flow_{int(window_s)}s_signed_move_bps": signed_move,
    }


def absorption_proxy(
    *,
    signed_break_flow_30s: float | None,
    signed_move_bps_30s: float | None,
    support_depth: float | None,
    support_depth_lag_30s: float | None,
) -> dict[str, Any]:
    """Continuous absorption proxies — no hard threshold decision."""
    refill = None
    if support_depth is not None and support_depth_lag_30s is not None:
        refill = support_depth - support_depth_lag_30s
    # high break-direction flow + little beyond move + support holds/refills
    flow = signed_break_flow_30s
    move = signed_move_bps_30s
    inefficiency = None
    if flow is not None and move is not None and abs(flow) > 1e-9:
        # move per unit flow; low = absorbed
        inefficiency = move / (abs(flow) / 1000.0)
    return {
        "abs_signed_break_flow_30s": flow,
        "abs_signed_move_bps_30s": move,
        "abs_support_depth": support_depth,
        "abs_support_refill_30s": refill,
        "abs_move_per_1k_flow": inefficiency,
    }


def timepoint_name_from_offsets(*, relative_to: str, offset_s: int) -> str:
    if relative_to == "FIRST_TOUCH":
        mapping = {
            -300: "PRE_TOUCH_5M",
            -120: "PRE_TOUCH_2M",
            -60: "PRE_TOUCH_1M",
            -30: "PRE_TOUCH_30S",
            -10: "PRE_TOUCH_10S",
            0: "FIRST_TOUCH",
        }
        return mapping.get(offset_s, f"TOUCH_PLUS_{offset_s}S")
    if relative_to == "FIRST_BREAK":
        mapping = {
            0: "FIRST_BREAK",
            5: "BREAK_PLUS_5S",
            10: "BREAK_PLUS_10S",
            20: "BREAK_PLUS_20S",
            30: "BREAK_PLUS_30S",
            60: "BREAK_PLUS_60S",
            120: "BREAK_PLUS_120S",
            300: "POSTMORTEM_PLUS_5M",
        }
        return mapping.get(offset_s, f"BREAK_PLUS_{offset_s}S")
    raise ValueError(relative_to)


def build_observation_schedule(
    *,
    first_touch: datetime | None,
    first_break: datetime | None,
) -> list[dict[str, Any]]:
    """Causal observation points. No post-break points without a real break."""
    schedule: list[dict[str, Any]] = []
    if first_touch is not None:
        ft = ensure_utc(first_touch)
        for off in PRE_TOUCH_OFFSETS_S:
            schedule.append(
                {
                    "timepoint": timepoint_name_from_offsets(relative_to="FIRST_TOUCH", offset_s=off),
                    "anchor": "FIRST_TOUCH",
                    "offset_s": off,
                    "ts": ft + timedelta(seconds=off),
                    "is_early_signal_candidate": True,
                }
            )
        schedule.append(
            {
                "timepoint": "FIRST_TOUCH",
                "anchor": "FIRST_TOUCH",
                "offset_s": 0,
                "ts": ft,
                "is_early_signal_candidate": True,
            }
        )
    if first_break is not None:
        fb = ensure_utc(first_break)
        schedule.append(
            {
                "timepoint": "FIRST_BREAK",
                "anchor": "FIRST_BREAK",
                "offset_s": 0,
                "ts": fb,
                "is_early_signal_candidate": True,
            }
        )
        for off in POST_BREAK_OFFSETS_S:
            schedule.append(
                {
                    "timepoint": timepoint_name_from_offsets(relative_to="FIRST_BREAK", offset_s=off),
                    "anchor": "FIRST_BREAK",
                    "offset_s": off,
                    "ts": fb + timedelta(seconds=off),
                    "is_early_signal_candidate": off <= 60,
                }
            )
        # postmortem +5m (not early)
        schedule.append(
            {
                "timepoint": "POSTMORTEM_PLUS_5M",
                "anchor": "FIRST_BREAK",
                "offset_s": 300,
                "ts": fb + timedelta(seconds=300),
                "is_early_signal_candidate": False,
            }
        )
    # dedupe by timepoint keeping first
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in schedule:
        if row["timepoint"] in seen:
            continue
        seen.add(row["timepoint"])
        out.append(row)
    return out


def derive_touch_break_from_trades(
    trades: Sequence[SimpleTrade],
    *,
    level: float,
    break_direction: str,
    window_start: datetime,
    window_end: datetime,
    touch_bps: float = 5.0,
) -> dict[str, Any]:
    """Derive first_touch / first_break from trades inside window (documented as derived)."""
    window_start, window_end = ensure_utc(window_start), ensure_utc(window_end)
    first_touch = None
    first_break = None
    for t in trades:
        ts = ensure_utc(t.trade_ts)
        if ts < window_start or ts > window_end:
            continue
        dist = abs(t.price - level) / level * 10_000.0 if level > 0 else None
        if first_touch is None and dist is not None and dist <= touch_bps:
            first_touch = ts
        if break_direction == "bearish":
            if first_break is None and t.price < level:
                first_break = ts
        else:
            if first_break is None and t.price > level:
                first_break = ts
        if first_touch is not None and first_break is not None:
            break
    return {
        "first_touch_ts": first_touch,
        "first_break_ts": first_break,
        "touch_break_source": "derived_from_trades",
    }

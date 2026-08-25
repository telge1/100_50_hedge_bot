"""Visible-range volume profile: bins, POC, 70% value area.

Not TPO. Bins are price rows of executed public trades.

Value-area method (documented API contract):
  1. POC = bin with highest total_base_volume.
  2. Tie-break: closer price_mid to volume-weighted average price (VWAP of
     bin mids by total_base_volume); then lower price_low.
  3. Value area starts at the POC bin and grows by repeatedly adding the
     neighbouring bin (above or below the current VA block) with more
     total_base_volume, until cumulative base volume >= 70% of the profile
     total. Neighbour tie: closer to VWAP, then lower price_low.

Binning:
  n equal-width rows on [price_min, price_max]. Index
  floor((price-min)/(max-min)*n), clamped to [0, n-1] so price_max is in
  the last bin. Single distinct price → one bin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

ALLOWED_ROWS = (24, 48, 72, 100)
AUTO_ROWS = 24
VALUE_AREA_FRACTION = Decimal("0.70")
MAX_RANGE_SECONDS = 7 * 24 * 3600
VOLUME_MODES = ("base", "quote")
NO_PUBLIC_TRADE_SYMBOLS = frozenset({"XAUUSDT"})

COVERAGE_FULL = "FULL"
COVERAGE_PARTIAL = "PARTIAL"
COVERAGE_NONE = "NONE"
COVERAGE_GAP_OPEN = "GAP_OPEN"

_ZERO = Decimal("0")


def _dec(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return _ZERO
    return Decimal(str(value))


def resolve_rows(rows: object) -> int:
    if rows is None or str(rows).strip().lower() in {"", "auto"}:
        return AUTO_ROWS
    try:
        n = int(rows)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_rows") from exc
    if n not in ALLOWED_ROWS:
        raise ValueError("invalid_rows")
    return n


def bin_index(price: Decimal, price_min: Decimal, price_max: Decimal, rows: int) -> int:
    if rows <= 0:
        raise ValueError("invalid_rows")
    if price_max == price_min:
        return 0
    span = price_max - price_min
    raw = (price - price_min) / span * rows
    idx = int(raw.to_integral_value(rounding="ROUND_FLOOR"))
    if idx < 0:
        return 0
    if idx >= rows:
        return rows - 1
    return idx


def bin_edges(price_min: Decimal, price_max: Decimal, rows: int, index: int) -> tuple[Decimal, Decimal, Decimal]:
    if rows <= 0:
        raise ValueError("invalid_rows")
    if price_max == price_min:
        mid = price_min
        return price_min, price_max, mid
    width = (price_max - price_min) / rows
    low = price_min + width * index
    high = price_max if index == rows - 1 else price_min + width * (index + 1)
    mid = (low + high) / 2
    return low, high, mid


@dataclass
class TradeRow:
    trade_id: str
    price: Decimal
    size: Decimal
    notional: Decimal
    side: str
    trade_ts: datetime

    def __post_init__(self) -> None:
        self.price = _dec(self.price)
        self.size = _dec(self.size)
        self.notional = _dec(self.notional)
        self.side = "Buy" if str(self.side) in {"Buy", "buy", "1"} else "Sell"


def dedupe_trades(rows: Sequence[TradeRow]) -> tuple[list[TradeRow], int, bool]:
    """Keep latest ingest order: last occurrence wins. Input should already be
    ingest-ordered if callers care; otherwise first-seen is kept unless a later
    row with the same trade_id appears (last wins).
    """
    chosen: dict[str, TradeRow] = {}
    conflicts = 0
    for row in rows:
        prev = chosen.get(row.trade_id)
        if prev is None:
            chosen[row.trade_id] = row
            continue
        if (
            prev.price != row.price
            or prev.size != row.size
            or prev.side != row.side
            or prev.notional != row.notional
        ):
            conflicts += 1
        chosen[row.trade_id] = row
    return list(chosen.values()), conflicts, len(rows) != len(chosen)


@dataclass
class Bin:
    index: int
    price_low: Decimal
    price_high: Decimal
    price_mid: Decimal
    buy_base: Decimal = _ZERO
    sell_base: Decimal = _ZERO
    buy_quote: Decimal = _ZERO
    sell_quote: Decimal = _ZERO
    buy_count: int = 0
    sell_count: int = 0
    in_value_area: bool = False
    is_poc: bool = False

    @property
    def total_base(self) -> Decimal:
        return self.buy_base + self.sell_base

    @property
    def total_quote(self) -> Decimal:
        return self.buy_quote + self.sell_quote

    @property
    def delta_base(self) -> Decimal:
        return self.buy_base - self.sell_base

    @property
    def delta_quote(self) -> Decimal:
        return self.buy_quote - self.sell_quote

    def volume(self, mode: str) -> Decimal:
        return self.total_quote if mode == "quote" else self.total_base

    def buy_volume(self, mode: str) -> Decimal:
        return self.buy_quote if mode == "quote" else self.buy_base

    def sell_volume(self, mode: str) -> Decimal:
        return self.sell_quote if mode == "quote" else self.sell_base

    def delta(self, mode: str) -> Decimal:
        return self.delta_quote if mode == "quote" else self.delta_base

    def to_api(self, mode: str) -> dict[str, Any]:
        return {
            "price_low": float(self.price_low),
            "price_high": float(self.price_high),
            "price_mid": float(self.price_mid),
            "buy_volume": float(self.buy_volume(mode)),
            "sell_volume": float(self.sell_volume(mode)),
            "total_volume": float(self.volume(mode)),
            "delta": float(self.delta(mode)),
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
            "total_count": self.buy_count + self.sell_count,
            "in_value_area": self.in_value_area,
            "is_poc": self.is_poc,
            "buy_base_volume": float(self.buy_base),
            "sell_base_volume": float(self.sell_base),
            "total_base_volume": float(self.total_base),
            "buy_quote_volume": float(self.buy_quote),
            "sell_quote_volume": float(self.sell_quote),
            "total_quote_volume": float(self.total_quote),
            "delta_base": float(self.delta_base),
            "delta_quote": float(self.delta_quote),
        }


def _vwap(bins: Sequence[Bin]) -> Decimal | None:
    num = _ZERO
    den = _ZERO
    for b in bins:
        if b.total_base <= 0:
            continue
        num += b.price_mid * b.total_base
        den += b.total_base
    if den <= 0:
        return None
    return num / den


def _closer(a: Bin, b: Bin, target: Decimal | None) -> Bin:
    if target is None:
        return a if a.price_low <= b.price_low else b
    da = abs(a.price_mid - target)
    db = abs(b.price_mid - target)
    if da < db:
        return a
    if db < da:
        return b
    return a if a.price_low <= b.price_low else b


def pick_poc(bins: Sequence[Bin]) -> Bin | None:
    live = [b for b in bins if b.total_base > 0]
    if not live:
        return None
    vwap = _vwap(live)
    best = live[0]
    for b in live[1:]:
        if b.total_base > best.total_base:
            best = b
        elif b.total_base == best.total_base:
            best = _closer(best, b, vwap)
    return best


def expand_value_area(bins: Sequence[Bin], poc: Bin | None) -> set[int]:
    if not bins or poc is None:
        return set()
    total = sum((b.total_base for b in bins), _ZERO)
    if total <= 0:
        return {poc.index}
    target = total * VALUE_AREA_FRACTION
    by_idx = {b.index: b for b in bins}
    chosen = {poc.index}
    acc = poc.total_base
    lo = hi = poc.index
    vwap = _vwap(bins)
    while acc < target:
        below = by_idx.get(lo - 1)
        above = by_idx.get(hi + 1)
        if below is None and above is None:
            break
        if below is None:
            nxt = above
        elif above is None:
            nxt = below
        elif above.total_base > below.total_base:
            nxt = above
        elif below.total_base > above.total_base:
            nxt = below
        else:
            nxt = _closer(below, above, vwap)
        chosen.add(nxt.index)
        acc += nxt.total_base
        lo = min(lo, nxt.index)
        hi = max(hi, nxt.index)
    return chosen


def classify_coverage(
    *,
    requested_start: datetime,
    requested_end: datetime,
    coverage_start: datetime | None,
    coverage_end: datetime | None,
    gap_start: datetime | None,
    gap_end: datetime | None,
    trade_count: int,
) -> tuple[str, str]:
    """Return (coverage_code, reason)."""
    if trade_count <= 0:
        if gap_start and gap_end and requested_end > gap_start and requested_start < gap_end:
            return COVERAGE_GAP_OPEN, "requested_window_inside_or_touching_cutover_gap"
        if coverage_start is None or coverage_end is None:
            return COVERAGE_NONE, "no_public_trades"
        if requested_end <= coverage_start or requested_start >= coverage_end:
            return COVERAGE_NONE, "requested_window_outside_coverage"
        return COVERAGE_NONE, "no_trades_in_window"

    gap_hit = bool(
        gap_start and gap_end and requested_end > gap_start and requested_start < gap_end
    )
    if coverage_start is None or coverage_end is None:
        code = COVERAGE_GAP_OPEN if gap_hit else COVERAGE_PARTIAL
        return code, "coverage_bounds_unknown"

    covers_left = requested_start >= coverage_start
    covers_right = requested_end <= coverage_end
    if gap_hit:
        return COVERAGE_GAP_OPEN, "cutover_gap_overlaps_requested_window"
    if covers_left and covers_right:
        return COVERAGE_FULL, "requested_window_inside_coverage"
    return COVERAGE_PARTIAL, "requested_window_clipped_to_coverage"


def coverage_label(code: str) -> str:
    return {
        COVERAGE_FULL: "Profil vollständig",
        COVERAGE_PARTIAL: "Profil teilweise",
        COVERAGE_NONE: "Keine Public-Trade-Daten",
        COVERAGE_GAP_OPEN: "Cutover-Lücke vorhanden",
    }.get(code, code)


def profile_from_bins(
    bins: Sequence[Bin],
    *,
    rows_requested: int,
    volume_mode: str,
    price_min: Decimal | None,
    price_max: Decimal | None,
) -> dict[str, Any]:
    if volume_mode not in VOLUME_MODES:
        raise ValueError("invalid_volume_mode")
    poc = pick_poc(bins)
    va_idx = expand_value_area(bins, poc)
    for b in bins:
        b.is_poc = poc is not None and b.index == poc.index
        b.in_value_area = b.index in va_idx
    va_vol = sum((bins[i].total_base for i in va_idx), _ZERO) if bins else _ZERO
    total_base = sum((b.total_base for b in bins), _ZERO)
    total_quote = sum((b.total_quote for b in bins), _ZERO)
    buy_base = sum((b.buy_base for b in bins), _ZERO)
    sell_base = sum((b.sell_base for b in bins), _ZERO)
    buy_quote = sum((b.buy_quote for b in bins), _ZERO)
    sell_quote = sum((b.sell_quote for b in bins), _ZERO)
    count = sum(b.buy_count + b.sell_count for b in bins)
    va_bins = [bins[i] for i in sorted(va_idx)] if va_idx else []
    vah = float(va_bins[-1].price_high) if va_bins else None
    val = float(va_bins[0].price_low) if va_bins else None
    mode_buy = buy_quote if volume_mode == "quote" else buy_base
    mode_sell = sell_quote if volume_mode == "quote" else sell_base
    return {
        "rows_requested": rows_requested,
        "rows_returned": len(bins),
        "volume_mode": volume_mode,
        "bins": [b.to_api(volume_mode) for b in bins],
        "poc": poc.to_api(volume_mode) if poc else None,
        "poc_price": float(poc.price_mid) if poc else None,
        "vah": vah,
        "val": val,
        "value_area_volume": float(va_vol),
        "value_area_percent_actual": float((va_vol / total_base * 100) if total_base else 0),
        "value_area_method": "poc_expand_70pct_base_volume",
        "total_buy_volume": float(mode_buy),
        "total_sell_volume": float(mode_sell),
        "total_volume": float(mode_buy + mode_sell),
        "total_delta": float(mode_buy - mode_sell),
        "total_buy_base_volume": float(buy_base),
        "total_sell_base_volume": float(sell_base),
        "total_base_volume": float(total_base),
        "total_buy_quote_volume": float(buy_quote),
        "total_sell_quote_volume": float(sell_quote),
        "total_quote_volume": float(total_quote),
        "total_delta_base": float(buy_base - sell_base),
        "total_delta_quote": float(buy_quote - sell_quote),
        "total_trade_count": count,
        "price_min": float(price_min) if price_min is not None else None,
        "price_max": float(price_max) if price_max is not None else None,
    }


def empty_profile(*, rows: int, volume_mode: str) -> dict[str, Any]:
    return profile_from_bins(
        [],
        rows_requested=rows,
        volume_mode=volume_mode,
        price_min=None,
        price_max=None,
    )


def build_profile(
    trades: Sequence[TradeRow],
    *,
    rows: int,
    volume_mode: str = "base",
) -> dict[str, Any]:
    if volume_mode not in VOLUME_MODES:
        raise ValueError("invalid_volume_mode")
    rows = int(rows)
    if rows not in ALLOWED_ROWS:
        raise ValueError("invalid_rows")

    if not trades:
        return empty_profile(rows=rows, volume_mode=volume_mode)

    prices = [t.price for t in trades]
    pmin = min(prices)
    pmax = max(prices)
    n = 1 if pmin == pmax else rows
    bins = make_empty_bins(pmin, pmax, n)
    for t in trades:
        idx = bin_index(t.price, pmin, pmax, n)
        b = bins[idx]
        if t.side == "Buy":
            b.buy_base += t.size
            b.buy_quote += t.notional
            b.buy_count += 1
        else:
            b.sell_base += t.size
            b.sell_quote += t.notional
            b.sell_count += 1
    return profile_from_bins(
        bins,
        rows_requested=rows,
        volume_mode=volume_mode,
        price_min=pmin,
        price_max=pmax,
    )


def make_empty_bins(price_min: Decimal, price_max: Decimal, rows: int) -> list[Bin]:
    out: list[Bin] = []
    for i in range(rows):
        low, high, mid = bin_edges(price_min, price_max, rows, i)
        out.append(Bin(index=i, price_low=low, price_high=high, price_mid=mid))
    return out


def unix_utc(ts: datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp())

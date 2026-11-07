"""Pure geometry for manual line ↔ 5m candle touch probes (no dashboard imports)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

Classification = Literal["TOUCH", "NEAR_0_10", "NEAR_0_20", "NO_TOUCH"]
AnchorStatus = Literal[
    "HLINE",
    "BEFORE_FIRST_ANCHOR",
    "BETWEEN_ANCHORS",
    "AFTER_SECOND_ANCHOR",
    "ON_EXTENDED_RAY",
    "OUTSIDE_FINITE_SEGMENT",
    "NOT_EVALUATED",
]

BAR_SEC_5M = 300


def utc_ts(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def fmt_utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass(frozen=True)
class LineDef:
    drawing_id: str
    drawing_type: str
    symbol: str
    extend_right: bool
    # trend anchors (unix sec, price)
    t1: Optional[float] = None
    p1: Optional[float] = None
    t2: Optional[float] = None
    p2: Optional[float] = None
    # hline
    price: Optional[float] = None

    @property
    def t_lo(self) -> Optional[float]:
        if self.t1 is None or self.t2 is None:
            return None
        return float(min(self.t1, self.t2))

    @property
    def t_hi(self) -> Optional[float]:
        if self.t1 is None or self.t2 is None:
            return None
        return float(max(self.t1, self.t2))

    def slope(self) -> Optional[float]:
        if self.t1 is None or self.t2 is None or self.p1 is None or self.p2 is None:
            return None
        dt = float(self.t2) - float(self.t1)
        if abs(dt) < 1e-12:
            return None
        return (float(self.p2) - float(self.p1)) / dt

    def price_at(self, t: float) -> Optional[float]:
        if self.drawing_type == "hline":
            return float(self.price) if self.price is not None else None
        if self.t1 is None or self.p1 is None or self.t2 is None or self.p2 is None:
            return None
        m = self.slope()
        if m is None:
            return float(self.p1)
        return float(self.p1) + m * (float(t) - float(self.t1))

    def anchor_status(self, candle_open: float) -> AnchorStatus:
        if self.drawing_type == "hline":
            return "HLINE"
        t_lo, t_hi = self.t_lo, self.t_hi
        if t_lo is None or t_hi is None:
            return "NOT_EVALUATED"
        t = float(candle_open)
        if t < t_lo:
            return "BEFORE_FIRST_ANCHOR"
        if t_lo <= t <= t_hi:
            return "BETWEEN_ANCHORS"
        if self.extend_right:
            return "ON_EXTENDED_RAY"
        return "AFTER_SECOND_ANCHOR"

    def active_for_candle(self, candle_open: float) -> bool:
        """Finite segments: no evaluation outside [t_lo, t_hi]. Hline + extend_right always on."""
        if self.drawing_type == "hline":
            return self.price is not None
        st = self.anchor_status(candle_open)
        if st in ("BEFORE_FIRST_ANCHOR", "AFTER_SECOND_ANCHOR", "NOT_EVALUATED"):
            return False
        return True


def line_price_range_over_interval(line: LineDef, t_start: float, t_end: float) -> Optional[tuple[float, float]]:
    if line.drawing_type == "hline":
        p = line.price
        if p is None:
            return None
        return (float(p), float(p))
    p0 = line.price_at(t_start)
    p1 = line.price_at(t_end)
    if p0 is None or p1 is None:
        return None
    return (min(p0, p1), max(p0, p1))


def candle_interval_intersects_line(low: float, high: float, p_min: float, p_max: float) -> bool:
    return not (p_max < low or p_min > high)


def distance_pct_at_open(line_price: float, low: float, high: float) -> float:
    if line_price <= 0:
        return 999.0
    if low <= line_price <= high:
        return 0.0
    nearest = low if abs(low - line_price) <= abs(high - line_price) else high
    return abs(nearest - line_price) / line_price * 100.0


def classify_distance(distance_pct: float, touching: bool) -> Classification:
    if touching:
        return "TOUCH"
    # inclusive thresholds (0.10% / 0.20%) with tiny float tolerance
    if distance_pct <= 0.10 + 1e-9:
        return "NEAR_0_10"
    if distance_pct <= 0.20 + 1e-9:
        return "NEAR_0_20"
    return "NO_TOUCH"


def classify_candle_interval(
    line: LineDef,
    candle_open: float,
    high: float,
    low: float,
) -> tuple[Classification, float, float, str, bool]:
    """
    Returns (classification, line_price_at_open, distance_pct, method, touching).
    Primary: interval overlap for diagonal; distance uses min gap candle band vs line price band.
    """
    t_end = float(candle_open) + BAR_SEC_5M
    open_p = line.price_at(candle_open)
    if open_p is None:
        return "NO_TOUCH", 0.0, 999.0, "invalid", False

    if not line.active_for_candle(candle_open):
        return "NO_TOUCH", open_p, 999.0, "inactive", False

    pr = line_price_range_over_interval(line, float(candle_open), t_end)
    if pr is None:
        return "NO_TOUCH", open_p, 999.0, "invalid", False
    p_min, p_max = pr
    touching = candle_interval_intersects_line(low, high, p_min, p_max)

    open_touch = distance_pct_at_open(open_p, low, high) == 0.0
    if touching:
        return "TOUCH", open_p, 0.0, "interval", True

    if low <= open_p <= high:
        gap = 0.0
    else:
        gaps = []
        if p_min > high:
            gaps.append(p_min - high)
        elif p_max < low:
            gaps.append(low - p_max)
        else:
            ref = open_p
            nearest = low if abs(low - ref) <= abs(high - ref) else high
            gaps.append(abs(nearest - ref))
        gap = min(gaps) if gaps else abs(open_p - (low if abs(low - open_p) <= abs(high - open_p) else high))
    dist_pct = (gap / open_p * 100.0) if open_p > 0 else 999.0
    cls = classify_distance(dist_pct, False)
    method = "interval"
    if open_touch and cls != "TOUCH":
        method = "open_mismatch_interval"
    return cls, open_p, dist_pct, method, False

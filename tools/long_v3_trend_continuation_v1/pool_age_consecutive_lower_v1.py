"""Consecutive ACTIVE depth2 age on exact lower atomic bands (D100-compatible semantics)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


class SnapshotAtClose(Protocol):
    def has_close(self, decision_close_utc: str) -> bool: ...

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]: ...


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lower_band_active_depth2(
    snapshot: dict[str, Any],
    pool_lo: float,
    pool_hi: float,
    *,
    min_unique_origins: int = 2,
) -> bool:
    for b in snapshot.get("lower_bands") or []:
        if str(b.get("pool_status") or "").upper() != "ACTIVE":
            continue
        lo, hi = float(b["lower_price"]), float(b["upper_price"])
        if abs(lo - pool_lo) > 1e-11 or abs(hi - pool_hi) > 1e-11:
            continue
        if int(b.get("unique_origin_count") or 0) < min_unique_origins:
            return False
        return True
    return False


def consecutive_active_streak_lower(
    session: SnapshotAtClose,
    *,
    decision_close_utc: str,
    pool_lo: float,
    pool_hi: float,
    min_unique_origins: int = 2,
    max_lookback_bars: int = 5000,
) -> tuple[int, bool, str | None]:
    """Exact band identity; streak breaks if band inactive or origins < min at any bar."""
    t = _parse_utc(decision_close_utc)
    streak = 0
    for _ in range(max_lookback_bars):
        close = _iso(t)
        if not session.has_close(close):
            if streak == 0:
                return 0, False, "AGE_HISTORY_MISSING_AT_DECISION"
            return streak, False, "AGE_WARMUP_HISTORY_INCOMPLETE"
        snap = session.collect_at_close(close)
        if not lower_band_active_depth2(
            snap, pool_lo, pool_hi, min_unique_origins=min_unique_origins
        ):
            break
        streak += 1
        t -= timedelta(minutes=5)
    return streak, True, None


def streak_start_close_utc(decision_close_utc: str, streak: int) -> str:
    t = _parse_utc(decision_close_utc)
    t -= timedelta(minutes=5 * max(streak - 1, 0))
    return _iso(t)

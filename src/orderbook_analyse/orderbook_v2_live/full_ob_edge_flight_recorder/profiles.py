"""Causal completed-profile edges for Full-OB flight recorder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.watcher import EdgeLevel


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def last_completed_window(now: datetime, *, window_minutes: int = 30) -> tuple[datetime, datetime]:
    now = as_utc(now)
    w = int(window_minutes)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = int((now - epoch).total_seconds())
    boundary = elapsed - (elapsed % (w * 60))
    end = epoch + timedelta(seconds=boundary)
    if end >= now:
        end -= timedelta(minutes=w)
    start = end - timedelta(minutes=w)
    return start, end


@dataclass(frozen=True)
class ProfileBundle:
    symbol: str
    profile_id: str
    session_start: datetime
    cutoff: datetime
    edges: tuple[EdgeLevel, ...]
    meta: dict[str, Any]


class ProfileProvider(Protocol):
    def load(self, symbol: str, now: datetime) -> ProfileBundle | None:
        ...


@dataclass
class StaticProfileProvider:
    bundles: dict[str, ProfileBundle]

    def load(self, symbol: str, now: datetime) -> ProfileBundle | None:
        return self.bundles.get(symbol.upper())


class ClickHouseCompletedProfileProvider:
    """Volume VAH/VAL (+ TPO marks) from last completed window only."""

    def __init__(self, client: Any, *, window_minutes: int = 30, value_area_pct: float = 0.70) -> None:
        self.client = client
        self.window_minutes = window_minutes
        self.value_area_pct = value_area_pct

    def load(self, symbol: str, now: datetime) -> ProfileBundle | None:
        from orderbook_analyse.market_profile.build import build_profile
        from orderbook_analyse.market_profile.contracts import ProfileWindow
        from orderbook_analyse.market_profile.loader import densify_bins, resolve_price_step
        from orderbook_analyse.market_profile.profile import compute_value_area
        from orderbook_analyse.public_trade_bubbles.loader import load_public_trade_records
        from orderbook_analyse.market_profile.contracts import ProfileBin

        start, end = last_completed_window(now, window_minutes=self.window_minutes)
        window = ProfileWindow(
            window_id=f"fr_{start.strftime('%Y%m%dT%H%M')}",
            anchor_mode="composite",
            label=f"{start.isoformat()}Z",
            start=start,
            end=end,
        )
        vol = build_profile(
            self.client,
            symbol.upper(),
            window,
            value_area_pct=self.value_area_pct,
            target_bins=100,
            use_final=True,
        )
        if vol is None:
            return None
        tpo_vah, tpo_val = vol.value_area.vah, vol.value_area.val
        tpo_src = "volume_proxy_fallback"
        try:
            trades = load_public_trade_records(
                symbol=symbol.upper(), start=start, end=end, client=self.client
            )
            if trades:
                prices = [float(t.price) for t in trades]
                lo, hi = min(prices), max(prices)
                if hi > lo:
                    step = float(resolve_price_step(lo, hi, 100))
                    marks: dict[int, set[int]] = {}
                    for t in trades:
                        ts = t.trade_ts
                        if getattr(ts, "tzinfo", None) is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        # Causal: trade_ts < cutoff (== end)
                        if ts >= end:
                            continue
                        bidx = int(float(t.price) // step)
                        marks.setdefault(bidx, set()).add(int(ts.timestamp()) // 60)
                    raw = [
                        ProfileBin(
                            bin_index=i,
                            price_low=i * step,
                            price_high=(i + 1) * step,
                            price_mid=i * step + step / 2,
                            volume=float(len(mins)),
                            buy_volume=0.0,
                            sell_volume=0.0,
                            trades=0,
                            notional=0.0,
                        )
                        for i, mins in sorted(marks.items())
                    ]
                    if raw:
                        va = compute_value_area(densify_bins(raw, step), self.value_area_pct)
                        tpo_vah, tpo_val = va.vah, va.val
                        tpo_src = "tpo_bracket_presence_1m_causal"
        except Exception:
            pass

        pid = f"{symbol.upper()}_{start.strftime('%Y%m%dT%H%M%S')}_{end.strftime('%H%M%S')}"
        edges = (
            EdgeLevel("TPO_VAH", float(tpo_vah), pid, end),
            EdgeLevel("TPO_VAL", float(tpo_val), pid, end),
            EdgeLevel("VOL_VAH", float(vol.value_area.vah), pid, end),
            EdgeLevel("VOL_VAL", float(vol.value_area.val), pid, end),
        )
        # Derived outer/inner
        upper = max(tpo_vah, vol.value_area.vah)
        lower = min(tpo_val, vol.value_area.val)
        inner_upper = min(tpo_vah, vol.value_area.vah)
        inner_lower = max(tpo_val, vol.value_area.val)
        edges = edges + (
            EdgeLevel("OUTER_UPPER", float(upper), pid, end),
            EdgeLevel("OUTER_LOWER", float(lower), pid, end),
            EdgeLevel("INNER_UPPER", float(inner_upper), pid, end),
            EdgeLevel("INNER_LOWER", float(inner_lower), pid, end),
        )
        return ProfileBundle(
            symbol=symbol.upper(),
            profile_id=pid,
            session_start=start,
            cutoff=end,
            edges=edges,
            meta={
                "bracket_minutes": self.window_minutes,
                "tpo_source": tpo_src,
                "volume_poc": vol.value_area.poc,
                "volume_vah": vol.value_area.vah,
                "volume_val": vol.value_area.val,
                "causality": "trades_strictly_before_cutoff",
                "completeness": "completed_window_only",
            },
        )

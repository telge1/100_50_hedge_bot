"""Wall / episode event specification for unified analysis."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from obfull_research_engine.mp_ob_feature_enrichment_v1.zones import build_zone_bands
from obfull_research_engine.timeparse import format_utc_z


def _parse_z(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)


@dataclass(frozen=True)
class WallEventSpec:
    """Minimum causal inputs for QDH_base on Silver."""

    event_id: str
    symbol: str
    wall_side: str  # ask | bid
    wall_price: float
    tick_size: float
    band_ticks: int
    coverage_start: datetime
    zone_available_at: datetime
    zone_touch_at: datetime
    wall_touch_at: datetime
    detection_at: datetime  # analysis end exclusive (episode detection)
    zone_id: str = ""
    level_id: str = ""
    wall_id: str = ""
    episode_id: str = ""
    chain_version: str | None = None
    # Optional MP fields (enrichment path)
    event_role: str | None = None
    confluence_low: float | None = None
    confluence_high: float | None = None
    window_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in (
            "coverage_start",
            "zone_available_at",
            "zone_touch_at",
            "wall_touch_at",
            "detection_at",
        ):
            d[k] = format_utc_z(getattr(self, k))
        return d

    @property
    def analysis_end(self) -> datetime:
        return self.detection_at


def wall_price_from_mp_event(event: dict[str, Any], *, tick_size: float = 0.1) -> tuple[str, float]:
    """Derive defense wall side/price from MP zone role + confluence edge."""
    bands = build_zone_bands(
        role=str(event["event_role"]),
        low=float(event["confluence_low"]),
        high=float(event["confluence_high"]),
    )
    # Prefer exact printed touch when it sits on the defense edge.
    touch = event.get("touch_price")
    if touch not in (None, "", "None"):
        px = float(touch)
        # snap to tick
        px = round(px / tick_size) * tick_size
        return bands.defense_side, float(f"{px:.10f}")
    # Fallback: UPPER→high (ask), LOWER→low (bid)
    edge = bands.high if bands.defense_side == "ask" else bands.low
    edge = round(edge / tick_size) * tick_size
    return bands.defense_side, float(f"{edge:.10f}")


def spec_from_mp_event(
    event: dict[str, Any],
    *,
    symbol: str = "BTCUSDT",
    tick_size: float = 0.1,
    band_ticks: int = 5,
    warmup_s: float = 300.0,
    chain_version: str | None = None,
) -> WallEventSpec:
    """Build WallEventSpec from a frozen MP batch event row."""
    touch_ns = int(event["first_touch_ts_ns"])
    touch_at = _ns_to_dt(touch_ns)
    trigger_raw = event.get("trigger_ts_ns")
    if trigger_raw not in (None, "", "None"):
        detection_at = _ns_to_dt(int(trigger_raw))
    else:
        # Research default: 2m after touch if no trigger (matches Episode-1 style window).
        detection_at = touch_at + timedelta(seconds=120)
    profile_avail = event.get("profile_available_ts_ns")
    if profile_avail not in (None, "", "None"):
        zone_available = _ns_to_dt(int(profile_avail))
    else:
        zone_available = touch_at - timedelta(seconds=60)
    coverage_start = min(zone_available, touch_at) - timedelta(seconds=float(warmup_s))
    wall_side, wall_price = wall_price_from_mp_event(event, tick_size=tick_size)
    eid = str(event.get("event_id") or "")
    return WallEventSpec(
        event_id=eid,
        symbol=symbol,
        wall_side=wall_side,
        wall_price=wall_price,
        tick_size=tick_size,
        band_ticks=band_ticks,
        coverage_start=coverage_start,
        zone_available_at=zone_available,
        zone_touch_at=touch_at,
        wall_touch_at=touch_at,
        detection_at=detection_at,
        zone_id=str(event.get("zone_id") or ""),
        level_id=str(event.get("level_ids") or ""),
        wall_id=f"w:{eid}",
        episode_id=f"ep:{eid}",
        chain_version=chain_version,
        event_role=str(event.get("event_role") or "") or None,
        confluence_low=float(event["confluence_low"]) if event.get("confluence_low") not in (None, "") else None,
        confluence_high=float(event["confluence_high"]) if event.get("confluence_high") not in (None, "") else None,
        window_id=str(event.get("window_id") or "") or None,
    )


def episode1_spec() -> WallEventSpec:
    """Frozen Episode-1 contract (parity target for Silver QDH bridge)."""
    return WallEventSpec(
        event_id="ep:pc_7775a856ab22f006:1788725942",
        symbol="BTCUSDT",
        wall_side="ask",
        wall_price=79780.0,
        tick_size=0.1,
        band_ticks=5,
        coverage_start=_parse_z("2026-09-06T19:55:00Z"),
        zone_available_at=_parse_z("2026-09-06T20:00:00Z"),
        zone_touch_at=_parse_z("2026-09-06T20:19:02.229Z"),
        wall_touch_at=_parse_z("2026-09-06T20:19:02.232Z"),
        detection_at=_parse_z("2026-09-06T20:21:00Z"),
        zone_id="pc_7775a856ab22f006",
        level_id="lvl:CLOSED:30m:TPO_VAL:1788723000",
        wall_id="w_781b6ed696e777e1",
        episode_id="ep:pc_7775a856ab22f006:1788725942",
        chain_version=(
            "canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459"
        ),
        event_role="UPPER",
        confluence_low=79780.0,
        confluence_high=79780.0,
    )

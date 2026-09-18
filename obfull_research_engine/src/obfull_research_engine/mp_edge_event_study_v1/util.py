"""Shared helpers: UTC ns, bps math, deterministic IDs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from obfull_research_engine.timeparse import format_utc_z


NS = 1_000_000_000
MS = 1_000_000


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected; UTC required")
    return dt.astimezone(timezone.utc)


def dt_to_ns(dt: datetime) -> int:
    dt = as_utc(dt)
    return int(dt.timestamp() * NS)


def ns_to_dt(ns: int) -> datetime:
    sec, rem = divmod(int(ns), NS)
    return datetime.fromtimestamp(sec, tz=timezone.utc).replace(microsecond=rem // 1000)


def format_ns_z(ns: int) -> str:
    return format_utc_z(ns_to_dt(ns))


def bps_distance(price_a: float, price_b: float) -> float:
    """Absolute distance in basis points relative to |price_b| (level/ref)."""
    ref = abs(float(price_b))
    if ref <= 0:
        raise ValueError("non-positive reference price for bps")
    return abs(float(price_a) - float(price_b)) / ref * 10_000.0


def bps_signed(move: float, ref: float) -> float:
    ref = abs(float(ref))
    if ref <= 0:
        raise ValueError("non-positive reference price for bps")
    return float(move) / ref * 10_000.0


def price_offset_bps(ref: float, bps: float) -> float:
    return abs(float(ref)) * float(bps) / 10_000.0


def stable_hash(parts: Iterable[Any], *, n: int = 16) -> str:
    payload = json.dumps(list(parts), sort_keys=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:n]


def make_event_id(
    *,
    symbol: str,
    zone_id: str,
    first_touch_ns: int,
    event_role: str,
    confluence_class: str,
    param_fingerprint: str,
) -> str:
    return "mpe_" + stable_hash(
        [
            "mp_edge_event_v1",
            symbol,
            zone_id,
            int(first_touch_ns),
            event_role,
            confluence_class,
            param_fingerprint,
        ],
        n=20,
    )


def param_fingerprint(manifest_params: dict[str, Any]) -> str:
    keys = (
        "touch_tolerance_bps",
        "confluence_tolerance_bps",
        "min_event_separation_s",
        "reset_distance_bps",
        "min_penetration_bps",
        "max_reclaim_delay_s",
        "reclaim_hold_s",
        "true_break_acceptance_s",
        "outcome_horizons_s",
        "tp_targets_bps",
        "sl_target_bps",
        "reclaim_tolerance_bps",
        "mp_kind",
        "mp_edge_definition",
    )
    subset = {k: manifest_params[k] for k in keys if k in manifest_params}
    return stable_hash([json.dumps(subset, sort_keys=True, separators=(",", ":"))], n=12)

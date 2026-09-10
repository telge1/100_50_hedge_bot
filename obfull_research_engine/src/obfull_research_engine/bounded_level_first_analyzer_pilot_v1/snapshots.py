"""MP/LLD snapshots via the Phase-2 serializers only. Cache identical payloads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..market_profile_lld_shared_event_materialization_v1.lld_serialize import (
    LLD_CANDLE_TIMEFRAME,
    materialize_lld_snapshot,
)
from ..market_profile_lld_shared_event_materialization_v1.mp_serialize import (
    materialize_market_profile_case,
)
from ..market_profile_lld_shared_event_materialization_v1.time_bounds import resolve_profile_window
from ..timeparse import format_utc_z
from . import LLD_SOURCE_TIMEFRAME, MP_TIMEFRAMES, PHASE2_LLD_CONFIG_HASH, PHASE2_MP_CONFIG_HASH


def completed_minutes(start: datetime, end: datetime) -> list[datetime]:
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    t = start
    out: list[datetime] = []
    while t < end:
        out.append(t)
        t += timedelta(minutes=1)
    return out


def snapshot_row_from_bundle(bundle: dict[str, Any], *, snapshot_ts: datetime) -> dict[str, Any]:
    event = bundle["event"]
    return {
        "snapshot_ts": format_utc_z(snapshot_ts),
        "timeframe": event["timeframe"],
        "profile_id": event["profile_id"],
        "profile_state": event["profile_state"],
        "profile_start": event["profile_start"],
        "natural_profile_end": event["natural_profile_end"],
        "effective_profile_end": event["effective_profile_end"],
        "TPO_POC": event["tpo_poc"],
        "TPO_VAH": event["tpo_vah"],
        "TPO_VAL": event["tpo_val"],
        "price_bin_size": event["price_bin_size"],
        "available_at": event["available_at"],
        "config_hash": event["config_hash"],
        "payload_hash": event["canonical_payload_hash"],
        "event_id": event["event_id"],
        "reused_identical_payload": False,
    }


class SnapshotCache:
    def __init__(self) -> None:
        self.mp_cache: dict[tuple[str, str, int, int], dict[str, Any]] = {}
        self.lld_cache: dict[int, dict[str, Any]] = {}
        self.mp_calls = 0
        self.mp_reused = 0
        self.lld_calls = 0
        self.lld_reused = 0

    def market_profile(
        self,
        *,
        symbol: str,
        request_as_of: datetime,
        timeframe: str,
        requested_state: str,
    ) -> dict[str, Any] | None:
        bounds = resolve_profile_window(
            request_as_of=request_as_of,
            timeframe=timeframe,
            requested_state=requested_state,
        )
        if bounds["profile_state"] == "INVALID":
            return None
        start = bounds["natural_profile_start"]
        end = bounds["effective_profile_end"]
        key = (timeframe, bounds["profile_state"], int(start.timestamp()), int(end.timestamp()))
        if key in self.mp_cache:
            self.mp_reused += 1
            bundle = self.mp_cache[key]
            row = snapshot_row_from_bundle(bundle, snapshot_ts=request_as_of)
            row["reused_identical_payload"] = True
            return {"bundle": bundle, "snapshot": row}
        self.mp_calls += 1
        bundle = materialize_market_profile_case(
            symbol=symbol,
            request_as_of=request_as_of,
            timeframe=timeframe,
            requested_state=requested_state,
            case_id=f"{timeframe}_{bounds['profile_state']}_{int(end.timestamp())}",
        )
        if bundle["event"]["config_hash"] != PHASE2_MP_CONFIG_HASH:
            raise RuntimeError(
                f"MP config_hash drifted: {bundle['event']['config_hash']} != {PHASE2_MP_CONFIG_HASH}"
            )
        self.mp_cache[key] = bundle
        return {"bundle": bundle, "snapshot": snapshot_row_from_bundle(bundle, snapshot_ts=request_as_of)}

    def lld(self, *, symbol: str, request_as_of: datetime) -> dict[str, Any]:
        key = int(request_as_of.timestamp())
        if key in self.lld_cache:
            self.lld_reused += 1
            return self.lld_cache[key]
        self.lld_calls += 1
        bundle = materialize_lld_snapshot(
            symbol=symbol,
            request_as_of=request_as_of,
            case_id=f"lld_{key}",
            timeframe=LLD_CANDLE_TIMEFRAME,
        )
        if bundle["snapshot"]["config_hash"] != PHASE2_LLD_CONFIG_HASH:
            raise RuntimeError(
                f"LLD config_hash drifted: {bundle['snapshot']['config_hash']} != {PHASE2_LLD_CONFIG_HASH}"
            )
        if bundle["snapshot"]["source_timeframe"] != LLD_SOURCE_TIMEFRAME:
            raise RuntimeError("LLD source timeframe is not 15m")
        self.lld_cache[key] = bundle
        return bundle


def materialize_minute_mp(
    cache: SnapshotCache,
    *,
    symbol: str,
    snapshot_ts: datetime,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tf in MP_TIMEFRAMES:
        for state in ("CLOSED", "DEVELOPING"):
            got = cache.market_profile(
                symbol=symbol,
                request_as_of=snapshot_ts,
                timeframe=tf,
                requested_state=state,
            )
            if got is None:
                continue
            event = got["bundle"]["event"]
            if event["available_at"] > format_utc_z(snapshot_ts):
                continue
            rows.append({**got["snapshot"], "bundle": got["bundle"]})
    return rows

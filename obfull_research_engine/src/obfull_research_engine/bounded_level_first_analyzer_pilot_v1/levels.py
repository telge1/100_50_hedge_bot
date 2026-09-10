"""Extract CLOSED/DEVELOPING TPO levels from Phase-2 MP events. No new formula."""

from __future__ import annotations

from typing import Any

from ..market_profile_lld_shared_event_materialization_v1.hashing import sha256_hex
from . import DEVELOPING_LEVEL_TYPES, VISIBLE_TPO_TYPES
from .bins import tpo_bin_zone


def logical_level_id(
    *,
    symbol: str,
    timeframe: str,
    profile_id: str,
    tpo_type: str,
    profile_state: str,
    config_hash: str,
    level_price: float | None = None,
    level_zone_low: float | None = None,
    level_zone_high: float | None = None,
) -> str:
    """CLOSED identity includes unchanged bin bounds. DEVELOPING omits moving prices."""
    payload: dict[str, Any] = {
        "symbol": str(symbol),
        "timeframe": str(timeframe),
        "profile_id": str(profile_id),
        "tpo_type": str(tpo_type),
        "profile_state": str(profile_state).upper(),
        "config_hash": str(config_hash),
    }
    if str(profile_state).upper() == "CLOSED":
        payload["level_price"] = float(level_price)
        payload["level_zone_low"] = float(level_zone_low)
        payload["level_zone_high"] = float(level_zone_high)
    return "ll_" + sha256_hex(payload)[:16]


def level_class(*, profile_state: str, timeframe: str, tpo_type: str) -> str:
    tf_token = {"30M": "30M", "1H": "1H", "4H": "4H", "30m": "30M", "1h": "1H", "4h": "4H"}[
        timeframe
    ]
    state = str(profile_state).upper()
    kind = str(tpo_type).upper()
    if kind not in VISIBLE_TPO_TYPES:
        raise ValueError(f"not a visible TPO level: {tpo_type}")
    if state not in {"CLOSED", "DEVELOPING"}:
        raise ValueError(f"unsupported profile_state: {profile_state}")
    return f"{state}_{tf_token}_{kind}"


def level_id(*, profile_state: str, timeframe: str, tpo_type: str, profile_start_unix: int) -> str:
    return (
        f"lvl:{profile_state}:{timeframe}:{tpo_type}:{int(profile_start_unix)}"
    )


def visible_tpo_prices(event: dict[str, Any]) -> list[tuple[str, float]]:
    return [
        ("TPO_POC", float(event["tpo_poc"])),
        ("TPO_VAH", float(event["tpo_vah"])),
        ("TPO_VAL", float(event["tpo_val"])),
    ]


def extract_levels_from_event(
    event: dict[str, Any],
    *,
    raw_profile: dict[str, Any] | None,
    snapshot_ts: str,
) -> list[dict[str, Any]]:
    if event.get("naked_poc_excluded_from_causal_event") is not True:
        raise RuntimeError("naked POC must stay excluded from causal levels")
    if str(event.get("profile_state")) not in {"CLOSED", "DEVELOPING"}:
        return []
    start = str(event["profile_start"])
    start_unix = _unix_from_z(start)
    rows: list[dict[str, Any]] = []
    for tpo_type, price in visible_tpo_prices(event):
        zone = tpo_bin_zone(
            level_price=price,
            price_bin_size=event.get("price_bin_size"),
            raw_profile=raw_profile,
        )
        klass = level_class(
            profile_state=str(event["profile_state"]),
            timeframe=str(event["timeframe"]),
            tpo_type=tpo_type,
        )
        lid = level_id(
            profile_state=str(event["profile_state"]),
            timeframe=str(event["timeframe"]),
            tpo_type=tpo_type,
            profile_start_unix=start_unix,
        )
        llid = logical_level_id(
            symbol=str(event["symbol"]),
            timeframe=str(event["timeframe"]),
            profile_id=str(event["profile_id"]),
            tpo_type=tpo_type,
            profile_state=str(event["profile_state"]),
            config_hash=str(event["config_hash"]),
            level_price=zone["level_price"],
            level_zone_low=zone["level_zone_low"],
            level_zone_high=zone["level_zone_high"],
        )
        rows.append(
            {
                "level_id": lid,
                "logical_level_id": llid,
                "level_class": klass,
                "symbol": event["symbol"],
                "timeframe": event["timeframe"],
                "profile_state": event["profile_state"],
                "tpo_type": tpo_type,
                "visible_chart_line": True,
                "profile_id": event["profile_id"],
                "profile_start": event["profile_start"],
                "natural_profile_end": event["natural_profile_end"],
                "effective_profile_end": event["effective_profile_end"],
                "snapshot_ts": snapshot_ts,
                "request_as_of": event["request_as_of"],
                "available_at": event["available_at"],
                "level_price": zone["level_price"],
                "level_zone_low": zone["level_zone_low"],
                "level_zone_high": zone["level_zone_high"],
                "price_bin_size": zone["price_bin_size"],
                "zone_source": zone["zone_source"],
                "config_hash": event["config_hash"],
                "payload_hash": event["canonical_payload_hash"],
                "source_event_id": event["event_id"],
                "developing": klass in DEVELOPING_LEVEL_TYPES,
            }
        )
    return rows


def unique_logical_levels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per logical_level_id. CLOSED snapshots collapse; DEVELOPING keeps first version."""
    out: dict[str, dict[str, Any]] = {}
    snapshot_n: dict[str, int] = {}
    for row in rows:
        llid = str(row.get("logical_level_id") or row["level_id"])
        snapshot_n[llid] = snapshot_n.get(llid, 0) + 1
        if llid not in out:
            out[llid] = dict(row)
            out[llid]["snapshot_version_count"] = 1
        else:
            out[llid]["snapshot_version_count"] = snapshot_n[llid]
    return [out[k] for k in sorted(out)]


def freeze_level(level: dict[str, Any]) -> dict[str, Any]:
    frozen = dict(level)
    frozen["frozen_at_approach"] = True
    frozen["frozen_level_price"] = level["level_price"]
    frozen["frozen_level_zone_low"] = level["level_zone_low"]
    frozen["frozen_level_zone_high"] = level["level_zone_high"]
    return frozen


def developing_update(*, frozen: dict[str, Any], later: dict[str, Any]) -> dict[str, Any] | None:
    if frozen["level_id"] != later["level_id"]:
        return None
    price_changed = float(later["level_price"]) != float(frozen["frozen_level_price"])
    zone_changed = (
        float(later["level_zone_low"]) != float(frozen["frozen_level_zone_low"])
        or float(later["level_zone_high"]) != float(frozen["frozen_level_zone_high"])
    )
    if not price_changed and not zone_changed:
        return None
    return {
        "level_id": frozen["level_id"],
        "episode_level_unchanged": True,
        "frozen_level_price": frozen["frozen_level_price"],
        "later_level_price": later["level_price"],
        "later_level_zone_low": later["level_zone_low"],
        "later_level_zone_high": later["level_zone_high"],
        "later_snapshot_ts": later["snapshot_ts"],
        "does_not_rewrite_episode_level": True,
        "does_not_spawn_silent_new_episode": True,
    }


def _unix_from_z(value: str) -> int:
    from datetime import datetime, timezone

    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    return int(dt.timestamp())

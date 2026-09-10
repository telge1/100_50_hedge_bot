"""Serialize dashboard Market Profile output. Does not recompute VA/POC."""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Any

from orderbook_analyse.market_profile.anchor import as_utc
from orderbook_analyse.market_profile.contracts import ShapeThresholds

from ..timeparse import format_utc_z
from . import (
    CONTEXT_ONLY_LEVEL_TYPES,
    CONTRACT_NAME,
    CONTRACT_VERSION,
    MP_SOURCE_NAME,
    MP_SOURCE_VERSION,
    VISIBLE_LEVEL_TYPES,
)
from .available_at import market_profile_available_at
from .hashing import sha256_hex
from .time_bounds import resolve_profile_window


def _ensure_paths() -> None:
    from ..market_profile_context.provenance import ensure_mp_import_path

    ensure_mp_import_path()


def chart_mp_generator_identity() -> dict[str, Any]:
    _ensure_paths()
    from market_profile_v1.dual_profile import build_dual_window_profile
    from market_profile_v1.service import load_profiles

    return {
        "load_profiles_qualname": f"{load_profiles.__module__}.{load_profiles.__qualname__}",
        "build_dual_window_profile_qualname": (
            f"{build_dual_window_profile.__module__}.{build_dual_window_profile.__qualname__}"
        ),
        "load_profiles_file": inspect.getsourcefile(load_profiles),
        "build_dual_window_profile_file": inspect.getsourcefile(build_dual_window_profile),
        "reimplements_compute_value_area": False,
    }


def _mp_config() -> dict[str, Any]:
    _ensure_paths()
    from market_profile_v1.dual_profile import DEFAULT_BRACKET_MINUTES, DUAL_CONTRACT_VERSION
    from market_profile_v1.service import DEFAULT_TARGET_BINS, DEFAULT_VALUE_AREA_PCT

    return {
        "value_area_pct": float(DEFAULT_VALUE_AREA_PCT),
        "target_bins": int(DEFAULT_TARGET_BINS),
        "tpo_bracket_minutes": int(DEFAULT_BRACKET_MINUTES),
        "use_final": False,
        "dual_contract_version": DUAL_CONTRACT_VERSION,
        "shape_thresholds": ShapeThresholds().to_dict(),
        "include_bins": True,
    }


def config_hash() -> str:
    return sha256_hex({"contract": CONTRACT_NAME, "mp": _mp_config()})


def _copy_tpo_volume(raw: dict[str, Any]) -> dict[str, Any]:
    tpo = raw.get("tpo") or {}
    vol = raw.get("volume") or {}
    tva = tpo.get("value_area") or {}
    vva = vol.get("value_area") or {}
    brackets = tpo.get("brackets") or {}
    return {
        "tpo_poc": tva.get("poc"),
        "tpo_vah": tva.get("vah"),
        "tpo_val": tva.get("val"),
        "volume_poc": vva.get("poc"),
        "volume_vah": vva.get("vah"),
        "volume_val": vva.get("val"),
        "tpo_bracket_minutes": brackets.get("bracket_minutes"),
        "profile_high": raw.get("price_high"),
        "profile_low": raw.get("price_low"),
        "price_bin_size": raw.get("price_step"),
        "tpo_status": tpo.get("status"),
        "volume_status": vol.get("status"),
        "dual_contract_version": raw.get("dual_contract_version"),
    }


def _visible_levels(copied: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = (
        ("TPO_POC", copied["tpo_poc"]),
        ("TPO_VAH", copied["tpo_vah"]),
        ("TPO_VAL", copied["tpo_val"]),
    )
    out = []
    for level_type, price in mapping:
        out.append(
            {
                "level_type": level_type,
                "level_family": "TPO",
                "level_price": price,
                "visible_chart_line": True,
            }
        )
    return out


def _context_levels(copied: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = (
        ("PROFILE_HIGH", copied["profile_high"], "RANGE"),
        ("PROFILE_LOW", copied["profile_low"], "RANGE"),
        ("VOLUME_VPOC", copied["volume_poc"], "VOLUME"),
        ("VOLUME_VAH", copied["volume_vah"], "VOLUME"),
        ("VOLUME_VAL", copied["volume_val"], "VOLUME"),
    )
    out = []
    for level_type, price, family in mapping:
        assert level_type in CONTEXT_ONLY_LEVEL_TYPES
        out.append(
            {
                "level_type": level_type,
                "level_family": family,
                "level_price": price,
                "visible_chart_line": False,
                "role": "CONTEXT_ONLY_NOT_DEFAULT_CHART_LINE",
            }
        )
    return out


def _strip_naked(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    causal = dict(raw)
    naked = causal.pop("naked_poc", None)
    sidecar = None
    if naked is not None:
        sidecar = {
            "naked_poc": naked,
            "historical_only_not_decision_eligible": True,
            "reason": "dual_profile._naked_poc_flag uses 1m candles after window.end",
        }
    return causal, sidecar


def materialize_market_profile_case(
    *,
    symbol: str,
    request_as_of: datetime,
    timeframe: str,
    requested_state: str,
    case_id: str,
) -> dict[str, Any]:
    """Call chart ``load_profiles`` for [natural_start, effective_end)."""
    _ensure_paths()
    from market_profile_v1.service import load_profiles

    bounds = resolve_profile_window(
        request_as_of=request_as_of,
        timeframe=timeframe,
        requested_state=requested_state,
    )
    if bounds["profile_state"] == "INVALID":
        raise ValueError(f"INVALID profile window for {case_id}: {bounds}")

    cfg = _mp_config()
    cfg_hash = config_hash()
    start = bounds["natural_profile_start"]
    end = bounds["effective_profile_end"]
    identity = chart_mp_generator_identity()

    payload = load_profiles(
        symbol=symbol.upper(),
        start=int(start.timestamp()),
        end=int(end.timestamp()),
        mp_timeframe=timeframe,
        value_area_pct=cfg["value_area_pct"],
        target_bins=cfg["target_bins"],
        use_final=cfg["use_final"],
        include_bins=cfg["include_bins"],
    )
    profiles = list(payload.get("profiles") or [])
    if not profiles:
        raise RuntimeError(f"load_profiles returned no profiles for {case_id}")

    chosen = None
    start_z = format_utc_z(start)
    end_z = format_utc_z(end)
    for raw in profiles:
        win = raw.get("window") or {}
        if str(win.get("start") or "").startswith(start_z[:19]) and str(win.get("end") or "").startswith(end_z[:19]):
            chosen = raw
            break
    if chosen is None:
        chosen = profiles[0]

    causal_raw, naked_sidecar = _strip_naked(chosen)
    copied = _copy_tpo_volume(causal_raw)
    missing = [k for k in ("tpo_poc", "tpo_vah", "tpo_val") if copied.get(k) is None]
    if missing:
        raise RuntimeError(f"chart payload missing {missing} for {case_id}")

    avail = market_profile_available_at(
        effective_profile_end=end,
        use_final=cfg["use_final"],
        uses_1m_ohlc=True,
    )
    state_ts = end
    available_at = avail["available_at"]
    win = causal_raw.get("window") or {}
    profile_id = str(win.get("window_id") or f"{timeframe}_{int(start.timestamp())}")
    event_id = (
        f"mp:{symbol.upper()}:{timeframe}:{bounds['profile_state']}:"
        f"{int(start.timestamp())}:{int(end.timestamp())}"
    )
    visible = _visible_levels(copied)
    assert tuple(v["level_type"] for v in visible) == VISIBLE_LEVEL_TYPES

    event = {
        "event_id": event_id,
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "profile_id": profile_id,
        "profile_start": start_z,
        "natural_profile_end": format_utc_z(bounds["natural_profile_end"]),
        "effective_profile_end": end_z,
        "request_as_of": format_utc_z(as_utc(request_as_of)),
        "profile_state": bounds["profile_state"],
        "tpo_poc": copied["tpo_poc"],
        "tpo_vah": copied["tpo_vah"],
        "tpo_val": copied["tpo_val"],
        "profile_high": copied["profile_high"],
        "profile_low": copied["profile_low"],
        "volume_poc": copied["volume_poc"],
        "volume_vah": copied["volume_vah"],
        "volume_val": copied["volume_val"],
        "tpo_bracket_minutes": copied["tpo_bracket_minutes"] or cfg["tpo_bracket_minutes"],
        "value_area_pct": cfg["value_area_pct"],
        "price_bin_size": copied["price_bin_size"],
        "tick_size": None,
        "tick_size_source": "NOT_IN_CHART_PAYLOAD",
        "source_name": MP_SOURCE_NAME,
        "source_version": MP_SOURCE_VERSION,
        "state_ts": format_utc_z(state_ts),
        "available_at": format_utc_z(available_at),
        "available_at_components": avail["components"],
        "config": cfg,
        "config_hash": cfg_hash,
        "visible_levels": visible,
        "context_only_levels": _context_levels(copied),
        "naked_poc_excluded_from_causal_event": True,
        "generator": identity,
        "case_id": case_id,
    }
    event["canonical_payload_hash"] = sha256_hex(
        {k: event[k] for k in event if k not in {"generator", "available_at_components"}}
    )
    raw_hash = sha256_hex(causal_raw)
    return {
        "event": event,
        "raw_profile": causal_raw,
        "raw_service_payload_meta": {
            "symbol": payload.get("symbol"),
            "mp_timeframe": payload.get("mp_timeframe"),
            "requested_start": payload.get("requested_start"),
            "requested_end": payload.get("requested_end"),
            "dual_contract_version": (payload.get("meta") or {}).get("dual_contract_version"),
            "profile_count": len(profiles),
            "compute_path": (payload.get("meta") or {}).get("compute_path"),
        },
        "raw_payload_hash": raw_hash,
        "naked_poc_sidecar": naked_sidecar,
        "bounds": {k: (format_utc_z(v) if isinstance(v, datetime) else v) for k, v in bounds.items()},
        "generator": identity,
    }

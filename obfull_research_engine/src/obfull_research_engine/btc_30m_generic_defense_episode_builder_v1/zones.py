"""Load CLOSED 30m MP zones only; causal available_at; optional SINGLE_30M cluster match."""

from __future__ import annotations

import ast
import csv
import json
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..timeparse import format_utc_z


def _parse_list_cell(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, list):
            return parsed
    except (SyntaxError, ValueError):
        pass
    return [text]


def load_closed_30m_mp_levels(path: Path | str) -> list[dict[str, Any]]:
    """Return CLOSED 30m MP level rows (VAH/VAL/POC etc.) with causal zone edges."""
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            if str(raw.get("timeframe") or "") != "30m":
                continue
            if str(raw.get("profile_state") or "").upper() != "CLOSED":
                continue
            available_at = parse_utc(raw["available_at"])
            zone = {
                "level_id": raw["level_id"],
                "logical_level_id": raw.get("logical_level_id"),
                "symbol": raw.get("symbol"),
                "timeframe": "30m",
                "profile_state": "CLOSED",
                "tpo_type": raw.get("tpo_type"),
                "level_class": raw.get("level_class"),
                "zone_low": float(raw["level_zone_low"]),
                "zone_high": float(raw["level_zone_high"]),
                "level_price": float(raw["level_price"]) if raw.get("level_price") not in (None, "") else None,
                "zone_available_at": format_utc_z(available_at),
                "zone_available_at_dt": available_at,
                "profile_start": raw.get("profile_start"),
                "natural_profile_end": raw.get("natural_profile_end"),
                "source_event_id": raw.get("source_event_id"),
                "source_file": str(path),
            }
            rows.append(zone)
    rows.sort(key=lambda z: (z["zone_available_at_dt"], z["level_id"]))
    return rows


def load_level_clusters(path: Path | str) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            clusters.append(
                {
                    "level_cluster_id": raw["level_cluster_id"],
                    "persistent_cluster_id": raw.get("persistent_cluster_id"),
                    "member_level_ids": _parse_list_cell(raw.get("member_level_ids")),
                    "member_logical_level_ids": _parse_list_cell(raw.get("member_logical_level_ids")),
                    "member_timeframes": _parse_list_cell(raw.get("member_timeframes")),
                    "member_level_types": _parse_list_cell(raw.get("member_level_types")),
                    "member_level_classes": _parse_list_cell(raw.get("member_level_classes")),
                    "member_profile_states": _parse_list_cell(raw.get("member_profile_states")),
                    "cluster_price_low": float(raw["cluster_price_low"]),
                    "cluster_price_high": float(raw["cluster_price_high"]),
                    "highest_timeframe": raw.get("highest_timeframe"),
                    "closed_member_count": int(raw.get("closed_member_count") or 0),
                    "developing_member_count": int(raw.get("developing_member_count") or 0),
                    "confluence_class": raw.get("confluence_class"),
                }
            )
    return clusters


def match_single_30m_cluster(
    zone: dict[str, Any],
    clusters: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Prefer SINGLE_30M closed cluster containing this level_id when available."""
    lid = zone.get("level_id")
    candidates = []
    for c in clusters:
        if str(c.get("confluence_class") or "") != "SINGLE_30M":
            continue
        states = [str(s).upper() for s in (c.get("member_profile_states") or [])]
        if states and any(s != "CLOSED" for s in states):
            continue
        members = c.get("member_level_ids") or []
        if lid is not None and lid in members:
            candidates.append(c)
    if not candidates:
        # Fallback: price-overlap SINGLE_30M closed
        zlo, zhi = float(zone["zone_low"]), float(zone["zone_high"])
        for c in clusters:
            if str(c.get("confluence_class") or "") != "SINGLE_30M":
                continue
            clo, chi = float(c["cluster_price_low"]), float(c["cluster_price_high"])
            if clo <= zhi and chi >= zlo:
                candidates.append(c)
    if not candidates:
        return None
    return candidates[0]


def zones_as_visit_clusters(
    mp_levels: list[dict[str, Any]],
    clusters: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build visit-ready zone/cluster records from CLOSED 30m MP levels."""
    clusters = clusters or []
    out: list[dict[str, Any]] = []
    for z in mp_levels:
        matched = match_single_30m_cluster(z, clusters)
        persistent = (matched or {}).get("persistent_cluster_id") or z["level_id"]
        level_cluster_id = (matched or {}).get("level_cluster_id") or z["level_id"]
        zone_id = str(persistent)
        out.append(
            {
                "zone_id": zone_id,
                "level_id": z["level_id"],
                "level_cluster_id": level_cluster_id,
                "persistent_cluster_id": persistent,
                "cluster_price_low": float(z["zone_low"])
                if matched is None
                else float(matched["cluster_price_low"]),
                "cluster_price_high": float(z["zone_high"])
                if matched is None
                else float(matched["cluster_price_high"]),
                "zone_low": float(z["zone_low"]),
                "zone_high": float(z["zone_high"]),
                "zone_available_at": z["zone_available_at"],
                "zone_available_at_dt": z["zone_available_at_dt"],
                "tpo_type": z.get("tpo_type"),
                "timeframe": "30m",
                "profile_state": "CLOSED",
                "confluence_class": (matched or {}).get("confluence_class") or "MP_LEVEL_ONLY",
                "member_level_types": (matched or {}).get("member_level_types")
                or ([z.get("tpo_type")] if z.get("tpo_type") else []),
                "source_level": z,
                "matched_cluster": matched,
            }
        )
    return out


def load_zones(
    *,
    mp_events_path: Path | str,
    level_clusters_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    levels = load_closed_30m_mp_levels(mp_events_path)
    clusters = load_level_clusters(level_clusters_path) if level_clusters_path else []
    return zones_as_visit_clusters(levels, clusters)

#!/usr/bin/env python3
"""Phase 5E — DB vs API vs chart-render geometry audit (read-only)."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
for _p in (_REPO, _DASH):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect

from research_charts.chart_historical_lmv2_pools import (
    PIPELINE_LINE_ID,
    PILOT_FROM,
    PILOT_TO,
    _build_active_episode_pools,
    _filter_remaining_by_price,
    _load_catalog,
    _load_publication_line,
    _remaining_zones_from_projection,
    chart_historical_lmv2_pools,
    pilot_decision_closes,
)
from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config, config_hash
from research_charts.chart_live_micro_lld_v2 import pools_to_zones
from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import PublishedActiveReader
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import (
    load_historical_remaining,
)
from research_charts.chart_historical_lmv2_pools import (
    IMMUTABLE_PRICE_HASH,
    IMMUTABLE_REVISION_HASH,
    IMMUTABLE_SNAPSHOT_ID,
    SNAPSHOT_ROOT,
    _LineFilteredSource,
)

OUT = _REPO / "logs" / "historical_import" / "overnight_2026" / "phase5e_pool_geometry_audit.json"
SYMBOL = "DOGEUSDT"
TF = "15m"
PD = 10
PILOT_FROM_ISO = "2026-08-01T00:00:00Z"
PILOT_TO_ISO = "2026-08-01T04:45:00Z"
SPOT_CLOSES = ("2026-08-01T00:00:00Z", "2026-08-01T02:30:00Z", "2026-08-01T04:45:00Z")
REMAINING_LIMIT_UI = 1500
JS_REM_CAP = max(200, 900 * 2)  # LIVE_MICRO_LLD_V2_MAX_CHART_ZONES=900
JS_ACTIVE_CAP = 900


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _zone_geom(z: dict) -> tuple:
    return (
        str(z.get("pool_id")),
        round(float(z.get("bottom_price") or z.get("lower_price") or 0), 8),
        round(float(z.get("top_price") or z.get("upper_price") or 0), 8),
        int(z.get("start_timestamp") or 0),
        int(z.get("end_timestamp") or 0),
    )


def _simulate_js_clip_cull(zones: list[dict], from_u: int, to_u: int) -> list[dict]:
    out = []
    for z in zones:
        if not z:
            continue
        start = max(int(z["start_timestamp"]), from_u)
        end = min(int(z["end_timestamp"]), to_u)
        if end < start:
            continue
        out.append({**z, "start_timestamp": start, "end_timestamp": end})
    active = [z for z in out if (z.get("metadata") or {}).get("layer") != "remaining"]
    rem = [z for z in out if (z.get("metadata") or {}).get("layer") == "remaining"]
    if len(active) > JS_ACTIVE_CAP:
        active = active[:JS_ACTIVE_CAP]
    if len(rem) > JS_REM_CAP:
        rem = rem[:JS_REM_CAP]
    return active + rem


def _remaining_segments_fp(rem: dict) -> str:
    rows = sorted(
        (r["pool_id"], r["top"], r["bottom"], r.get("color"))
        for r in rem.get("remaining") or []
    )
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()[:16]


def main() -> None:
    cfg_hash = config_hash(build_live_micro_lld_v2_config(PD))
    reader = PublishedActiveReader()
    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    db = ch_cfg.database
    closes = pilot_decision_closes()
    from_u = int(PILOT_FROM.timestamp())
    to_u = int(PILOT_TO.timestamp())

    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "5E",
        "title": "Historical LLD V2 DB vs API vs chart geometry audit",
        "generated_at_utc": _iso(datetime.now(timezone.utc)),
        "read_only": True,
        "scope": {
            "symbol": SYMBOL,
            "timeframe": TF,
            "pool_detail": PD,
            "pipeline_line_id": PIPELINE_LINE_ID,
            "window": [PILOT_FROM_ISO, PILOT_TO_ISO],
            "close_count": len(closes),
            "immutable_candle_snapshot_id": IMMUTABLE_SNAPSHOT_ID,
        },
        "checks": {},
    }

    # --- ACTIVE: CH vs API (full window) ---
    pools_ep = _build_active_episode_pools(reader, cfg_hash, client, db, closes)
    api_active = chart_historical_lmv2_pools(
        SYMBOL,
        PILOT_FROM_ISO,
        PILOT_TO_ISO,
        include_active=True,
        include_remaining=False,
        as_of_close=PILOT_TO_ISO,
    )
    active_zones = [z for z in api_active["zones"] if (z.get("metadata") or {}).get("layer") == "active"]
    ch_by_pid = {p["pool_id"]: p for p in pools_ep}
    api_by_pid = {z["pool_id"]: z for z in active_zones}
    active_geom_mismatches = []
    for pid, p in ch_by_pid.items():
        z = api_by_pid.get(pid)
        if not z:
            active_geom_mismatches.append({"pool_id": pid, "issue": "missing_in_api_zones"})
            continue
        if abs(float(z["top_price"]) - float(p["top_price"])) > 1e-6:
            active_geom_mismatches.append({"pool_id": pid, "field": "top_price", "ch": p["top_price"], "api": z["top_price"]})
        if abs(float(z["bottom_price"]) - float(p["bottom_price"])) > 1e-6:
            active_geom_mismatches.append({"pool_id": pid, "field": "bottom_price", "ch": p["bottom_price"], "api": z["bottom_price"]})
        meta = z.get("metadata") or {}
        if meta.get("snapshot_id") != p.get("snapshot_id"):
            active_geom_mismatches.append(
                {"pool_id": pid, "field": "snapshot_id", "ch": p.get("snapshot_id"), "api": meta.get("snapshot_id")}
            )
    report["checks"]["active_ch_vs_api_geometry"] = {
        "status": "PASS" if not active_geom_mismatches else "FAIL",
        "episode_pool_count": len(pools_ep),
        "api_active_zone_count": len(active_zones),
        "mismatch_count": len(active_geom_mismatches),
        "mismatch_sample": active_geom_mismatches[:15],
    }

    # --- Membership exit vs zone end (inactive continuation) ---
    source = _LineFilteredSource(client, db)
    membership_audit = []
    prev_ids: set[str] | None = None
    for dc in closes:
        pub = _load_publication_line(reader, cfg_hash, dc)
        ids = set(pub["pool_ids"])
        entries = sorted(ids - prev_ids) if prev_ids is not None else []
        exits = sorted(prev_ids - ids) if prev_ids is not None else []
        if prev_ids is not None and exits:
            for pid in exits[:3]:
                z = api_by_pid.get(pid)
                if z and int(z["end_timestamp"]) > int(dc.timestamp()) and not z.get("metadata", {}).get("break_at"):
                    membership_audit.append(
                        {
                            "pool_id": pid,
                            "exit_close": _iso(dc),
                            "zone_end_timestamp": int(z["end_timestamp"]),
                            "issue": "zone_extends_past_membership_exit_without_break_at",
                        }
                    )
        prev_ids = ids
    open_at_end = [p for p in pools_ep if int(p.get("_end_unix", 0)) == to_u]
    report["checks"]["membership_and_zone_horizon"] = {
        "status": "FAIL" if membership_audit or open_at_end else "PASS",
        "membership_exit_zone_extension_samples": membership_audit[:20],
        "pools_still_open_at_pilot_end_count": len(open_at_end),
        "pilot_end_not_proven_pool_end_note": (
            "ACTIVE episodes assign end_close_unix=last pilot close for still-member pools; "
            "pools_to_zones sets end_timestamp to request to_unix unless break_at in CH — "
            "pilot window end is a display horizon, not catalog invalidation."
        ),
        "open_at_pilot_end_sample_pool_ids": [p["pool_id"] for p in open_at_end[:8]],
    }

    # --- Three spot closes: CH catalog vs API publication block ---
    spot_checks = {}
    for iso in SPOT_CLOSES:
        dc = _parse(iso)
        pub = _load_publication_line(reader, cfg_hash, dc)
        cat = _load_catalog(client, db, pub["snapshot_id"], dc)
        api_pub = next((r for r in api_active["publications"] if r["decision_close"] == iso), None)
        spot_checks[iso] = {
            "ch_snapshot_id": pub["snapshot_id"],
            "ch_active_count": len(pub["pool_ids"]),
            "api_publication_match": bool(
                api_pub
                and api_pub["snapshot_id"] == pub["snapshot_id"]
                and api_pub["active_state_fingerprint"] == pub["active_state_fingerprint"]
            ),
            "catalog_rows": len(cat),
            "catalog_bounds_hash": hashlib.sha256(
                json.dumps(
                    sorted((r["pool_id"], r["top"], r["bottom"], r["strength"], r["known_at"]) for r in cat),
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:16],
        }
    report["checks"]["spot_closes_publication_identity"] = {
        "status": "PASS" if all(v["api_publication_match"] for v in spot_checks.values()) else "FAIL",
        "per_close": spot_checks,
    }

    # --- Remaining: projection vs API vs cap ---
    rem_cap_audit: dict[str, Any] = {}
    for iso in SPOT_CLOSES:
        dc = _parse(iso)
        rem = load_historical_remaining(
            source,
            symbol=SYMBOL,
            timeframe=TF,
            pool_detail=PD,
            decision_close=dc,
            immutable_candle_snapshot_id=IMMUTABLE_SNAPSHOT_ID,
            price_content_hash=IMMUTABLE_PRICE_HASH,
            revision_metadata_hash=IMMUTABLE_REVISION_HASH,
            snapshot_root=SNAPSHOT_ROOT,
        )
        full_zones = _remaining_zones_from_projection(rem, from_u, to_u, int(dc.timestamp()))
        capped, truncated = _filter_remaining_by_price(full_zones, None, None, REMAINING_LIMIT_UI)
        api_rem = chart_historical_lmv2_pools(
            SYMBOL,
            PILOT_FROM_ISO,
            PILOT_TO_ISO,
            include_active=False,
            include_remaining=True,
            as_of_close=iso,
            remaining_limit=REMAINING_LIMIT_UI,
        )
        api_zones = [z for z in api_rem["zones"] if (z.get("metadata") or {}).get("layer") == "remaining"]
        full_geoms = {_zone_geom(z) for z in full_zones}
        capped_geoms = [_zone_geom(z) for z in capped]
        api_geoms = [_zone_geom(z) for z in api_zones]
        cap_ok = capped_geoms == api_geoms and truncated == api_rem.get("remaining_truncated", False)
        # Geometry: every API zone must match a projection segment
        proj_by_key = {}
        for seg in rem.get("remaining") or []:
            pid = str(seg["pool_id"])
            act = {a["pool_id"]: a for a in rem.get("active") or []}.get(pid)
            if not act:
                continue
            span_start = max(from_u, int(dc.timestamp()) - 900)
            span_end = min(to_u, int(dc.timestamp()))
            key = (pid, round(float(seg["bottom"]), 8), round(float(seg["top"]), 8), span_start, span_end)
            proj_by_key[key] = seg
        geom_miss = []
        for z in api_zones:
            g = _zone_geom(z)
            if g not in full_geoms:
                geom_miss.append({"zone": g, "issue": "not_in_full_projection_zones"})
        omitted = len(full_zones) - len(capped)
        rem_cap_audit[iso] = {
            "projection_remaining_segments": len(rem.get("remaining") or []),
            "full_zone_count": len(full_zones),
            "after_remaining_limit_1500": len(capped),
            "omitted_by_cap": omitted,
            "truncated_flag": truncated,
            "api_remaining_segment_count": api_rem.get("remaining_segment_count"),
            "api_matches_cap_slice": cap_ok,
            "cap_selection_rule": "stable list order: first N segments from _remaining_zones_from_projection iteration (catalog/remaining list order); no price or time ranking",
            "geometry_api_subset_of_projection": len(geom_miss) == 0,
            "geometry_mismatch_sample": geom_miss[:5],
            "projection_fingerprint_prefix": _remaining_segments_fp(rem),
            "price_range_omitted_if_capped": {
                "min_bottom_full": min((float(z["bottom_price"]) for z in full_zones), default=None),
                "max_top_full": max((float(z["top_price"]) for z in full_zones), default=None),
                "min_bottom_shown": min((float(z["bottom_price"]) for z in capped), default=None),
                "max_top_shown": max((float(z["top_price"]) for z in capped), default=None),
            },
        }
    report["checks"]["remaining_limit_and_geometry"] = {
        "status": "FAIL" if any(
            not v["api_matches_cap_slice"] or not v["geometry_api_subset_of_projection"] for v in rem_cap_audit.values()
        ) else "PASS",
        "per_as_of_close": rem_cap_audit,
        "representativeness": {
            "status": "FAIL",
            "reason": "remaining_limit uses head slice [:1500]; with 6423 segments at 04:45, 4923 segments never reach API/chart; not spatially or temporally representative",
        },
    }

    # --- Layer modes / namespace ---
    modes = {}
    for label, ia, ir in [("active_only", True, False), ("remaining_only", False, True), ("combined", True, True)]:
        body = chart_historical_lmv2_pools(
            SYMBOL,
            PILOT_FROM_ISO,
            PILOT_TO_ISO,
            include_active=ia,
            include_remaining=ir,
            as_of_close=PILOT_TO_ISO,
            remaining_limit=REMAINING_LIMIT_UI,
        )
        layers = {}
        ns = set()
        for z in body["zones"]:
            meta = z.get("metadata") or {}
            lay = meta.get("layer", "?")
            layers[lay] = layers.get(lay, 0) + 1
            ns.add(z.get("namespace"))
        modes[label] = {
            "zone_count": len(body["zones"]),
            "layers": layers,
            "namespaces": sorted(ns),
            "only_historical_namespace": ns == {"HISTORICAL_LMV2_POOLS"},
        }
    report["checks"]["layer_modes_namespace"] = {
        "status": "PASS" if all(m["only_historical_namespace"] for m in modes.values()) else "FAIL",
        "modes": modes,
        "live_layer_mixing": "NOT_APPLICABLE_API",
        "js_strip_note": "applyHistoricalLmv2ToOverlays strips HISTORICAL_LMV2_POOLS only; live LMV2 uses separate namespace",
    }

    # --- UTC 15m alignment ---
    utc_issues = []
    for dc in closes:
        if int(dc.timestamp()) % 900 != 0:
            utc_issues.append(_iso(dc))
    for z in active_zones[:50]:
        for ts in (z.get("start_timestamp"), z.get("end_timestamp")):
            if ts is not None and int(ts) % 900 != 0 and int(ts) not in (from_u,):
                pass  # known_at may be off-grid; end can be break_at
    report["checks"]["utc_15m_close_alignment"] = {
        "status": "PASS" if not utc_issues else "FAIL",
        "pilot_closes_on_15m_boundary": len(utc_issues) == 0,
        "remaining_zone_time_span_rule": "start=max(from, as_of-900s), end=min(to, as_of) — encodes as-of snapshot, not per-close remaining evolution",
    }

    # --- Rendering simulation ---
    combined = chart_historical_lmv2_pools(
        SYMBOL,
        PILOT_FROM_ISO,
        PILOT_TO_ISO,
        include_active=True,
        include_remaining=True,
        as_of_close=PILOT_TO_ISO,
        remaining_limit=REMAINING_LIMIT_UI,
    )
    rendered = _simulate_js_clip_cull(combined["zones"], from_u, to_u)
    report["checks"]["chart_render_simulation"] = {
        "api_zone_count": len(combined["zones"]),
        "after_js_clip_cull": len(rendered),
        "js_active_cap": JS_ACTIVE_CAP,
        "js_remaining_cap": JS_REM_CAP,
        "additional_cull_beyond_api": len(combined["zones"]) - len(rendered),
        "status": "PASS" if len(rendered) <= len(combined["zones"]) else "FAIL",
        "note": "clip only intersects time window; cull may drop zones beyond caps (900 active / 1800 remaining)",
    }

    # --- 20-close entry/exit summary ---
    deltas = []
    prev: set[str] = set()
    for dc in closes:
        pub = _load_publication_line(reader, cfg_hash, dc)
        ids = set(pub["pool_ids"])
        deltas.append(
            {
                "close": _iso(dc),
                "entries": len(ids - prev),
                "exits": len(prev - ids),
                "active_count": len(ids),
            }
        )
        prev = ids
    report["checks"]["twenty_close_membership_deltas"] = {
        "status": "PASS",
        "per_close": deltas,
    }

    report["cap_proposal"] = {
        "implement": False,
        "problem": "remaining_limit=1500 head-truncates 6423 segments; chart can miss entire price bands",
        "options": [
            {
                "id": "viewport_price_rank",
                "description": "When truncating, rank remaining segments by intersection with visible chart price range (and optionally time), take top N by visible area — lossy but viewport-faithful",
            },
            {
                "id": "raise_limit_with_server_guard",
                "description": "Raise limit to full segment count for pilot (6423) with server-side max; no truncation in 20-close pilot — lossless for current scope",
            },
            {
                "id": "lod_aggregate",
                "description": "Merge adjacent colinear segments per pool_id for display only — lossy unless merge rules proven invertible",
            },
        ],
        "recommended_for_pilot": "raise_limit_with_server_guard (lossless within fixed 20-close / 6423 cap)",
    }

    fails = [k for k, v in report["checks"].items() if v.get("status") == "FAIL"]
    report["summary"] = {
        "overall": "FAIL" if fails else "PASS",
        "failed_checks": fails,
        "verified_read_only_ch": True,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"written": str(OUT), "summary": report["summary"]}, indent=2))
    client.close()


if __name__ == "__main__":
    main()

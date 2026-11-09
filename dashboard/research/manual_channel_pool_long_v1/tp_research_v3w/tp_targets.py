"""Resolve TP targets per model (static at entry decision)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pool_d2_metrics import analyze_d2_landscape

from config_v3w import TP2Params, TP2_GRID, TP3_VARIANTS
from zone_features import ResearchZone, enrich_zone_origins, merge_qualified_zones, zone_score


@dataclass
class TPResolution:
    model_id: str
    ok: bool
    reason: str
    pool_tp: float | None
    channel_line: Any | None
    tp_prices: list[float]
    leg_weights: list[float]
    meta: dict[str, Any]


def _qualified(row: dict) -> tuple[list[dict], datetime | None, list, Any]:
    members = row.get("_pool_members")
    dc_s = row.get("_pool_decision_close") or ""
    if not members or not dc_s:
        return [], None, [], None
    from datetime import timezone

    dc = datetime.strptime(dc_s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    entry = float(row["entry_price"])
    _m, _bands, qualified = analyze_d2_landscape(members, dc, entry, apply_provisional_research_filter=False)
    return qualified, dc, members, dc


def _fail(model_id: str, reason: str) -> TPResolution:
    return TPResolution(model_id, False, reason, None, None, [], [], {})


def resolve_tp1(row: dict) -> TPResolution:
    qualified, dc, members, _ = _qualified(row)
    if not qualified:
        return _fail("TP1_nearest_d2", "NO_QUALIFIED")
    entry = float(row["entry_price"])
    t = float(min(qualified, key=lambda x: float(x["lower_bound"]))["lower_bound"])
    return TPResolution(
        "TP1_nearest_d2",
        True,
        "",
        t,
        None,
        [t],
        [1.0],
        {"dist_pct": (t - entry) / entry * 100.0},
    )


def resolve_tp4(row: dict) -> TPResolution:
    qualified, _, _, _ = _qualified(row)
    if not qualified:
        return _fail("TP4_far_d2_max", "NO_QUALIFIED")
    entry = float(row["entry_price"])
    t = float(max(qualified, key=lambda x: float(x["lower_bound"]))["lower_bound"])
    return TPResolution(
        "TP4_far_d2_max",
        True,
        "",
        t,
        None,
        [t],
        [1.0],
        {"dist_pct": (t - entry) / entry * 100.0},
    )


def resolve_tp5_v3u(row: dict) -> TPResolution:
    pool_tp = row.get("_pool_tp")
    ch = row.get("_channel_line")
    if pool_tp is None and ch is None:
        return _fail("TP5_v3u_channel_pool", "NO_TP_NO_CHANNEL")
    tp = float(pool_tp) if pool_tp is not None else None
    mode = row.get("tp_mode", "")
    return TPResolution(
        "TP5_v3u_channel_pool",
        True,
        "",
        tp,
        ch,
        [tp] if tp else [],
        [1.0],
        {"tp_mode": mode, "channel_id": row.get("tp_channel_line_id", "")},
    )


def resolve_tp2(row: dict, params: TP2Params) -> TPResolution:
    qualified, dc, members, _ = _qualified(row)
    if not qualified or dc is None:
        return _fail(params.code, "NO_QUALIFIED")
    entry = float(row["entry_price"])
    zones = merge_qualified_zones(qualified, entry, merge_gap_pct=0.15)
    enrich_zone_origins(zones, members, dc, entry)
    pick: ResearchZone | None = None
    for z in zones:
        if z.dist_from_entry_pct < 0.5 - 1e-9:
            continue
        if z.width_pct + 1e-12 < params.min_width_pct:
            continue
        if z.max_depth < params.min_depth:
            continue
        if z.unique_origins < params.min_origins:
            continue
        pick = z
        break
    if pick is None:
        return _fail(params.code, "NO_SUBSTANTIAL_ZONE")
    t = pick.bottom
    return TPResolution(
        params.code,
        True,
        "",
        t,
        None,
        [t],
        [1.0],
        {
            "zone_top": pick.top,
            "zone_depth": pick.max_depth,
            "zone_origins": pick.unique_origins,
            "zone_width_pct": pick.width_pct,
        },
    )


def resolve_tp3(row: dict, variant: str) -> TPResolution:
    qualified, dc, members, _ = _qualified(row)
    if not qualified or dc is None:
        return _fail(variant, "NO_QUALIFIED")
    entry = float(row["entry_price"])
    zones = merge_qualified_zones(qualified, entry, merge_gap_pct=0.15)
    enrich_zone_origins(zones, members, dc, entry)
    cand = [z for z in zones if z.dist_from_entry_pct >= 0.5 - 1e-9 and z.max_depth >= 2]
    if not cand:
        return _fail(variant, "NO_ZONE")
    pick = max(cand, key=lambda z: zone_score(z, variant))
    t = pick.bottom
    return TPResolution(
        variant,
        True,
        "",
        t,
        None,
        [t],
        [1.0],
        {"score": zone_score(pick, variant), "zone_top": pick.top},
    )


def resolve_multi(row: dict, model_id: str) -> TPResolution:
    qualified, dc, members, _ = _qualified(row)
    if not qualified:
        return _fail(model_id, "NO_QUALIFIED")
    entry = float(row["entry_price"])
    ql = sorted(qualified, key=lambda x: float(x["lower_bound"]))
    zones = merge_qualified_zones(qualified, entry, merge_gap_pct=0.15)
    enrich_zone_origins(zones, members, dc, entry) if dc and members else None
    subs = [
        z
        for z in zones
        if z.dist_from_entry_pct >= 0.5 - 1e-9 and z.max_depth >= 3 and z.unique_origins >= 3
    ]

    if model_id == "M1_50_50_nearest_2d2":
        if len(ql) < 2:
            return _fail(model_id, "NEED_2_D2")
        tps = [float(ql[0]["lower_bound"]), float(ql[1]["lower_bound"])]
        return TPResolution(model_id, True, "", tps[0], None, tps, [0.5, 0.5], {})

    if model_id == "M2_30_30_40_nearest_3d2":
        if len(ql) < 3:
            return _fail(model_id, "NEED_3_D2")
        tps = [float(x["lower_bound"]) for x in ql[:3]]
        return TPResolution(model_id, True, "", tps[0], None, tps, [0.3, 0.3, 0.4], {})

    if model_id == "M3_50_50_substantial_2zones":
        if len(subs) < 2:
            return _fail(model_id, "NEED_2_SUBSTANTIAL")
        tps = [subs[0].bottom, subs[1].bottom]
        return TPResolution(model_id, True, "", tps[0], None, tps, [0.5, 0.5], {})

    if model_id == "M4_half_substantial_half_far":
        far = resolve_tp4(row)
        if not subs:
            return _fail(model_id, "NO_SUBSTANTIAL")
        if not far.ok:
            return _fail(model_id, "NO_FAR")
        tps = [subs[0].bottom, float(far.pool_tp)]
        return TPResolution(model_id, True, "", tps[0], None, tps, [0.5, 0.5], {})

    if model_id == "M5_half_nearest_be_rest_far":
        if not ql:
            return _fail(model_id, "NO_D2")
        far = resolve_tp4(row)
        if not far.ok:
            return _fail(model_id, "NO_FAR")
        tps = [float(ql[0]["lower_bound"]), float(far.pool_tp)]
        return TPResolution(model_id, True, "", tps[0], None, tps, [0.5, 0.5], {"breakeven_after_leg": 0})

    return _fail(model_id, "UNKNOWN_MODEL")


def all_models() -> list[str]:
    ids = ["TP1_nearest_d2", "TP4_far_d2_max", "TP5_v3u_channel_pool"]
    ids.extend(p.code for p in TP2_GRID)
    ids.extend(TP3_VARIANTS)
    ids.extend(
        [
            "M1_50_50_nearest_2d2",
            "M2_30_30_40_nearest_3d2",
            "M3_50_50_substantial_2zones",
            "M4_half_substantial_half_far",
            "M5_half_nearest_be_rest_far",
        ]
    )
    return ids


def resolve(model_id: str, row: dict) -> TPResolution:
    if model_id == "TP1_nearest_d2":
        return resolve_tp1(row)
    if model_id == "TP4_far_d2_max":
        return resolve_tp4(row)
    if model_id == "TP5_v3u_channel_pool":
        return resolve_tp5_v3u(row)
    for p in TP2_GRID:
        if model_id == p.code:
            return resolve_tp2(row, p)
    if model_id in TP3_VARIANTS:
        return resolve_tp3(row, model_id)
    if model_id.startswith("M"):
        return resolve_multi(row, model_id)
    return _fail(model_id, "UNKNOWN")

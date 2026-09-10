"""Compose quality, replay, features, families, and overall class."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..bounded_level_first_analyzer_pilot_v1.local_analyzer import evidence_window
from .classify import overall_class
from .families import evaluate_families, temporal_from_families
from .features import raw_book_features
from .quality import quality_precheck, replay_quality_fail, spatial_coverage
from .replay_local import replay_evidence_window
from .sides import normalized_sides


def require_level_zone(zone_low: Any, zone_high: Any) -> tuple[float, float]:
    if zone_low in {None, ""} or zone_high in {None, ""}:
        raise ValueError("level zone required")
    lo, hi = float(zone_low), float(zone_high)
    if hi < lo:
        raise ValueError("level zone inverted")
    return lo, hi


def require_direction(value: Any) -> str:
    if value not in {"BULLISH", "BEARISH"}:
        raise ValueError("causal reaction_direction required")
    return str(value)


def analyze_episode(
    episode: dict[str, Any],
    *,
    contract: dict[str, Any],
    engine_cfg: dict[str, Any],
    localized_coverage: dict[str, Any] | None = None,
    bundle: dict[str, Any] | None = None,
    allow_replay: bool = True,
) -> dict[str, Any]:
    symbol = str(episode.get("symbol") or "BTCUSDT")
    reaction_class = str(episode.get("reaction_class") or "")
    direction_raw = episode.get("reaction_direction") or None
    undirected = direction_raw not in {"BULLISH", "BEARISH"}
    touch = parse_utc(episode["first_touch_ts"]) if episode.get("first_touch_ts") else None
    det = parse_utc(episode["detection_available_at"]) if episode.get("detection_available_at") else None
    start, end = (None, None)
    if touch and det:
        start, end = evidence_window(first_touch=touch, detection=det)
        if end > det:
            raise RuntimeError("evidence after detection")

    pre = {
        "pass": False,
        "replay_allowed": False,
        "reason": "REACTION_DIRECTION_UNAVAILABLE" if undirected else "MISSING_TIMESTAMPS",
        "overall_if_stopped": "FULL_OB_DIRECTION_NOT_EVALUATED" if undirected else "FULL_OB_NOT_EVALUATED",
        "coverage_span_id": episode.get("coverage_span_id") or "",
    }
    if not undirected and touch and det:
        pre = quality_precheck(
            symbol=symbol,
            first_touch=touch,
            detection=det,
            reaction_direction=direction_raw,
            localized_coverage=localized_coverage,
        )
    if bundle is not None:
        pre["replay_allowed"] = True
        pre["pass"] = True

    replayed = False
    if undirected:
        families = {}
        overall = overall_class(families, quality_ok=False, undirected=True, min_support_for_strong=2)
        return _pack(episode, pre, None, None, {}, families, {}, overall, replayed=False, skipped_reason="UNDIRECTED")

    if not pre.get("replay_allowed"):
        families = {}
        overall = overall_class(families, quality_ok=False, undirected=False, min_support_for_strong=2)
        overall["reason"] = pre.get("reason") or overall["reason"]
        return _pack(episode, pre, None, None, {}, families, {}, overall, replayed=False, skipped_reason=pre.get("reason"))

    if bundle is None:
        if not allow_replay:
            raise RuntimeError("replay required but allow_replay is false")
        bundle = replay_evidence_window(
            symbol=symbol,
            evidence_start=start,
            evidence_end=end,
            first_touch=touch,
            detection=det,
            cfg=engine_cfg,
        )
        replayed = True

    rq = replay_quality_fail(
        replay=bundle.get("replay") if bundle.get("ok", True) else bundle.get("replay") or bundle,
        states=bundle.get("states_100ms") or [],
        ordering=bundle.get("ordering_confidence"),
        sequence_gaps=int(bundle.get("sequence_gaps") or (bundle.get("replay") or {}).get("sequence_gaps") or 0),
    )
    if not bundle.get("ok", True):
        rq = {"pass": False, "reasons": [bundle.get("error") or "REPLAY_NOT_OK"], "crossed_book_buckets": 0, "sequence_gaps": 0}

    zone_low, zone_high = require_level_zone(episode.get("level_zone_low"), episode.get("level_zone_high"))
    adjacent = float(engine_cfg["nearby_refill_max_bps"])
    bids = bundle.get("bids_at_touch") or (bundle.get("replay") or {}).get("initial_bids") or {}
    asks = bundle.get("asks_at_touch") or (bundle.get("replay") or {}).get("initial_asks") or {}
    spatial = spatial_coverage(
        reaction_class=reaction_class,
        reaction_direction=str(direction_raw),
        zone_low=zone_low,
        zone_high=zone_high,
        adjacent_bps=adjacent,
        bids=bids,
        asks=asks,
        mid_hint=bundle.get("mid_at_touch"),
    )
    quality_ok = bool(rq.get("pass") and spatial.get("spatial_pass"))
    features = raw_book_features(bundle=bundle, first_touch=touch, detection=det, spatial=spatial) if bundle.get("ok", True) else {}
    families = evaluate_families(
        reaction_direction=str(direction_raw),
        spatial=spatial,
        walls=bundle.get("walls") or [],
        refills=bundle.get("refills") or [],
        features=features,
        contract=contract,
        first_touch=touch,
        detection=det,
        quality_ok=quality_ok,
    )
    temporal = temporal_from_families(families, first_touch=touch, detection=det) if quality_ok else {
        "first_support_ts": None,
        "last_support_ts": None,
        "first_contradiction_ts": None,
        "last_contradiction_ts": None,
        "support_present_at_detection": False,
        "contradiction_present_at_detection": False,
        "evidence_persisted": False,
        "evidence_disappeared_before_detection": False,
    }
    min_strong = int((contract.get("v1_contract_rules_predeclared") or {}).get("strongly_supports_min_distinct_support_families") or 2)
    overall = overall_class(families, quality_ok=quality_ok, undirected=False, min_support_for_strong=min_strong)
    if not quality_ok:
        overall["quality_reasons"] = list(rq.get("reasons") or []) + (
            ["SPATIAL_COVERAGE_MISSING"] if not spatial.get("spatial_pass") else []
        )
    return _pack(
        episode,
        pre,
        rq,
        spatial,
        features,
        families,
        temporal,
        overall,
        replayed=replayed,
        skipped_reason=None,
        bundle=bundle,
        sides=normalized_sides(str(direction_raw)),
    )


def reclassify_stored_episode(rec: dict[str, Any], *, contract: dict[str, Any]) -> dict[str, Any]:
    """Recompute families from stored walls/refills/features. No archive replay."""
    ep = rec["episode"]
    direction = ep.get("reaction_direction")
    if direction not in {"BULLISH", "BEARISH"}:
        return rec
    touch = parse_utc(ep["first_touch_ts"])
    det = parse_utc(ep["detection_available_at"])
    rq = dict(rec.get("replay_quality") or {})
    reasons = [r for r in rq.get("reasons") or [] if r != "ORDERING_INSUFFICIENT"]
    rq["reasons"] = reasons
    rq["pass"] = not reasons
    rq["ordering_fail_closed_for_book_families"] = False
    rq["ordering_ambiguous_trade_book_ties_recorded"] = bool(
        "AMBIGUOUS" in str(rq.get("ordering_quality") or "")
    )
    spatial = rec.get("spatial") or {}
    quality_ok = bool(rq.get("pass") and spatial.get("spatial_pass"))
    bundle = rec.get("bundle") or {}
    families = evaluate_families(
        reaction_direction=str(direction),
        spatial=spatial,
        walls=bundle.get("walls") or [],
        refills=bundle.get("refills") or [],
        features=rec.get("features") or {},
        contract=contract,
        first_touch=touch,
        detection=det,
        quality_ok=quality_ok,
    )
    temporal = temporal_from_families(families, first_touch=touch, detection=det) if quality_ok else rec.get("temporal")
    min_strong = int((contract.get("v1_contract_rules_predeclared") or {}).get("strongly_supports_min_distinct_support_families") or 2)
    overall = overall_class(families, quality_ok=quality_ok, undirected=False, min_support_for_strong=min_strong)
    if not quality_ok:
        overall["quality_reasons"] = list(rq.get("reasons") or []) + (
            ["SPATIAL_COVERAGE_MISSING"] if not spatial.get("spatial_pass") else []
        )
    rec = dict(rec)
    rec["replay_quality"] = rq
    rec["families"] = families
    rec["temporal"] = temporal
    rec["overall"] = overall
    rec["status"] = "COMPLETE"
    rec["replayed"] = True
    rec["reclassified_without_replay"] = True
    rec["candidate_type_used"] = False
    rec["continuation_slots_used"] = False
    return rec


def _pack(episode, pre, rq, spatial, features, families, temporal, overall, **extra) -> dict[str, Any]:
    clean_features = {k: v for k, v in features.items() if not str(k).startswith("_")}
    return {
        "episode": episode,
        "precheck": pre,
        "replay_quality": rq,
        "spatial": spatial,
        "features": clean_features,
        "families": families,
        "temporal": temporal,
        "overall": overall,
        "candidate_type_used": False,
        "continuation_slots_used": False,
        **extra,
    }

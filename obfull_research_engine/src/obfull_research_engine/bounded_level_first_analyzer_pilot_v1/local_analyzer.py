"""Call existing Full-OB / footprint / OI analyzers on the local evidence window only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..avr_multiscale.config import map_avr_state
from ..single_case_inspector import OI_MAX_AGE_SECONDS, OI_WINDOWS_S
from ..single_case_inspector.ch_modalities import load_oi_samples, oi_window_metrics
from ..single_case_inspector.evidence import CLEAR_BUY, CLEAR_SELL
from ..single_case_inspector.footprint_avr import build_avr_1s, load_second_series
from ..single_case_inspector.full_ob import FULL_OB_NULL_FIELDS
from ..timeparse import format_utc_z
from . import EVIDENCE_PRE_TOUCH_S
from .episodes import parse_utc


def evidence_window(*, first_touch: datetime, detection: datetime) -> tuple[datetime, datetime]:
    start = parse_utc(first_touch) - timedelta(seconds=EVIDENCE_PRE_TOUCH_S)
    end = parse_utc(detection)
    if end < start:
        raise RuntimeError("detection before evidence start")
    return start, end


def _map_vs_reaction(evidence_dir: str | None, reaction_dir: str | None) -> str:
    if reaction_dir is None:
        return "NOT_EVALUATED"
    if evidence_dir in {None, "UNCLEAR", "UNAVAILABLE"}:
        return "UNCLEAR"
    if evidence_dir == reaction_dir:
        return "SUPPORTS"
    if evidence_dir in {"BULLISH", "BEARISH"} and evidence_dir != reaction_dir:
        return "CONTRADICTS"
    return "UNCLEAR"


def _parse_span_ts(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def window_hits_excluded(start: datetime, end: datetime, excluded: list[dict[str, Any]]) -> bool:
    for span in excluded or []:
        a = _parse_span_ts(span["start"])
        b = _parse_span_ts(span["end"])
        if start < b and end > a:
            return True
    return False


def window_inside_usable(start: datetime, end: datetime, usable: list[dict[str, Any]]) -> bool:
    for span in usable or []:
        a = _parse_span_ts(span["start"])
        b = _parse_span_ts(span["end"])
        if a <= start and end <= b:
            return True
    return False


def evaluate_full_ob(
    *,
    symbol: str,
    first_touch: datetime,
    detection: datetime,
    reaction_direction: str | None,
    localized_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start, end = evidence_window(first_touch=first_touch, detection=detection)
    usable = (localized_coverage or {}).get("usable_spans") or []
    excluded = (localized_coverage or {}).get("excluded_spans") or []
    hit_gap = window_hits_excluded(start, end, excluded)
    inside = window_inside_usable(start, end, usable)
    gap_reason = _excluded_reason(start, end, excluded) if hit_gap else None
    if hit_gap or not inside:
        label = "NOT_EVALUATED"
        if gap_reason == "HARD_LOCAL_GAP":
            reason = "ARCHIVE_HARD_LOCAL_GAP"
        elif gap_reason == "TAINTED_SEQUENCE_GAP":
            reason = "SEQUENCE_GAP_OR_TAINTED"
        elif gap_reason == "TOO_SHORT_AFTER_EXCLUSION":
            reason = "SPAN_TOO_SHORT_AFTER_EXCLUSION"
        elif not inside:
            reason = "WINDOW_NOT_INSIDE_USABLE_SPAN"
        else:
            reason = "CROSSED_OR_COVERAGE_FAIL_CLOSED"
        status = "FULL_OB_UNAVAILABLE"
        imbalance = None
    else:
        # Existing assess_full_ob exposes availability only; directional fields stay NOT_AVAILABLE.
        evidence_dir = None
        mapped = _map_vs_reaction(evidence_dir, reaction_direction)
        label = {
            "SUPPORTS": "FULL_OB_SUPPORTS_REACTION",
            "CONTRADICTS": "FULL_OB_CONTRADICTS_REACTION",
            "UNCLEAR": "FULL_OB_UNCLEAR",
            "NOT_EVALUATED": "FULL_OB_UNCLEAR",
        }[mapped]
        reason = "EXISTING_ANALYZER_DIRECTIONAL_FIELDS_NOT_AVAILABLE"
        status = "FULL_OB_AVAILABLE"
        imbalance = None
    return {
        "full_ob_label": label,
        "full_ob_reason": reason,
        "full_ob_crossed_book_buckets": None,
        "full_ob_crossed_provides_no_direction": True,
        "full_ob_replayed_local_window_only": False,
        "full_ob_window_start": format_utc_z(start),
        "full_ob_window_end": format_utc_z(end),
        "full_ob_quality": {
            "policy": "LOCALIZED_EXCLUSION_V1",
            "inside_usable_span": inside,
            "hits_excluded": hit_gap,
            "excluded_reason": gap_reason,
            "unavailable_fields": dict(FULL_OB_NULL_FIELDS) if status == "FULL_OB_AVAILABLE" else {},
        },
        "full_ob_assessment_status": status,
        "full_ob_imbalance_direction": imbalance,
        "market_wide_ob_scan": False,
    }


def _excluded_reason(start: datetime, end: datetime, excluded: list[dict[str, Any]]) -> str | None:
    for span in excluded or []:
        a = _parse_span_ts(span["start"])
        b = _parse_span_ts(span["end"])
        if start < b and end > a:
            return str(span.get("reason") or "OTHER")
    return None


def evaluate_footprint(
    *,
    symbol: str,
    first_touch: datetime,
    detection: datetime,
    reaction_direction: str | None,
    series: Any = None,
    series_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start, end = evidence_window(first_touch=first_touch, detection=detection)
    if series is None:
        series, _buckets, meta = load_second_series(
            symbol=symbol,
            start_unix=int(start.timestamp()),
            end_unix=int(end.timestamp()),
        )
    else:
        meta = series_meta or {}
    avr = series_meta.get("avr_df") if series_meta else None
    if avr is None:
        avr = build_avr_1s(
            symbol=symbol,
            series=series,
            start_unix=int(start.timestamp()),
            end_unix=int(end.timestamp()),
        )
    dirs: list[str] = []
    states_seen: list[str] = []
    if avr is not None and not getattr(avr, "empty", True):
        det_u = int(parse_utc(detection).timestamp())
        start_u = int(start.timestamp())
        pre = avr[(avr["available_at_unix"] > start_u) & (avr["available_at_unix"] <= det_u)]
        for raw in pre["avr_state"].astype(str):
            mapped_state = map_avr_state(raw)
            states_seen.append(mapped_state)
            if mapped_state in CLEAR_BUY:
                dirs.append("BULLISH")
            elif mapped_state in CLEAR_SELL:
                dirs.append("BEARISH")
    if avr is None or getattr(avr, "empty", True):
        mapped = "UNCLEAR"
        reason = "AVR_EMPTY_OR_NOT_EXECUTED"
        evidence_dir = None
    elif not dirs:
        mapped = "UNCLEAR"
        reason = "NO_CLEAR_MAPPED_AVR_STATE_IN_EVIDENCE_WINDOW"
        evidence_dir = None
    else:
        uniq = set(dirs)
        if uniq == {"BULLISH"}:
            evidence_dir = "BULLISH"
        elif uniq == {"BEARISH"}:
            evidence_dir = "BEARISH"
        else:
            evidence_dir = "UNCLEAR"
        mapped = _map_vs_reaction(evidence_dir, reaction_direction)
        reason = "EXISTING_AVR_CLASSIFY_FEATURES_MAPPED"
    series_meta = {**(series_meta or {}), "mapped_avr_states": states_seen[-12:]}
    label = {
        "SUPPORTS": "FOOTPRINT_SUPPORTS_REACTION",
        "CONTRADICTS": "FOOTPRINT_CONTRADICTS_REACTION",
        "UNCLEAR": "FOOTPRINT_UNCLEAR",
        "NOT_EVALUATED": "NOT_EVALUATED",
    }[mapped]
    return {
        "footprint_label": label,
        "footprint_reason": reason,
        "footprint_direction": evidence_dir if dirs else None,
        "footprint_window_start": format_utc_z(start),
        "footprint_window_end": format_utc_z(end),
        "footprint_meta": {
            **{k: meta.get(k) for k in ("n_buckets", "source")},
            "mapped_avr_state_n": len(states_seen),
            "mapped_avr_unique": sorted(set(states_seen)),
        },
        "uses_dashboard_response_engine": True,
    }


def evaluate_oi(
    *,
    symbol: str,
    first_touch: datetime,
    detection: datetime,
    reaction_direction: str | None,
    price_at_touch: float | None,
    price_at_detection: float | None,
    oi_samples: list[dict[str, Any]] | None = None,
    oi_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start, end = evidence_window(first_touch=first_touch, detection=detection)
    if oi_samples is None:
        samples, meta = load_oi_samples(
            symbol=symbol, start=start - timedelta(seconds=max(OI_WINDOWS_S)), end=end
        )
    else:
        samples, meta = oi_samples, oi_meta or {}
    usable = []
    for window_s in OI_WINDOWS_S:
        w_start = parse_utc(detection) - timedelta(seconds=int(window_s))
        if w_start < start:
            continue
        usable.append(
            oi_window_metrics(
                samples,
                focus_ts=parse_utc(detection),
                window_s=int(window_s),
                price_start=price_at_touch,
                price_end=price_at_detection,
            )
        )
    if not usable:
        return {
            "oi_label": "NOT_EVALUATED",
            "oi_reason": "NO_OI_WINDOW_FITS_INSIDE_EVIDENCE",
            "oi_windows": [],
            "oi_max_age_s": OI_MAX_AGE_SECONDS,
        }
    if any(w.get("coverage") == "UNAVAILABLE" for w in usable) and all(
        w.get("coverage") == "UNAVAILABLE" for w in usable
    ):
        return {
            "oi_label": "OI_UNAVAILABLE",
            "oi_reason": "ASOF_OI_MISSING_OR_STALE",
            "oi_windows": usable,
            "oi_sample_n": meta.get("n"),
        }
    quads = {w.get("quadrant") for w in usable if w.get("coverage") == "OK"}
    if reaction_direction is None:
        label = "OI_MIXED" if len(quads) > 1 else "OI_UNAVAILABLE"
        if quads and reaction_direction is None:
            label = "OI_MIXED"
        return {
            "oi_label": label if reaction_direction is None else label,
            "oi_reason": "NO_REACTION_DIRECTION",
            "oi_windows": usable,
            "oi_quadrants": sorted(quads),
        }
    supportive = 0
    contra = 0
    mixed = 0
    for w in usable:
        if w.get("coverage") != "OK":
            continue
        q = w.get("quadrant")
        if reaction_direction == "BULLISH" and q in {"PRICE_UP_OI_UP", "PRICE_UP_OI_DOWN"}:
            supportive += 1
        elif reaction_direction == "BEARISH" and q in {"PRICE_DOWN_OI_UP", "PRICE_DOWN_OI_DOWN"}:
            supportive += 1
        elif q in {"MIXED_OR_FLAT", "PRICE_FLAT_OR_MIXED"}:
            mixed += 1
        elif q in {"PRICE_UP_OI_UP", "PRICE_UP_OI_DOWN", "PRICE_DOWN_OI_UP", "PRICE_DOWN_OI_DOWN"}:
            contra += 1
        else:
            mixed += 1
    if supportive and contra:
        label = "OI_MIXED"
    elif supportive and not contra:
        label = "OI_SUPPORTIVE"
    elif contra and not supportive:
        label = "OI_CONTRADICTORY"
    elif mixed:
        label = "OI_MIXED"
    else:
        label = "OI_UNAVAILABLE"
    return {
        "oi_label": label,
        "oi_reason": "EXISTING_OI_WINDOW_METRICS_INSIDE_EVIDENCE",
        "oi_windows": usable,
        "oi_quadrants": sorted(quads),
        "oi_raw_directions": [w.get("oi_direction") for w in usable],
        "oi_price_directions": [w.get("price_direction") for w in usable],
        "oi_direction_not_copied_from_reaction": True,
        "oi_sample_n": meta.get("n"),
        "asof_uses_source_event_time_lt_detection": True,
    }


def evaluate_local_analyzers(
    *,
    symbol: str,
    first_touch: datetime,
    detection: datetime,
    reaction_direction: str | None,
    price_at_touch: float | None,
    price_at_detection: float | None,
    localized_coverage: dict[str, Any] | None = None,
    series: Any = None,
    series_meta: dict[str, Any] | None = None,
    oi_samples: list[dict[str, Any]] | None = None,
    oi_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fo = evaluate_full_ob(
        symbol=symbol,
        first_touch=first_touch,
        detection=detection,
        reaction_direction=reaction_direction,
        localized_coverage=localized_coverage,
    )
    fp = evaluate_footprint(
        symbol=symbol,
        first_touch=first_touch,
        detection=detection,
        reaction_direction=reaction_direction,
        series=series,
        series_meta=series_meta,
    )
    oi = evaluate_oi(
        symbol=symbol,
        first_touch=first_touch,
        detection=detection,
        reaction_direction=reaction_direction,
        price_at_touch=price_at_touch,
        price_at_detection=price_at_detection,
        oi_samples=oi_samples,
        oi_meta=oi_meta,
    )
    return {
        **fo,
        **fp,
        **oi,
        "analyzer_window_ends_at_detection": True,
        "does_not_force_direction_from_partial_modalities": True,
        "market_wide_scan": False,
    }

"""Phase-1 RegimeSnapshot + thin SetupActivation (no entry / PA / momentum).

Architecture boundary
--------------------
``RegimeSnapshot`` / ``SetupActivation`` stop at setup activation.
Final entries, TP observation, and lockouts remain in
``signal_tp_audit`` as a **legacy research baseline** only and must not be
imported or called from this module.
"""

from __future__ import annotations

from typing import Any, Literal

TrendDirection = Literal["long", "short", "neutral", "mixed", "unavailable"]
TrendStrength = Literal["weak", "normal", "strong", "unavailable"]
SetupSide = Literal["long", "short"]
SetupType = Literal["continuation_weakness", "regime_change"]
Confidence = Literal["low", "medium", "high"]
Alignment = Literal[
    "aligned",
    "opposing",
    "transition",
    "neutral",
    "mixed",
    "unavailable",
    "unknown",
]

BULLISH_REGIMES = frozenset(
    {
        "strong_bullish_trend",
        "bullish_trend",
        "bullish_trend_with_trend_weakness",
    }
)
BEARISH_REGIMES = frozenset(
    {
        "strong_bearish_trend",
        "bearish_trend",
        "bearish_trend_with_trend_weakness",
    }
)
WEAKNESS_REGIMES = frozenset(
    {
        "bullish_trend_with_trend_weakness",
        "bearish_trend_with_trend_weakness",
    }
)
STRONG_REGIMES = frozenset({"strong_bullish_trend", "strong_bearish_trend"})
INTACT_BULLISH = frozenset({"strong_bullish_trend", "bullish_trend"})
INTACT_BEARISH = frozenset({"strong_bearish_trend", "bearish_trend"})

# Fields that must never appear on snapshot / setup (entry belongs downstream).
_FORBIDDEN_ENTRY_KEYS = frozenset(
    {
        "entry_price",
        "tp_price",
        "tp_pct",
        "tp_reached",
        "tp_hit",
        "candles_to_tp",
        "max_favorable_excursion_pct",
        "max_adverse_excursion_pct",
        "mfe_pct",
        "mae_pct",
    }
)


def regime_direction(regime: object | None) -> TrendDirection:
    name = str(regime or "")
    if not name or name == "unavailable":
        return "unavailable"
    if name in BULLISH_REGIMES:
        return "long"
    if name in BEARISH_REGIMES:
        return "short"
    if name == "neutral":
        return "neutral"
    if name == "transition":
        return "mixed"
    return "mixed"


def regime_strength(regime: object | None) -> TrendStrength:
    name = str(regime or "")
    if not name or name == "unavailable":
        return "unavailable"
    if name in STRONG_REGIMES:
        return "strong"
    if name in WEAKNESS_REGIMES:
        return "weak"
    if name in {"bullish_trend", "bearish_trend"}:
        return "normal"
    if name in {"neutral", "transition"}:
        return "weak"
    return "unavailable"


def _tf_regime_from_payload(payload: object | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("regime") is not None:
        return str(payload.get("regime"))
    summary = payload.get("regime_summary") or {}
    if isinstance(summary, dict) and summary.get("regime") is not None:
        return str(summary.get("regime"))
    return None


def _normalize_by_timeframe(
    by_timeframe: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Reduce full TF audits or summaries to lightweight per-TF dicts."""
    out: dict[str, dict[str, Any]] = {}
    for tf, payload in (by_timeframe or {}).items():
        if not isinstance(payload, dict):
            continue
        summary = payload.get("regime_summary") if "regime_summary" in payload else None
        if isinstance(summary, dict) and "regime" in summary:
            base = dict(summary)
        elif "regime" in payload:
            base = {
                "regime": payload.get("regime"),
                "confidence": payload.get("confidence"),
                "reason_codes": payload.get("reason_codes") or [],
                "bullish_trend_intact": payload.get("bullish_trend_intact"),
                "bearish_trend_intact": payload.get("bearish_trend_intact"),
                "structural_weakness": payload.get("structural_weakness"),
                "momentum_weakness_families": payload.get("momentum_weakness_families")
                or [],
                "multi_metric_exhaustion": payload.get("multi_metric_exhaustion"),
                "last_bar_rollover": payload.get("last_bar_rollover"),
            }
        else:
            base = {"regime": _tf_regime_from_payload(payload)}
        base["timeframe"] = str(tf)
        out[str(tf)] = base
    return out


def _alignment(reference_direction: TrendDirection, tf_regime: object | None) -> Alignment:
    if reference_direction in {"unavailable", "mixed"}:
        return "unknown"
    tf_dir = regime_direction(tf_regime)
    if tf_dir == "unavailable":
        return "unavailable"
    if tf_dir == "mixed":
        return "transition"
    if tf_dir == "neutral":
        return "neutral"
    if tf_dir == reference_direction:
        return "aligned"
    if {tf_dir, reference_direction} == {"long", "short"}:
        return "opposing"
    return "mixed"


def _has_weakness_flags(tf_summary: dict[str, Any] | None) -> bool:
    if not tf_summary:
        return False
    if tf_summary.get("structural_weakness"):
        return True
    if tf_summary.get("multi_metric_exhaustion"):
        return True
    families = tf_summary.get("momentum_weakness_families") or []
    if isinstance(families, list) and len(families) >= 2:
        return True
    if tf_summary.get("last_bar_rollover") is True:
        return True
    return False


def _reason_code_list(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return []


def build_regime_snapshot(
    *,
    decision_time: object,
    combined_regime: object | None = None,
    previous_combined_regime: object | None = None,
    regime_5m: object | None = None,
    regime_15m: object | None = None,
    regime_30m: object | None = None,
    by_timeframe: dict[str, Any] | None = None,
    reason_codes: list[Any] | None = None,
    point_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a serialisable RegimeSnapshot (no entry / TP fields).

    Prefer explicit regime strings for unit tests. When ``point_audit`` is
    provided, TF/combined fields are taken from it unless overridden.
    """
    audit = point_audit or {}
    combined_payload = audit.get("combined_regime") or audit.get("regime_summary") or {}
    if not isinstance(combined_payload, dict):
        combined_payload = {}

    tf_map = _normalize_by_timeframe(
        by_timeframe
        if by_timeframe is not None
        else (combined_payload.get("by_timeframe") or audit.get("by_timeframe"))
    )

    r5 = regime_5m if regime_5m is not None else _tf_regime_from_payload(tf_map.get("5m"))
    r15 = (
        regime_15m if regime_15m is not None else _tf_regime_from_payload(tf_map.get("15m"))
    )
    r30 = (
        regime_30m if regime_30m is not None else _tf_regime_from_payload(tf_map.get("30m"))
    )

    if combined_regime is not None:
        combined_name = str(combined_regime) if combined_regime != "" else None
    else:
        combined_name = combined_payload.get("regime")
        if combined_name is None and r15 is not None:
            # Mirror combine_timeframe_regimes primary rule (15m).
            combined_name = str(r15)
        elif combined_name is None and r5 is not None:
            combined_name = str(r5)

    combined_name = str(combined_name) if combined_name is not None else "unavailable"
    prev = (
        None
        if previous_combined_regime is None or previous_combined_regime == ""
        else str(previous_combined_regime)
    )
    regime_change = bool(prev is not None and prev != combined_name)

    direction = regime_direction(combined_name)
    strength = regime_strength(combined_name)

    tf_regimes = [r for r in (r5, r15, r30, combined_name) if r]
    transition_detected = any(str(r) == "transition" for r in tf_regimes)

    trend_weakness = any(str(r) in WEAKNESS_REGIMES for r in tf_regimes)
    if not trend_weakness:
        for tf_summary in tf_map.values():
            if _has_weakness_flags(tf_summary):
                trend_weakness = True
                break

    codes = reason_codes
    if codes is None:
        codes = _reason_code_list(combined_payload.get("reason_codes"))

    # Ensure by_timeframe always exposes the three canonical slots when known.
    for tf, regime in (("5m", r5), ("15m", r15), ("30m", r30)):
        if regime is None:
            continue
        slot = tf_map.get(tf) or {"timeframe": tf}
        slot = dict(slot)
        slot["regime"] = str(regime)
        slot["timeframe"] = tf
        tf_map[tf] = slot

    snapshot: dict[str, Any] = {
        "decision_time": str(decision_time),
        "regime_5m": r5,
        "regime_15m": r15,
        "regime_30m": r30,
        "combined_regime": combined_name,
        "previous_combined_regime": prev,
        "regime_change": regime_change,
        "trend_direction": direction,
        "trend_strength": strength,
        "trend_weakness": bool(trend_weakness),
        "transition_detected": bool(transition_detected),
        "reason_codes": codes,
        "higher_timeframe_alignment": _alignment(direction, r30),
        "lower_timeframe_alignment": _alignment(direction, r5),
        "by_timeframe": tf_map,
    }
    _assert_no_entry_fields(snapshot)
    return snapshot


def build_regime_snapshot_from_point_audit(
    point_audit: dict[str, Any],
    *,
    previous_combined_regime: object | None = None,
) -> dict[str, Any]:
    """Convenience wrapper around :func:`build_regime_snapshot`."""
    return build_regime_snapshot(
        decision_time=point_audit.get("decision_time"),
        previous_combined_regime=previous_combined_regime,
        point_audit=point_audit,
    )


def _bullish_context_intact(snapshot: dict[str, Any]) -> bool:
    combined = str(snapshot.get("combined_regime") or "")
    r15 = str(snapshot.get("regime_15m") or "")
    if combined in BULLISH_REGIMES or r15 in BULLISH_REGIMES:
        return True
    tf15 = (snapshot.get("by_timeframe") or {}).get("15m") or {}
    return bool(tf15.get("bullish_trend_intact"))


def _bearish_context_intact(snapshot: dict[str, Any]) -> bool:
    combined = str(snapshot.get("combined_regime") or "")
    r15 = str(snapshot.get("regime_15m") or "")
    if combined in BEARISH_REGIMES or r15 in BEARISH_REGIMES:
        return True
    tf15 = (snapshot.get("by_timeframe") or {}).get("15m") or {}
    return bool(tf15.get("bearish_trend_intact"))


def _has_bullish_weakness(snapshot: dict[str, Any]) -> bool:
    for key in ("combined_regime", "regime_5m", "regime_15m", "regime_30m"):
        if str(snapshot.get(key) or "") == "bullish_trend_with_trend_weakness":
            return True
    return False


def _has_bearish_weakness(snapshot: dict[str, Any]) -> bool:
    for key in ("combined_regime", "regime_5m", "regime_15m", "regime_30m"):
        if str(snapshot.get(key) or "") == "bearish_trend_with_trend_weakness":
            return True
    return False


def _confidence_rank(value: Confidence) -> int:
    return {"low": 0, "medium": 1, "high": 2}[value]


def _cap_confidence(value: Confidence, ceiling: Confidence) -> Confidence:
    return value if _confidence_rank(value) <= _confidence_rank(ceiling) else ceiling


def _empty_setup(
    *,
    snapshot: dict[str, Any],
    blockers: list[str] | None = None,
    warnings: list[str] | None = None,
    invalidation_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "setup_activated": False,
        "setup_side": None,
        "setup_type": None,
        "setup_activation_timestamp": snapshot.get("decision_time"),
        "activating_regime": snapshot.get("combined_regime"),
        "previous_regime": snapshot.get("previous_combined_regime"),
        "confidence": None,
        "blockers": list(blockers or []),
        "warnings": list(warnings or []),
        "invalidation_reason": invalidation_reason,
        "source_snapshot": snapshot,
    }


def _apply_htf_policy(
    *,
    setup_side: SetupSide,
    snapshot: dict[str, Any],
    confidence: Confidence,
) -> tuple[bool, Confidence, list[str], list[str]]:
    """Return (allowed, confidence, blockers, warnings) for HTF policy v1."""
    blockers: list[str] = []
    warnings: list[str] = []
    r30 = snapshot.get("regime_30m")
    r30_dir = regime_direction(r30)

    if str(r30 or "") == "transition":
        warnings.append("HTF_TRANSITION")
        confidence = _cap_confidence(confidence, "medium")

    opposing = (setup_side == "long" and r30_dir == "short") or (
        setup_side == "short" and r30_dir == "long"
    )
    if opposing:
        blockers.append("HTF_OPPOSING_TREND")
        return False, confidence, blockers, warnings

    return True, confidence, blockers, warnings


def evaluate_setup_activation(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Thin SetupActivation from a RegimeSnapshot (no entry / PA / momentum).

    Rules (Phase 1)
    ---------------
    * continuation_weakness long/short when weakness regime present and
      directional context remains intact (15m/combined).
    * regime_change long/short on combined edge into intact trend labels.
    * intact trends without edge/weakness → context_only (no setup).
    * unavailable → blocked; neutral → no directional setup.
    * transition alone does not activate; HTF transition warns / caps confidence.
    """
    _assert_no_entry_fields(snapshot)
    combined = str(snapshot.get("combined_regime") or "unavailable")
    previous = snapshot.get("previous_combined_regime")
    prev_name = str(previous) if previous is not None else None

    if combined == "unavailable":
        out = _empty_setup(
            snapshot=snapshot,
            blockers=["UNAVAILABLE"],
            invalidation_reason="unavailable",
        )
        _assert_no_entry_fields(out)
        return out

    if combined == "neutral":
        out = _empty_setup(snapshot=snapshot, invalidation_reason="neutral_context")
        _assert_no_entry_fields(out)
        return out

    # Candidate selection (weakness preferred over regime_change).
    candidate_side: SetupSide | None = None
    candidate_type: SetupType | None = None
    confidence: Confidence = "medium"
    activating = combined

    if _has_bullish_weakness(snapshot) and _bullish_context_intact(snapshot):
        candidate_side = "long"
        candidate_type = "continuation_weakness"
        activating = next(
            (
                str(snapshot.get(k))
                for k in ("regime_5m", "regime_15m", "combined_regime", "regime_30m")
                if str(snapshot.get(k) or "") == "bullish_trend_with_trend_weakness"
            ),
            combined,
        )
        confidence = "high" if combined == "bullish_trend_with_trend_weakness" else "medium"
    elif _has_bearish_weakness(snapshot) and _bearish_context_intact(snapshot):
        candidate_side = "short"
        candidate_type = "continuation_weakness"
        activating = next(
            (
                str(snapshot.get(k))
                for k in ("regime_5m", "regime_15m", "combined_regime", "regime_30m")
                if str(snapshot.get(k) or "") == "bearish_trend_with_trend_weakness"
            ),
            combined,
        )
        confidence = "high" if combined == "bearish_trend_with_trend_weakness" else "medium"
    elif snapshot.get("regime_change") and combined in INTACT_BULLISH:
        if prev_name is None or prev_name not in BULLISH_REGIMES:
            candidate_side = "long"
            candidate_type = "regime_change"
            activating = combined
            confidence = "high" if combined == "strong_bullish_trend" else "medium"
    elif snapshot.get("regime_change") and combined in INTACT_BEARISH:
        if prev_name is None or prev_name not in BEARISH_REGIMES:
            candidate_side = "short"
            candidate_type = "regime_change"
            activating = combined
            confidence = "high" if combined == "strong_bearish_trend" else "medium"

    if candidate_side is None or candidate_type is None:
        # Intact trend / transition / other → context only.
        warnings: list[str] = []
        if combined == "transition" or snapshot.get("transition_detected"):
            warnings.append("TRANSITION_CONTEXT")
        out = _empty_setup(snapshot=snapshot, warnings=warnings)
        _assert_no_entry_fields(out)
        return out

    allowed, confidence, blockers, warnings = _apply_htf_policy(
        setup_side=candidate_side,
        snapshot=snapshot,
        confidence=confidence,
    )

    # 15m carries direction + 5m weakness/pullback is explicitly allowed by policy.
    # (Already encoded: weakness may sit on 5m while 15m/combined stay directional.)

    if not allowed:
        out = _empty_setup(
            snapshot=snapshot,
            blockers=blockers,
            warnings=warnings,
            invalidation_reason=blockers[0] if blockers else "blocked",
        )
        out["setup_side"] = candidate_side
        out["setup_type"] = candidate_type
        out["activating_regime"] = activating
        out["confidence"] = confidence
        _assert_no_entry_fields(out)
        return out

    result = {
        "setup_activated": True,
        "setup_side": candidate_side,
        "setup_type": candidate_type,
        "setup_activation_timestamp": snapshot.get("decision_time"),
        "activating_regime": activating,
        "previous_regime": prev_name,
        "confidence": confidence,
        "blockers": blockers,
        "warnings": warnings,
        "invalidation_reason": None,
        "source_snapshot": snapshot,
    }
    _assert_no_entry_fields(result)
    return result


def _assert_no_entry_fields(payload: dict[str, Any]) -> None:
    stack = [payload]
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        bad = _FORBIDDEN_ENTRY_KEYS.intersection(item)
        # Allow nested source_snapshot recursion but forbid entry keys anywhere.
        if bad:
            raise ValueError(f"entry fields are not allowed in Phase-1 payloads: {sorted(bad)}")
        for value in item.values():
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(v for v in value if isinstance(v, dict))


def snapshot_and_setup_from_point_audit(
    point_audit: dict[str, Any],
    *,
    previous_combined_regime: object | None = None,
) -> dict[str, Any]:
    """Return ``{regime_snapshot, setup_activation}`` for a causal point audit."""
    snapshot = build_regime_snapshot_from_point_audit(
        point_audit,
        previous_combined_regime=previous_combined_regime,
    )
    return {
        "regime_snapshot": snapshot,
        "setup_activation": evaluate_setup_activation(snapshot),
    }

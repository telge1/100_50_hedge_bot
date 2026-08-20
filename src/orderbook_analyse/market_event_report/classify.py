"""Diagnostic classification labels (not trading signals)."""

from __future__ import annotations

from typing import Any

CLASSIFICATION_LABELS: tuple[str, ...] = (
    "RANGE_EXPANSION",
    "FLOW_ALIGNED_MOVE",
    "FLOW_OPPOSED_MOVE",
    "POSSIBLE_ABSORPTION",
    "POSSIBLE_RECLAIM",
    "POSSIBLE_BREAKOUT",
    "NO_CLEAR_CONFIRMATION",
)

# Stable thresholds for unit tests / reproducible diagnostics
RANGE_EXPANSION_MIN_BOTH_MFE_MAE = 0.004  # 0.4% each way on 60m path
RANGE_RATIO_VS_PRE15 = 1.5
FLOW_ALIGN_ABS_DELTA_RATIO = 0.15
FLOW_ALIGN_MIN_ABS_RET_60M = 0.0025
ABSORPTION_MAX_ABS_RET_60M = 0.0015
ABSORPTION_MIN_ABS_DELTA_RATIO = 0.25
BREAKOUT_MIN_ABS_RET_60M = 0.005
RECLAIM_NEAR_BPS = 15.0


def _f(x: Any) -> float | None:
    try:
        if x is None:
            return None
        v = float(x)
        if v != v:  # NaN
            return None
        return v
    except (TypeError, ValueError):
        return None


def classify_event(
    *,
    pre_range_15m: float | None,
    path_60m_long: dict[str, Any] | None,
    path_60m_short: dict[str, Any] | None,
    future_return_60m: float | None,
    event_delta_ratio: float | None,
    event_ofi: float | None = None,
    lld: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return ordered diagnostic labels + primary label.

    Rules are intentionally simple and stable. Multiple labels may apply;
    ``primary`` is the first match in a fixed priority order (not a signal rank).
    """
    labels: list[str] = []
    reasons: dict[str, str] = {}

    long_mfe = _f((path_60m_long or {}).get("mfe"))
    long_mae = _f((path_60m_long or {}).get("mae"))
    short_mfe = _f((path_60m_short or {}).get("mfe"))
    # both-way excursion: use LONG mfe (up) and LONG mae (down) as unsigned ranges
    up = long_mfe
    down = long_mae
    ret60 = _f(future_return_60m)
    d_ratio = _f(event_delta_ratio)
    pre_r = _f(pre_range_15m)

    # RANGE_EXPANSION: both directions move meaningfully
    if (
        up is not None
        and down is not None
        and up >= RANGE_EXPANSION_MIN_BOTH_MFE_MAE
        and down >= RANGE_EXPANSION_MIN_BOTH_MFE_MAE
    ):
        labels.append("RANGE_EXPANSION")
        reasons["RANGE_EXPANSION"] = (
            f"60m up-excursion={up:.4%} and down-excursion={down:.4%} both >= "
            f"{RANGE_EXPANSION_MIN_BOTH_MFE_MAE:.4%}"
        )
    elif (
        pre_r is not None
        and pre_r > 0
        and up is not None
        and down is not None
        and (up + down) / pre_r >= RANGE_RATIO_VS_PRE15
        and (up + down) >= 2 * RANGE_EXPANSION_MIN_BOTH_MFE_MAE
    ):
        labels.append("RANGE_EXPANSION")
        reasons["RANGE_EXPANSION"] = (
            f"60m two-way range {(up + down):.4%} >= {RANGE_RATIO_VS_PRE15}x pre-15m range {pre_r:.4%}"
        )

    flow_sign = None
    if d_ratio is not None and abs(d_ratio) >= FLOW_ALIGN_ABS_DELTA_RATIO:
        flow_sign = 1 if d_ratio > 0 else -1
    elif event_ofi is not None:
        ofi = _f(event_ofi)
        if ofi is not None and abs(ofi) > 0:
            flow_sign = 1 if ofi > 0 else -1

    if ret60 is not None and flow_sign is not None and abs(ret60) >= FLOW_ALIGN_MIN_ABS_RET_60M:
        move_sign = 1 if ret60 > 0 else -1
        if move_sign == flow_sign:
            labels.append("FLOW_ALIGNED_MOVE")
            reasons["FLOW_ALIGNED_MOVE"] = (
                f"60m return {ret60:.4%} aligned with event flow_sign={flow_sign}"
            )
        else:
            labels.append("FLOW_OPPOSED_MOVE")
            reasons["FLOW_OPPOSED_MOVE"] = (
                f"60m return {ret60:.4%} opposed to event flow_sign={flow_sign}"
            )

    if (
        d_ratio is not None
        and abs(d_ratio) >= ABSORPTION_MIN_ABS_DELTA_RATIO
        and ret60 is not None
        and abs(ret60) <= ABSORPTION_MAX_ABS_RET_60M
    ):
        labels.append("POSSIBLE_ABSORPTION")
        reasons["POSSIBLE_ABSORPTION"] = (
            f"|delta_ratio|={abs(d_ratio):.3f} with small |ret60|={abs(ret60):.4%}"
        )

    lld = lld or {}
    if lld.get("available"):
        touch = str(lld.get("event_interaction") or "")
        broke = "break" in touch
        reclaimed = "reclaim" in touch
        near = "near" in touch or "touch" in touch
        if broke and ret60 is not None and abs(ret60) >= BREAKOUT_MIN_ABS_RET_60M:
            # held away from pool after break
            labels.append("POSSIBLE_BREAKOUT")
            reasons["POSSIBLE_BREAKOUT"] = f"LLD interaction={touch}, |ret60|={abs(ret60):.4%}"
        if reclaimed or (broke and ret60 is not None and abs(ret60) < ABSORPTION_MAX_ABS_RET_60M):
            labels.append("POSSIBLE_RECLAIM")
            reasons["POSSIBLE_RECLAIM"] = f"LLD interaction={touch}"
        elif near and "POSSIBLE_BREAKOUT" not in labels and "POSSIBLE_RECLAIM" not in labels:
            # near-touch alone is not a confirmation label
            pass

    # Priority for primary (first applicable in this order)
    priority = [
        "POSSIBLE_BREAKOUT",
        "POSSIBLE_RECLAIM",
        "POSSIBLE_ABSORPTION",
        "FLOW_ALIGNED_MOVE",
        "FLOW_OPPOSED_MOVE",
        "RANGE_EXPANSION",
    ]
    primary = "NO_CLEAR_CONFIRMATION"
    for lab in priority:
        if lab in labels:
            primary = lab
            break
    if not labels:
        labels = ["NO_CLEAR_CONFIRMATION"]
        reasons["NO_CLEAR_CONFIRMATION"] = "No diagnostic rule fired"
    elif "NO_CLEAR_CONFIRMATION" not in labels and primary == "NO_CLEAR_CONFIRMATION":
        labels.append("NO_CLEAR_CONFIRMATION")

    # Ensure primary is always in labels
    if primary not in labels:
        labels.insert(0, primary)

    return {
        "primary": primary,
        "labels": labels,
        "reasons": reasons,
        "disclaimer": (
            "Diagnostic labels only — not a trading signal, not an entry recommendation, "
            "no strategy / exit / parameter retuning."
        ),
        "label_set": list(CLASSIFICATION_LABELS),
    }

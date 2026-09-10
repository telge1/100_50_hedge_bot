"""Full-OB support for 1s BID/ASK imbalance-shift candidates.

Reuses the existing candidate contract only:

* feature: ``depth_imbalance_bps_0_2``
* band: 0–2 bps from mid, USDT notional ``price * qty``
* formula: ``(bid_depth - ask_depth) / (bid_depth + ask_depth)`` if sum > 1e-12 else 0
* BID_IMBALANCE_SHIFT / high_abs_delta_vs_baseline_median → imbalance increases (bullish book)
* ASK_IMBALANCE_SHIFT / low_abs_delta_vs_baseline_median → imbalance decreases (bearish book)

Outcome-blind. A more bid-heavy book is not executed-buy proof.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

# Same band/feature as episode_candidate_v1.json BID_IMBALANCE_SHIFT / ASK_IMBALANCE_SHIFT.
IMBALANCE_FEATURE = "depth_imbalance_bps_0_2"
IMBALANCE_BAND = "0_2"
IMBALANCE_BAND_LO_BPS = 0.0
IMBALANCE_BAND_HI_BPS = 2.0
SAFE_DIV_EPS = 1e-12
BUCKET_MS = 100

REASON_SUPPORTED_STABLE = "IMBALANCE_SHIFT_SUPPORTED_STABLE"
REASON_PARTIAL_NO_STABILITY = "IMBALANCE_SHIFT_PARTIAL_NO_STABILITY"
REASON_OPPOSITE = "IMBALANCE_SHIFT_OPPOSITE"
REASON_NO_CHANGE = "IMBALANCE_SHIFT_NO_CHANGE"
REASON_MISSING_100MS = "MISSING_100MS_BUCKETS"
REASON_SEQUENCE_GAP = "SEQUENCE_GAP_IN_EVIDENCE_WINDOW"
REASON_CROSSED_BOOK = "CROSSED_BOOK"
REASON_ORDERING_AMBIGUOUS = "ORDERING_AMBIGUOUS"
REASON_BOOK_INVALID = "BOOK_INVALID"
REASON_NOT_IMBALANCE = "NOT_AN_IMBALANCE_CANDIDATE"


def is_imbalance_candidate(candidate_type: str) -> bool:
    return "BID_IMBALANCE_SHIFT" in candidate_type or "ASK_IMBALANCE_SHIFT" in candidate_type


def expected_sign(candidate_type: str) -> int | None:
    """+1 = bullish bid-heavier shift; -1 = bearish ask-heavier shift."""
    if "BID_IMBALANCE_SHIFT" in candidate_type:
        return 1
    if "ASK_IMBALANCE_SHIFT" in candidate_type:
        return -1
    return None


def depth_imbalance(bid_depth: float | None, ask_depth: float | None) -> float | None:
    """Identical to 1s ``depth_imbalance_bps_*`` and 100ms ``_near_band_depths``."""
    if bid_depth is None or ask_depth is None:
        return None
    try:
        b = float(bid_depth)
        a = float(ask_depth)
    except (TypeError, ValueError):
        return None
    if not (pd.notna(b) and pd.notna(a)):
        return None
    total = b + a
    if total <= SAFE_DIV_EPS:
        return 0.0
    return (b - a) / total


def _as_ts(value: Any) -> pd.Timestamp:
    return pd.to_datetime(value, utc=True)


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not pd.notna(x):
        return None
    return x


def row_band_depths(row: dict[str, Any]) -> tuple[float | None, float | None]:
    bid = row.get("bid_depth_notional_usdt_bps_0_2")
    if bid is None:
        bid = row.get("bid_depth_2bps")
    ask = row.get("ask_depth_notional_usdt_bps_0_2")
    if ask is None:
        ask = row.get("ask_depth_2bps")
    return _finite(bid), _finite(ask)


def row_imbalance(row: dict[str, Any]) -> float | None:
    stored = _finite(row.get(IMBALANCE_FEATURE))
    if stored is not None:
        return stored
    bid, ask = row_band_depths(row)
    return depth_imbalance(bid, ask)


def _is_crossed(row: dict[str, Any]) -> bool:
    bb = _finite(row.get("best_bid"))
    ba = _finite(row.get("best_ask"))
    if bb is None or ba is None:
        return False
    return bb >= ba


def _book_present(row: dict[str, Any]) -> bool:
    bb = _finite(row.get("best_bid"))
    ba = _finite(row.get("best_ask"))
    return bb is not None and ba is not None


def _ordering_ambiguous(row: dict[str, Any]) -> bool:
    return "AMBIGUOUS" in str(row.get("ordering_confidence") or "")


def _sequence_invalid(row: dict[str, Any]) -> bool:
    flag = row.get("sequence_valid")
    if flag is None:
        return False
    try:
        return int(flag) == 0
    except (TypeError, ValueError):
        return str(flag).lower() in {"false", "0"}


def _mechanism(bid_change: float | None, ask_change: float | None) -> str:
    if bid_change is None or ask_change is None:
        return "DEPTH_UNAVAILABLE"
    bid_up = bid_change > SAFE_DIV_EPS
    bid_dn = bid_change < -SAFE_DIV_EPS
    ask_up = ask_change > SAFE_DIV_EPS
    ask_dn = ask_change < -SAFE_DIV_EPS
    if bid_up and ask_dn:
        return "BID_ADD_AND_ASK_REMOVE"
    if bid_up and not ask_up and not ask_dn:
        return "BID_ADD_ASK_UNCHANGED"
    if ask_dn and not bid_up and not bid_dn:
        return "ASK_REMOVE_BID_UNCHANGED"
    if bid_dn and ask_up:
        return "BID_REMOVE_AND_ASK_ADD"
    if bid_dn and not ask_up and not ask_dn:
        return "BID_REMOVE_ASK_UNCHANGED"
    if ask_up and not bid_up and not bid_dn:
        return "ASK_ADD_BID_UNCHANGED"
    if bid_up and ask_up:
        return "BOTH_SIDES_ADDED"
    if bid_dn and ask_dn:
        return "BOTH_SIDES_REMOVED"
    return "NO_MATERIAL_DEPTH_CHANGE"


def _relative_change(before: float, after: float) -> float | None:
    if abs(before) <= SAFE_DIV_EPS:
        return None
    return (after - before) / abs(before)


def empty_metrics(**overrides: Any) -> dict[str, Any]:
    base = {
        "imbalance_feature": IMBALANCE_FEATURE,
        "imbalance_band": IMBALANCE_BAND,
        "imbalance_band_lo_bps": IMBALANCE_BAND_LO_BPS,
        "imbalance_band_hi_bps": IMBALANCE_BAND_HI_BPS,
        "imbalance_before": None,
        "imbalance_after": None,
        "imbalance_close": None,
        "imbalance_abs_change": None,
        "imbalance_rel_change": None,
        "bid_depth_before": None,
        "bid_depth_after": None,
        "bid_depth_change": None,
        "ask_depth_before": None,
        "ask_depth_after": None,
        "ask_depth_change": None,
        "bid_ask_mechanism": None,
        "stability": None,
        "n_pre_buckets": 0,
        "n_post_buckets": 0,
        "n_subsequent_stable_buckets": 0,
        "ordering_quality": None,
        "support_reason_code": None,
        "not_execution_claim": True,
        "expected_sign": None,
    }
    base.update(overrides)
    return base


def assess_imbalance_shift(
    *,
    candidate_type: str,
    states_100ms: list[dict[str, Any]],
    trigger_ts: pd.Timestamp,
    causal_end: pd.Timestamp,
) -> dict[str, Any]:
    """Return raw support_status plus imbalance metrics. Never reads outcomes."""
    trigger_ts = _as_ts(trigger_ts)
    causal_end = _as_ts(causal_end)
    sign = expected_sign(candidate_type)
    if sign is None:
        return {
            "support_status": "INCONCLUSIVE",
            "support_reasons": [],
            "contradiction_reasons": [REASON_NOT_IMBALANCE],
            "exact_evidence": [],
            "proxy_evidence": ["imbalance_is_book_composition_not_execution"],
            **empty_metrics(support_reason_code=REASON_NOT_IMBALANCE),
        }

    usable: list[dict[str, Any]] = []
    for raw in states_100ms:
        ts = _as_ts(raw["bucket_start"])
        if ts >= causal_end:
            continue
        usable.append({**raw, "_ts": ts})
    usable.sort(key=lambda r: r["_ts"])

    pre = [r for r in usable if r["_ts"] < trigger_ts]
    post = [r for r in usable if trigger_ts <= r["_ts"] < causal_end]
    evidence = (pre[-1:] if pre else []) + post

    orderings = {str(r.get("ordering_confidence") or "") for r in evidence}
    ordering_quality = (
        "ORDERING_AMBIGUOUS"
        if any("AMBIGUOUS" in o for o in orderings)
        else ("ORDERING_DETERMINISTIC_CONTRACT" if evidence else None)
    )

    def refuse(code: str, status: str = "INCONCLUSIVE", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        metrics = empty_metrics(
            expected_sign=sign,
            n_pre_buckets=len(pre),
            n_post_buckets=len(post),
            ordering_quality=ordering_quality,
            support_reason_code=code,
        )
        if extra:
            metrics.update(extra)
        contradictions = [code] if status != "SUPPORTED_BY_EVENT_TRACE" else []
        reasons = [code] if status == "INCONCLUSIVE" else []
        return {
            "support_status": status,
            "support_reasons": reasons,
            "contradiction_reasons": contradictions,
            "exact_evidence": [],
            "proxy_evidence": ["imbalance_is_book_composition_not_execution"],
            **metrics,
        }

    if any(_ordering_ambiguous(r) for r in evidence):
        return refuse(REASON_ORDERING_AMBIGUOUS)
    if any(_sequence_invalid(r) for r in evidence):
        return refuse(REASON_SEQUENCE_GAP)
    if any(_is_crossed(r) for r in evidence):
        return refuse(REASON_CROSSED_BOOK)
    if evidence and any(not _book_present(r) for r in evidence):
        return refuse(REASON_BOOK_INVALID)

    if not pre or not post:
        return refuse(REASON_MISSING_100MS)

    expected_post = []
    t = trigger_ts
    while t < causal_end:
        expected_post.append(t)
        t = t + pd.Timedelta(milliseconds=BUCKET_MS)
    have_post = {int(_as_ts(r["_ts"]).value) for r in post}
    if expected_post and any(int(pd.Timestamp(ts).value) not in have_post for ts in expected_post):
        return refuse(REASON_MISSING_100MS)

    before_row = pre[-1]
    imb_before = row_imbalance(before_row)
    bid_b, ask_b = row_band_depths(before_row)
    if imb_before is None or bid_b is None or ask_b is None:
        return refuse(REASON_BOOK_INVALID)

    post_imbs: list[float] = []
    for r in post:
        imb = row_imbalance(r)
        if imb is None:
            return refuse(REASON_BOOK_INVALID, extra={"imbalance_before": imb_before})
        post_imbs.append(imb)

    shift_idx = None
    for i, imb in enumerate(post_imbs):
        if (imb - imb_before) * sign > 0:
            shift_idx = i
            break
    after_row = post[shift_idx] if shift_idx is not None else post[-1]
    close_row = post[-1]
    imb_after = row_imbalance(after_row)
    imb_close = row_imbalance(close_row)
    bid_a, ask_a = row_band_depths(after_row)
    if imb_after is None or imb_close is None or bid_a is None or ask_a is None:
        return refuse(REASON_BOOK_INVALID, extra={"imbalance_before": imb_before})

    abs_change = imb_after - imb_before
    rel_change = _relative_change(imb_before, imb_after)
    bid_change = bid_a - bid_b
    ask_change = ask_a - ask_b
    mechanism = _mechanism(bid_change, ask_change)

    subsequent = post_imbs[shift_idx + 1 :] if shift_idx is not None else []
    n_stable = 0
    for imb in subsequent:
        if (imb - imb_before) * sign > 0:
            n_stable += 1
    if shift_idx is None:
        stability = "NO_DIRECTIONAL_SHIFT"
    elif not subsequent:
        stability = "INSUFFICIENT_POST_BUCKETS"
    elif n_stable == len(subsequent):
        stability = "STABLE"
    else:
        stability = "UNSTABLE"

    extra = {
        "imbalance_before": imb_before,
        "imbalance_after": imb_after,
        "imbalance_close": imb_close,
        "imbalance_abs_change": abs_change,
        "imbalance_rel_change": rel_change,
        "bid_depth_before": bid_b,
        "bid_depth_after": bid_a,
        "bid_depth_change": bid_change,
        "ask_depth_before": ask_b,
        "ask_depth_after": ask_a,
        "ask_depth_change": ask_change,
        "bid_ask_mechanism": mechanism,
        "stability": stability,
        "n_pre_buckets": len(pre),
        "n_post_buckets": len(post),
        "n_subsequent_stable_buckets": n_stable,
        "ordering_quality": ordering_quality,
        "expected_sign": sign,
        "not_execution_claim": True,
    }
    proxy = [
        "imbalance_is_book_composition_not_execution",
        f"band={IMBALANCE_BAND}",
        f"mechanism={mechanism}",
    ]
    exact = [
        f"imbalance_before={imb_before:.6f}",
        f"imbalance_after={imb_after:.6f}",
        f"imbalance_abs_change={abs_change:.6f}",
        f"bid_depth_change={bid_change:.4f}",
        f"ask_depth_change={ask_change:.4f}",
    ]

    directional = abs_change * sign
    if directional <= 0:
        code = REASON_NO_CHANGE if abs(abs_change) <= SAFE_DIV_EPS else REASON_OPPOSITE
        return {
            "support_status": "NOT_SUPPORTED_BY_EVENT_TRACE",
            "support_reasons": [],
            "contradiction_reasons": [code],
            "exact_evidence": exact,
            "proxy_evidence": proxy,
            **empty_metrics(support_reason_code=code, **extra),
        }

    if stability == "STABLE":
        return {
            "support_status": "SUPPORTED_BY_EVENT_TRACE",
            "support_reasons": [REASON_SUPPORTED_STABLE, f"stability={stability}"],
            "contradiction_reasons": [],
            "exact_evidence": exact,
            "proxy_evidence": proxy,
            **empty_metrics(support_reason_code=REASON_SUPPORTED_STABLE, **extra),
        }

    return {
        "support_status": "PARTIALLY_SUPPORTED",
        "support_reasons": [REASON_PARTIAL_NO_STABILITY, f"stability={stability}"],
        "contradiction_reasons": [],
        "exact_evidence": exact,
        "proxy_evidence": proxy,
        **empty_metrics(support_reason_code=REASON_PARTIAL_NO_STABILITY, **extra),
    }

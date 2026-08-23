"""EMA_MULTI_SOURCE_GATE_V1 — transparent reason-coded verdicts."""

from __future__ import annotations

from typing import Any

from ..fake_impulse_filter.frozen_gate import (
    FrozenGateLabel,
    classify_long_frozen,
    classify_short_frozen,
)
from .config import POLICY_VERSION
from .models import Direction, FinalVerdict, SourceVerdict

# Conservative symmetric thresholds — not outcome-tuned
OI_SUPPORT_MIN_PCT = 0.002
OI_CONTRA_MIN_PCT = 0.005
LIQ_SUPPORT_RATIO = 1.5
LIQ_CONTRA_RATIO = 0.5
LLD_OPPOSING_CONTRA_PCT = 0.15
LLD_OPPOSING_STRONG_PCT = 0.08


def _map_frozen(label: FrozenGateLabel, direction: str) -> SourceVerdict:
    bull = direction.upper() == Direction.BULLISH.value
    if label == FrozenGateLabel.MIXED:
        return SourceVerdict.CONTRADICTING
    if bull:
        if label in (FrozenGateLabel.PUMP_CONFIRMED, FrozenGateLabel.PUMP_CONFIRMING):
            return SourceVerdict.CONFIRMING
        if label == FrozenGateLabel.EARLY_PRESSURE:
            return SourceVerdict.SUPPORTING
        if label in (FrozenGateLabel.DUMP_CONFIRMING, FrozenGateLabel.DUMP_CONFIRMED, FrozenGateLabel.EARLY_SELL_PRESSURE):
            return SourceVerdict.STRONGLY_CONTRADICTING
    else:
        if label in (FrozenGateLabel.DUMP_CONFIRMED, FrozenGateLabel.DUMP_CONFIRMING):
            return SourceVerdict.CONFIRMING
        if label == FrozenGateLabel.EARLY_SELL_PRESSURE:
            return SourceVerdict.SUPPORTING
        if label in (FrozenGateLabel.PUMP_CONFIRMING, FrozenGateLabel.PUMP_CONFIRMED, FrozenGateLabel.EARLY_PRESSURE):
            return SourceVerdict.STRONGLY_CONTRADICTING
    return SourceVerdict.NEUTRAL


def _trades_verdict(feat: dict[str, Any], direction: str) -> SourceVerdict:
    cross = (feat.get("windows") or {}).get("cross_candle") or {}
    pre = (feat.get("windows") or {}).get("pre_timeframe") or (feat.get("windows") or {}).get("pre_15m") or {}
    if cross.get("trades_status") == "MISSING":
        return SourceVerdict.INCONCLUSIVE_DATA
    if cross.get("trades_status") == "EMPTY_WINDOW":
        return SourceVerdict.NEUTRAL
    tbr = cross.get("taker_buy_ratio") or pre.get("taker_buy_ratio")
    delta = cross.get("delta") if cross.get("delta") is not None else pre.get("delta")
    flip = (feat.get("trade_flow") or {}).get("flow_flip")
    bull = direction.upper() == Direction.BULLISH.value
    if tbr is None and delta is None:
        return SourceVerdict.INCONCLUSIVE_DATA
    if flip == "CONFIRMING":
        return SourceVerdict.SUPPORTING
    if flip == "CONTRADICTING":
        return SourceVerdict.CONTRADICTING
    if bull:
        if tbr is not None and tbr >= 0.55:
            return SourceVerdict.CONFIRMING
        if tbr is not None and tbr <= 0.42:
            return SourceVerdict.STRONGLY_CONTRADICTING
        if delta is not None and delta > 0:
            return SourceVerdict.SUPPORTING
        if delta is not None and delta < 0:
            return SourceVerdict.CONTRADICTING
    else:
        if tbr is not None and tbr <= 0.45:
            return SourceVerdict.CONFIRMING
        if tbr is not None and tbr >= 0.58:
            return SourceVerdict.STRONGLY_CONTRADICTING
        if delta is not None and delta < 0:
            return SourceVerdict.SUPPORTING
        if delta is not None and delta > 0:
            return SourceVerdict.CONTRADICTING
    return SourceVerdict.NEUTRAL


def _ob_verdict(feat: dict[str, Any], direction: str) -> SourceVerdict:
    cross = (feat.get("windows") or {}).get("cross_candle") or {}
    pre = (feat.get("windows") or {}).get("pre_timeframe") or (feat.get("windows") or {}).get("pre_15m") or {}
    ob_meta = feat.get("ob_meta") or {}
    if ob_meta.get("status") == "STALE":
        return SourceVerdict.INCONCLUSIVE_DATA
    ob_st = cross.get("ob_status") or pre.get("ob_status")
    if ob_st == "MISSING":
        return SourceVerdict.INCONCLUSIVE_DATA
    if ob_st == "EMPTY_WINDOW":
        return SourceVerdict.NEUTRAL
    imb = cross.get("imbalance_l50_mean") or pre.get("imbalance_l50_mean")
    if imb is None:
        return SourceVerdict.INCONCLUSIVE_DATA
    bull = direction.upper() == Direction.BULLISH.value
    if bull:
        if imb > 0.08:
            return SourceVerdict.CONFIRMING
        if imb < -0.10:
            return SourceVerdict.STRONGLY_CONTRADICTING
    else:
        if imb < -0.08:
            return SourceVerdict.CONFIRMING
        if imb > 0.10:
            return SourceVerdict.STRONGLY_CONTRADICTING
    return SourceVerdict.NEUTRAL


def _oi_verdict(feat: dict[str, Any], direction: str) -> SourceVerdict:
    pre = (feat.get("windows") or {}).get("pre_timeframe") or (feat.get("windows") or {}).get("pre_15m") or {}
    baseline = (feat.get("windows") or {}).get("baseline_60m") or {}
    oi_feat = feat.get("oi_features") or {}
    if pre.get("oi_status") == "MISSING" or baseline.get("oi_status") == "MISSING":
        return SourceVerdict.INCONCLUSIVE_DATA
    if pre.get("oi_status") == "EMPTY_WINDOW":
        return SourceVerdict.NEUTRAL
    rel = oi_feat.get("oi_change_rel_baseline")
    ret = (feat.get("frozen_gate_features") or {}).get("ret_5m")
    bull = direction.upper() == Direction.BULLISH.value
    if rel is None or ret is None:
        return SourceVerdict.NEUTRAL
    if bull:
        if ret > 0 and rel >= OI_SUPPORT_MIN_PCT:
            return SourceVerdict.SUPPORTING
        if ret > 0 and rel <= -OI_CONTRA_MIN_PCT:
            return SourceVerdict.CONTRADICTING
        if ret < 0 and rel >= OI_CONTRA_MIN_PCT:
            return SourceVerdict.CONTRADICTING
    else:
        if ret < 0 and rel >= OI_SUPPORT_MIN_PCT:
            return SourceVerdict.SUPPORTING
        if ret < 0 and rel <= -OI_CONTRA_MIN_PCT:
            return SourceVerdict.CONTRADICTING
        if ret > 0 and rel >= OI_CONTRA_MIN_PCT:
            return SourceVerdict.CONTRADICTING
    return SourceVerdict.NEUTRAL


def _liq_verdict(feat: dict[str, Any], direction: str) -> SourceVerdict:
    pre = (feat.get("windows") or {}).get("pre_timeframe") or (feat.get("windows") or {}).get("pre_15m") or {}
    baseline = (feat.get("windows") or {}).get("baseline_60m") or {}
    liq_feat = feat.get("liquidation_features") or {}
    st = pre.get("liq_status")
    if st in ("MISSING", "EMPTY_TABLE_SLICE") or baseline.get("liq_status") == "MISSING":
        return SourceVerdict.INCONCLUSIVE_DATA
    if st == "EMPTY_WINDOW":
        return SourceVerdict.NEUTRAL
    ln = pre.get("liq_long_notional") or 0
    sn = pre.get("liq_short_notional") or 0
    intensity = liq_feat.get("intensity_rel_baseline")
    bull = direction.upper() == Direction.BULLISH.value
    if bull:
        if sn > ln * LIQ_SUPPORT_RATIO and (intensity is None or intensity >= 1.0):
            return SourceVerdict.SUPPORTING
        if ln > sn * LIQ_SUPPORT_RATIO and intensity is not None and intensity >= 1.5:
            return SourceVerdict.CONTRADICTING
    else:
        if ln > sn * LIQ_SUPPORT_RATIO and (intensity is None or intensity >= 1.0):
            return SourceVerdict.SUPPORTING
        if sn > ln * LIQ_SUPPORT_RATIO and intensity is not None and intensity >= 1.5:
            return SourceVerdict.CONTRADICTING
    return SourceVerdict.NEUTRAL


def _liquidity_verdict(feat: dict[str, Any], direction: str) -> SourceVerdict:
    lld = feat.get("liquidity_confluence") or {}
    lld_feat = feat.get("lld_features") or {}
    if lld.get("lld_status") not in (None, "VALID"):
        return SourceVerdict.INCONCLUSIVE_DATA
    bull = direction.upper() == Direction.BULLISH.value
    primary = lld.get("primary_cluster")
    opposing = lld.get("opposing_cluster")
    opp_dist = lld_feat.get("opposing_barrier_distance_pct")
    if opp_dist is not None and abs(opp_dist) < LLD_OPPOSING_STRONG_PCT:
        return SourceVerdict.STRONGLY_CONTRADICTING
    if bull:
        if primary and primary.get("inside_cluster"):
            return SourceVerdict.SUPPORTING
        if opposing and opposing.get("distance_pct") is not None and abs(opposing["distance_pct"]) < LLD_OPPOSING_CONTRA_PCT:
            return SourceVerdict.CONTRADICTING
        if lld_feat.get("free_room_pct") is not None and lld_feat["free_room_pct"] > 0.3:
            return SourceVerdict.SUPPORTING
    else:
        if primary and primary.get("inside_cluster"):
            return SourceVerdict.SUPPORTING
        if opposing and opposing.get("distance_pct") is not None and abs(opposing["distance_pct"]) < LLD_OPPOSING_CONTRA_PCT:
            return SourceVerdict.CONTRADICTING
        if lld_feat.get("free_room_pct") is not None and lld_feat["free_room_pct"] > 0.3:
            return SourceVerdict.SUPPORTING
    return SourceVerdict.NEUTRAL


def _vol_verdict(feat: dict[str, Any], direction: str) -> SourceVerdict:
    vol = feat.get("volatility") or {}
    body_atr = vol.get("body_atr")
    if body_atr is None:
        return SourceVerdict.NEUTRAL
    if body_atr >= 0.45:
        return SourceVerdict.SUPPORTING
    if body_atr < 0.15:
        return SourceVerdict.CONTRADICTING
    return SourceVerdict.NEUTRAL


def apply_gate(
    *,
    direction: str,
    features: dict[str, Any],
    coverage: dict[str, Any],
) -> tuple[FinalVerdict, list[str], dict[str, str]]:
    if coverage.get("coverage_gate") == "INCONCLUSIVE_DATA":
        missing = coverage.get("critical_missing") or []
        partial = coverage.get("partial_sources") or []
        reasons = ["CRITICAL_COVERAGE_MISSING"] + [f"MISSING_{m.upper()}" for m in missing]
        if partial:
            reasons += [f"PARTIAL_{p.upper()}" for p in partial]
        return FinalVerdict.INCONCLUSIVE_DATA, reasons, {}

    bull = direction.upper() == Direction.BULLISH.value
    frozen = features.get("frozen_gate_features") or {}
    fake_label = classify_long_frozen(frozen) if bull else classify_short_frozen(frozen)
    fake_v = _map_frozen(fake_label, direction)

    sv = {
        "trades": _trades_verdict(features, direction).value,
        "ob": _ob_verdict(features, direction).value,
        "oi": _oi_verdict(features, direction).value,
        "liquidations": _liq_verdict(features, direction).value,
        "liquidity": _liquidity_verdict(features, direction).value,
        "volatility": _vol_verdict(features, direction).value,
        "fake_impulse": fake_v.value,
    }

    inconclusive_sources = [k for k, v in sv.items() if v == SourceVerdict.INCONCLUSIVE_DATA.value]
    required_sources = ("trades", "ob", "oi", "liquidations")
    if inconclusive_sources and any(k in inconclusive_sources for k in required_sources):
        return (
            FinalVerdict.INCONCLUSIVE_DATA,
            ["INCONCLUSIVE_EVIDENCE"] + [f"INCONCLUSIVE_{k.upper()}" for k in inconclusive_sources],
            sv,
        )

    strong_contra = [k for k, v in sv.items() if v == SourceVerdict.STRONGLY_CONTRADICTING.value]
    contra = [k for k, v in sv.items() if v == SourceVerdict.CONTRADICTING.value]
    confirming = [k for k, v in sv.items() if v in (SourceVerdict.CONFIRMING.value, SourceVerdict.SUPPORTING.value)]

    if fake_label == FrozenGateLabel.MIXED:
        return FinalVerdict.BLOCK, ["FAKE_IMPULSE_MIXED"] + [f"BLOCK_{k.upper()}" for k in strong_contra + contra], sv
    if strong_contra:
        return FinalVerdict.BLOCK, ["STRONG_CONTRADICTION"] + [f"BLOCK_{k.upper()}" for k in strong_contra], sv
    if len(contra) >= 2:
        return FinalVerdict.BLOCK, ["BLOCK_MIXED"] + [f"BLOCK_{k.upper()}" for k in contra], sv
    if confirming and not strong_contra:
        reasons = ["MULTISOURCE_CONFIRMATION"] + [f"CONFIRM_{k.upper()}" for k in confirming]
        if contra:
            reasons.append("MINOR_CONTRA_PRESENT")
        return FinalVerdict.ALLOW, reasons, sv
    if contra:
        return FinalVerdict.BLOCK, ["BLOCK_MIXED"] + [f"BLOCK_{k.upper()}" for k in contra], sv
    return FinalVerdict.BLOCK, ["BLOCK_MIXED", "INSUFFICIENT_CONFIRMATION"], sv


def policy_document() -> dict[str, Any]:
    return {
        "policy_version": POLICY_VERSION,
        "final_verdicts": [v.value for v in FinalVerdict if v != FinalVerdict.REJECTED],
        "source_verdicts": [v.value for v in SourceVerdict],
        "rules": [
            "Invalid EMA candidate → no gate, no entry",
            "Critical coverage missing/stale → INCONCLUSIVE_DATA",
            "require_ob_for_allow=true for full multi-source",
            "require_oi_for_allow=true and require_liq_for_allow=true for EMA_MULTI_SOURCE_GATE_V1",
            "OI/Liq MISSING before candidate bar → INCONCLUSIVE_DATA (not NEUTRAL)",
            "EMPTY_WINDOW only when source covers window but no events",
            "Strong contradiction → BLOCK",
            "Confirming without strong contradiction → ALLOW",
            "Mixed without clear confirmation → BLOCK",
            "OI/Liquidations/LLD can SUPPORT/CONTRADICT/INCONCLUSIVE",
            "No outcome-based tuning",
        ],
        "thresholds": {
            "oi_support_min_pct": OI_SUPPORT_MIN_PCT,
            "oi_contra_min_pct": OI_CONTRA_MIN_PCT,
            "liq_support_ratio": LIQ_SUPPORT_RATIO,
            "lld_opposing_contra_pct": LLD_OPPOSING_CONTRA_PCT,
        },
        "fake_impulse_adapter": "orderbook_analyse.fake_impulse_filter.frozen_gate (unchanged)",
    }

"""LEVEL 2 AVAILABLE_SOURCE_RESEARCH policy (research-only, not production)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...fake_impulse_filter.frozen_gate import FrozenGateLabel, classify_long_frozen, classify_short_frozen
from ..gate_policy import (
    _liq_verdict,
    _liquidity_verdict,
    _map_frozen,
    _ob_verdict,
    _oi_verdict,
    _trades_verdict,
    _vol_verdict,
)
from ..models import Direction, FinalVerdict, SourceVerdict

RESEARCH_POLICY_VERSION = "AVAILABLE_SOURCE_RESEARCH_V1"

# Evaluated in LEVEL 2 (MTF not wired; OI/Liq tracked but not required)
LEVEL2_EVAL_SOURCES = ("trades", "ob", "liquidity", "volatility", "fake_impulse")
TRACKED_SOURCES = ("trades", "ob", "liquidity", "volatility", "oi", "liquidations", "fake_impulse")


def research_policy_document() -> dict[str, Any]:
    return {
        "policy_version": RESEARCH_POLICY_VERSION,
        "levels": {
            "LEVEL1_EMA_RAW_RESEARCH": "All causal EMA candidates; MFE/MAE only; no gate label as production.",
            "LEVEL2_AVAILABLE_SOURCE_RESEARCH": (
                "Requires candles VALID + trades VALID + orderbook VALID. "
                "Uses existing source verdict fns on data <= decision_at. "
                "Never ALLOW. Outcomes: RESEARCH_SUPPORTIVE/ADVERSE/MIXED/INSUFFICIENT."
            ),
            "LEVEL3_STRICT_FULL_MULTISOURCE": "Unchanged production gate (require OI+Liq+trades+OB).",
        },
        "truth_table": [
            "1. candles != VALID -> RESEARCH_INSUFFICIENT",
            "2. public_trades_cross in MISSING/STALE/EMPTY_TABLE_SLICE -> RESEARCH_INSUFFICIENT",
            "3. orderbook in MISSING/STALE -> RESEARCH_INSUFFICIENT",
            "4. fake_impulse MIXED (via frozen gate) -> RESEARCH_ADVERSE",
            "5. any evaluated STRONGLY_CONTRADICTING -> RESEARCH_ADVERSE",
            "6. >=2 evaluated CONTRADICTING -> RESEARCH_MIXED",
            "7. any CONFIRMING/SUPPORTING and no strong contra -> RESEARCH_SUPPORTIVE (minor contra allowed)",
            "8. any single CONTRADICTING without support -> RESEARCH_MIXED",
            "9. else -> RESEARCH_INSUFFICIENT",
        ],
        "missing_never_supportive": True,
        "oi_liq_not_required_level2": True,
        "mtf_not_in_gate": True,
    }


def compute_all_source_verdicts(*, direction: str, features: dict[str, Any]) -> dict[str, str]:
    """Always compute source verdicts (even when production coverage fails)."""
    bull = direction.upper() == Direction.BULLISH.value
    frozen = features.get("frozen_gate_features") or {}
    fake_label = classify_long_frozen(frozen) if bull else classify_short_frozen(frozen)
    fake_v = _map_frozen(fake_label, direction)
    return {
        "trades": _trades_verdict(features, direction).value,
        "ob": _ob_verdict(features, direction).value,
        "oi": _oi_verdict(features, direction).value,
        "liquidations": _liq_verdict(features, direction).value,
        "liquidity": _liquidity_verdict(features, direction).value,
        "volatility": _vol_verdict(features, direction).value,
        "fake_impulse": fake_v.value,
        "_fake_impulse_label": fake_label.value,
    }


def _coverage_status(coverage: dict[str, Any], key: str) -> str | None:
    rec = coverage.get(key) or {}
    return rec.get("status")


def coverage_profile(coverage: dict[str, Any], *, decision_at: datetime | str) -> str:
    """PRE_OI_LIQ_COVERAGE | PARTIAL_OI_LIQ_COVERAGE | FULL_OI_LIQ_COVERAGE."""
    oi_st = _coverage_status(coverage, "open_interest")
    liq_st = _coverage_status(coverage, "liquidations")
    if oi_st in ("MISSING", "EMPTY_TABLE_SLICE") and liq_st in ("MISSING", "EMPTY_TABLE_SLICE"):
        return "PRE_OI_LIQ_COVERAGE"
    if oi_st in ("VALID", "EMPTY_WINDOW") and liq_st in ("VALID", "EMPTY_WINDOW"):
        return "FULL_OI_LIQ_COVERAGE"
    return "PARTIAL_OI_LIQ_COVERAGE"


def map_source_contribution(
    *,
    source: str,
    coverage: dict[str, Any],
    source_verdicts: dict[str, str],
    production_verdict: str,
    production_reasons: list[str],
    available_research_verdict: str,
) -> dict[str, str]:
    """Per-source contribution tags for export."""
    cov_key = {
        "trades": "public_trades_cross",
        "ob": "orderbook_ob200_v3",
        "oi": "open_interest",
        "liquidations": "liquidations",
        "liquidity": "liquidity_locations",
        "volatility": None,
        "fake_impulse": None,
        "candles": "candles",
    }.get(source)

    if source == "candles":
        st = _coverage_status(coverage, "candles")
        qual = "MISSING" if st != "VALID" else "NEUTRAL"
        decision = "DECISION_CAUSAL" if st != "VALID" else "DECISION_NON_CAUSAL"
        return {"contribution": qual, "decision_role": decision}

    if cov_key:
        st = _coverage_status(coverage, cov_key)
        if st == "MISSING" or st == "EMPTY_TABLE_SLICE":
            qual = "MISSING"
        elif st == "STALE":
            qual = "STALE"
        elif st == "VALID" or (source == "liquidations" and st == "EMPTY_WINDOW"):
            sv = source_verdicts.get(source if source != "liquidations" else "liquidations", "NEUTRAL")
            qual = _sv_to_contribution(sv)
        else:
            qual = "MISSING"
    else:
        sv = source_verdicts.get(source, SourceVerdict.NEUTRAL.value)
        qual = _sv_to_contribution(sv) if sv != SourceVerdict.INCONCLUSIVE_DATA.value else "MISSING"

    prod_short = production_verdict == FinalVerdict.INCONCLUSIVE_DATA.value and any(
        x in (production_reasons or []) for x in ("CRITICAL_COVERAGE_MISSING", "INCONCLUSIVE_EVIDENCE")
    )
    if prod_short and source in ("oi", "liquidations") and qual == "MISSING":
        decision = "NOT_EVALUATED_DUE_TO_CRITICAL_COVERAGE"
    elif production_verdict == FinalVerdict.INCONCLUSIVE_DATA.value and qual in ("MISSING", "STALE"):
        if source in ("oi", "liquidations"):
            decision = "NOT_EVALUATED_DUE_TO_CRITICAL_COVERAGE"
        else:
            decision = "DECISION_NON_CAUSAL"
    else:
        decision = _decision_role(source, qual, source_verdicts, available_research_verdict, production_verdict)
    return {"contribution": qual, "decision_role": decision}


def _sv_to_contribution(sv: str) -> str:
    if sv in (SourceVerdict.CONFIRMING.value, SourceVerdict.SUPPORTING.value):
        return "SUPPORTIVE"
    if sv in (SourceVerdict.CONTRADICTING.value, SourceVerdict.STRONGLY_CONTRADICTING.value):
        return "ADVERSE"
    if sv == SourceVerdict.INCONCLUSIVE_DATA.value:
        return "MISSING"
    return "NEUTRAL"


def _decision_role(
    source: str,
    qual: str,
    sv: dict[str, str],
    research_verdict: str,
    production_verdict: str,
) -> str:
    if qual in ("MISSING", "STALE"):
        return "NOT_EVALUATED_DUE_TO_CRITICAL_COVERAGE" if source in ("oi", "liquidations") else "DECISION_NON_CAUSAL"
    sv_val = sv.get(source if source != "liquidations" else "liquidations", "NEUTRAL")
    if production_verdict == FinalVerdict.INCONCLUSIVE_DATA.value and source in ("oi", "liquidations"):
        return "NOT_EVALUATED_DUE_TO_CRITICAL_COVERAGE"
    if sv_val in (SourceVerdict.CONFIRMING.value, SourceVerdict.SUPPORTING.value):
        if research_verdict == "RESEARCH_SUPPORTIVE":
            return "DECISION_CAUSAL"
    if sv_val in (SourceVerdict.CONTRADICTING.value, SourceVerdict.STRONGLY_CONTRADICTING.value):
        if research_verdict in ("RESEARCH_ADVERSE", "RESEARCH_MIXED"):
            return "DECISION_CAUSAL"
    if production_verdict == FinalVerdict.ALLOW.value and sv_val in (
        SourceVerdict.CONFIRMING.value,
        SourceVerdict.SUPPORTING.value,
    ):
        return "DECISION_CAUSAL"
    if production_verdict == FinalVerdict.BLOCK.value and sv_val in (
        SourceVerdict.CONTRADICTING.value,
        SourceVerdict.STRONGLY_CONTRADICTING.value,
    ):
        return "DECISION_CAUSAL"
    return "DECISION_NON_CAUSAL"


def apply_available_source_research(
    *,
    direction: str,
    features: dict[str, Any],
    coverage: dict[str, Any],
    source_verdicts: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    """LEVEL 2 verdict — never ALLOW."""
    sv = source_verdicts or compute_all_source_verdicts(direction=direction, features=features)
    reasons: list[str] = []

    if _coverage_status(coverage, "candles") != "VALID":
        return "RESEARCH_INSUFFICIENT", ["INSUFFICIENT_CANDLES"]
    tr_st = _coverage_status(coverage, "public_trades_cross")
    if tr_st in ("MISSING", "STALE", "EMPTY_TABLE_SLICE"):
        return "RESEARCH_INSUFFICIENT", [f"INSUFFICIENT_TRADES_{tr_st}"]
    ob_st = _coverage_status(coverage, "orderbook_ob200_v3")
    if ob_st in ("MISSING", "STALE"):
        return "RESEARCH_INSUFFICIENT", [f"INSUFFICIENT_ORDERBOOK_{ob_st}"]

    fake_label = sv.get("_fake_impulse_label") or ""
    if fake_label == FrozenGateLabel.MIXED.value:
        return "RESEARCH_ADVERSE", ["RESEARCH_FAKE_IMPULSE_MIXED"]

    evaluated = {k: sv[k] for k in LEVEL2_EVAL_SOURCES if k in sv}
    strong = [k for k, v in evaluated.items() if v == SourceVerdict.STRONGLY_CONTRADICTING.value]
    contra = [k for k, v in evaluated.items() if v == SourceVerdict.CONTRADICTING.value]
    confirming = [k for k, v in evaluated.items() if v in (SourceVerdict.CONFIRMING.value, SourceVerdict.SUPPORTING.value)]

    if strong:
        return "RESEARCH_ADVERSE", ["RESEARCH_STRONG_CONTRA"] + [f"ADVERSE_{k.upper()}" for k in strong]
    if len(contra) >= 2:
        return "RESEARCH_MIXED", ["RESEARCH_MULTI_CONTRA"] + [f"MIXED_{k.upper()}" for k in contra]
    if confirming and not strong:
        reasons = ["RESEARCH_MULTISOURCE_SUPPORT"] + [f"SUPPORT_{k.upper()}" for k in confirming]
        if contra:
            reasons.append("MINOR_CONTRA_PRESENT")
        return "RESEARCH_SUPPORTIVE", reasons
    if contra:
        return "RESEARCH_MIXED", ["RESEARCH_SINGLE_CONTRA"] + [f"MIXED_{k.upper()}" for k in contra]
    return "RESEARCH_INSUFFICIENT", ["RESEARCH_INSUFFICIENT_CONFIRMATION"]


def ablate_research_verdict(
    *,
    direction: str,
    features: dict[str, Any],
    coverage: dict[str, Any],
    source_verdicts: dict[str, str],
    drop_source: str | None = None,
) -> tuple[str, list[str]]:
    sv = dict(source_verdicts)
    if drop_source and drop_source in sv:
        sv[drop_source] = SourceVerdict.NEUTRAL.value
    if drop_source == "fake_impulse":
        sv["_fake_impulse_label"] = FrozenGateLabel.NO_EVIDENCE.value
    return apply_available_source_research(direction=direction, features=features, coverage=coverage, source_verdicts=sv)

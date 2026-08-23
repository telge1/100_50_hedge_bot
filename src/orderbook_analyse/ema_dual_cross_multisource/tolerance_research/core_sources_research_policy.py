"""AVAILABLE_CORE_SOURCES_RESEARCH policy for 30d XRP comparison (research-only)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..gate_policy import apply_gate
from ..models import FinalVerdict, SourceVerdict
from .research_policy import (
    LEVEL2_EVAL_SOURCES,
    _coverage_status,
    compute_all_source_verdicts,
)

CORE_RESEARCH_POLICY_VERSION = "AVAILABLE_CORE_SOURCES_RESEARCH_30D_V1"
CORE_EVAL_SOURCES = LEVEL2_EVAL_SOURCES  # trades, ob, liquidity, volatility, fake_impulse


def core_research_policy_document() -> dict[str, Any]:
    return {
        "policy_version": CORE_RESEARCH_POLICY_VERSION,
        "core_required": [
            "candles",
            "public_trades_cross",
            "orderbook_ob200_v3",
            "liquidity_locations",
        ],
        "evaluated_when_present": ["open_interest", "liquidations"],
        "outcomes": [
            "CORE_RESEARCH_SUPPORTIVE",
            "CORE_RESEARCH_ADVERSE",
            "CORE_RESEARCH_MIXED",
            "CORE_RESEARCH_INSUFFICIENT",
        ],
        "production_unchanged": "STRICT_FULL_MULTISOURCE via apply_gate",
        "missing_never_neutral": True,
        "research_never_allow": True,
        "oi_liq_missing_does_not_block_core_research": True,
    }


def assign_coverage_segment(coverage: dict[str, Any]) -> str:
    """CORE_INCOMPLETE | CORE_FULL_OI_LIQ_MISSING | CORE_FULL_OI_LIQ_PARTIAL | FULL_MULTISOURCE."""
    candles_ok = _coverage_status(coverage, "candles") == "VALID"
    trades_ok = _coverage_status(coverage, "public_trades_cross") == "VALID"
    ob_ok = _coverage_status(coverage, "orderbook_ob200_v3") == "VALID"
    lld = _coverage_status(coverage, "liquidity_locations")
    lld_ok = lld in ("VALID", None) or str(lld).upper() == "VALID"

    if not (candles_ok and trades_ok and ob_ok and lld_ok):
        return "CORE_INCOMPLETE"

    oi_st = _coverage_status(coverage, "open_interest")
    liq_st = _coverage_status(coverage, "liquidations")
    oi_ok = oi_st in ("VALID", "EMPTY_WINDOW")
    liq_ok = liq_st in ("VALID", "EMPTY_WINDOW")

    if oi_ok and liq_ok:
        return "FULL_MULTISOURCE"
    if oi_ok or liq_ok:
        return "CORE_FULL_OI_LIQ_PARTIAL"
    return "CORE_FULL_OI_LIQ_MISSING"


def apply_core_sources_research(
    *,
    direction: str,
    features: dict[str, Any],
    coverage: dict[str, Any],
    source_verdicts: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    """Core research verdict — never ALLOW, never production label."""
    sv = source_verdicts or compute_all_source_verdicts(direction=direction, features=features)

    if _coverage_status(coverage, "candles") != "VALID":
        return "CORE_RESEARCH_INSUFFICIENT", ["CORE_INSUFFICIENT_CANDLES"]
    tr_st = _coverage_status(coverage, "public_trades_cross")
    if tr_st in ("MISSING", "STALE", "EMPTY_TABLE_SLICE"):
        return "CORE_RESEARCH_INSUFFICIENT", [f"CORE_INSUFFICIENT_TRADES_{tr_st}"]
    ob_st = _coverage_status(coverage, "orderbook_ob200_v3")
    if ob_st in ("MISSING", "STALE"):
        return "CORE_RESEARCH_INSUFFICIENT", [f"CORE_INSUFFICIENT_ORDERBOOK_{ob_st}"]
    lld_st = _coverage_status(coverage, "liquidity_locations")
    if lld_st not in (None, "VALID") and str(lld_st).upper() not in ("VALID",):
        return "CORE_RESEARCH_INSUFFICIENT", [f"CORE_INSUFFICIENT_LLD_{lld_st}"]

    fake_label = sv.get("_fake_impulse_label") or ""
    if fake_label == "MIXED":
        return "CORE_RESEARCH_ADVERSE", ["CORE_FAKE_IMPULSE_MIXED"]

    evaluated = {k: sv[k] for k in CORE_EVAL_SOURCES if k in sv}
    strong = [k for k, v in evaluated.items() if v == SourceVerdict.STRONGLY_CONTRADICTING.value]
    contra = [k for k, v in evaluated.items() if v == SourceVerdict.CONTRADICTING.value]
    confirming = [k for k, v in evaluated.items() if v in (SourceVerdict.CONFIRMING.value, SourceVerdict.SUPPORTING.value)]

    if strong:
        return "CORE_RESEARCH_ADVERSE", ["CORE_STRONG_CONTRA"] + [f"ADVERSE_{k.upper()}" for k in strong]
    if len(contra) >= 2:
        return "CORE_RESEARCH_MIXED", ["CORE_MULTI_CONTRA"] + [f"MIXED_{k.upper()}" for k in contra]
    if confirming and not strong:
        reasons = ["CORE_MULTISOURCE_SUPPORT"] + [f"SUPPORT_{k.upper()}" for k in confirming]
        if contra:
            reasons.append("MINOR_CONTRA_PRESENT")
        return "CORE_RESEARCH_SUPPORTIVE", reasons
    if contra:
        return "CORE_RESEARCH_MIXED", ["CORE_SINGLE_CONTRA"] + [f"MIXED_{k.upper()}" for k in contra]
    return "CORE_RESEARCH_INSUFFICIENT", ["CORE_INSUFFICIENT_CONFIRMATION"]


def apply_production_gate(
    *,
    direction: str,
    features: dict[str, Any],
    coverage: dict[str, Any],
    source_verdicts: dict[str, str] | None = None,
) -> tuple[str, list[str], dict[str, str]]:
    """Unchanged production gate wrapper."""
    sv = source_verdicts or compute_all_source_verdicts(direction=direction, features=features)
    verdict, reasons, prod_sv = apply_gate(direction=direction, features=features, coverage=coverage)
    if verdict == FinalVerdict.INCONCLUSIVE_DATA and not prod_sv:
        prod_sv = {k: v for k, v in sv.items() if not k.startswith("_")}
    return verdict.value, list(reasons), prod_sv

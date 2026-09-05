"""Frozen fight data eligibility contract v1."""

from __future__ import annotations

from typing import Any, Final

ELIGIBILITY_CONTRACT_VERSION: Final[str] = "fight_data_eligibility_contract_v1"
DATA_SOURCE_RESEARCH_DB: Final[str] = "BTC_DOGE_RESEARCH_DB"
DATA_SOURCE_RAW_LEGACY: Final[str] = "RAW_LEGACY_SLOW_REPLAY"
RESEARCH_DATABASE: Final[str] = "btc_doge_research"

ALLOWED_SYMBOLS: Final[frozenset[str]] = frozenset({"BTCUSDT", "DOGEUSDT"})

# Gate statuses
DATA_COMPLETE = "DATA_COMPLETE"
CONTEXT_PARTIAL = "CONTEXT_PARTIAL"
DATA_PARTIAL_FACTS_ONLY = "DATA_PARTIAL_FACTS_ONLY"
DATA_NOT_AVAILABLE = "DATA_NOT_AVAILABLE"
DATA_CONTRACT_ERROR = "DATA_CONTRACT_ERROR"

MANDATORY_SOURCES: Final[tuple[str, ...]] = (
    "PUBLIC_TRADES",
    "OB200",
    "PROFILE_TRADES",  # causal trades for session_start <= ts < anchor
)
CONTEXT_SOURCES: Final[tuple[str, ...]] = (
    "OPEN_INTEREST",
    "LIQUIDATIONS",
    "CANDLES_1M",
)

OI_EXPECTED_FREQUENCY_MS: Final[int] = 5000

EXIT_OK = 0
EXIT_TECH = 1
EXIT_CLI = 2
EXIT_DATA_NOT_AVAILABLE = 3
EXIT_PARTIAL_REQUIRE_COMPLETE = 4
EXIT_CONTRACT_ERROR = 5
# Backward-compatible alias used by older callers
EXIT_DATA = EXIT_DATA_NOT_AVAILABLE


def empty_flags() -> dict[str, Any]:
    return {
        "facts_computation_allowed": False,
        "interpretation_allowed": False,
        "trade_decision_eligible": False,
        "profile_causality_passed": False,
        "mandatory_data_complete": False,
        "context_data_complete": False,
        "rules_frozen": False,
        "trade_verdict_evaluated": False,
        "direction": None,
    }


def evaluate_eligibility(
    *,
    mandatory_statuses: dict[str, str],
    context_statuses: dict[str, str],
    profile_causality_passed: bool,
    contract_error: str | None = None,
) -> dict[str, Any]:
    """Derive gate status and boolean flags from per-source effective statuses.

    Effective statuses expected: COMPLETE | PARTIAL | NOT_AVAILABLE | CONTRACT_ERROR
    """
    flags = empty_flags()
    if contract_error:
        flags["profile_causality_passed"] = bool(profile_causality_passed)
        return {
            "eligibility_contract": ELIGIBILITY_CONTRACT_VERSION,
            "eligibility_status": DATA_CONTRACT_ERROR,
            "contract_error": contract_error,
            **flags,
            "decision_blocked_reason": "DATA_CONTRACT_ERROR",
        }

    mand_values = list(mandatory_statuses.values())
    ctx_values = list(context_statuses.values())

    if any(v == "CONTRACT_ERROR" for v in mand_values + ctx_values):
        flags["profile_causality_passed"] = bool(profile_causality_passed)
        return {
            "eligibility_contract": ELIGIBILITY_CONTRACT_VERSION,
            "eligibility_status": DATA_CONTRACT_ERROR,
            **flags,
            "decision_blocked_reason": "SOURCE_CONTRACT_ERROR",
        }

    if all(v == "NOT_AVAILABLE" for v in mand_values):
        flags["profile_causality_passed"] = bool(profile_causality_passed)
        return {
            "eligibility_contract": ELIGIBILITY_CONTRACT_VERSION,
            "eligibility_status": DATA_NOT_AVAILABLE,
            **flags,
            "decision_blocked_reason": "MANDATORY_DATA_ABSENT",
        }

    # Fight observation requires OB200; complete absence → DATA_NOT_AVAILABLE
    # even if trades/candles exist elsewhere (e.g. pre-OB-history anchors).
    if mandatory_statuses.get("OB200") == "NOT_AVAILABLE":
        flags["profile_causality_passed"] = bool(profile_causality_passed)
        return {
            "eligibility_contract": ELIGIBILITY_CONTRACT_VERSION,
            "eligibility_status": DATA_NOT_AVAILABLE,
            **flags,
            "decision_blocked_reason": "OB200_HISTORY_ABSENT",
        }

    # Public trade events must come from research_public_trades (source purity).
    if mandatory_statuses.get("PUBLIC_TRADES") == "NOT_AVAILABLE":
        flags["profile_causality_passed"] = bool(profile_causality_passed)
        return {
            "eligibility_contract": ELIGIBILITY_CONTRACT_VERSION,
            "eligibility_status": DATA_NOT_AVAILABLE,
            **flags,
            "decision_blocked_reason": "RESEARCH_TRADE_EVENTS_MISSING",
        }

    if any(v == "NOT_AVAILABLE" for v in mand_values):
        # Partial presence of some mandatory sources: still not fully absent
        any_present = any(v in {"COMPLETE", "PARTIAL"} for v in mand_values)
        status = DATA_PARTIAL_FACTS_ONLY if any_present else DATA_NOT_AVAILABLE
        flags["facts_computation_allowed"] = status == DATA_PARTIAL_FACTS_ONLY
        flags["profile_causality_passed"] = bool(profile_causality_passed)
        return {
            "eligibility_contract": ELIGIBILITY_CONTRACT_VERSION,
            "eligibility_status": status,
            **flags,
            "decision_blocked_reason": "MANDATORY_DATA_GAP",
        }

    if any(v == "PARTIAL" for v in mand_values) or not profile_causality_passed:
        flags["facts_computation_allowed"] = True
        flags["profile_causality_passed"] = bool(profile_causality_passed)
        return {
            "eligibility_contract": ELIGIBILITY_CONTRACT_VERSION,
            "eligibility_status": DATA_PARTIAL_FACTS_ONLY,
            **flags,
            "decision_blocked_reason": "MANDATORY_DATA_GAP"
            if any(v == "PARTIAL" for v in mand_values)
            else "PROFILE_CAUSALITY_FAILED",
        }

    # All mandatory COMPLETE and profile causal
    flags["mandatory_data_complete"] = True
    flags["profile_causality_passed"] = True
    flags["facts_computation_allowed"] = True
    context_complete = all(v == "COMPLETE" for v in ctx_values) if ctx_values else True
    flags["context_data_complete"] = context_complete
    # interpretation_allowed / trade_decision_eligible stay False while rules_frozen=false
    status = DATA_COMPLETE if context_complete else CONTEXT_PARTIAL
    return {
        "eligibility_contract": ELIGIBILITY_CONTRACT_VERSION,
        "eligibility_status": status,
        **flags,
        "decision_blocked_reason": None
        if status == DATA_COMPLETE
        else "CONTEXT_DATA_GAP",
    }


def exit_code_for(
    eligibility_status: str,
    *,
    require_complete: bool,
    coverage_only: bool = False,
) -> int:
    if eligibility_status == DATA_CONTRACT_ERROR:
        return EXIT_CONTRACT_ERROR
    if eligibility_status == DATA_NOT_AVAILABLE:
        return EXIT_DATA_NOT_AVAILABLE
    if require_complete and eligibility_status != DATA_COMPLETE:
        return EXIT_PARTIAL_REQUIRE_COMPLETE
    if eligibility_status in {DATA_COMPLETE, CONTEXT_PARTIAL}:
        return EXIT_OK
    if eligibility_status == DATA_PARTIAL_FACTS_ONLY:
        return EXIT_PARTIAL_REQUIRE_COMPLETE if require_complete else EXIT_OK
    return EXIT_TECH

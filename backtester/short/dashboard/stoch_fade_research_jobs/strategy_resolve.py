"""Server-side strategy whitelist resolution. Never map browser strings to modules."""

from __future__ import annotations

from typing import Any

from .config import (
    ALLOWED_STRATEGY_IDS,
    EZM_RESULT_CONTRACT_VERSION,
    EZM_RUN_INTENT,
    EZM_RUNNER_KIND,
    EZM_STRATEGY_ID,
    FROZEN_RESULT_CONTRACT_VERSION,
    FROZEN_RUN_INTENT,
    FROZEN_RUNNER_KIND,
    STRATEGY_VERSION,
)


class StrategyResolveError(ValueError):
    """Unknown or forbidden strategy_id."""


def resolve_strategy_id(raw: str | None) -> str:
    """Empty/missing → Frozen Fade (backward compatible)."""
    if raw is None or str(raw).strip() == "":
        return STRATEGY_VERSION
    sid = str(raw).strip()
    if sid not in ALLOWED_STRATEGY_IDS:
        raise StrategyResolveError("UNKNOWN_STRATEGY_ID")
    return sid


def is_ezm_strategy(strategy_id: str | None) -> bool:
    return str(strategy_id or "") == EZM_STRATEGY_ID


def strategy_manifest_fields(strategy_id: str, *, strategy_spec_hash: str = "") -> dict[str, Any]:
    if is_ezm_strategy(strategy_id):
        return {
            "strategy_id": EZM_STRATEGY_ID,
            "fixed_strategy_version": EZM_STRATEGY_ID,
            "run_intent": EZM_RUN_INTENT,
            "runner_kind": EZM_RUNNER_KIND,
            "result_contract_version": EZM_RESULT_CONTRACT_VERSION,
            "strategy_spec_hash": strategy_spec_hash or "",
            "confirmation_policy": None,
            "confirmation_source": None,
            "causal_manifest_hash": None,
            "exit_policy": None,
            "intrabar_policy": None,
        }
    return {
        "strategy_id": STRATEGY_VERSION,
        "fixed_strategy_version": STRATEGY_VERSION,
        "run_intent": FROZEN_RUN_INTENT,
        "runner_kind": FROZEN_RUNNER_KIND,
        "result_contract_version": FROZEN_RESULT_CONTRACT_VERSION,
        "strategy_spec_hash": strategy_spec_hash or "",
    }

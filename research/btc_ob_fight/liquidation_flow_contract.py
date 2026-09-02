"""Frozen liquidation_flow_facts_v1 contract (Phase 2A.4 hardening)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

LIQUIDATION_FLOW_CONTRACT: Final[str] = "liquidation_flow_facts_v1"
LIQUIDATION_FLOW_CONTRACT_FROZEN: Final[bool] = True
EVENT_KEY_VERSION: Final[str] = "event_key_v1"
EVENT_KEY_FIELDS: Final[tuple[str, ...]] = (
    "exchange",
    "symbol",
    "event_ms",
    "position_side_raw",
    "size",
    "bankruptcy_price",
)
EVENT_KEY_FORMAT: Final[str] = (
    "{exchange}|{symbol}|{event_ms}|{position_side_raw}|{size}|{bankruptcy_price}"
)

ATTRIBUTION_METHOD: Final[str] = "HEURISTIC_TEMPORAL_VOLUME_ASSOCIATION"
SENSITIVITY_WINDOWS_MS: Final[tuple[int, ...]] = (100, 250, 500, 1000)

BYBIT_ALL_LIQUIDATION_DOCS_URL: Final[str] = (
    "https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation"
)

# S = position side (Bybit allLiquidation stream), NOT taker aggressor.
BYBIT_SIDE_MAPPING: Final[dict[str, dict[str, str]]] = {
    "Buy": {
        "position_side_raw": "Buy",
        "liquidated_position_side": "LIQUIDATED_LONG",
        "forced_trade_direction": "FORCED_SELL",
    },
    "Sell": {
        "position_side_raw": "Sell",
        "liquidated_position_side": "LIQUIDATED_SHORT",
        "forced_trade_direction": "FORCED_BUY",
    },
}

UNITS: Final[dict[str, str]] = {
    "executed_base_size": "BTC base asset",
    "bankruptcy_reference_quote": "USD quote (v × bankruptcy_price; reference only)",
    "taker_buy_base": "BTC base asset",
    "taker_sell_base": "BTC base asset",
    "taker_buy_quote": "USD quote notional",
    "taker_sell_quote": "USD quote notional",
    "taker_delta_quote": "USD quote notional (buy_quote − sell_quote)",
    "execution_price": "UNKNOWN — not stored",
    "execution_notional": "UNKNOWN — not stored",
}

INPUT_SOURCES: Final[dict[str, str]] = {
    "liquidations": "orderbook_analysis.all_liquidations",
    "public_trades": "orderbook_analysis.public_trades_canonical",
    "open_interest": "orderbook_analysis.open_interest_5s",
}

# Canonical pipeline must never ingest superseded explanatory-audit outputs.
FORBIDDEN_RESEARCH_INPUT_PATHS: Final[tuple[str, ...]] = (
    "results/btc_ob_fight_explanatory_audit_20260831_1900_v1",
    "research/btc_ob_fight_explanatory_audit/association.py",
)

SUPERSEDED_EXPLANATORY_AUDIT: Final[dict[str, Any]] = {
    "contract": "btc_ob_fight_explanatory_audit_v1",
    "do_not_use_for_research": True,
    "superseded_by": LIQUIDATION_FLOW_CONTRACT,
    "reason": "overlapping-window double counting and denominator mismatch",
    "superseded_output_marker_file": "SUPERSEDED.json",
    "historical_outputs_preserved": True,
}

PHASE_ROLE_CAUSAL: Final[str] = "CAUSAL"
PHASE_ROLE_HINDSIGHT: Final[str] = "HINDSIGHT"

# Maps legacy analysis_role values to frozen phase_role.
PHASE_ROLE_FROM_ANALYSIS: Final[dict[str, str]] = {
    "CAUSAL_OBSERVABLE": PHASE_ROLE_CAUSAL,
    "EXPLANATORY_HINDSIGHT_SEGMENT": PHASE_ROLE_HINDSIGHT,
    "PARTIAL_BOUNDARY": PHASE_ROLE_CAUSAL,
}


def map_bybit_position_side(raw_s: str) -> dict[str, str]:
    key = str(raw_s).strip()
    if key not in BYBIT_SIDE_MAPPING:
        raise ValueError(f"unknown Bybit position side raw S={raw_s!r}")
    return dict(BYBIT_SIDE_MAPPING[key])


def phase_live_usable(phase_role: str, *, partial_boundary: bool = False) -> bool:
    if phase_role == PHASE_ROLE_HINDSIGHT:
        return False
    if partial_boundary:
        return False
    return True


def assert_canonical_input_allowed(path: str | Path) -> None:
    """Raise if path points at superseded explanatory-audit research output."""
    p = Path(path).as_posix()
    for blocked in FORBIDDEN_RESEARCH_INPUT_PATHS:
        if blocked in p or p.endswith(blocked.rstrip("/")):
            raise ValueError(
                f"canonical pipeline must not load superseded research input: {path} "
                f"(superseded_by={LIQUIDATION_FLOW_CONTRACT})"
            )


def frozen_contract_schema() -> dict[str, Any]:
    return {
        "contract_version": LIQUIDATION_FLOW_CONTRACT,
        "frozen": LIQUIDATION_FLOW_CONTRACT_FROZEN,
        "event_key_version": EVENT_KEY_VERSION,
        "event_key_fields": list(EVENT_KEY_FIELDS),
        "event_key_format": EVENT_KEY_FORMAT,
        "bybit_side_mapping": BYBIT_SIDE_MAPPING,
        "bybit_documentation_url": BYBIT_ALL_LIQUIDATION_DOCS_URL,
        "attribution_method": ATTRIBUTION_METHOD,
        "sensitivity_windows_ms": list(SENSITIVITY_WINDOWS_MS),
        "units": UNITS,
        "input_sources": INPUT_SOURCES,
        "superseded_explanatory_audit": SUPERSEDED_EXPLANATORY_AUDIT,
        "forbidden_research_input_paths": list(FORBIDDEN_RESEARCH_INPUT_PATHS),
        "required_sensitivity_metrics": [
            "allocated_liquidation_base",
            "total_taker_buy_base",
            "allocated_liquidation_share_of_total_taker_buy_base",
            "union_window_taker_buy_base",
            "liquidation_capacity_coverage_pct",
            "remaining_unattributed_taker_buy_base",
        ],
        "required_phase_fields": [
            "phase_role",
            "usable_for_live_signal",
        ],
        "bankruptcy_reference_field": "bankruptcy_reference_quote",
        "execution_fields_always_null": ["execution_price", "execution_notional"],
    }

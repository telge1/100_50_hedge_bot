"""Phase 6F — read-only LMV2 strategy input consumer (no trades, no signals)."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .historical_lmv2_input_dry_run import DEFAULT_DATA_SOURCE

CONSUMER_VERSION = "historical_lmv2_strategy_input_consumer_v1"
CONSUMER_ENTRYPOINT = (
    "orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1."
    "historical_lmv2_strategy_input_consumer_v1.consume_historical_lmv2_strategy_input_v1"
)
VALID_POOL_DETAILS = frozenset({1, 5, 10})
REQUIRED_STATE_KEYS = (
    "bridge_version",
    "data_kind",
    "availability_class",
    "scanner_pool_equivalence",
    "symbol",
    "timeframe",
    "pool_detail",
    "pipeline_line_id",
    "config_hash",
    "decision_close",
    "snapshot_id",
    "active_state_fingerprint",
    "members_hash",
    "input_state_hash",
    "active_members",
    "active_member_count",
)


class StrategyInputConsumerError(Exception):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


@dataclass(frozen=True)
class StrategyInputReceiptV1:
    consumer_version: str
    consumer_entrypoint: str
    decision_close: str
    candle_open_utc: str
    pool_detail: int
    snapshot_id: str
    pipeline_line_id: str
    config_hash: str
    members_hash: str
    input_state_hash: str
    active_member_count: int
    availability_class: str
    data_kind: str
    receipt_hash: str
    trading_executed: bool
    signals_generated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumer_version": self.consumer_version,
            "consumer_entrypoint": self.consumer_entrypoint,
            "decision_close": self.decision_close,
            "candle_open_utc": self.candle_open_utc,
            "pool_detail": self.pool_detail,
            "snapshot_id": self.snapshot_id,
            "pipeline_line_id": self.pipeline_line_id,
            "config_hash": self.config_hash,
            "members_hash": self.members_hash,
            "input_state_hash": self.input_state_hash,
            "active_member_count": self.active_member_count,
            "availability_class": self.availability_class,
            "data_kind": self.data_kind,
            "receipt_hash": self.receipt_hash,
            "trading_executed": self.trading_executed,
            "signals_generated": self.signals_generated,
        }


def _receipt_hash(body: dict[str, Any]) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _assert_state_unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    if before != after:
        raise StrategyInputConsumerError(
            "STATE_MUTATION",
            "lmv2_state mutated by consumer boundary",
        )


def _validate_causality(state: dict[str, Any]) -> None:
    dc = str(state.get("decision_close") or "")
    for m in state.get("active_members") or []:
        known = m.get("known_at")
        if known is not None and str(known) > dc:
            raise StrategyInputConsumerError(
                "CAUSALITY_VIOLATION",
                "known_at after decision_close",
                {"pool_id": m.get("pool_id"), "known_at": known, "decision_close": dc},
            )


def consume_historical_lmv2_strategy_input_v1(
    *,
    candle: dict[str, Any],
    lmv2_state: dict[str, Any],
    pool_detail: int,
) -> dict[str, Any]:
    """Accept LMV2 ACTIVE state at one decision close; emit audit receipt only."""
    pd = int(pool_detail)
    if pd not in VALID_POOL_DETAILS:
        raise StrategyInputConsumerError("INVALID_HISTORICAL_PD", "pool_detail must be 1, 5, or 10")
    missing = [k for k in REQUIRED_STATE_KEYS if k not in lmv2_state]
    if missing:
        raise StrategyInputConsumerError("INVALID_STATE", "missing lmv2 fields", {"missing": missing})
    if int(lmv2_state["pool_detail"]) != pd:
        raise StrategyInputConsumerError("PD_MISMATCH", "state pool_detail mismatch")
    dc = str(lmv2_state["decision_close"])
    candle_dc = str(candle.get("decision_close") or "")
    if candle_dc != dc:
        raise StrategyInputConsumerError(
            "CANDLE_CLOSE_MISMATCH",
            "candle decision_close does not match lmv2_state",
            {"candle": candle_dc, "state": dc},
        )
    if lmv2_state.get("data_kind") != DEFAULT_DATA_SOURCE:
        raise StrategyInputConsumerError("INVALID_DATA_KIND", "unexpected data_kind")
    if str(lmv2_state.get("availability_class") or "") != "RESEARCH_HISTORICAL":
        raise StrategyInputConsumerError("INVALID_AVAILABILITY", "availability_class must be RESEARCH_HISTORICAL")

    before = copy.deepcopy(lmv2_state)
    _validate_causality(before)

    # Strategy hook point (Phase 6F): no signals, orders, or PnL.
    strategy_view = {
        "pool_detail": pd,
        "decision_close": dc,
        "snapshot_id": before["snapshot_id"],
        "members_hash": before["members_hash"],
        "input_state_hash": before["input_state_hash"],
        "active_members": copy.deepcopy(list(before.get("active_members") or [])),
        "availability_class": before["availability_class"],
        "live_tradability_claimed": False,
    }

    after = copy.deepcopy(lmv2_state)
    _assert_state_unchanged(before, after)

    receipt_body = {
        "consumer_version": CONSUMER_VERSION,
        "decision_close": dc,
        "candle_open_utc": str(candle.get("candle_open_utc") or ""),
        "pool_detail": pd,
        "snapshot_id": before["snapshot_id"],
        "pipeline_line_id": before["pipeline_line_id"],
        "config_hash": before["config_hash"],
        "members_hash": before["members_hash"],
        "input_state_hash": before["input_state_hash"],
        "active_member_count": int(before.get("active_member_count") or 0),
        "availability_class": before["availability_class"],
        "data_kind": before["data_kind"],
        "trading_executed": False,
        "signals_generated": False,
    }
    rh = _receipt_hash(receipt_body)
    receipt = StrategyInputReceiptV1(
        consumer_entrypoint=CONSUMER_ENTRYPOINT,
        receipt_hash=rh,
        **{k: receipt_body[k] for k in receipt_body if k not in ("trading_executed", "signals_generated")},
        trading_executed=False,
        signals_generated=False,
    )
    return {
        "receipt": receipt.to_dict(),
        "strategy_view": strategy_view,
        "consumer_version": CONSUMER_VERSION,
    }


def describe_consumer_contract() -> dict[str, Any]:
    return {
        "consumer_version": CONSUMER_VERSION,
        "entrypoint": CONSUMER_ENTRYPOINT,
        "inputs": {
            "candle": "15m bar with candle_open_utc and decision_close",
            "lmv2_state": "Phase 6C/6D bridge payload dict",
            "pool_detail": "1 | 5 | 10",
        },
        "outputs": {
            "receipt": "deterministic audit record per close",
            "strategy_view": "read-only copy for future strategy kernel",
        },
        "forbidden": [
            "entry_signals",
            "exit_signals",
            "orders",
            "pnl",
            "pool_recomputation",
            "pool_record_conversion",
        ],
        "availability_note": (
            "RESEARCH_HISTORICAL publication replay; does not prove live tradability at decision time."
        ),
    }

"""Phase 6C — read-only Historical LMV2 backtester input bridge (no trades).

Delivers an explicit LMV2 publication ACTIVE state for future backtester wiring.
Uses only ``historical_lmv2_backtester_adapter_v1`` (Phase 6B SSOT). Does not map
to scanner ``PoolRecord`` / ``load_engine_pools_at`` semantics.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from research_charts.historical_lmv2_backtester_adapter_v1 import (
    ADAPTER_VERSION,
    active_members_hash,
    load_backtester_active_state_at_close,
)
from research_charts.historical_lmv2_pd_contract_v1 import resolve_historical_pd_config
from research_charts.historical_pool_state_v1 import HistoricalPoolStateError
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)

BRIDGE_VERSION = "historical_lmv2_backtester_input_bridge_v1"
DATA_KIND = "historical_lmv2_active_publication_state"
AVAILABILITY_CLASS = "RESEARCH_HISTORICAL"
SCANNER_POOL_EQUIVALENCE = "NOT_CLAIMED"
ADAPTER_MODULE = "research_charts.historical_lmv2_backtester_adapter_v1"

# Documented contrast to nested-ask engine path (no import of orderbook_analyse).
ENGINE_BACKTESTER_POOL_SOURCE = (
    "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.pools."
    "load_engine_pools_at + pool_from_engine"
)


@dataclass(frozen=True)
class HistoricalLmv2BacktesterInputStateV1:
    """Causal ACTIVE LMV2 state at one 15m decision close (backtester input plane)."""

    bridge_version: str
    data_kind: str
    availability_class: str
    scanner_pool_equivalence: str
    adapter_version: str
    visibility_mode: str
    symbol: str
    timeframe: str
    pool_detail: int
    pipeline_line_id: str
    config_hash: str
    decision_close: str
    decision_close_unix: int
    snapshot_id: str
    active_state_fingerprint: str
    active_members: tuple[dict[str, Any], ...]
    active_member_count: int
    members_hash: str
    input_state_hash: str
    source_adapter: str
    reader_kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bridge_version": self.bridge_version,
            "data_kind": self.data_kind,
            "availability_class": self.availability_class,
            "scanner_pool_equivalence": self.scanner_pool_equivalence,
            "adapter_version": self.adapter_version,
            "visibility_mode": self.visibility_mode,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "pool_detail": self.pool_detail,
            "pipeline_line_id": self.pipeline_line_id,
            "config_hash": self.config_hash,
            "decision_close": self.decision_close,
            "decision_close_unix": self.decision_close_unix,
            "snapshot_id": self.snapshot_id,
            "active_state_fingerprint": self.active_state_fingerprint,
            "active_member_count": self.active_member_count,
            "members_hash": self.members_hash,
            "input_state_hash": self.input_state_hash,
            "active_members": list(self.active_members),
            "source_adapter": self.source_adapter,
            "reader_kind": self.reader_kind,
        }


def compute_input_state_hash(payload: dict[str, Any]) -> str:
    """Deterministic hash over bridge payload (excludes redundant nested copies)."""
    body = {k: v for k, v in payload.items() if k != "active_members"}
    body["members_hash"] = payload.get("members_hash")
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _immutable_members(members: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(copy.deepcopy(m) for m in members)


def load_historical_lmv2_backtester_input_at_close(
    decision_close,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    pool_detail: int,
    client=None,
    database: str | None = None,
    reader: PublishedActiveReader | None = None,
) -> HistoricalLmv2BacktesterInputStateV1:
    """Load LMV2 ACTIVE input state at ``decision_close`` via Phase 6B adapter only."""
    hcfg = resolve_historical_pd_config(int(pool_detail))
    sym = (symbol or hcfg.symbol).upper()
    tf = timeframe or hcfg.timeframe

    adapted = load_backtester_active_state_at_close(
        decision_close,
        pool_detail=hcfg.pool_detail,
        symbol=sym,
        timeframe=tf,
        client=client,
        database=database,
        reader=reader,
    )
    members = _immutable_members(adapted.active_members)
    mhash = active_members_hash(list(members))
    if mhash != adapted.members_hash:
        raise HistoricalPoolStateError(
            "BRIDGE_HASH_MISMATCH",
            "members_hash drift after immutable copy",
            {"adapter": adapted.members_hash, "bridge": mhash},
        )

    base = {
        "bridge_version": BRIDGE_VERSION,
        "data_kind": DATA_KIND,
        "availability_class": AVAILABILITY_CLASS,
        "scanner_pool_equivalence": SCANNER_POOL_EQUIVALENCE,
        "adapter_version": adapted.adapter_version,
        "visibility_mode": adapted.visibility_mode,
        "symbol": adapted.symbol,
        "timeframe": adapted.timeframe,
        "pool_detail": adapted.pool_detail,
        "pipeline_line_id": adapted.pipeline_line_id,
        "config_hash": adapted.config_hash,
        "decision_close": adapted.decision_close,
        "decision_close_unix": adapted.decision_close_unix,
        "snapshot_id": adapted.snapshot_id,
        "active_state_fingerprint": adapted.active_state_fingerprint,
        "active_member_count": adapted.active_member_count,
        "members_hash": mhash,
        "source_adapter": ADAPTER_MODULE,
        "reader_kind": adapted.reader_kind,
    }
    ish = compute_input_state_hash({**base, "active_members": list(members)})

    return HistoricalLmv2BacktesterInputStateV1(
        **base,
        active_members=members,
        input_state_hash=ish,
    )


def bridge_matches_adapter(
    adapted,
    bridge: HistoricalLmv2BacktesterInputStateV1,
) -> dict[str, Any]:
    """Field-level parity check adapter → bridge (for harness/tests)."""
    mismatches: list[str] = []
    pairs = (
        ("adapter_version", adapted.adapter_version, bridge.adapter_version),
        ("snapshot_id", adapted.snapshot_id, bridge.snapshot_id),
        ("members_hash", adapted.members_hash, bridge.members_hash),
        ("active_member_count", adapted.active_member_count, bridge.active_member_count),
        ("decision_close", adapted.decision_close, bridge.decision_close),
        ("config_hash", adapted.config_hash, bridge.config_hash),
        ("pipeline_line_id", adapted.pipeline_line_id, bridge.pipeline_line_id),
    )
    for name, left, right in pairs:
        if left != right:
            mismatches.append(name)
    member_equal = list(adapted.active_members) == list(bridge.active_members)
    if not member_equal:
        mismatches.append("active_members")
    return {
        "ok": not mismatches,
        "mismatches": mismatches,
        "adapter_version_expected": ADAPTER_VERSION,
    }


def describe_engine_backtester_pool_contrast() -> dict[str, Any]:
    """Static contrast: engine pools vs this bridge (no orderbook_analyse import)."""
    return {
        "engine_path": {
            "module": "orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.backtest",
            "function": "_pool_snapshot_at_end",
            "pool_loader": ENGINE_BACKTESTER_POOL_SOURCE,
            "model": "PoolRecord (scanner/engine recomputation from candles)",
            "timeframes": ["1m", "5m", "15m"],
            "historical_lmv2_publications": False,
        },
        "lmv2_bridge_path": {
            "module": "research_charts.historical_lmv2_backtester_input_bridge_v1",
            "function": "load_historical_lmv2_backtester_input_at_close",
            "pool_loader": ADAPTER_MODULE,
            "model": DATA_KIND,
            "timeframe": "15m",
            "pool_detail": "1 | 5 | 10 via pd contract",
            "include_remaining": False,
            "availability_class": AVAILABILITY_CLASS,
        },
        "semantic_mapping": SCANNER_POOL_EQUIVALENCE,
        "notes": [
            "Pool IDs and geometry from publications may differ from engine PoolRecord sets.",
            "Bridge does not convert LMV2 members to PoolRecord.",
            "Live trading-time availability is not proven; state is RESEARCH_HISTORICAL.",
        ],
    }

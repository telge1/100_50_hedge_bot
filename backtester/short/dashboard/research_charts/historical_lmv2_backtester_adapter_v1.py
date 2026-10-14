"""Read-only Historical LMV2 ACTIVE state for future backtester wiring (Phase 6B).

Wraps ``historical_pool_state_v1.load_historical_pool_state_at_close`` only.
No pool recomputation, no chart geometry, no trading logic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from research_charts.historical_lmv2_pd_contract_v1 import resolve_historical_pd_config
from research_charts.historical_pool_state_v1 import (
    HistoricalPoolStateError,
    VISIBILITY_MODE,
    load_historical_pool_state_at_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)

ADAPTER_VERSION = "historical_lmv2_backtester_adapter_v1"
SSOT_LOADER = "research_charts.historical_pool_state_v1.load_historical_pool_state_at_close"
READER_KIND = "PublishedActiveReader"


@dataclass(frozen=True)
class HistoricalLmv2BacktesterActiveStateV1:
    """Causal ACTIVE membership at one 15m decision close (backtester-facing)."""

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
    source_loader: str
    reader_kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
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
            "active_members": list(self.active_members),
            "source_loader": self.source_loader,
            "reader_kind": self.reader_kind,
        }


def _parse_close_unix(decision_close: datetime | str) -> tuple[str, int]:
    if isinstance(decision_close, datetime):
        dt = decision_close
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
    else:
        text = str(decision_close).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text).astimezone(timezone.utc)
    iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return iso, int(dt.timestamp())


def canonical_active_member(row: dict[str, Any]) -> dict[str, Any]:
    """Stable ACTIVE member shape for parity hashing (sorted externally)."""
    lower = row.get("lower_bound")
    if lower is None:
        lower = row.get("lower")
    upper = row.get("upper_bound")
    if upper is None:
        upper = row.get("upper")
    return {
        "pool_id": row.get("pool_id"),
        "pipeline_line_id": row.get("pipeline_line_id"),
        "snapshot_id": row.get("snapshot_id"),
        "side": row.get("side"),
        "pool_type": row.get("pool_type"),
        "lower_bound": lower,
        "upper_bound": upper,
        "strength": row.get("strength"),
        "strength_class": row.get("strength_class"),
        "known_at": row.get("known_at"),
        "break_at": row.get("break_at"),
        "confirmed_at": row.get("confirmed_at"),
        "invalidated_at": row.get("invalidated_at"),
        "decision_close": row.get("decision_close"),
        "active_membership": row.get("active_membership"),
        "removal_cause": row.get("removal_cause"),
    }


def active_members_hash(members: list[dict[str, Any]]) -> str:
    body = [canonical_active_member(m) for m in members]
    body.sort(key=lambda r: str(r.get("pool_id") or ""))
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _assert_causal_known_at(members: list[dict[str, Any]], decision_close_iso: str) -> None:
    dc = decision_close_iso
    for m in members:
        known = m.get("known_at")
        if known is None:
            continue
        if str(known) > dc:
            raise HistoricalPoolStateError(
                "CAUSALITY_VIOLATION",
                "member known_at after decision_close",
                {"pool_id": m.get("pool_id"), "known_at": known, "decision_close": dc},
            )


def load_backtester_active_state_at_close(
    decision_close: datetime | str,
    *,
    pool_detail: int,
    symbol: str | None = None,
    timeframe: str | None = None,
    client=None,
    database: str | None = None,
    reader: PublishedActiveReader | None = None,
) -> HistoricalLmv2BacktesterActiveStateV1:
    """Load ACTIVE pool state at ``decision_close`` via HistoricalPoolState SSOT."""
    hcfg = resolve_historical_pd_config(int(pool_detail))
    sym = (symbol or hcfg.symbol).upper()
    tf = timeframe or hcfg.timeframe
    if sym != hcfg.symbol or tf != hcfg.timeframe:
        raise HistoricalPoolStateError(
            "INVALID_SCOPE",
            "adapter supports DOGEUSDT 15m historical lines only",
            {"symbol": sym, "timeframe": tf, "pool_detail": hcfg.pool_detail},
        )

    close_iso, close_unix = _parse_close_unix(decision_close)
    st = load_historical_pool_state_at_close(
        decision_close,
        symbol=sym,
        timeframe=tf,
        pool_detail=hcfg.pool_detail,
        client=client,
        database=database,
        reader=reader,
        include_remaining=False,
    )
    members = [canonical_active_member(m) for m in list(st.active_members or [])]
    members.sort(key=lambda r: str(r.get("pool_id") or ""))
    _assert_causal_known_at(members, close_iso)
    mhash = active_members_hash(members)

    if st.pipeline_line_id != hcfg.pipeline_line_id:
        raise HistoricalPoolStateError(
            "CONFIG_ISOLATION",
            "state pipeline_line_id does not match pd contract",
            {"state": st.pipeline_line_id, "contract": hcfg.pipeline_line_id},
        )
    if st.config_hash != hcfg.config_hash:
        raise HistoricalPoolStateError(
            "CONFIG_ISOLATION",
            "state config_hash does not match pd contract",
            {"state": st.config_hash, "contract": hcfg.config_hash},
        )

    return HistoricalLmv2BacktesterActiveStateV1(
        adapter_version=ADAPTER_VERSION,
        visibility_mode=st.visibility_mode or VISIBILITY_MODE,
        symbol=st.symbol,
        timeframe=st.timeframe,
        pool_detail=st.pool_detail,
        pipeline_line_id=st.pipeline_line_id,
        config_hash=st.config_hash,
        decision_close=close_iso,
        decision_close_unix=close_unix,
        snapshot_id=st.snapshot_id,
        active_state_fingerprint=st.active_state_fingerprint,
        active_members=tuple(members),
        active_member_count=len(members),
        members_hash=mhash,
        source_loader=SSOT_LOADER,
        reader_kind=READER_KIND,
    )

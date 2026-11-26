"""MC4R-C1 / MC4S-C1 — bounded cross-window Phase-5S member prefill reuse."""

from __future__ import annotations

import copy
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from research_charts.historical_lmv2_pd_contract_v1 import HistoricalPdConfig

CONTRACT_V1 = "phase5s_member_prefill_reuse_v1"
DEFAULT_MAX_ENTRIES = 4096


def member_prefill_reuse_enabled() -> bool:
    return os.environ.get("PHASE5S_MEMBER_PREFILL_REUSE_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def member_prefill_low_copy_enabled() -> bool:
    if not member_prefill_reuse_enabled():
        return False
    return os.environ.get("PHASE5S_MEMBER_PREFILL_LOW_COPY_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _freeze_member_value(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze_member_value(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_member_value(v) for v in value)
    return value


def _member_row_mapping(row: dict[str, Any]) -> Mapping[str, Any]:
    snap = copy.deepcopy(row)
    return MappingProxyType({k: _freeze_member_value(v) for k, v in snap.items()})


def _max_entries() -> int:
    raw = os.environ.get("PHASE5S_MEMBER_PREFILL_REUSE_MAX_ENTRIES", "").strip()
    if not raw:
        return DEFAULT_MAX_ENTRIES
    try:
        return max(1, min(20000, int(raw)))
    except ValueError:
        return DEFAULT_MAX_ENTRIES


@dataclass(frozen=True)
class MemberPrefillIdentity:
    symbol: str
    pipeline_line_id: str
    config_hash: str
    timeframe: str
    pool_detail: int
    decision_close_iso: str

    @classmethod
    def from_hcfg(cls, hcfg: HistoricalPdConfig, decision_close_iso: str) -> MemberPrefillIdentity:
        return cls(
            symbol=str(hcfg.symbol).upper(),
            pipeline_line_id=str(hcfg.pipeline_line_id),
            config_hash=str(hcfg.config_hash),
            timeframe=str(hcfg.timeframe),
            pool_detail=int(hcfg.pool_detail),
            decision_close_iso=decision_close_iso,
        )


def _members_match_identity(
    members: list[dict] | tuple[Mapping[str, Any], ...], ident: MemberPrefillIdentity
) -> bool:
    if not members:
        return True
    for row in members:
        if str(row.get("pipeline_line_id") or "") != ident.pipeline_line_id:
            return False
        snap = row.get("snapshot_id")
        if snap is None or str(snap).strip() == "":
            return False
        dc = row.get("decision_close")
        if dc is not None and str(dc) != ident.decision_close_iso:
            return False
    return True


def _storage_key(ident: MemberPrefillIdentity) -> tuple:
    return (
        ident.symbol,
        ident.pipeline_line_id,
        ident.config_hash,
        ident.timeframe,
        ident.pool_detail,
        ident.decision_close_iso,
    )


@dataclass
class _StoreEntry:
    ident: MemberPrefillIdentity
    members: list[dict] | tuple[Mapping[str, Any], ...]
    frozen: bool = False


class _MemberPrefillReuseStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: OrderedDict[tuple, _StoreEntry] = OrderedDict()
        self._get_deepcopy_count = 0
        self._get_low_copy_count = 0

    def _canon_members_for_store(self, members: list[dict]) -> tuple[list[dict] | tuple[Mapping[str, Any], ...], bool]:
        if member_prefill_low_copy_enabled():
            frozen = tuple(_member_row_mapping(m) for m in members)
            return frozen, True
        return copy.deepcopy(members), False

    def get(self, hcfg: HistoricalPdConfig, decision_close_iso: str) -> list[dict] | None:
        ident = MemberPrefillIdentity.from_hcfg(hcfg, decision_close_iso)
        key = _storage_key(ident)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.ident != ident:
                return None
            if not _members_match_identity(entry.members, ident):
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            if member_prefill_low_copy_enabled() and entry.frozen:
                self._get_low_copy_count += 1
                return list(entry.members)
            if member_prefill_low_copy_enabled() and not entry.frozen:
                return [dict(m) for m in entry.members]
            self._get_deepcopy_count += 1
            return copy.deepcopy(entry.members)

    def put(self, hcfg: HistoricalPdConfig, decision_close_iso: str, members: list[dict]) -> None:
        ident = MemberPrefillIdentity.from_hcfg(hcfg, decision_close_iso)
        if not _members_match_identity(members, ident):
            return
        key = _storage_key(ident)
        canon, frozen = self._canon_members_for_store(members)
        with self._lock:
            self._entries[key] = _StoreEntry(ident=ident, members=canon, frozen=frozen)
            self._entries.move_to_end(key)
            while len(self._entries) > _max_entries():
                self._entries.popitem(last=False)

    def clear_for_tests(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "max_entries": _max_entries(),
                "contract": CONTRACT_V1,
                "get_deepcopy_count": self._get_deepcopy_count,
                "get_low_copy_count": self._get_low_copy_count,
                "low_copy_enabled": member_prefill_low_copy_enabled(),
            }


_STORE = _MemberPrefillReuseStore()


def try_reuse_member_row(
    hcfg: HistoricalPdConfig, decision_close_iso: str
) -> list[dict] | None:
    if not member_prefill_reuse_enabled():
        return None
    return _STORE.get(hcfg, decision_close_iso)


def store_member_row(
    hcfg: HistoricalPdConfig, decision_close_iso: str, members: list[dict]
) -> None:
    if not member_prefill_reuse_enabled():
        return
    _STORE.put(hcfg, decision_close_iso, members)


def clear_member_prefill_reuse_cache_for_tests() -> None:
    _STORE.clear_for_tests()


def reuse_cache_stats() -> dict[str, Any]:
    return _STORE.stats_snapshot()
"""Isolated incremental landscape paper runner (research only)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from market_memory_bootstrap_engine import (
    apply_ssot_event,
    extract_birth_events,
    extract_death_events,
    unique_dcs,
    walk_ssot,
    _sync_staircase_to_memory,
)
from market_memory_state import (
    EVENT_DIAG_SELLOFF,
    EVENT_DROP_EVENT,
    EVENT_ORIGIN_BIRTH,
    EVENT_POOL_LOSS,
    EVENT_REFERENCE_LOW_INVALIDATED,
    EVENT_REINFORCEMENT,
    EVENT_TRUE_UPWARD,
    MarketMemoryState,
    _iso,
    _parse_utc,
)

_PDS = (1, 10)

# External taxonomy aliases for jsonl contract
EVENT_PRICE = "PRICE_EVENT"
EVENT_UPPER_LANDSCAPE_CHANGE = "UPPER_LANDSCAPE_CHANGE"
EVENT_UPWARD_STAIRCASE = "UPWARD_STAIRCASE"
EVENT_ACTIVE_LOSS = "ACTIVE_LOSS"
EVENT_REFERENCE_LOW_INVALIDATION = "REFERENCE_LOW_INVALIDATION"


def landscape_snapshot(state: MarketMemoryState, pool_detail: int) -> dict[str, Any]:
    lowers = {
        oid: {"bottom": r.bottom, "top": r.top, "strength": r.strength, "pool_ids": sorted(r.pool_ids)}
        for oid, r in state.origins[pool_detail].items()
    }
    uppers = {
        oid: {"bottom": r.bottom, "top": r.top, "strength": r.strength, "pool_ids": sorted(r.pool_ids)}
        for oid, r in state.upper_origins[pool_detail].items()
    }
    return {
        "pool_detail": pool_detail,
        "active_pool_count": len(state.active_pool_ids[pool_detail]),
        "lower_origin_count": len(lowers),
        "upper_origin_count": len(uppers),
        "spatial_step_count": len(state.spatial_steps[pool_detail]),
        "lower_origins": lowers,
        "upper_origins": uppers,
        "spatial_steps": list(state.spatial_steps[pool_detail]),
    }


def _upper_map_fingerprint(snap: dict) -> str:
    return json.dumps(snap.get("upper_origins", {}), sort_keys=True, default=str)


def _classify_memory_events(events: list, pool_detail: int) -> list[str]:
    types: list[str] = []
    for e in events:
        et = e.event_type if hasattr(e, "event_type") else e.get("event_type")
        if et == EVENT_ORIGIN_BIRTH:
            note = (e.note if hasattr(e, "note") else e.get("note")) or ""
            if "upper" in str(note):
                types.append(EVENT_ORIGIN_BIRTH)
                types.append(EVENT_UPPER_LANDSCAPE_CHANGE)
            else:
                types.append(EVENT_ORIGIN_BIRTH)
        elif et == EVENT_REINFORCEMENT:
            types.append(EVENT_REINFORCEMENT)
        elif et == EVENT_TRUE_UPWARD:
            types.append(EVENT_TRUE_UPWARD)
            types.append(EVENT_UPWARD_STAIRCASE)
        elif et == EVENT_POOL_LOSS:
            types.append(EVENT_ACTIVE_LOSS)
        elif et == EVENT_DROP_EVENT:
            types.append(EVENT_DROP_EVENT)
        elif et == EVENT_REFERENCE_LOW_INVALIDATED:
            types.append(EVENT_REFERENCE_LOW_INVALIDATION)
        elif et == EVENT_DIAG_SELLOFF:
            types.append(EVENT_DIAG_SELLOFF)
    return types


@dataclass
class PaperRunnerConfig:
    symbol: str = "DOGEUSDT"
    retention_days: int = 90
    checkpoint_every_publications: int = 48
    degraded_on_missing_pool: bool = True


@dataclass
class PaperRunner:
    """Event-driven incremental processor; one decision_close per step."""

    memory: MarketMemoryState
    bars_5m: dict[datetime, dict]
    price_processor: Any
    spatial_idx: dict
    load_members_fn: Callable
    client: Any
    db: str
    config: PaperRunnerConfig = field(default_factory=PaperRunnerConfig)
    status: str = "OK"
    feed_gaps: list[dict[str, Any]] = field(default_factory=list)
    processed_event_ids: set[str] = field(default_factory=set)
    processed_decision_closes: set[str] = field(default_factory=set)
    publications_since_checkpoint: int = 0
    metrics: dict[str, Any] = field(default_factory=lambda: {"event_latencies_ms": [], "db_reads": 0})

    _prev_ids: dict[int, set[str]] = field(default_factory=dict)
    _seen_origin: dict[int, set[str]] = field(default_factory=dict)
    _main_last_step_bottom: dict[int, float | None] = field(default_factory=dict)
    _main_step_seq: dict[int, int] = field(default_factory=dict)
    _main_step_initialized: dict[int, bool] = field(default_factory=dict)
    _bar_times: list[datetime] = field(default_factory=list)
    _bar_idx: int = 0
    _bar_cursor: datetime | None = None
    _member_cache: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        for pd in _PDS:
            self._prev_ids[pd] = set(self.memory.active_pool_ids[pd])
            self._seen_origin[pd] = set(self.memory.seen_origins[pd])
            self._main_last_step_bottom[pd] = self.memory.main_last_step_bottom.get(pd)
            self._main_step_seq[pd] = int(self.memory.main_step_seq.get(pd, 0))
            self._main_step_initialized[pd] = bool(self.memory.main_step_initialized.get(pd))
        self._bar_times = sorted(self.bars_5m.keys())
        if self.price_processor and getattr(self.price_processor, "watermark_5m", None):
            self._bar_cursor = _parse_utc(self.price_processor.watermark_5m)
        elif self.memory.watermark_utc:
            self._bar_cursor = _parse_utc(self.memory.watermark_utc)
        if self._bar_cursor and self._bar_times:
            while self._bar_idx < len(self._bar_times) and self._bar_times[self._bar_idx] <= self._bar_cursor:
                self._bar_idx += 1

    def _process_5m_until(self, until: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.price_processor:
            return out
        t0 = time.perf_counter()
        while self._bar_idx < len(self._bar_times) and self._bar_times[self._bar_idx] <= until:
            t = self._bar_times[self._bar_idx]
            if self._bar_cursor is not None and t > self._bar_cursor:
                before = len(self.memory.price_timeline)
                self.price_processor.process_bar(self.memory, t, self.bars_5m[t])
                for ev in self.memory.price_timeline[before:]:
                    out.append(ev)
            self._bar_idx += 1
        self._bar_cursor = until
        if out:
            self.metrics["event_latencies_ms"].append(round((time.perf_counter() - t0) * 1000, 3))
        return out

    def _pool_event_id(self, ev: dict) -> str:
        return f"{ev['event_kind']}:{ev['pool_detail']}:{ev.get('pool_id','')}:{ev['decision_close']}"

    def process_decision_close(self, dc: datetime, log_fn: Callable[[dict], None] | None = None) -> dict[str, Any]:
        dc_s = _iso(dc)
        if dc_s in self.processed_decision_closes:
            return {"decision_close": dc_s, "skipped": True, "runner_status": self.status}
        self.processed_decision_closes.add(dc_s)
        price_events = self._process_5m_until(dc)
        row_events: list[dict[str, Any]] = []
        upper_fp_before = {pd: _upper_map_fingerprint(landscape_snapshot(self.memory, pd)) for pd in _PDS}
        log_start = len(self.memory.event_log)

        for pd in _PDS:
            self.metrics["db_reads"] += 1
            try:
                members = self.load_members_fn(self._member_cache, dc, pd, self.client, self.db)
            except Exception as e:
                if self.config.degraded_on_missing_pool:
                    self.status = "DEGRADED"
                    self.feed_gaps.append(
                        {"decision_close": dc_s, "pool_detail": pd, "error": str(e), "kind": "pool_load_failure"}
                    )
                members = []

            if not members and self.config.degraded_on_missing_pool:
                gap = {"decision_close": dc_s, "pool_detail": pd, "kind": "empty_publication"}
                if gap not in self.feed_gaps:
                    self.status = "DEGRADED"
                    self.feed_gaps.append(gap)

            births = extract_birth_events(self._prev_ids[pd], members, pd, dc_s)
            deaths = extract_death_events(self._prev_ids[pd], members, pd, dc_s)
            for ev in births + deaths:
                eid = self._pool_event_id(ev)
                if eid in self.processed_event_ids:
                    continue
                self.processed_event_ids.add(eid)
                apply_ssot_event(
                    self.memory,
                    ev,
                    self._seen_origin,
                    self._main_last_step_bottom,
                    self._main_step_seq,
                    self.spatial_idx,
                )
                row_events.append({"source": "pool_ssot", "event_id": eid, **ev})

            self._prev_ids[pd] = {m["pool_id"] for m in members}
            self.memory.note_active_pools(pd, self._prev_ids[pd])
            _sync_staircase_to_memory(
                self.memory,
                self._seen_origin,
                self._main_last_step_bottom,
                self._main_step_seq,
                self._main_step_initialized,
            )
            self.memory.processed_publications += 1

        self.memory._advance_asof(dc_s)
        self.memory.watermark_utc = dc_s
        if self.price_processor:
            self.memory.price_processor = self.price_processor.to_dict()
        self.publications_since_checkpoint += 1

        mem_new = self.memory.event_log[log_start:]
        taxonomy = _classify_memory_events(mem_new, 0)
        for pe in price_events:
            et = pe.get("event_type", EVENT_PRICE)
            if et == EVENT_DIAG_SELLOFF:
                taxonomy.append(EVENT_DIAG_SELLOFF)
            elif et == EVENT_DROP_EVENT:
                taxonomy.append(EVENT_DROP_EVENT)
            elif et == EVENT_REFERENCE_LOW_INVALIDATED:
                taxonomy.append(EVENT_REFERENCE_LOW_INVALIDATION)
            else:
                taxonomy.append(EVENT_PRICE)

        landscapes = {pd: landscape_snapshot(self.memory, pd) for pd in _PDS}
        for pd in _PDS:
            if _upper_map_fingerprint(landscapes[pd]) != upper_fp_before[pd]:
                if EVENT_UPPER_LANDSCAPE_CHANGE not in taxonomy:
                    taxonomy.append(EVENT_UPPER_LANDSCAPE_CHANGE)

        record = {
            "decision_close": dc_s,
            "runner_status": self.status,
            "event_taxonomy": sorted(set(taxonomy)),
            "pool_events": row_events,
            "price_events_count": len(price_events),
            "semantic_hash": self.memory.semantic_state_hash(),
            "landscape_pd1": {
                "lower_count": landscapes[1]["lower_origin_count"],
                "upper_count": landscapes[1]["upper_origin_count"],
                "active_pools": landscapes[1]["active_pool_count"],
            },
            "landscape_pd10": {
                "lower_count": landscapes[10]["lower_origin_count"],
                "upper_count": landscapes[10]["upper_origin_count"],
                "active_pools": landscapes[10]["active_pool_count"],
            },
        }
        if log_fn:
            log_fn(record)
        return record

    def flush_price(self) -> None:
        if self.price_processor:
            self.price_processor.flush_pending_drop(self.memory)
            self.memory.price_processor = self.price_processor.to_dict()

    def maybe_checkpoint(self, path: Path) -> bool:
        if self.publications_since_checkpoint < self.config.checkpoint_every_publications:
            return False
        self.flush_price()
        cp = self.memory.checkpoint()
        cp["paper_runner_id"] = str(uuid.uuid4())
        cp["runner_status"] = self.status
        cp["feed_gaps"] = self.feed_gaps
        path.write_text(json.dumps(cp), encoding="utf-8")
        self.publications_since_checkpoint = 0
        return True

    def write_checkpoint(self, path: Path) -> dict[str, Any]:
        self.flush_price()
        cp = self.memory.checkpoint()
        cp["paper_runner_id"] = str(uuid.uuid4())
        cp["runner_status"] = self.status
        cp["feed_gaps"] = self.feed_gaps
        path.write_text(json.dumps(cp), encoding="utf-8")
        return cp


def backfill_price_through(
    memory: MarketMemoryState,
    processor: Any,
    bars: dict,
    until: datetime,
    calib_start: datetime | None = None,
    calib_end: datetime | None = None,
) -> None:
    """Price-only causal stream through `until` (no SSOT republication)."""
    if calib_start and calib_end and processor.calib_range_p90 is None:
        processor.calibrate_diagnostic_threshold(bars, calib_start, calib_end)
    for t in sorted(bars.keys()):
        if t <= until:
            processor.process_bar(memory, t, bars[t])
    processor.flush_pending_drop(memory)
    memory.price_processor = processor.to_dict()


def run_continuous_reference(
    start: datetime,
    end: datetime,
    memory: MarketMemoryState,
    bars: dict,
    processor: Any,
    spatial_idx: dict,
    load_members_fn: Callable,
    client: Any,
    db: str,
) -> MarketMemoryState:
    dcs = unique_dcs(start, end)
    mem, _, _ = walk_ssot(
        dcs,
        load_members_fn,
        client,
        db,
        memory,
        spatial_idx,
        None,
        bars_5m=bars,
        price_processor=processor,
    )
    if processor:
        processor.flush_pending_drop(mem)
        mem.price_processor = processor.to_dict()
    return mem


def episode_linkage_summary(state: MarketMemoryState) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for pd in _PDS:
        drops = [e for e in state.drop_episodes[pd] if not e.diagnostic_only]
        lowers = sorted(state.origins[pd].values(), key=lambda r: r.birth_decision_close)
        for ep in drops:
            src = ep.source_drop_close_utc or ep.recorded_at_utc
            src_t = _parse_utc(src) if src else None
            after = []
            if src_t:
                for lo in lowers:
                    if _parse_utc(lo.birth_decision_close) >= src_t:
                        after.append(lo.origin_id)
            linkage = "EPISODE_LINKAGE_UNCLEAR"
            if ep.status == "INVALIDATED":
                linkage = "REFERENCE_INVALIDATED"
            elif not after:
                linkage = "NO_LOWER_AFTER_DROP"
            elif len(after) == 1:
                linkage = "SINGLE_CANDIDATE_LOWER"
            rows.append(
                {
                    "pool_detail": pd,
                    "episode_id": ep.episode_id,
                    "linkage": linkage,
                    "lowers_after": len(after),
                }
            )
    return {
        "rows": rows[:100],
        "unclear_count": sum(1 for r in rows if r["linkage"] == "EPISODE_LINKAGE_UNCLEAR"),
        "total_drops": len(rows),
    }

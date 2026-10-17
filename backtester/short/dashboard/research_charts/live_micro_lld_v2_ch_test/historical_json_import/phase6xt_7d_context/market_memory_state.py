"""Research-only persistent market memory (Phase 6XT-90D). Not for live trading."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_EPS = 0.00012

EVENT_ORIGIN_BIRTH = "ORIGIN_BIRTH"
EVENT_REINFORCEMENT = "REINFORCEMENT"
EVENT_TRUE_UPWARD = "TRUE_UPWARD_FORMATION"
EVENT_DIAG_SELLOFF = "DIAGNOSTIC_LOCAL_SELLOFF"
EVENT_DROP_REFERENCE = "DROP_EPISODE_REFERENCE"
EVENT_POOL_LOSS = "POOL_ACTIVE_LOSS"
EVENT_DROP_EVENT = "DROP_EVENT"
EVENT_REFERENCE_LOW_INVALIDATED = "REFERENCE_LOW_INVALIDATED"
CHECKPOINT_VERSION = 3
SCHEMA_CODE_VERSION = "phase6xt_price_stream_recovery_v1"


def _parse_utc(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    if "T" not in s:
        s = s.replace(" ", "T")
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class OriginRecord:
    origin_id: str
    pool_detail: int
    first_causal_available_at: str
    birth_decision_close: str
    bottom: float
    top: float
    strength: float | None
    pool_ids: list[str] = field(default_factory=list)
    spatial_step_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin_id": self.origin_id,
            "pool_detail": self.pool_detail,
            "first_causal_available_at": self.first_causal_available_at,
            "birth_decision_close": self.birth_decision_close,
            "bottom": self.bottom,
            "top": self.top,
            "strength": self.strength,
            "pool_ids": sorted(self.pool_ids),
            "spatial_step_ids": self.spatial_step_ids,
        }


@dataclass
class MemoryEvent:
    asof_utc: str
    pool_detail: int
    event_type: str
    origin_id: str | None = None
    pool_id: str | None = None
    bottom: float | None = None
    top: float | None = None
    step_id: str | None = None
    note: str | None = None
    diagnostic_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class DropEpisodeRecord:
    episode_id: str
    reference_low: float
    source_drop_close_utc: str | None
    recorded_at_utc: str
    status: str  # ACTIVE | INVALIDATED | UNLINKED
    diagnostic_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# Backward-compatible alias
LowerOriginRecord = OriginRecord


@dataclass
class MarketMemoryState:
    """Causal pool / structure memory; PD1 and PD10 isolated."""

    asof_utc: str
    retention_days: int = 90
    origins: dict[int, dict[str, OriginRecord]] = field(default_factory=lambda: {1: {}, 10: {}})
    upper_origins: dict[int, dict[str, OriginRecord]] = field(default_factory=lambda: {1: {}, 10: {}})
    spatial_steps: dict[int, list[dict[str, Any]]] = field(default_factory=lambda: {1: [], 10: []})
    drop_episodes: dict[int, list[DropEpisodeRecord]] = field(default_factory=lambda: {1: [], 10: []})
    active_pool_ids: dict[int, set[str]] = field(default_factory=lambda: {1: set(), 10: set()})
    watermark_utc: str | None = None
    processed_publications: int = 0
    event_log: list[MemoryEvent] = field(default_factory=list)
    applied_event_fingerprints: set[str] = field(default_factory=set)
    price_timeline: list[dict[str, Any]] = field(default_factory=list)
    price_processor: dict[str, Any] = field(default_factory=dict)
    seen_origins: dict[int, set[str]] = field(default_factory=lambda: {1: set(), 10: set()})
    main_last_step_bottom: dict[int, float | None] = field(default_factory=lambda: {1: None, 10: None})
    main_step_seq: dict[int, int] = field(default_factory=lambda: {1: 0, 10: 0})
    main_step_initialized: dict[int, bool] = field(default_factory=lambda: {1: False, 10: False})
    event_cursor: str | None = None

    def _canonical_spatial_steps(self) -> dict[int, list[dict[str, Any]]]:
        out: dict[int, list[dict[str, Any]]] = {}
        for pd in (1, 10):
            out[pd] = sorted(self.spatial_steps[pd], key=lambda s: (s.get("decision_close", ""), s.get("step_id", "")))
        return out

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_code_version": SCHEMA_CODE_VERSION,
            "asof_utc": self.asof_utc,
            "watermark_utc": self.watermark_utc,
            "event_cursor": self.event_cursor,
            "origins": {pd: {k: v.to_dict() for k, v in sorted(self.origins[pd].items())} for pd in (1, 10)},
            "upper_origins": {
                pd: {k: v.to_dict() for k, v in sorted(self.upper_origins[pd].items())} for pd in (1, 10)
            },
            "spatial_steps": self._canonical_spatial_steps(),
            "drop_episodes": {
                pd: sorted([e.to_dict() for e in self.drop_episodes[pd]], key=lambda e: e.get("episode_id", ""))
                for pd in (1, 10)
            },
            "active_pool_ids": {pd: sorted(self.active_pool_ids[pd]) for pd in (1, 10)},
            "seen_origins": {pd: sorted(self.seen_origins[pd]) for pd in (1, 10)},
            "main_last_step_bottom": self.main_last_step_bottom,
            "main_step_seq": self.main_step_seq,
            "main_step_initialized": self.main_step_initialized,
            "price_processor": self.price_processor,
            "price_timeline": self.price_timeline,
        }

    def semantic_state_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.semantic_payload(), sort_keys=True, default=str).encode()).hexdigest()

    def state_hash(self) -> str:
        return self.semantic_state_hash()

    def price_events_append(self, ev: dict[str, Any]) -> None:
        fp = json.dumps(ev, sort_keys=True, default=str)
        if fp in self.applied_event_fingerprints:
            return
        self.applied_event_fingerprints.add(fp)
        self.price_timeline.append(ev)

    def checkpoint(self) -> dict[str, Any]:
        return {
            "version": CHECKPOINT_VERSION,
            "asof_utc": self.asof_utc,
            "retention_days": self.retention_days,
            "watermark_utc": self.watermark_utc,
            "processed_publications": self.processed_publications,
            "origins": {str(pd): {k: v.to_dict() for k, v in self.origins[pd].items()} for pd in (1, 10)},
            "upper_origins": {
                str(pd): {k: v.to_dict() for k, v in self.upper_origins[pd].items()} for pd in (1, 10)
            },
            "spatial_steps": deepcopy(self.spatial_steps),
            "drop_episodes": {
                str(pd): [e.to_dict() for e in self.drop_episodes[pd]] for pd in (1, 10)
            },
            "active_pool_ids": {str(pd): sorted(self.active_pool_ids[pd]) for pd in (1, 10)},
            "seen_origins": {str(pd): sorted(self.seen_origins[pd]) for pd in (1, 10)},
            "main_last_step_bottom": self.main_last_step_bottom,
            "main_step_seq": self.main_step_seq,
            "main_step_initialized": self.main_step_initialized,
            "price_processor": deepcopy(self.price_processor),
            "price_timeline": deepcopy(self.price_timeline),
            "event_cursor": self.event_cursor,
            "schema_code_version": SCHEMA_CODE_VERSION,
            "semantic_state_hash": self.semantic_state_hash(),
            "state_hash": self.state_hash(),
        }

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any]) -> MarketMemoryState:
        ver = data.get("version", 1)
        if ver not in (1, 2, CHECKPOINT_VERSION):
            raise ValueError(f"incompatible checkpoint schema version {ver}")
        st = cls(
            asof_utc=data["asof_utc"],
            retention_days=data.get("retention_days", 90),
            watermark_utc=data.get("watermark_utc"),
            processed_publications=int(data.get("processed_publications") or 0),
        )

        def _load_origins(target: dict, blob: dict) -> None:
            for pd_s, omap in blob.items():
                pd = int(pd_s)
                for oid, rec in omap.items():
                    target[pd][oid] = OriginRecord(
                        origin_id=rec["origin_id"],
                        pool_detail=rec["pool_detail"],
                        first_causal_available_at=rec["first_causal_available_at"],
                        birth_decision_close=rec["birth_decision_close"],
                        bottom=float(rec["bottom"]),
                        top=float(rec["top"]),
                        strength=rec.get("strength"),
                        pool_ids=list(rec.get("pool_ids") or []),
                        spatial_step_ids=list(rec.get("spatial_step_ids") or []),
                    )

        _load_origins(st.origins, data.get("origins", {}))
        _load_origins(st.upper_origins, data.get("upper_origins", {}))
        st.spatial_steps = {int(k): v for k, v in data.get("spatial_steps", {}).items()}
        for pd_s, eps in data.get("drop_episodes", {}).items():
            pd = int(pd_s)
            st.drop_episodes[pd] = [DropEpisodeRecord(**e) for e in eps]
        for pd_s, ids in data.get("active_pool_ids", {}).items():
            st.active_pool_ids[int(pd_s)] = set(ids)
        for pd_s, ids in data.get("seen_origins", {}).items():
            st.seen_origins[int(pd_s)] = set(ids)
        st.main_last_step_bottom = {int(k): v for k, v in (data.get("main_last_step_bottom") or {1: None, 10: None}).items()}
        st.main_step_seq = {int(k): int(v) for k, v in (data.get("main_step_seq") or {1: 0, 10: 0}).items()}
        st.main_step_initialized = {
            int(k): bool(v) for k, v in (data.get("main_step_initialized") or {1: False, 10: False}).items()
        }
        st.price_processor = data.get("price_processor") or {}
        st.price_timeline = list(data.get("price_timeline") or [])
        st.event_cursor = data.get("event_cursor")
        st.applied_event_fingerprints = {json.dumps(e, sort_keys=True, default=str) for e in st.price_timeline}
        for pd in (1, 10):
            if not st.main_step_initialized.get(pd) and int(st.main_step_seq.get(pd, 0)) > 0:
                st.main_step_initialized[pd] = True
        if ver < CHECKPOINT_VERSION:
            cls._migrate_legacy_checkpoint(st)
        return st

    @staticmethod
    def _migrate_legacy_checkpoint(st: MarketMemoryState) -> None:
        """Infer staircase / seen-origin fields from v1/v2 checkpoints (pool-only bootstrap)."""
        for pd in (1, 10):
            if not st.seen_origins[pd]:
                st.seen_origins[pd] = set(st.origins[pd]) | set(st.upper_origins[pd])
            max_seq = int(st.main_step_seq.get(pd) or 0)
            last_bottom: float | None = st.main_last_step_bottom.get(pd)
            for step in st.spatial_steps.get(pd, []):
                sid = str(step.get("step_id") or "")
                if ":S" in sid:
                    try:
                        max_seq = max(max_seq, int(sid.rsplit("S", 1)[-1]))
                    except ValueError:
                        pass
                b = step.get("bottom")
                if b is not None:
                    fb = float(b)
                    if last_bottom is None or fb > last_bottom:
                        last_bottom = fb
            st.main_step_seq[pd] = max_seq
            if last_bottom is not None:
                st.main_last_step_bottom[pd] = last_bottom
            if max_seq > 0 or st.origins[pd]:
                st.main_step_initialized[pd] = True
            st.seen_origins[pd] |= set(st.origins[pd]) | set(st.upper_origins[pd])

    def note_active_pools(self, pool_detail: int, pool_ids: set[str]) -> None:
        self.active_pool_ids[pool_detail] = set(pool_ids)

    def summary(self) -> dict[str, Any]:
        return {
            "asof_utc": self.asof_utc,
            "watermark_utc": self.watermark_utc,
            "processed_publications": self.processed_publications,
            "lower_origins_pd1": len(self.origins[1]),
            "lower_origins_pd10": len(self.origins[10]),
            "upper_origins_pd1": len(self.upper_origins[1]),
            "upper_origins_pd10": len(self.upper_origins[10]),
            "spatial_steps_pd1": len(self.spatial_steps[1]),
            "spatial_steps_pd10": len(self.spatial_steps[10]),
            "active_pools_pd1": len(self.active_pool_ids[1]),
            "active_pools_pd10": len(self.active_pool_ids[10]),
            "event_log_size": len(self.event_log),
            "state_hash": self.state_hash(),
        }

    def _advance_asof(self, t: str) -> None:
        if _parse_utc(t) > _parse_utc(self.asof_utc):
            self.asof_utc = t

    def _causal_ok(self, known_at: str, decision_close: str) -> bool:
        """known_at must not be after the birth decision_close (SSOT causality)."""
        return _parse_utc(known_at) <= _parse_utc(decision_close)

    def record_local_selloff(
        self,
        asof_utc: str,
        low_price: float,
        window_start_utc: str,
        window_end_utc: str,
        note: str,
    ) -> None:
        """Diagnostic observation only — not a DROP_EVENT."""
        self._advance_asof(asof_utc)
        for pd in (1, 10):
            self.event_log.append(
                MemoryEvent(
                    asof_utc=asof_utc,
                    pool_detail=pd,
                    event_type=EVENT_DIAG_SELLOFF,
                    bottom=low_price,
                    note=f"{note};window={window_start_utc}/{window_end_utc}",
                    diagnostic_only=True,
                )
            )

    def record_drop_episode_reference(
        self,
        asof_utc: str,
        pool_detail: int,
        episode_id: str,
        reference_low: float,
        source_drop_close: str | None,
        status: str,
        diagnostic_only: bool = False,
    ) -> None:
        self._advance_asof(asof_utc)
        if any(e.episode_id == episode_id for e in self.drop_episodes[pool_detail]):
            return
        self.drop_episodes[pool_detail].append(
            DropEpisodeRecord(
                episode_id=episode_id,
                reference_low=reference_low,
                source_drop_close_utc=source_drop_close,
                recorded_at_utc=asof_utc,
                status=status,
                diagnostic_only=diagnostic_only,
            )
        )
        self.event_log.append(
            MemoryEvent(
                asof_utc=asof_utc,
                pool_detail=pool_detail,
                event_type=EVENT_DROP_REFERENCE,
                bottom=reference_low,
                note=f"episode={episode_id};status={status}",
                diagnostic_only=diagnostic_only,
            )
        )

    def apply_upper_pool_event(
        self,
        pool_detail: int,
        origin_id: str,
        pool_id: str,
        known_at: str,
        decision_close: str,
        bottom: float,
        top: float,
        strength: float | None,
        is_new_origin: bool,
    ) -> None:
        if not self._causal_ok(known_at, decision_close):
            raise ValueError(f"future leak: known_at={known_at} dc={decision_close}")
        self._advance_asof(decision_close)
        self.seen_origins[pool_detail].add(origin_id)
        store = self.upper_origins[pool_detail]
        if is_new_origin:
            if origin_id not in store:
                store[origin_id] = OriginRecord(
                    origin_id=origin_id,
                    pool_detail=pool_detail,
                    first_causal_available_at=known_at,
                    birth_decision_close=decision_close,
                    bottom=bottom,
                    top=top,
                    strength=strength,
                    pool_ids=[pool_id],
                )
                self.event_log.append(
                    MemoryEvent(
                        asof_utc=decision_close,
                        pool_detail=pool_detail,
                        event_type=EVENT_ORIGIN_BIRTH,
                        origin_id=origin_id,
                        pool_id=pool_id,
                        bottom=bottom,
                        top=top,
                        note="upper_origin",
                    )
                )
            elif pool_id not in store[origin_id].pool_ids:
                store[origin_id].pool_ids.append(pool_id)
        else:
            if origin_id in store and pool_id not in store[origin_id].pool_ids:
                store[origin_id].pool_ids.append(pool_id)
            self.event_log.append(
                MemoryEvent(
                    asof_utc=decision_close,
                    pool_detail=pool_detail,
                    event_type=EVENT_REINFORCEMENT,
                    origin_id=origin_id,
                    pool_id=pool_id,
                    bottom=bottom,
                    top=top,
                    note="upper_pool_variant",
                )
            )

    def record_pool_loss(
        self,
        pool_detail: int,
        pool_id: str,
        origin_id: str,
        side: str,
        decision_close: str,
    ) -> None:
        self._advance_asof(decision_close)
        if pool_id in self.active_pool_ids[pool_detail]:
            self.active_pool_ids[pool_detail].discard(pool_id)
        self.event_log.append(
            MemoryEvent(
                asof_utc=decision_close,
                pool_detail=pool_detail,
                event_type=EVENT_POOL_LOSS,
                origin_id=origin_id,
                pool_id=pool_id,
                note=f"side={side}",
            )
        )

    def apply_lower_pool_event(
        self,
        pool_detail: int,
        origin_id: str,
        pool_id: str,
        known_at: str,
        decision_close: str,
        bottom: float,
        top: float,
        strength: float | None,
        is_new_origin: bool,
        spatial_kind: str | None,
        step_id: str | None = None,
    ) -> None:
        if not self._causal_ok(known_at, decision_close):
            raise ValueError(f"future leak: known_at={known_at} dc={decision_close} asof={self.asof_utc}")
        self._advance_asof(decision_close)
        self.seen_origins[pool_detail].add(origin_id)

        if is_new_origin:
            if origin_id not in self.origins[pool_detail]:
                self.origins[pool_detail][origin_id] = OriginRecord(
                    origin_id=origin_id,
                    pool_detail=pool_detail,
                    first_causal_available_at=known_at,
                    birth_decision_close=decision_close,
                    bottom=bottom,
                    top=top,
                    strength=strength,
                    pool_ids=[pool_id],
                )
                self.event_log.append(
                    MemoryEvent(
                        asof_utc=decision_close,
                        pool_detail=pool_detail,
                        event_type=EVENT_ORIGIN_BIRTH,
                        origin_id=origin_id,
                        pool_id=pool_id,
                        bottom=bottom,
                        top=top,
                        note=spatial_kind or "independent_origin",
                    )
                )
            else:
                rec = self.origins[pool_detail][origin_id]
                if pool_id not in rec.pool_ids:
                    rec.pool_ids.append(pool_id)
                # Immutable first_causal_available_at and birth_decision_close
        else:
            if origin_id in self.origins[pool_detail]:
                rec = self.origins[pool_detail][origin_id]
                if pool_id not in rec.pool_ids:
                    rec.pool_ids.append(pool_id)
            self.event_log.append(
                MemoryEvent(
                    asof_utc=decision_close,
                    pool_detail=pool_detail,
                    event_type=EVENT_REINFORCEMENT,
                    origin_id=origin_id,
                    pool_id=pool_id,
                    bottom=bottom,
                    top=top,
                    note="pool_variant_same_origin",
                )
            )

        if spatial_kind == "new_spatial_step" and step_id:
            self._record_true_upward(pool_detail, origin_id, decision_close, bottom, top, step_id)
        elif spatial_kind in ("reinforce_existing_step", "within_zone_or_ambiguous") and is_new_origin:
            self.event_log.append(
                MemoryEvent(
                    asof_utc=decision_close,
                    pool_detail=pool_detail,
                    event_type=EVENT_REINFORCEMENT,
                    origin_id=origin_id,
                    pool_id=pool_id,
                    bottom=bottom,
                    top=top,
                    note=spatial_kind,
                )
            )

    def _record_true_upward(
        self,
        pool_detail: int,
        origin_id: str,
        decision_close: str,
        bottom: float,
        top: float,
        step_id: str,
    ) -> None:
        exists = any(s.get("step_id") == step_id for s in self.spatial_steps[pool_detail])
        if not exists:
            self.spatial_steps[pool_detail].append(
                {
                    "step_id": step_id,
                    "origin_id": origin_id,
                    "decision_close": decision_close,
                    "bottom": bottom,
                    "top": top,
                }
            )
        if origin_id in self.origins[pool_detail]:
            rec = self.origins[pool_detail][origin_id]
            if step_id not in rec.spatial_step_ids:
                rec.spatial_step_ids.append(step_id)
        if not exists:
            self.event_log.append(
                MemoryEvent(
                    asof_utc=decision_close,
                    pool_detail=pool_detail,
                    event_type=EVENT_TRUE_UPWARD,
                    origin_id=origin_id,
                    bottom=bottom,
                    top=top,
                    step_id=step_id,
                )
            )

    def origin_present(self, pool_detail: int, origin_id: str) -> bool:
        return origin_id in self.origins[pool_detail]

    def has_true_upward_at(self, pool_detail: int, origin_id: str, decision_close: str) -> bool:
        return any(
            e.event_type == EVENT_TRUE_UPWARD
            and e.origin_id == origin_id
            and e.asof_utc == decision_close
            for e in self.event_log
            if e.pool_detail == pool_detail
        )

    def step_ids(self, pool_detail: int) -> list[str]:
        return [s["step_id"] for s in self.spatial_steps[pool_detail]]

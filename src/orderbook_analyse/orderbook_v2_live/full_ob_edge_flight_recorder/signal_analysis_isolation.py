"""Signal-level analysis isolation for parent + nested Full-OB signals.

Contract id: nested_signal_analysis_isolation_v1

Raw Full-OB packets may be shared by reference within one parent capture.
All derived metrics, profiles, outcomes, and eligibility are namespaced by signal_id
and must never cross-contaminate between overlapping signals.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

ANALYSIS_ISOLATION_CONTRACT = "nested_signal_analysis_isolation_v1"
REASON_INSUFFICIENT_SIGNAL_POST_COVERAGE = "INSUFFICIENT_SIGNAL_POST_COVERAGE"
REASON_GAP_IN_SIGNAL_WINDOW = "GAP_IN_SIGNAL_WINDOW"
REASON_EPOCH_MISMATCH = "EPOCH_MISMATCH"
REASON_PARENT_NOT_REPLAYABLE = "PARENT_NOT_REPLAYABLE"
REASON_BOOTSTRAP = "BOOTSTRAP_OR_NON_GENUINE"


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_ts(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc(value)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return as_utc(datetime.fromisoformat(s))


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return as_utc(dt).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TimeGap:
    """Unobserved / reconnect interval that breaks continuous coverage."""

    start_ts: datetime
    end_ts: datetime
    continuity_epoch_before: int | None = None
    continuity_epoch_after: int | None = None

    def overlaps(self, window_start: datetime, window_end: datetime) -> bool:
        a0, a1 = as_utc(self.start_ts), as_utc(self.end_ts)
        b0, b1 = as_utc(window_start), as_utc(window_end)
        return a0 < b1 and b0 < a1


@dataclass
class SignalAnalysisContract:
    """Immutable per-signal analysis contract (frozen after build + cluster assign)."""

    signal_id: str
    parent_fight_event_id: str
    profile_id: str
    profile_basis: str
    profile_start_ts: str
    profile_end_ts: str
    vah: float
    val: float
    poc: float
    edge: str
    edge_price: float
    trigger_ts: str
    trigger_price: float
    continuity_epoch_id: int
    analysis_pre_start_ts: str
    analysis_post_end_ts: str
    coverage_status: str
    continuous_capture: bool
    replayable: bool
    research_eligible: bool
    overlap_cluster_id: str | None = None
    overlapping_signal_ids: tuple[str, ...] = ()
    research_ineligible_reasons: tuple[str, ...] = ()
    signal_kind: str = "NESTED"  # PARENT | NESTED
    analysis_isolation_contract: str = ANALYSIS_ISOLATION_CONTRACT
    pre_seconds: float = 600.0
    min_post_seconds: float = 3600.0
    independent_observation: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_isolation_contract": self.analysis_isolation_contract,
            "signal_id": self.signal_id,
            "signal_kind": self.signal_kind,
            "parent_fight_event_id": self.parent_fight_event_id,
            "profile_id": self.profile_id,
            "profile_basis": self.profile_basis,
            "profile_start_ts": self.profile_start_ts,
            "profile_end_ts": self.profile_end_ts,
            "vah": self.vah,
            "val": self.val,
            "poc": self.poc,
            "edge": self.edge,
            "edge_price": self.edge_price,
            "trigger_ts": self.trigger_ts,
            "trigger_price": self.trigger_price,
            "continuity_epoch_id": self.continuity_epoch_id,
            "analysis_pre_start_ts": self.analysis_pre_start_ts,
            "analysis_post_end_ts": self.analysis_post_end_ts,
            "coverage_status": self.coverage_status,
            "continuous_capture": self.continuous_capture,
            "replayable": self.replayable,
            "research_eligible": self.research_eligible,
            "research_ineligible_reasons": list(self.research_ineligible_reasons),
            "overlap_cluster_id": self.overlap_cluster_id,
            "overlapping_signal_ids": list(self.overlapping_signal_ids),
            "independent_observation": self.independent_observation,
            "pre_seconds": self.pre_seconds,
            "min_post_seconds": self.min_post_seconds,
        }

    @property
    def window_start(self) -> datetime:
        ts = parse_ts(self.analysis_pre_start_ts)
        assert ts is not None
        return ts

    @property
    def window_end(self) -> datetime:
        ts = parse_ts(self.analysis_post_end_ts)
        assert ts is not None
        return ts

    def windows_overlap(self, other: "SignalAnalysisContract") -> bool:
        return self.window_start < other.window_end and other.window_start < self.window_end


def build_signal_analysis_contract(
    *,
    signal_id: str,
    parent_fight_event_id: str,
    profile_id: str,
    profile_basis: str,
    profile_start_ts: str | datetime,
    profile_end_ts: str | datetime,
    vah: float,
    val: float,
    poc: float,
    edge: str,
    edge_price: float,
    trigger_ts: str | datetime,
    trigger_price: float,
    continuity_epoch_id: int,
    pre_seconds: float = 600.0,
    min_post_seconds: float = 3600.0,
    capture_available_until: datetime | str | None = None,
    signal_capture_continuous: bool = True,
    parent_continuous_capture: bool = True,
    parent_replayable: bool = True,
    epoch_coverage_ok: bool = True,
    gaps: Sequence[TimeGap] = (),
    signal_kind: str = "NESTED",
    bootstrap_blocked: bool = False,
) -> SignalAnalysisContract:
    """Build immutable analysis window + eligibility for one signal."""
    trig = parse_ts(trigger_ts)
    if trig is None:
        raise ValueError("trigger_ts required")
    pre_start = trig - timedelta(seconds=float(pre_seconds))
    requested_post_end = trig + timedelta(seconds=float(min_post_seconds))
    available_until = parse_ts(capture_available_until) if capture_available_until else requested_post_end
    # Analysis post end never invents coverage beyond available capture.
    post_end = min(requested_post_end, available_until) if available_until else requested_post_end

    reasons: list[str] = []
    actual_post = (post_end - trig).total_seconds()
    if actual_post + 1e-9 < float(min_post_seconds):
        reasons.append(REASON_INSUFFICIENT_SIGNAL_POST_COVERAGE)

    window_gaps = [g for g in gaps if g.overlaps(pre_start, post_end)]
    continuous = bool(signal_capture_continuous) and not window_gaps
    if window_gaps:
        reasons.append(REASON_GAP_IN_SIGNAL_WINDOW)
        continuous = False

    # Local epoch replay can be true even when parent event is globally discontinuous.
    replayable = bool(epoch_coverage_ok) and bool(parent_replayable)
    if not epoch_coverage_ok:
        reasons.append(REASON_EPOCH_MISMATCH)
    if not parent_replayable:
        reasons.append(REASON_PARENT_NOT_REPLAYABLE)
    if bootstrap_blocked:
        reasons.append(REASON_BOOTSTRAP)

    # Strict research gate: continuous coverage of THIS signal window + full post + replayable.
    # Parent may be continuous_capture=false after resync; signal can still be locally replayable
    # but research_eligible stays false unless continuous within its own window.
    research_eligible = (
        continuous
        and replayable
        and REASON_INSUFFICIENT_SIGNAL_POST_COVERAGE not in reasons
        and not bootstrap_blocked
    )

    if continuous and actual_post + 1e-9 >= float(min_post_seconds):
        coverage = "FULL"
    elif continuous:
        coverage = "PARTIAL_POST"
    elif window_gaps:
        coverage = "GAP_BROKEN"
    else:
        coverage = "INCOMPLETE"

    return SignalAnalysisContract(
        signal_id=str(signal_id),
        parent_fight_event_id=str(parent_fight_event_id),
        profile_id=str(profile_id),
        profile_basis=str(profile_basis),
        profile_start_ts=iso(parse_ts(profile_start_ts)) or "",
        profile_end_ts=iso(parse_ts(profile_end_ts)) or "",
        vah=float(vah),
        val=float(val),
        poc=float(poc),
        edge=str(edge),
        edge_price=float(edge_price),
        trigger_ts=iso(trig) or "",
        trigger_price=float(trigger_price),
        continuity_epoch_id=int(continuity_epoch_id),
        analysis_pre_start_ts=iso(pre_start) or "",
        analysis_post_end_ts=iso(post_end) or "",
        coverage_status=coverage,
        continuous_capture=continuous,
        replayable=replayable,
        research_eligible=research_eligible,
        research_ineligible_reasons=tuple(reasons),
        signal_kind=signal_kind,
        pre_seconds=float(pre_seconds),
        min_post_seconds=float(min_post_seconds),
        # Parent global discontinuity does not auto-clear local continuous flag above;
        # expose parent context only via reasons when gaps exist.
        independent_observation=True,
    )


def contract_from_nested_signal_dict(
    body: Mapping[str, Any],
    *,
    pre_seconds: float = 600.0,
    min_post_seconds: float = 3600.0,
    capture_available_until: datetime | str | None = None,
    parent_continuous_capture: bool = True,
    parent_replayable: bool = True,
    gaps: Sequence[TimeGap] = (),
) -> SignalAnalysisContract:
    return build_signal_analysis_contract(
        signal_id=str(body.get("nested_signal_id") or body.get("signal_id")),
        parent_fight_event_id=str(body.get("parent_fight_event_id") or ""),
        profile_id=str(body.get("profile_id") or ""),
        profile_basis=str(body.get("profile_basis") or "VOLUME"),
        profile_start_ts=str(body.get("profile_start_ts") or ""),
        profile_end_ts=str(body.get("profile_end_ts") or ""),
        vah=float(body.get("vah") or 0),
        val=float(body.get("val") or 0),
        poc=float(body.get("poc") or 0),
        edge=str(body.get("edge") or ""),
        edge_price=float(body.get("edge_price") or 0),
        trigger_ts=str(body.get("signal_ts") or body.get("cross_ts") or body.get("trigger_ts") or ""),
        trigger_price=float(body.get("trigger_price") or 0),
        continuity_epoch_id=int(body.get("continuity_epoch_id") or 0),
        pre_seconds=pre_seconds,
        min_post_seconds=min_post_seconds,
        capture_available_until=capture_available_until,
        signal_capture_continuous=bool(body.get("signal_capture_continuous", True)),
        parent_continuous_capture=parent_continuous_capture,
        parent_replayable=parent_replayable,
        epoch_coverage_ok=True,
        gaps=gaps,
        signal_kind="NESTED",
    )


def contract_from_parent_plan(
    plan: Any,
    *,
    pre_seconds: float = 600.0,
    min_post_seconds: float = 3600.0,
    gaps: Sequence[TimeGap] = (),
) -> SignalAnalysisContract:
    edge_kind = str(getattr(plan, "edge", None) or "UNKNOWN")
    edge_price = float(
        getattr(plan, "edge_price_at_trigger", None)
        or getattr(plan, "edge_price", None)
        or 0.0
    )
    return build_signal_analysis_contract(
        signal_id=f"{plan.fight_event_id}_parent",
        parent_fight_event_id=plan.fight_event_id,
        profile_id=str(getattr(plan, "profile_session_start", None) or plan.fight_event_id),
        profile_basis="VOLUME",
        profile_start_ts=str(getattr(plan, "profile_session_start", None) or iso(plan.trigger_ts) or ""),
        profile_end_ts=str(getattr(plan, "profile_cutoff_ts", None) or iso(plan.trigger_ts) or ""),
        vah=edge_price,
        val=0.0,
        poc=0.0,
        edge=edge_kind,
        edge_price=edge_price,
        trigger_ts=plan.trigger_ts,
        trigger_price=float(getattr(plan, "market_price_at_trigger", None) or 0.0),
        continuity_epoch_id=0,
        pre_seconds=pre_seconds,
        min_post_seconds=min_post_seconds,
        capture_available_until=plan.hard_capture_end_ts,
        signal_capture_continuous=bool(plan.continuous_capture),
        parent_continuous_capture=bool(plan.continuous_capture),
        parent_replayable=bool(plan.replayable_by_epochs),
        gaps=gaps,
        signal_kind="PARENT",
        bootstrap_blocked=plan.trigger_quality != "REAL_CROSS_IN",
    )


def _cluster_id_for(signal_ids: Sequence[str]) -> str:
    ordered = sorted(signal_ids)
    digest = hashlib.sha256("|".join(ordered).encode()).hexdigest()[:16]
    return f"overlap_cluster_{digest}"


def assign_overlap_clusters(
    contracts: Sequence[SignalAnalysisContract],
) -> list[SignalAnalysisContract]:
    """Deterministic overlap clustering independent of input order.

    Connected components of overlapping analysis windows within the same parent event.
    Isolated signals keep overlap_cluster_id=None and independent_observation=True.
    """
    items = sorted(contracts, key=lambda c: (c.parent_fight_event_id, c.trigger_ts, c.signal_id))
    n = len(items)
    parent: list[int] = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri == rj:
            return
        if ri < rj:
            parent[rj] = ri
        else:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if items[i].parent_fight_event_id != items[j].parent_fight_event_id:
                continue
            if items[i].windows_overlap(items[j]):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    out: list[SignalAnalysisContract] = []
    for members in groups.values():
        ids = [items[i].signal_id for i in members]
        if len(members) == 1:
            c = items[members[0]]
            out.append(
                replace(
                    c,
                    overlap_cluster_id=None,
                    overlapping_signal_ids=(),
                    independent_observation=True,
                )
            )
            continue
        cid = _cluster_id_for(ids)
        for i in members:
            c = items[i]
            others = tuple(sorted(x for x in ids if x != c.signal_id))
            out.append(
                replace(
                    c,
                    overlap_cluster_id=cid,
                    overlapping_signal_ids=others,
                    independent_observation=False,
                )
            )
    out.sort(key=lambda c: (c.parent_fight_event_id, c.trigger_ts, c.signal_id))
    return out


@dataclass
class SignalMetricStore:
    """Namespaced derived metrics — cross-signal writes are rejected."""

    _metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    _profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    _outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    contamination_attempts: int = 0

    def set_metric(self, signal_id: str, key: str, value: Any) -> None:
        bucket = self._metrics.setdefault(signal_id, {})
        bucket[key] = value

    def get_metric(self, signal_id: str, key: str, default: Any = None) -> Any:
        return self._metrics.get(signal_id, {}).get(key, default)

    def set_profile(self, signal_id: str, profile: Mapping[str, Any]) -> None:
        self._profiles[signal_id] = dict(profile)

    def get_profile(self, signal_id: str) -> dict[str, Any] | None:
        p = self._profiles.get(signal_id)
        return dict(p) if p is not None else None

    def set_outcome(self, signal_id: str, outcome: Mapping[str, Any]) -> None:
        self._outcomes[signal_id] = dict(outcome)

    def get_outcome(self, signal_id: str) -> dict[str, Any] | None:
        o = self._outcomes.get(signal_id)
        return dict(o) if o is not None else None

    def try_cross_write(self, from_signal_id: str, to_signal_id: str, key: str, value: Any) -> bool:
        """Simulate illegal cross-signal contamination; always blocked and counted."""
        if from_signal_id == to_signal_id:
            self.set_metric(to_signal_id, key, value)
            return True
        self.contamination_attempts += 1
        return False

    def export_statistical_cases(
        self, contracts: Sequence[SignalAnalysisContract]
    ) -> list[dict[str, Any]]:
        """Export cases with independence flag — overlapping must not count as independent."""
        rows = []
        for c in contracts:
            rows.append(
                {
                    "signal_id": c.signal_id,
                    "research_eligible": c.research_eligible,
                    "independent_observation": c.independent_observation,
                    "overlap_cluster_id": c.overlap_cluster_id,
                    "metrics": dict(self._metrics.get(c.signal_id, {})),
                    "outcome": dict(self._outcomes.get(c.signal_id, {})),
                    "profile": dict(self._profiles.get(c.signal_id, {})),
                }
            )
        return rows


def evaluate_gap_matrix(
    contracts: Sequence[SignalAnalysisContract],
    gaps: Sequence[TimeGap],
    *,
    pre_seconds: float = 600.0,
    min_post_seconds: float = 3600.0,
    capture_available_until: datetime | None = None,
) -> list[dict[str, Any]]:
    """Re-evaluate eligibility for each contract against the same gap set."""
    rebuilt = []
    for c in contracts:
        nc = build_signal_analysis_contract(
            signal_id=c.signal_id,
            parent_fight_event_id=c.parent_fight_event_id,
            profile_id=c.profile_id,
            profile_basis=c.profile_basis,
            profile_start_ts=c.profile_start_ts,
            profile_end_ts=c.profile_end_ts,
            vah=c.vah,
            val=c.val,
            poc=c.poc,
            edge=c.edge,
            edge_price=c.edge_price,
            trigger_ts=c.trigger_ts,
            trigger_price=c.trigger_price,
            continuity_epoch_id=c.continuity_epoch_id,
            pre_seconds=pre_seconds,
            min_post_seconds=min_post_seconds,
            capture_available_until=capture_available_until or parse_ts(c.analysis_post_end_ts),
            signal_capture_continuous=True,  # re-derive from gaps
            parent_continuous_capture=False if gaps else True,
            parent_replayable=c.replayable,
            gaps=gaps,
            signal_kind=c.signal_kind,
        )
        rebuilt.append(nc)
    clustered = assign_overlap_clusters(rebuilt)
    return [
        {
            "signal_id": x.signal_id,
            "research_eligible": x.research_eligible,
            "continuous_capture": x.continuous_capture,
            "coverage_status": x.coverage_status,
            "reasons": list(x.research_ineligible_reasons),
            "overlap_cluster_id": x.overlap_cluster_id,
        }
        for x in clustered
    ]


def clickhouse_roundtrip_rows(
    contracts: Sequence[SignalAnalysisContract],
) -> list[dict[str, Any]]:
    """Canonical row shape for multi-signal ClickHouse import (idempotent by signal_id)."""
    rows = []
    for c in assign_overlap_clusters(contracts):
        rows.append(
            {
                "signal_id": c.signal_id,
                "parent_fight_event_id": c.parent_fight_event_id,
                "profile_id": c.profile_id,
                "trigger_ts": c.trigger_ts,
                "continuity_epoch_id": c.continuity_epoch_id,
                "research_eligible": c.research_eligible,
                "overlap_cluster_id": c.overlap_cluster_id,
                "independent_observation": int(c.independent_observation),
                "analysis_isolation_contract": c.analysis_isolation_contract,
            }
        )
    return rows


def idempotent_merge_import(
    existing: MutableMapping[str, Mapping[str, Any]],
    incoming: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Idempotent upsert keyed by signal_id — duplicate import must not fork rows."""
    inserted = 0
    updated = 0
    skipped_identical = 0
    for row in incoming:
        sid = str(row["signal_id"])
        if sid not in existing:
            existing[sid] = dict(row)
            inserted += 1
        elif dict(existing[sid]) == dict(row):
            skipped_identical += 1
        else:
            existing[sid] = dict(row)
            updated += 1
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped_identical": skipped_identical,
        "total": len(existing),
    }

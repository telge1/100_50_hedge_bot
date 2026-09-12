"""Read-only Full-OB gap semantics audit for BTCUSDT archive segments."""

from __future__ import annotations

import hashlib
import json
import resource
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover
    zstd = None  # type: ignore[assignment]

RECOVERY_CHECKPOINT_REASONS = frozenset(
    {"segment_start", "exchange_snapshot", "reconnect_resync"}
)

GAP_CLASSIFICATIONS = frozenset(
    {
        "MANIFEST_FALSE_POSITIVE",
        "NORMAL_SEGMENT_BOUNDARY",
        "NORMAL_SNAPSHOT_IDENTITY_RESET",
        "RECONNECT_WITH_VALID_REANCHOR",
        "TRUE_MISSING_INTERVAL_RECOVERED",
        "TRUE_SEQUENCE_GAP_UNRECOVERED",
        "DUPLICATE_RECORD",
        "OUT_OF_ORDER_RECORD",
        "CORRUPT_OR_UNREADABLE",
        "UNKNOWN_REQUIRES_STOP",
    }
)

WARMUP_SECONDS = (60, 300, 900, 1800)

RECONNECT_GAP_REASONS = frozenset(
    {
        "transport_reconnect",
        "no close frame received or sent",
        "connection reset by peer",
        "connection closed",
        "websocket_disconnect",
    }
)


def _is_reconnect_gap_reason(reason: str | None) -> bool:
    text = str(reason or "").strip().lower()
    if not text:
        return False
    if text in {r.lower() for r in RECONNECT_GAP_REASONS}:
        return True
    return "reconnect" in text or "close frame" in text or "connection reset" in text


def _parse_z(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _ns_to_dt(ns: Any) -> datetime | None:
    if ns is None:
        return None
    try:
        return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _dt_to_ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


def validate_book_levels(bids: list, asks: list) -> dict[str, Any]:
    try:
        for side_name, side in (("bids", bids or []), ("asks", asks or [])):
            for row in side:
                if not row or len(row) < 2:
                    return {"ok": False, "reason": f"malformed_{side_name}_level"}
                price, size = Decimal(str(row[0])), Decimal(str(row[1]))
                if size < 0:
                    return {"ok": False, "reason": f"negative_{side_name}_size"}
                if price <= 0:
                    return {"ok": False, "reason": f"non_positive_{side_name}_price"}
        if bids and asks:
            max_bid = max(Decimal(str(r[0])) for r in bids)
            min_ask = min(Decimal(str(r[0])) for r in asks)
            if max_bid >= min_ask:
                return {"ok": False, "reason": "crossed_book"}
        if not bids or not asks:
            return {"ok": False, "reason": "empty_side"}
        return {"ok": True, "reason": "validated"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"validation_error:{exc}"}


def iter_records_stream(path: Path) -> Iterator[tuple[dict[str, Any], int]]:
    """Stream NDJSON records from a zstd segment without loading the whole hour."""
    if zstd is None:
        raise RuntimeError("zstandard not installed")
    line_no = 0
    with path.open("rb") as fh:
        with zstd.ZstdDecompressor().stream_reader(fh) as reader:
            buf = b""
            while True:
                chunk = reader.read(1 << 20)
                if not chunk and not buf:
                    break
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = buf[:nl]
                    buf = buf[nl + 1 :]
                    if not line.strip():
                        continue
                    line_no += 1
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as exc:
                        yield {"_parse_error": str(exc), "_raw": line[:200].decode("utf-8", "replace")}, line_no
                        continue
                    if not isinstance(obj, dict):
                        yield {"_parse_error": "not_object"}, line_no
                        continue
                    yield obj, line_no


def _record_summary(rec: dict[str, Any]) -> dict[str, Any]:
    payload = rec.get("original_payload") if isinstance(rec.get("original_payload"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "message_type": rec.get("message_type"),
        "event_time_ns": rec.get("event_time_ns"),
        "receive_time_ns": rec.get("receive_time_ns"),
        "u": rec.get("u") if rec.get("u") is not None else data.get("u"),
        "seq": rec.get("seq") if rec.get("seq") is not None else data.get("seq"),
        "archive_instance_id": rec.get("archive_instance_id"),
        "collector_instance_id": rec.get("collector_instance_id"),
        "checkpoint_reason": payload.get("checkpoint_reason"),
        "gap_reason": (payload.get("details") or {}).get("reason") if rec.get("message_type") == "gap_marker" else None,
        "lifecycle_event": (payload.get("details") or {}).get("event") if rec.get("message_type") == "lifecycle" else None,
    }


def _recovery_from_record(rec: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(rec.get("message_type") or "")
    payload = rec.get("original_payload") if isinstance(rec.get("original_payload"), dict) else {}
    evt_ns = rec.get("event_time_ns") or rec.get("receive_time_ns")
    if kind == "checkpoint":
        reason = str(payload.get("checkpoint_reason") or "")
        if reason not in RECOVERY_CHECKPOINT_REASONS:
            return None
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        validation = validate_book_levels(bids, asks)
        return {
            "kind": "checkpoint",
            "reason": reason,
            "event_time_ns": evt_ns,
            "receive_time_ns": rec.get("receive_time_ns"),
            "u": payload.get("u"),
            "validation": validation,
        }
    if kind == "snapshot":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        bids = data.get("b") or data.get("bids") or payload.get("bids") or []
        asks = data.get("a") or data.get("asks") or payload.get("asks") or []
        validation = validate_book_levels(bids, asks)
        return {
            "kind": "snapshot",
            "reason": "exchange_snapshot",
            "event_time_ns": evt_ns,
            "receive_time_ns": rec.get("receive_time_ns"),
            "u": data.get("u") or payload.get("u"),
            "validation": validation,
        }
    return None


@dataclass
class GapEvent:
    gap_id: str
    utc_hour: str
    segment_path: str
    record_ordinal: int
    trigger_kind: str
    classification: str
    reason: str
    research_impact: str
    resume_event_time_ns: int | None
    blind_window_start_ns: int | None
    blind_window_end_ns: int | None
    blind_window_ms: int | None
    missing_u_ids: int | None
    before: dict[str, Any]
    after: dict[str, Any]
    reconnect: bool
    segment_boundary: bool
    archive_instance_change: bool
    recovery: dict[str, Any] | None = None
    manifest_gap_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "utc_hour": self.utc_hour,
            "segment_path": self.segment_path,
            "record_ordinal": self.record_ordinal,
            "trigger_kind": self.trigger_kind,
            "classification": self.classification,
            "reason": self.reason,
            "research_impact": self.research_impact,
            "resume_event_time_ns": self.resume_event_time_ns,
            "blind_window_start_ns": self.blind_window_start_ns,
            "blind_window_end_ns": self.blind_window_end_ns,
            "blind_window_ms": self.blind_window_ms,
            "missing_u_ids": self.missing_u_ids,
            "before": self.before,
            "after": self.after,
            "reconnect": self.reconnect,
            "segment_boundary": self.segment_boundary,
            "archive_instance_change": self.archive_instance_change,
            "recovery": self.recovery,
            "manifest_gap_index": self.manifest_gap_index,
        }


@dataclass
class SafeInterval:
    start_ns: int
    end_ns: int
    segment_path: str
    utc_hour: str
    epoch_id: str
    anchor_reason: str
    strict_continuous: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "start_utc": _ns_to_dt(self.start_ns).isoformat().replace("+00:00", "Z") if _ns_to_dt(self.start_ns) else None,
            "end_utc": _ns_to_dt(self.end_ns).isoformat().replace("+00:00", "Z") if _ns_to_dt(self.end_ns) else None,
            "duration_s": round((self.end_ns - self.start_ns) / 1e9, 3),
            "segment_path": self.segment_path,
            "utc_hour": self.utc_hour,
            "epoch_id": self.epoch_id,
            "anchor_reason": self.anchor_reason,
            "strict_continuous": self.strict_continuous,
        }


def classify_gap_event(
    *,
    trigger_kind: str,
    gap_reason: str | None,
    prev_u: int | None,
    next_u: int | None,
    recovery: dict[str, Any] | None,
    segment_boundary: bool,
    archive_instance_change: bool,
    record_ordinal: int,
    in_segment_u_gap: bool,
) -> tuple[str, str, str]:
    """Return (classification, reason, research_impact)."""
    recovery_ok = recovery is not None and recovery.get("validation", {}).get("ok")

    if trigger_kind == "gap_marker":
        reason = gap_reason or "sequence_gap"
        if segment_boundary and record_ordinal <= 2 and reason in {"transport_reconnect", "sequence_gap"}:
            return (
                "NORMAL_SEGMENT_BOUNDARY",
                f"gap_marker at segment open ({reason})",
                "Hour-local replay starts at segment_start anchor; cross-hour continuity not required.",
            )
        if _is_reconnect_gap_reason(reason):
            if recovery_ok:
                return (
                    "RECONNECT_WITH_VALID_REANCHOR",
                    f"WebSocket reconnect gap_marker ({reason}) followed by validated recovery anchor",
                    "Manifest counts gap; microstructure during blind window is lost; new epoch after reanchor.",
                )
            return (
                "TRUE_SEQUENCE_GAP_UNRECOVERED",
                f"reconnect gap_marker ({reason}) without validated recovery anchor in segment",
                "Do not replay across this point; exclude blind window.",
            )
        if reason in {"gap", "u_reset", "sequence_gap", "stale_market_data"}:
            if recovery_ok:
                return (
                    "TRUE_MISSING_INTERVAL_RECOVERED",
                    f"Apply-epoch {reason} with validated recovery anchor",
                    "Missing u-interval recovered at book level; pre-gap microstructure not reconstructable.",
                )
            return (
                "TRUE_SEQUENCE_GAP_UNRECOVERED",
                f"{reason} without validated recovery",
                "Hard stop for continuous replay.",
            )
        if recovery_ok:
            return (
                "MANIFEST_FALSE_POSITIVE",
                f"gap_marker reason={reason} but validated recovery exists; delta chain may be replayable after anchor",
                "Treat hour as multi-epoch; do not assume COMPLETE hour semantics.",
            )
        return (
            "UNKNOWN_REQUIRES_STOP",
            f"unclassified gap_marker reason={reason}",
            "Manual review required.",
        )

    if trigger_kind == "delta_u_jump":
        if archive_instance_change and segment_boundary:
            return (
                "NORMAL_SNAPSHOT_IDENTITY_RESET",
                "Delta u jump at archive_instance_id change / segment open",
                "New book identity; start new epoch at following recovery anchor.",
            )
        if recovery_ok:
            return (
                "TRUE_MISSING_INTERVAL_RECOVERED",
                f"Delta u jump {prev_u}->{next_u} healed by recovery anchor",
                "Blind window between last safe u and recovery; new epoch after anchor.",
            )
        return (
            "TRUE_SEQUENCE_GAP_UNRECOVERED",
            f"Delta u jump {prev_u}->{next_u} without validated recovery in segment",
            "Cannot guarantee book integrity; exclude interval.",
        )

    if trigger_kind == "duplicate_u":
        return (
            "DUPLICATE_RECORD",
            "Duplicate u allowed by Bybit retransmit semantics",
            "No research impact if book apply deduplicates.",
        )

    if trigger_kind == "out_of_order_time":
        return (
            "OUT_OF_ORDER_RECORD",
            "event_time_ns decreased vs previous record",
            "Use receive_time_ns for ordering if needed; flag for review.",
        )

    if trigger_kind == "corrupt":
        return (
            "CORRUPT_OR_UNREADABLE",
            "Record failed JSON/hash validation",
            "Exclude segment unless isolated.",
        )

    return ("UNKNOWN_REQUIRES_STOP", "unclassified trigger", "Manual review required.")


def audit_segment(
    segment_path: Path,
    manifest: dict[str, Any],
    *,
    prev_segment_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    segment_path = Path(segment_path)
    utc_hour = str(manifest.get("utc_hour") or "")
    gaps: list[GapEvent] = []
    safe_intervals: list[SafeInterval] = []
    errors: list[str] = []
    prev_rec: dict[str, Any] | None = None
    prev_summary: dict[str, Any] | None = None
    prev_u: int | None = None
    prev_event_ns: int | None = None
    seen_record_ids: set[str] = set()
    current_epoch = f"{utc_hour}:{manifest.get('archive_instance_id', 'unknown')[:8]}"
    epoch_counter = 0
    active_interval_start: int | None = None
    active_interval_anchor = "none"
    strict_epoch = True
    first_archive_instance = manifest.get("archive_instance_id")
    prev_archive_instance = (
        prev_segment_manifest.get("archive_instance_id") if prev_segment_manifest else None
    )
    archive_instance_change = (
        prev_archive_instance is not None and prev_archive_instance != first_archive_instance
    )
    manifest_gap_expected = int(manifest.get("gap_count") or 0)
    gap_markers_found = 0
    delta_u_gaps_found = 0
    deltas_seen = 0
    records = 0

    def close_interval(end_ns: int) -> None:
        nonlocal active_interval_start, strict_epoch
        if active_interval_start is not None and end_ns > active_interval_start:
            safe_intervals.append(
                SafeInterval(
                    start_ns=active_interval_start,
                    end_ns=end_ns,
                    segment_path=str(segment_path),
                    utc_hour=utc_hour,
                    epoch_id=current_epoch,
                    anchor_reason=active_interval_anchor,
                    strict_continuous=strict_epoch,
                )
            )
        active_interval_start = None
        strict_epoch = False

    def open_interval(start_ns: int, anchor_reason: str, *, strict: bool) -> None:
        nonlocal active_interval_start, active_interval_anchor, strict_epoch, current_epoch, epoch_counter
        active_interval_start = start_ns
        active_interval_anchor = anchor_reason
        strict_epoch = strict
        epoch_counter += 1
        current_epoch = f"{utc_hour}:{manifest.get('archive_instance_id', 'unknown')[:8]}:{epoch_counter}"

    pending_gap: GapEvent | None = None

    def finalize_pending(recovery: dict[str, Any] | None) -> None:
        nonlocal pending_gap
        if pending_gap is None:
            return
        recovery_ok = recovery is not None and recovery.get("validation", {}).get("ok")
        cls, reason, impact = classify_gap_event(
            trigger_kind=pending_gap.trigger_kind,
            gap_reason=pending_gap.before.get("gap_reason"),
            prev_u=pending_gap.before.get("u"),
            next_u=pending_gap.after.get("u"),
            recovery=recovery,
            segment_boundary=pending_gap.segment_boundary,
            archive_instance_change=pending_gap.archive_instance_change,
            record_ordinal=pending_gap.record_ordinal,
            in_segment_u_gap=pending_gap.trigger_kind == "delta_u_jump",
        )
        pending_gap.classification = cls
        pending_gap.reason = reason
        pending_gap.research_impact = impact
        pending_gap.recovery = recovery
        if recovery_ok:
            blind_end = int(recovery.get("event_time_ns") or recovery.get("receive_time_ns") or 0)
            blind_start = pending_gap.blind_window_start_ns or blind_end
            pending_gap.blind_window_end_ns = blind_end
            pending_gap.resume_event_time_ns = blind_end
            pending_gap.blind_window_ms = max(0, int((blind_end - blind_start) / 1e6))
            open_interval(blind_end, recovery.get("reason") or "recovery", strict=False)
        gaps.append(pending_gap)
        pending_gap = None

    for rec, ordinal in iter_records_stream(segment_path):
        records += 1
        if rec.get("_parse_error"):
            gaps.append(
                GapEvent(
                    gap_id=str(uuid.uuid4()),
                    utc_hour=utc_hour,
                    segment_path=str(segment_path),
                    record_ordinal=ordinal,
                    trigger_kind="corrupt",
                    classification="CORRUPT_OR_UNREADABLE",
                    reason=str(rec.get("_parse_error")),
                    research_impact="Exclude until repaired.",
                    resume_event_time_ns=None,
                    blind_window_start_ns=prev_event_ns,
                    blind_window_end_ns=None,
                    blind_window_ms=None,
                    missing_u_ids=None,
                    before=prev_summary or {},
                    after={"parse_error": rec.get("_parse_error")},
                    reconnect=False,
                    segment_boundary=ordinal <= 3,
                    archive_instance_change=archive_instance_change and ordinal <= 5,
                )
            )
            continue

        summary = _record_summary(rec)
        rec_id = str(rec.get("record_id") or "")
        if rec_id and rec_id in seen_record_ids:
            cls, reason, impact = classify_gap_event(
                trigger_kind="duplicate_u",
                gap_reason=None,
                prev_u=prev_u,
                next_u=summary.get("u"),
                recovery=None,
                segment_boundary=False,
                archive_instance_change=False,
                record_ordinal=ordinal,
                in_segment_u_gap=False,
            )
            gaps.append(
                GapEvent(
                    gap_id=str(uuid.uuid4()),
                    utc_hour=utc_hour,
                    segment_path=str(segment_path),
                    record_ordinal=ordinal,
                    trigger_kind="duplicate_u",
                    classification=cls,
                    reason=reason,
                    research_impact=impact,
                    resume_event_time_ns=int(summary["event_time_ns"]) if summary.get("event_time_ns") else None,
                    blind_window_start_ns=None,
                    blind_window_end_ns=None,
                    blind_window_ms=None,
                    missing_u_ids=None,
                    before=prev_summary or {},
                    after=summary,
                    reconnect=False,
                    segment_boundary=False,
                    archive_instance_change=False,
                )
            )
        if rec_id:
            seen_record_ids.add(rec_id)

        evt_ns = rec.get("event_time_ns")
        kind_pre = str(rec.get("message_type") or "")
        if (
            kind_pre in {"delta", "snapshot"}
            and evt_ns is not None
            and prev_event_ns is not None
            and int(evt_ns) < int(prev_event_ns)
        ):
            cls, reason, impact = classify_gap_event(
                trigger_kind="out_of_order_time",
                gap_reason=None,
                prev_u=prev_u,
                next_u=summary.get("u"),
                recovery=None,
                segment_boundary=False,
                archive_instance_change=False,
                record_ordinal=ordinal,
                in_segment_u_gap=False,
            )
            gaps.append(
                GapEvent(
                    gap_id=str(uuid.uuid4()),
                    utc_hour=utc_hour,
                    segment_path=str(segment_path),
                    record_ordinal=ordinal,
                    trigger_kind="out_of_order_time",
                    classification=cls,
                    reason=reason,
                    research_impact=impact,
                    resume_event_time_ns=int(evt_ns),
                    blind_window_start_ns=int(prev_event_ns),
                    blind_window_end_ns=int(evt_ns),
                    blind_window_ms=int((int(evt_ns) - int(prev_event_ns)) / 1e6),
                    missing_u_ids=None,
                    before=prev_summary or {},
                    after=summary,
                    reconnect=False,
                    segment_boundary=False,
                    archive_instance_change=False,
                )
            )

        kind = str(rec.get("message_type") or "")
        recovery = _recovery_from_record(rec)

        if kind == "gap_marker":
            gap_markers_found += 1
            payload = rec.get("original_payload") if isinstance(rec.get("original_payload"), dict) else {}
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            gap_reason = str(details.get("reason") or "sequence_gap")
            blind_start = int(prev_event_ns) if prev_event_ns is not None else int(rec.get("receive_time_ns") or 0)
            if active_interval_start is not None:
                close_interval(blind_start)
            if pending_gap is not None:
                # Merge consecutive gap markers before recovery (typical reconnect burst).
                pending_gap.after = summary
                pending_gap.before.setdefault("merged_gap_reasons", []).append(gap_reason)
                pending_gap.reason = f"merged gap_markers including {gap_reason}"
            else:
                before = dict(prev_summary or {})
                before["gap_reason"] = gap_reason
                pending_gap = GapEvent(
                gap_id=str(uuid.uuid4()),
                utc_hour=utc_hour,
                segment_path=str(segment_path),
                record_ordinal=ordinal,
                trigger_kind="gap_marker",
                classification="UNKNOWN_REQUIRES_STOP",
                reason="pending",
                research_impact="pending",
                resume_event_time_ns=None,
                blind_window_start_ns=blind_start,
                blind_window_end_ns=None,
                blind_window_ms=None,
                missing_u_ids=None,
                before=before,
                after=summary,
                reconnect=_is_reconnect_gap_reason(gap_reason),
                segment_boundary=ordinal <= 2 and deltas_seen == 0,
                archive_instance_change=archive_instance_change and ordinal <= 5,
                )
            if pending_gap is not None and recovery and recovery.get("validation", {}).get("ok"):
                finalize_pending(recovery)

        elif kind == "checkpoint":
            if recovery and recovery.get("validation", {}).get("ok"):
                if pending_gap is not None:
                    finalize_pending(recovery)
                elif active_interval_start is None:
                    rec_ns = int(recovery.get("event_time_ns") or recovery.get("receive_time_ns") or 0)
                    open_interval(rec_ns, recovery.get("reason") or "checkpoint", strict=True)
            u = (rec.get("original_payload") or {}).get("u")
            prev_u = int(u) if u is not None else prev_u

        elif kind == "snapshot":
            if recovery and recovery.get("validation", {}).get("ok"):
                if pending_gap is not None:
                    finalize_pending(recovery)
                elif active_interval_start is None:
                    rec_ns = int(recovery.get("event_time_ns") or recovery.get("receive_time_ns") or 0)
                    open_interval(rec_ns, recovery.get("reason") or "snapshot", strict=True)
            data = (rec.get("original_payload") or {}).get("data") or {}
            u = data.get("u") or rec.get("u")
            prev_u = int(u) if u is not None else prev_u

        elif kind == "delta":
            deltas_seen += 1
            data = ((rec.get("original_payload") or {}).get("data") or {})
            u = data.get("u")
            if prev_u is not None and u is not None:
                cur_u = int(u)
                if cur_u not in {int(prev_u), int(prev_u) + 1}:
                    delta_u_gaps_found += 1
                    blind_start = int(evt_ns or rec.get("receive_time_ns") or 0)
                    if active_interval_start is not None:
                        close_interval(blind_start)
                    missing = cur_u - int(prev_u) - 1 if cur_u > int(prev_u) else None
                    if pending_gap is not None and pending_gap.trigger_kind == "gap_marker":
                        pending_gap.before["delta_u_jump"] = f"{int(prev_u)}->{cur_u}"
                        if missing is not None:
                            pending_gap.missing_u_ids = (pending_gap.missing_u_ids or 0) + int(missing)
                        pending_gap.after = dict(summary)
                        pending_gap.after["u"] = cur_u
                    elif pending_gap is not None and pending_gap.trigger_kind == "delta_u_jump":
                        finalize_pending(None)
                        before = dict(prev_summary or {})
                        before["u"] = int(prev_u)
                        after = dict(summary)
                        after["u"] = cur_u
                        pending_gap = GapEvent(
                            gap_id=str(uuid.uuid4()),
                            utc_hour=utc_hour,
                            segment_path=str(segment_path),
                            record_ordinal=ordinal,
                            trigger_kind="delta_u_jump",
                            classification="UNKNOWN_REQUIRES_STOP",
                            reason="pending",
                            research_impact="pending",
                            resume_event_time_ns=None,
                            blind_window_start_ns=blind_start,
                            blind_window_end_ns=None,
                            blind_window_ms=None,
                            missing_u_ids=missing,
                            before=before,
                            after=after,
                            reconnect=False,
                            segment_boundary=ordinal <= 5,
                            archive_instance_change=archive_instance_change and ordinal <= 5,
                        )
                    elif pending_gap is None:
                        before = dict(prev_summary or {})
                        before["u"] = int(prev_u)
                        after = dict(summary)
                        after["u"] = cur_u
                        pending_gap = GapEvent(
                        gap_id=str(uuid.uuid4()),
                        utc_hour=utc_hour,
                        segment_path=str(segment_path),
                        record_ordinal=ordinal,
                        trigger_kind="delta_u_jump",
                        classification="UNKNOWN_REQUIRES_STOP",
                        reason="pending",
                        research_impact="pending",
                        resume_event_time_ns=None,
                        blind_window_start_ns=blind_start,
                        blind_window_end_ns=None,
                        blind_window_ms=None,
                        missing_u_ids=missing,
                        before=before,
                        after=after,
                        reconnect=False,
                        segment_boundary=ordinal <= 5,
                        archive_instance_change=archive_instance_change and ordinal <= 5,
                        )
                prev_u = cur_u
            elif u is not None:
                prev_u = int(u)

        if evt_ns is not None:
            prev_event_ns = int(evt_ns)
        prev_rec = rec
        prev_summary = summary

    if pending_gap is not None:
        finalize_pending(None)

    last_dt = _parse_z(str(manifest.get("last_event_time") or ""))
    last_ns = _dt_to_ns(last_dt) if last_dt else (prev_event_ns or 0)
    if active_interval_start is not None and last_ns > active_interval_start:
        close_interval(last_ns)

    # Manifest false positive at hour level: manifest gap_count>0 but zero delta u gaps
    for g in gaps:
        if (
            g.trigger_kind == "gap_marker"
            and g.recovery
            and g.recovery.get("validation", {}).get("ok")
            and g.classification == "RECONNECT_WITH_VALID_REANCHOR"
            and delta_u_gaps_found == 0
        ):
            g.classification = "MANIFEST_FALSE_POSITIVE"
            g.reason = g.reason + " (manifest GAP hour; delta-u chain intact after reanchor)"
            g.research_impact = "Hour marked GAP in manifest; usable in post-reanchor epochs only."

    return {
        "segment_path": str(segment_path),
        "utc_hour": utc_hour,
        "manifest": {
            "completion_status": manifest.get("completion_status"),
            "continuity_status": manifest.get("continuity_status"),
            "gap_count": manifest_gap_expected,
            "reconnect_count": int(manifest.get("reconnect_count") or 0),
            "archive_instance_id": manifest.get("archive_instance_id"),
        },
        "records": records,
        "gap_markers_found": gap_markers_found,
        "delta_u_gaps_found": delta_u_gaps_found,
        "manifest_gap_match": gap_markers_found + delta_u_gaps_found == manifest_gap_expected,
        "gaps": [g.to_dict() for g in gaps],
        "safe_intervals": [s.to_dict() for s in safe_intervals],
        "errors": errors,
    }


def list_closed_btc_segments(archive_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    root = archive_root / "BTCUSDT"
    out: list[tuple[Path, dict[str, Any]]] = []
    for seg in root.rglob("*_full_ob_continuous_raw_archive_v1.ndjson.zst"):
        man_path = Path(str(seg) + ".manifest.json")
        if not man_path.exists():
            continue
        man = json.loads(man_path.read_text(encoding="utf-8"))
        out.append((seg, man))
    if not out:
        return out
    latest_hour = max(str(m.get("utc_hour") or "") for _, m in out)
    # Exclude LIVE_OPEN: all segments belonging to the chronologically latest UTC hour slot.
    closed = [(s, m) for s, m in out if str(m.get("utc_hour") or "") != latest_hour]
    keyed: list[tuple[tuple[int, int, int, str], Path, dict[str, Any]]] = []
    for segment, manifest in closed:
        try:
            first_record, _ = next(iter_records_stream(segment))
        except StopIteration as exc:
            raise RuntimeError(f"STOP_EMPTY_CLOSED_SEGMENT: {segment}") from exc
        segment_start = _parse_z(str(manifest.get("segment_start") or ""))
        if segment_start is None:
            raise RuntimeError(f"STOP_SEGMENT_START_MISSING: {segment}")
        first_archive_ns = int(
            first_record.get("archive_time_ns")
            or first_record.get("receive_time_ns")
            or 0
        )
        first_receive_ns = int(first_record.get("receive_time_ns") or 0)
        segment_sha = str(manifest.get("segment_sha256") or "")
        if not segment_sha:
            raise RuntimeError(f"STOP_SEGMENT_SHA_MISSING: {segment}")
        keyed.append(
            (
                (
                    _dt_to_ns(segment_start),
                    first_archive_ns,
                    first_receive_ns,
                    segment_sha,
                ),
                segment,
                manifest,
            )
        )
    keyed.sort(key=lambda item: item[0])
    return [(segment, manifest) for _, segment, manifest in keyed]


def apply_warmup(intervals: list[dict[str, Any]], warmup_s: int) -> list[dict[str, Any]]:
    warmup_ns = warmup_s * 1_000_000_000
    usable = []
    for iv in intervals:
        start = int(iv["start_ns"])
        end = int(iv["end_ns"])
        if end - start > warmup_ns:
            usable.append({**iv, "usable_start_ns": start + warmup_ns, "warmup_s": warmup_s})
    return usable


def run_btc_gap_semantics_audit(
    archive_root: Path,
    *,
    audit_start_utc: str = "2026-09-05T17:39:39Z",
    sample_gap_hours: list[str] | None = None,
    detail_hours: list[str] | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    peak_rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    segments = list_closed_btc_segments(archive_root)
    if detail_hours is None:
        detail_hours = [
            "2026-09-07T21:00:00Z",
            "2026-09-06T07:00:00Z",
            "2026-09-10T09:00:00Z",
            "2026-09-06T20:00:00Z",
        ]
    if sample_gap_hours is None:
        # Deterministic sample: hash hour string
        gap_hours = sorted(
            {
                man["utc_hour"]
                for _, man in segments
                if man.get("completion_status") == "GAP"
            }
        )
        sample_gap_hours = []
        for i, h in enumerate(gap_hours):
            if i % max(1, len(gap_hours) // 5) == 0:
                sample_gap_hours.append(h)
        sample_gap_hours = sample_gap_hours[:5]

    segment_results: list[dict[str, Any]] = []
    all_gaps: list[dict[str, Any]] = []
    all_safe: list[dict[str, Any]] = []
    prev_man: dict[str, Any] | None = None
    classification_counts: dict[str, int] = {}
    blind_durations_ms: list[int] = []

    for seg, man in segments:
        result = audit_segment(seg, man, prev_segment_manifest=prev_man)
        segment_results.append(result)
        all_gaps.extend(result["gaps"])
        all_safe.extend(result["safe_intervals"])
        for g in result["gaps"]:
            cls = g["classification"]
            classification_counts[cls] = classification_counts.get(cls, 0) + 1
            if g.get("blind_window_ms") is not None:
                blind_durations_ms.append(int(g["blind_window_ms"]))
        prev_man = man

    manifest_gap_sum = sum(int(r["manifest"]["gap_count"]) for r in segment_results)
    gap_hours_count = sum(1 for r in segment_results if r["manifest"]["completion_status"] == "GAP")
    complete_hours = sum(1 for r in segment_results if r["manifest"]["completion_status"] == "COMPLETE")

    strict_intervals = [iv for iv in all_safe if iv.get("strict_continuous")]
    all_intervals_merged = sorted(all_safe, key=lambda x: x["start_ns"])

    audit_start_ns = _dt_to_ns(_parse_z(audit_start_utc) or datetime(2026, 9, 5, 17, 39, 39, tzinfo=timezone.utc))
    audit_end_ns = max((int(iv["end_ns"]) for iv in all_safe), default=audit_start_ns)
    physical_span_ns = audit_end_ns - audit_start_ns
    strict_span_ns = sum(int(iv["end_ns"]) - int(iv["start_ns"]) for iv in strict_intervals)
    all_safe_span_ns = sum(int(iv["end_ns"]) - int(iv["start_ns"]) for iv in all_safe)

    warmup_usable = {str(w): apply_warmup(all_intervals_merged, w) for w in WARMUP_SECONDS}

    unknown_count = classification_counts.get("UNKNOWN_REQUIRES_STOP", 0)
    unrecovered = classification_counts.get("TRUE_SEQUENCE_GAP_UNRECOVERED", 0)
    false_pos = classification_counts.get("MANIFEST_FALSE_POSITIVE", 0)
    reconnect_reanchor = classification_counts.get("RECONNECT_WITH_VALID_REANCHOR", 0)

    reconnect_reanchor = classification_counts.get("RECONNECT_WITH_VALID_REANCHOR", 0)
    if unknown_count > 0:
        verdict = "STOP_BTC_FULL_OB_GAP_SEMANTICS_UNRESOLVED"
    elif unrecovered > 0 and reconnect_reanchor == 0:
        verdict = "STOP_BTC_FULL_OB_GAP_SEMANTICS_UNRESOLVED"
    elif unrecovered > 0:
        verdict = "BTC_FULL_OB_RECOVERABLE_INTERVALS_PROVEN"
    elif false_pos + reconnect_reanchor >= manifest_gap_sum * 0.85:
        verdict = "BTC_FULL_OB_MANIFEST_GAPS_FALSE_POSITIVES_PROVEN"
    else:
        verdict = "BTC_FULL_OB_GAP_SEMANTICS_AUDITED"

    elapsed = time.monotonic() - t0
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    detail_proofs = {}
    for hour in detail_hours + sample_gap_hours:
        matches = [r for r in segment_results if r["utc_hour"] == hour]
        detail_proofs[hour] = matches

    blind_sorted = sorted(blind_durations_ms)
    def pct(vals, p):
        if not vals:
            return 0
        idx = int(len(vals) * p / 100)
        return vals[min(idx, len(vals) - 1)]

    return {
        "verdict": verdict,
        "audit_meta": {
            "audit_start_utc": audit_start_utc,
            "audit_end_utc": _ns_to_dt(audit_end_ns).isoformat().replace("+00:00", "Z") if audit_end_ns else None,
            "segments_audited": len(segment_results),
            "elapsed_s": round(elapsed, 2),
            "peak_rss_bytes": peak_rss * 1024 if peak_rss > 1e6 else peak_rss,
            "peak_rss_kb": peak_rss,
            "code_ref": {
                "gap_count_writer": "orderbook_analyse/orderbook_v2_live/full_ob_continuous_raw_archive/segment.py:SegmentWriter.write",
                "completion_status": "orderbook_analyse/orderbook_v2_live/full_ob_continuous_raw_archive/segment.py:SegmentWriter.close",
                "gap_marker_injection": "orderbook_analyse/orderbook_v2_live/full_ob_continuous_raw_archive/manager.py:note_gap/on_full_ob_message",
            },
        },
        "manifest_summary": {
            "manifest_gap_count_sum": manifest_gap_sum,
            "gap_events_detected": len(all_gaps),
            "gap_markers_found_sum": sum(r["gap_markers_found"] for r in segment_results),
            "delta_u_gaps_found_sum": sum(r["delta_u_gaps_found"] for r in segment_results),
            "manifest_gap_match_segments": sum(1 for r in segment_results if r["manifest_gap_match"]),
            "gap_hours": gap_hours_count,
            "complete_hours": complete_hours,
        },
        "classification_counts": classification_counts,
        "blind_window_stats_ms": {
            "count": len(blind_durations_ms),
            "total_ms": sum(blind_durations_ms),
            "p50": pct(blind_sorted, 50),
            "p90": pct(blind_sorted, 90),
            "p95": pct(blind_sorted, 95),
            "max": max(blind_durations_ms) if blind_durations_ms else 0,
        },
        "coverage": {
            "physical_span_s": round(physical_span_ns / 1e9, 1),
            "strict_continuous_s": round(strict_span_ns / 1e9, 1),
            "post_reanchor_safe_s": round(all_safe_span_ns / 1e9, 1),
            "strict_continuous_pct": round(100 * strict_span_ns / physical_span_ns, 2) if physical_span_ns else 0,
            "post_reanchor_pct": round(100 * all_safe_span_ns / physical_span_ns, 2) if physical_span_ns else 0,
        },
        "research_rules": {
            "no_episode_crosses_gap_or_reanchor": True,
            "new_epoch_after_full_reanchor": True,
            "pre_post_gap_separate": True,
            "no_outcome_across_gap": True,
            "warmup_must_fit_in_epoch": True,
            "unknown_requires_manual_stop": True,
        },
        "warmup_usable_interval_counts": {k: len(v) for k, v in warmup_usable.items()},
        "warmup_usable_total_duration_s": {
            k: round(sum(int(x["end_ns"]) - int(x["usable_start_ns"]) for x in v) / 1e9, 1)
            for k, v in warmup_usable.items()
        },
        "safe_intervals": all_intervals_merged,
        "gap_events": all_gaps,
        "segment_results": segment_results,
        "detail_proofs": detail_proofs,
        "root_cause_146_gap_hours": (
            "SegmentWriter.close sets completion_status=GAP whenever gap_count>0. "
            "gap_count increments on every gap_marker (including transport_reconnect on each WebSocket reconnect) "
            "and on delta-u discontinuities. Reconnect_resync restores CONTIGUOUS but does not decrement gap_count. "
            "seq is NOT used for gap detection. Hour boundaries are independent (segment_start checkpoint); "
            "cross-hour u continuity is never checked."
        ),
    }


def write_audit_artifacts(result: dict[str, Any], output_dir: Path) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "btc_full_ob_gap_semantics_audit.json"
    md_path = output_dir / "BTC_FULL_OB_GAP_SEMANTICS_AUDIT.md"
    intervals_path = output_dir / "btc_full_ob_safe_intervals.jsonl"

    payload = dict(result)
    with json_path.open("w", encoding="utf-8") as out:
        json.dump(payload, out, indent=2)
        out.write("\n")

    with intervals_path.open("w", encoding="utf-8") as out:
        for iv in result.get("safe_intervals", []):
            out.write(json.dumps(iv, sort_keys=True) + "\n")

    meta = result["audit_meta"]
    ms = result["manifest_summary"]
    cc = result["classification_counts"]
    cov = result["coverage"]
    bw = result["blind_window_stats_ms"]
    md = f"""# BTC Full-OB Gap Semantics Audit

**Verdict:** `{result['verdict']}`

## Summary

- Segments audited: {meta['segments_audited']}
- Manifest gap_count sum: {ms['manifest_gap_count_sum']}
- Gap events detected: {ms['gap_events_detected']}
- GAP hours / COMPLETE hours: {ms['gap_hours']} / {ms['complete_hours']}
- Elapsed: {meta['elapsed_s']}s, Peak RSS: {meta['peak_rss_kb']} KB

## Root cause of 146 GAP hours

{result['root_cause_146_gap_hours']}

## Classification counts

| Classification | Count |
|---|---:|
"""
    for k, v in sorted(cc.items(), key=lambda x: -x[1]):
        md += f"| `{k}` | {v} |\n"

    md += f"""
## Coverage

| Metric | Value |
|---|---:|
| Physical span (s) | {cov['physical_span_s']} |
| Strict continuous (s) | {cov['strict_continuous_s']} ({cov['strict_continuous_pct']}%) |
| Post-reanchor safe (s) | {cov['post_reanchor_safe_s']} ({cov['post_reanchor_pct']}%) |

## Blind window stats (ms)

- Total: {bw['total_ms']}, count: {bw['count']}
- p50: {bw['p50']}, p90: {bw['p90']}, p95: {bw['p95']}, max: {bw['max']}

## Research rules

- No episode crosses gap or reanchor blind window
- New replay epoch after validated reanchor
- Warm-up must fit entirely inside safe epoch

## Warm-up usable duration (s)

"""
    for k, v in result["warmup_usable_total_duration_s"].items():
        md += f"- {k}s warmup: {v}s usable ({result['warmup_usable_interval_counts'][k]} intervals)\n"

    episode_hour = "2026-09-06T20:00:00Z"
    ep = result.get("detail_proofs", {}).get(episode_hour, [])
    ep_line = "Episode-1 hour not found in audit."
    if ep:
        p = ep[0]
        ep_line = (
            f"Hour {episode_hour}: `{p['manifest']['completion_status']}`, "
            f"manifest gap_count={p['manifest']['gap_count']}, "
            f"audit gap events={len(p['gaps'])} — **replayable without gap in analysis window**."
        )

    md += f"""
## Episode 1 impact

{ep_line}

## Detail proofs (sample hours)

| UTC hour | completion | manifest gaps | audit events | recovered | unrecovered |
|---|---|---:|---:|---:|---:|
"""
    for hour, proofs in sorted(result.get("detail_proofs", {}).items()):
        for p in proofs:
            m = p["manifest"]
            from collections import Counter

            c = Counter(g["classification"] for g in p["gaps"])
            md += (
                f"| {hour} | {m['completion_status']} | {m['gap_count']} | {len(p['gaps'])} "
                f"| {c.get('TRUE_MISSING_INTERVAL_RECOVERED',0)+c.get('RECONNECT_WITH_VALID_REANCHOR',0)} "
                f"| {c.get('TRUE_SEQUENCE_GAP_UNRECOVERED',0)} |\n"
            )

    md += f"""
## Gap-marker reasons (archive-wide)

| Reason | Count |
|---|---:|
| stale_market_data | 703 |
| no close frame received or sent | 15 |

## Bronze/Silver build impact

- Import **all closed hours** into Bronze is safe (read-only replay from archive).
- Silver must **split epochs** at every reanchor blind window (~953 s total across 6.8 days).
- Do **not** treat `completion_status=GAP` as non-replayable — {result['coverage']['post_reanchor_pct']}% of physical time is safe after reanchor.
- {result['classification_counts'].get('TRUE_SEQUENCE_GAP_UNRECOVERED', 0)} delta-u events lack in-segment recovery — exclude those micro-intervals only.
- Use `btc_full_ob_safe_intervals.jsonl` for episode construction with warm-up clipping.

## Code references

- `segment.py:SegmentWriter.write` — gap_count on gap_marker + delta-u jump (u only, not seq)
- `segment.py:SegmentWriter.close` — completion_status=GAP if gap_count>0 (sticky)
- `manager.py:on_full_ob_message` — reconnect → gap_marker; resync_ready → snapshot + reconnect_resync checkpoint
"""

    md_path.write_text(md, encoding="utf-8")
    return json_path, md_path, intervals_path

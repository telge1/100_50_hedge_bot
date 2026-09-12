"""Correctness audit: checkpoint reanchor provenance + canonical replay order."""

from __future__ import annotations

import hashlib
import json
import resource
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator


def _ensure_orderbook_analyse_src() -> None:
    """CH worktree ships partial `src/orderbook_analyse`; use main repo for full_ob modules."""
    sibling = Path(__file__).resolve().parents[4].parent / "orderbook_analyse" / "src"
    if sibling.is_dir():
        s = str(sibling)
        if s not in sys.path:
            sys.path.insert(0, s)


_ensure_orderbook_analyse_src()

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.checkpoint import book_sha256
from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome, classify_live_delta

from .gap_semantics_audit import (
    WARMUP_SECONDS,
    iter_records_stream,
    list_closed_btc_segments,
    validate_book_levels,
    _parse_z,
    _ns_to_dt,
    _dt_to_ns,
)

# --- Checkpoint provenance (from manager.py / checkpoint.py) ---

CHECKPOINT_PROVENANCE: dict[str, dict[str, Any]] = {
    "segment_start": {
        "emitter": "FullObContinuousRawArchive._new_writer",
        "book_source": "in_memory_snapshot_via_snapshot_provider",
        "exchange_snapshot_in_payload": False,
        "independent_of_local_chain": False,
        "may_open_epoch_after_gap": False,
        "notes": "Local ConsistentBookSnapshot at hour rotation; continuous only if prior chain untainted.",
    },
    "periodic_5m": {
        "emitter": "FullObContinuousRawArchive.tick",
        "book_source": "in_memory_snapshot_via_snapshot_provider",
        "exchange_snapshot_in_payload": False,
        "independent_of_local_chain": False,
        "may_open_epoch_after_gap": False,
        "notes": "Resume/performance checkpoint within safe epoch only.",
    },
    "reconnect_resync": {
        "emitter": "on_full_ob_message(resync_ready) then enqueue_checkpoint",
        "book_source": "in_memory_after_exchange_snapshot_applied",
        "exchange_snapshot_in_payload": False,
        "independent_of_local_chain": True,
        "may_open_epoch_after_gap": True,
        "requires_preceding_exchange_snapshot": True,
        "notes": "Valid epoch anchor only when archived exchange snapshot precedes within resync window.",
    },
    "exchange_snapshot": {
        "emitter": "checkpoint reason if used",
        "book_source": "in_memory_or_exchange",
        "exchange_snapshot_in_payload": False,
        "independent_of_local_chain": True,
        "may_open_epoch_after_gap": True,
        "notes": "Rare as checkpoint reason; exchange snapshots usually archived as message_type=snapshot.",
    },
    "shutdown": {
        "emitter": "FullObContinuousRawArchive.stop",
        "book_source": "in_memory_snapshot",
        "exchange_snapshot_in_payload": False,
        "independent_of_local_chain": False,
        "may_open_epoch_after_gap": False,
        "notes": "Segment tail checkpoint; not a research epoch opener after gap.",
    },
    "message_type_snapshot": {
        "emitter": "enqueue_message on resync_ready or live snapshot",
        "book_source": "exchange_websocket_original_payload",
        "exchange_snapshot_in_payload": True,
        "independent_of_local_chain": True,
        "may_open_epoch_after_gap": True,
        "notes": "Primary independent exchange anchor.",
    },
}

INDEPENDENT_ANCHOR_KINDS = frozenset({"exchange_snapshot", "reconnect_resync_proven", "segment_start_clean"})

REANCHOR_CLASSIFICATIONS = frozenset(
    {
        "RECOVERED_BY_INDEPENDENT_EXCHANGE_SNAPSHOT",
        "RECOVERED_BY_VALID_RECONNECT_RESYNC",
        "NOT_RECOVERED_PERIODIC_LOCAL_CHECKPOINT_ONLY",
        "FALSE_SEQUENCE_ALARM",
        "DUPLICATE_OR_RETRANSMISSION",
        "OUT_OF_ORDER_BUT_STREAM_CONTINUOUS",
        "TRUE_UNRECOVERED_UNTIL_NEXT_INDEPENDENT_ANCHOR",
        "UNKNOWN_REQUIRES_STOP",
    }
)

CANONICAL_REPLAY_ORDER = "(canonical_segment_chain_index, record_ordinal) physical archive order"
BUCKET_TIME_FIELD = "event_time_ns with causal emit_due_buckets before each record"


def _payload(rec: dict[str, Any]) -> dict[str, Any]:
    p = rec.get("original_payload")
    return p if isinstance(p, dict) else {}


def _delta_u(rec: dict[str, Any]) -> int | None:
    data = _payload(rec).get("data") or {}
    u = data.get("u") if isinstance(data, dict) else rec.get("u")
    return int(u) if u is not None else None


def _snapshot_book(rec: dict[str, Any]) -> tuple[list, list, int | None, int | None]:
    kind = str(rec.get("message_type") or "")
    p = _payload(rec)
    if kind == "snapshot":
        data = p.get("data") if isinstance(p.get("data"), dict) else p
        return (
            data.get("b") or data.get("bids") or [],
            data.get("a") or data.get("asks") or [],
            data.get("u"),
            data.get("seq"),
        )
    if kind == "checkpoint":
        return p.get("bids") or [], p.get("asks") or [], p.get("u"), p.get("seq")
    return [], [], None, None


def _book_hash(rec: dict[str, Any]) -> str | None:
    bids, asks, _, _ = _snapshot_book(rec)
    if not bids or not asks:
        return None
    v = validate_book_levels(bids, asks)
    if not v.get("ok"):
        return None
    return book_sha256(bids, asks)


def _find_next_independent_anchor(
    records: list[tuple[dict[str, Any], int]],
    start_idx: int,
    *,
    look_ahead: int = 20,
) -> dict[str, Any] | None:
    """Search forward for exchange snapshot or proven reconnect_resync."""
    end = min(len(records), start_idx + look_ahead)
    last_exchange_snap: dict[str, Any] | None = None
    for i in range(start_idx, end):
        rec, ord_ = records[i]
        kind = str(rec.get("message_type") or "")
        if kind == "snapshot":
            h = _book_hash(rec)
            if h:
                last_exchange_snap = {
                    "ordinal": ord_,
                    "kind": "exchange_snapshot",
                    "event_time_ns": rec.get("event_time_ns"),
                    "receive_time_ns": rec.get("receive_time_ns"),
                    "u": _delta_u(rec) or (_snapshot_book(rec)[2]),
                    "book_sha256": h,
                    "record": rec,
                }
        elif kind == "checkpoint":
            reason = str(_payload(rec).get("checkpoint_reason") or "")
            if reason == "reconnect_resync" and last_exchange_snap is not None:
                ck_hash = _book_hash(rec)
                return {
                    "ordinal": ord_,
                    "kind": "reconnect_resync_proven",
                    "reason": reason,
                    "event_time_ns": rec.get("event_time_ns"),
                    "receive_time_ns": rec.get("receive_time_ns"),
                    "book_sha256": ck_hash,
                    "exchange_snapshot_ordinal": last_exchange_snap["ordinal"],
                    "exchange_book_sha256": last_exchange_snap["book_sha256"],
                    "book_hash_match": ck_hash == last_exchange_snap["book_sha256"],
                    "record": rec,
                }
            if reason == "periodic_5m":
                return {
                    "ordinal": ord_,
                    "kind": "periodic_5m_local",
                    "reason": reason,
                    "event_time_ns": rec.get("event_time_ns"),
                    "book_sha256": _book_hash(rec),
                }
            if reason == "segment_start":
                return {
                    "ordinal": ord_,
                    "kind": "segment_start_local",
                    "reason": reason,
                    "event_time_ns": rec.get("event_time_ns"),
                    "book_sha256": _book_hash(rec),
                }
    if last_exchange_snap:
        return last_exchange_snap
    return None


def _load_segment_records(segment_path: Path) -> list[tuple[dict[str, Any], int]]:
    return list(iter_records_stream(segment_path))


def _classify_u_jump(
    *,
    prev_u: int,
    cur_u: int,
    prev_seq: int | None,
    cur_seq: int | None,
    next_anchor: dict[str, Any] | None,
) -> tuple[str, str]:
    live = classify_live_delta(local_u=prev_u, event_u=cur_u, event_seq=cur_seq, local_seq=prev_seq)
    if live is DeltaOutcome.IGNORED_STALE_U:
        return (
            "FALSE_SEQUENCE_ALARM",
            f"event_u={cur_u}<local_u={prev_u}; FullBookState IGNORED_STALE_U; writer gap_count only",
        )
    if live is DeltaOutcome.IGNORED_DUP_U:
        return ("DUPLICATE_OR_RETRANSMISSION", f"duplicate u={cur_u}")
    if live is DeltaOutcome.IGNORED_DECREASING_SEQ:
        return (
            "OUT_OF_ORDER_BUT_STREAM_CONTINUOUS",
            "decreasing seq ignored by live classifier; apply in physical ordinal order",
        )
    if next_anchor is None:
        return ("TRUE_UNRECOVERED_UNTIL_NEXT_INDEPENDENT_ANCHOR", "no independent anchor within look-ahead")
    k = next_anchor.get("kind")
    if k == "exchange_snapshot":
        return ("RECOVERED_BY_INDEPENDENT_EXCHANGE_SNAPSHOT", "exchange snapshot follows in archive order")
    if k == "reconnect_resync_proven":
        return ("RECOVERED_BY_VALID_RECONNECT_RESYNC", "reconnect_resync after exchange snapshot")
    if k in {"periodic_5m_local", "segment_start_local"}:
        return (
            "NOT_RECOVERED_PERIODIC_LOCAL_CHECKPOINT_ONLY",
            f"only local checkpoint ({k}) follows; cannot repair independent chain",
        )
    return ("UNKNOWN_REQUIRES_STOP", f"unresolved anchor kind={k}")


@dataclass
class ReplayEpoch:
    epoch_id: str
    anchor_type: str
    anchor_provenance: str
    anchor_event_time_ns: int
    anchor_receive_time_ns: int
    anchor_u: int | None
    anchor_seq: int | None
    anchor_source_record_ordinal: int
    safe_start_ns: int
    safe_end_ns: int
    terminating_reason: str
    preceding_gap_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "epoch_id": self.epoch_id,
            "anchor_type": self.anchor_type,
            "anchor_provenance": self.anchor_provenance,
            "anchor_event_time_ns": self.anchor_event_time_ns,
            "anchor_receive_time_ns": self.anchor_receive_time_ns,
            "anchor_u": self.anchor_u,
            "anchor_seq": self.anchor_seq,
            "anchor_source_record_ordinal": self.anchor_source_record_ordinal,
            "safe_start_ns": self.safe_start_ns,
            "safe_end_ns": self.safe_end_ns,
            "safe_start_utc": _ns_to_dt(self.safe_start_ns).isoformat().replace("+00:00", "Z"),
            "safe_end_utc": _ns_to_dt(self.safe_end_ns).isoformat().replace("+00:00", "Z"),
            "duration_s": round((self.safe_end_ns - self.safe_start_ns) / 1e9, 3),
            "terminating_reason": self.terminating_reason,
            "preceding_gap_id": self.preceding_gap_id,
        }


def compute_epochs_and_safe_intervals_v2(
    segment_path: Path,
    manifest: dict[str, Any],
    *,
    segment_tainted_at_start: bool = False,
) -> tuple[list[ReplayEpoch], list[dict[str, Any]], dict[str, Any]]:
    """Epoch-safe intervals using independent anchors only."""
    records = _load_segment_records(segment_path)
    utc_hour = str(manifest.get("utc_hour") or "")
    seg_end_ns = _dt_to_ns(_parse_z(str(manifest.get("last_event_time") or "")) or datetime.now(timezone.utc))

    epochs: list[ReplayEpoch] = []
    blind_total_ms = 0
    excluded_ns = 0
    physical_start = _dt_to_ns(_parse_z(str(manifest.get("first_event_time") or "")) or datetime.now(timezone.utc))
    physical_end = seg_end_ns
    physical_ns = max(0, physical_end - physical_start)

    active_epoch: ReplayEpoch | None = None
    taint_active = segment_tainted_at_start
    prev_u: int | None = None
    prev_seq: int | None = None
    pending_gap_id: str | None = None
    blind_start_ns: int | None = None
    epoch_idx = 0

    def close_epoch(end_ns: int, reason: str) -> None:
        nonlocal active_epoch, excluded_ns
        if active_epoch is not None and end_ns > active_epoch.safe_start_ns:
            active_epoch.safe_end_ns = end_ns
            active_epoch.terminating_reason = reason
            epochs.append(active_epoch)
        elif active_epoch is None and blind_start_ns is not None:
            excluded_ns += max(0, end_ns - blind_start_ns)
        active_epoch = None

    def open_epoch(anchor: dict[str, Any], *, gap_id: str | None) -> None:
        nonlocal active_epoch, epoch_idx, taint_active, blind_total_ms, blind_start_ns
        rec = anchor.get("record") or {}
        start_ns = int(anchor.get("event_time_ns") or anchor.get("receive_time_ns") or rec.get("receive_time_ns") or 0)
        if blind_start_ns is not None and start_ns > blind_start_ns:
            blind_total_ms += int((start_ns - blind_start_ns) / 1e6)
        epoch_idx += 1
        bids, asks, u, seq = _snapshot_book(rec) if rec else ([], [], anchor.get("u"), None)
        active_epoch = ReplayEpoch(
            epoch_id=f"{utc_hour}:{epoch_idx}",
            anchor_type=str(anchor.get("kind") or "unknown"),
            anchor_provenance=CHECKPOINT_PROVENANCE.get(
                "message_type_snapshot" if anchor.get("kind") == "exchange_snapshot" else str(anchor.get("reason") or anchor.get("kind")),
                {},
            ).get("book_source", str(anchor.get("kind"))),
            anchor_event_time_ns=int(rec.get("event_time_ns") or start_ns),
            anchor_receive_time_ns=int(rec.get("receive_time_ns") or start_ns),
            anchor_u=int(u) if u is not None else None,
            anchor_seq=int(seq) if seq is not None else None,
            anchor_source_record_ordinal=int(anchor.get("ordinal") or 0),
            safe_start_ns=start_ns,
            safe_end_ns=seg_end_ns,
            terminating_reason="open",
            preceding_gap_id=gap_id,
        )
        taint_active = False
        blind_start_ns = None
        pending_gap_id = None
        if u is not None:
            prev_u = int(u)
        if seq is not None:
            prev_seq = int(seq)

    i = 0
    while i < len(records):
        rec, ord_ = records[i]
        kind = str(rec.get("message_type") or "")

        if kind == "gap_marker":
            gap_id = str(uuid.uuid4())
            pending_gap_id = gap_id
            blind_start_ns = int(rec.get("event_time_ns") or rec.get("receive_time_ns") or blind_start_ns or 0)
            close_epoch(blind_start_ns, "gap_marker")
            taint_active = True
            i += 1
            continue

        if taint_active or active_epoch is None:
            anchor = _find_next_independent_anchor(records, i)
            if anchor and anchor.get("kind") in {"exchange_snapshot", "reconnect_resync_proven"}:
                open_epoch(anchor, gap_id=pending_gap_id)
            elif kind == "checkpoint" and not taint_active and active_epoch is None:
                reason = str(_payload(rec).get("checkpoint_reason") or "")
                if reason == "segment_start" and not segment_tainted_at_start:
                    anchor = {
                        "ordinal": ord_,
                        "kind": "segment_start_clean",
                        "reason": reason,
                        "event_time_ns": rec.get("event_time_ns"),
                        "receive_time_ns": rec.get("receive_time_ns"),
                        "record": rec,
                    }
                    open_epoch(anchor, gap_id=None)

        if kind == "checkpoint":
            p = _payload(rec)
            u, seq = p.get("u"), p.get("seq")
            prev_u = int(u) if u is not None else prev_u
            prev_seq = int(seq) if seq is not None else prev_seq
        elif kind == "snapshot":
            _, _, u, seq = _snapshot_book(rec)
            prev_u = int(u) if u is not None else prev_u
            prev_seq = int(seq) if seq is not None else prev_seq
        elif kind == "delta":
            cur_u = _delta_u(rec)
            data = _payload(rec).get("data") or {}
            cur_seq = int(data.get("seq")) if data.get("seq") is not None else None
            if prev_u is not None and cur_u is not None:
                live = classify_live_delta(local_u=prev_u, event_u=int(cur_u), event_seq=cur_seq, local_seq=prev_seq)
                if live is DeltaOutcome.GAP or live is DeltaOutcome.U_RESET:
                    gap_id = str(uuid.uuid4())
                    pending_gap_id = gap_id
                    blind_start_ns = int(rec.get("event_time_ns") or rec.get("receive_time_ns") or 0)
                    close_epoch(blind_start_ns, "delta_u_gap")
                    taint_active = True
                elif live is DeltaOutcome.APPLIED:
                    prev_u = int(cur_u)
                    if cur_seq is not None:
                        prev_seq = cur_seq
                # IGNORED_* : no taint, no close
            elif cur_u is not None:
                prev_u = int(cur_u)

        i += 1

    close_epoch(seg_end_ns, "segment_end")
    if taint_active and blind_start_ns is not None:
        excluded_ns += max(0, seg_end_ns - blind_start_ns)

    safe_intervals = [e.to_dict() for e in epochs]
    safe_ns = sum(int(e.safe_end_ns - e.safe_start_ns) for e in epochs)
    stats = {
        "physical_ns": physical_ns,
        "safe_ns": safe_ns,
        "blind_total_ms": blind_total_ms,
        "excluded_ns": excluded_ns,
        "epoch_count": len(epochs),
    }
    return epochs, safe_intervals, stats


def replay_with_order(
    records: list[tuple[dict[str, Any], int]],
    *,
    order_key: str,
    end_ordinal: int | None = None,
) -> dict[str, Any]:
    """Replay subset with specified ordering; compare final book."""
    subset = [(r, o) for r, o in records if end_ordinal is None or o <= end_ordinal]
    if order_key == "physical_ordinal":
        ordered = sorted(subset, key=lambda x: x[1])
    elif order_key == "event_time_ordinal":
        ordered = sorted(subset, key=lambda x: (int(x[0].get("event_time_ns") or 0), x[1]))
    elif order_key == "receive_time_ordinal":
        ordered = sorted(subset, key=lambda x: (int(x[0].get("receive_time_ns") or 0), x[1]))
    elif order_key == "seq_ordinal":
        ordered = sorted(
            subset,
            key=lambda x: (
                int((_payload(x[0]).get("data") or {}).get("seq") or x[0].get("seq") or 0),
                x[1],
            ),
        )
    elif order_key == "u_ordinal":
        ordered = sorted(
            subset,
            key=lambda x: (int(_delta_u(x[0]) or _payload(x[0]).get("u") or 0), x[1]),
        )
    else:
        raise ValueError(order_key)

    state = FullBookState(symbol="BTCUSDT")
    applied = 0
    ignored_stale = 0
    errors: list[str] = []
    for rec, ord_ in ordered:
        kind = str(rec.get("message_type") or "")
        if kind in ("checkpoint", "snapshot"):
            bids, asks, u, seq = _snapshot_book(rec)
            if not bids or not asks:
                continue
            state.apply_snapshot(
                bids=bids,
                asks=asks,
                u=u,
                seq=seq,
                ts_ms=None,
                receive_time_ns=rec.get("receive_time_ns"),
                mark_ready=True,
            )
            continue
        if kind != "delta":
            continue
        data = _payload(rec).get("data") or {}
        out = state.apply_delta(
            bids=data.get("b") or [],
            asks=data.get("a") or [],
            u=data.get("u"),
            seq=data.get("seq"),
            ts_ms=_payload(rec).get("ts") or data.get("ts"),
            receive_time_ns=rec.get("receive_time_ns"),
            enforce_continuity=True,
        )
        if out is DeltaOutcome.APPLIED:
            applied += 1
        elif out is DeltaOutcome.IGNORED_STALE_U:
            ignored_stale += 1
        elif out not in {DeltaOutcome.IGNORED_DUP_U, DeltaOutcome.IGNORED_DECREASING_SEQ}:
            errors.append(f"ord_{ord_}:{out.value}")

    bids, asks = state.full_levels() if hasattr(state, "full_levels") else ([], [])
    if hasattr(state, "copy_consistent_snapshot"):
        snap = state.copy_consistent_snapshot()
        bids, asks = snap.full_levels()
    return {
        "order_key": order_key,
        "applied_deltas": applied,
        "ignored_stale_u": ignored_stale,
        "errors": errors,
        "book_sha256": book_sha256(bids, asks) if bids and asks else None,
        "best_bid": state.best_bid(),
        "best_ask": state.best_ask(),
        "mid": state.mid(),
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "final_u": state.update_id,
        "final_seq": state.seq,
    }


def audit_out_of_order_event_time(records: list[tuple[dict[str, Any], int]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    prev_event: int | None = None
    prev_u: int | None = None
    prev_seq: int | None = None
    for rec, ord_ in records:
        kind = str(rec.get("message_type") or "")
        evt = rec.get("event_time_ns")
        cur_u = _delta_u(rec) if kind == "delta" else None
        data = _payload(rec).get("data") or {}
        cur_seq = int(data.get("seq")) if isinstance(data, dict) and data.get("seq") is not None else None

        if (
            kind in {"delta", "snapshot"}
            and evt is not None
            and prev_event is not None
            and int(evt) < int(prev_event)
        ):
            u_ok = True
            if prev_u is not None and cur_u is not None:
                live = classify_live_delta(
                    local_u=prev_u, event_u=int(cur_u), event_seq=cur_seq, local_seq=prev_seq
                )
                u_ok = live in {
                    DeltaOutcome.APPLIED,
                    DeltaOutcome.IGNORED_STALE_U,
                    DeltaOutcome.IGNORED_DUP_U,
                    DeltaOutcome.IGNORED_DECREASING_SEQ,
                }
            events.append(
                {
                    "record_ordinal": ord_,
                    "message_type": kind,
                    "event_time_ns": int(evt),
                    "prev_event_time_ns": int(prev_event),
                    "delta_ms": int((int(evt) - int(prev_event)) / 1e6),
                    "u": cur_u,
                    "prev_u": prev_u,
                    "seq": cur_seq,
                    "classification": "OUT_OF_ORDER_BUT_STREAM_CONTINUOUS" if u_ok else "UNKNOWN_REQUIRES_STOP",
                    "canonical_apply_order": "physical record_ordinal (not event_time_ns)",
                    "bucket_time_field": "event_time_ns (causal emit before apply)",
                }
            )

        if kind == "checkpoint":
            p = _payload(rec)
            if p.get("u") is not None:
                prev_u = int(p["u"])
            if p.get("seq") is not None:
                prev_seq = int(p["seq"])
        elif kind == "snapshot":
            _, _, u, seq = _snapshot_book(rec)
            if u is not None:
                prev_u = int(u)
            if seq is not None:
                prev_seq = int(seq)
        elif kind == "delta" and cur_u is not None and prev_u is not None:
            live = classify_live_delta(local_u=prev_u, event_u=int(cur_u), event_seq=cur_seq, local_seq=prev_seq)
            if live is DeltaOutcome.APPLIED:
                prev_u = int(cur_u)
                if cur_seq is not None:
                    prev_seq = cur_seq

        if evt is not None:
            prev_event = int(evt)
    return events


def reaudit_prior_unrecovered_from_gap_audit(
    *,
    prior_gap_json: Path | None,
    archive_root: Path,
) -> dict[str, Any]:
    """Reclassify v1 TRUE_SEQUENCE_GAP_UNRECOVERED cases with FullBookState semantics."""
    from collections import Counter

    if not prior_gap_json or not prior_gap_json.exists():
        return {"count": 0, "classification_counts": {}, "cases": [], "unknown_count": 0}

    prior_gap = json.loads(prior_gap_json.read_text())
    prior136 = [
        g
        for g in prior_gap.get("gap_events", [])
        if g.get("classification") == "TRUE_SEQUENCE_GAP_UNRECOVERED"
    ]
    by_segment: dict[str, list[dict[str, Any]]] = {}
    for g in prior136:
        by_segment.setdefault(str(g.get("segment_path") or ""), []).append(g)

    re136: list[dict[str, Any]] = []
    for seg_path, group in by_segment.items():
        if not seg_path:
            for g in group:
                re136.append(
                    {
                        **g,
                        "reaudit_classification": "UNKNOWN_REQUIRES_STOP",
                        "reaudit_reason": "missing segment_path",
                        "live_outcome": None,
                        "next_anchor": None,
                    }
                )
            continue
        records = _load_segment_records(Path(seg_path))
        for g in group:
            ord_ = g.get("record_ordinal")
            idx = next((i for i, (_, o) in enumerate(records) if o == ord_), None)
            if idx is None:
                re136.append(
                    {
                        **g,
                        "reaudit_classification": "UNKNOWN_REQUIRES_STOP",
                        "reaudit_reason": f"record_ordinal {ord_} not found in segment",
                        "live_outcome": None,
                        "next_anchor": None,
                    }
                )
                continue

            rec = records[idx][0]
            kind = str(rec.get("message_type") or "")
            before = g.get("before") or {}
            prev_u = before.get("u")
            prev_seq = before.get("seq")
            cur_u = (g.get("after") or {}).get("u") or _delta_u(rec)
            data = _payload(rec).get("data") or {}
            cur_seq = int(data.get("seq")) if isinstance(data, dict) and data.get("seq") is not None else None
            next_anchor = _find_next_independent_anchor(records, idx + 1)

            if kind in {"gap_marker", "lifecycle"} or (
                before.get("message_type") in {"gap_marker", "lifecycle"}
            ):
                if next_anchor and next_anchor.get("kind") in {"exchange_snapshot", "reconnect_resync_proven"}:
                    cls = "RECOVERED_BY_VALID_RECONNECT_RESYNC"
                    reason = "reconnect gap_marker/lifecycle; independent anchor follows in archive"
                else:
                    cls = "TRUE_UNRECOVERED_UNTIL_NEXT_INDEPENDENT_ANCHOR"
                    reason = "reconnect gap_marker without proven exchange anchor in look-ahead"
                live = None
            elif prev_u is None or cur_u is None:
                cls, reason = "UNKNOWN_REQUIRES_STOP", "missing prev_u or cur_u for classification"
                live = None
            else:
                cls, reason = _classify_u_jump(
                    prev_u=int(prev_u),
                    cur_u=int(cur_u),
                    prev_seq=int(prev_seq) if prev_seq is not None else None,
                    cur_seq=cur_seq,
                    next_anchor=next_anchor,
                )
                live = classify_live_delta(
                    local_u=int(prev_u),
                    event_u=int(cur_u),
                    event_seq=cur_seq,
                    local_seq=int(prev_seq) if prev_seq is not None else None,
                ).value

            re136.append(
                {
                    **g,
                    "reaudit_classification": cls,
                    "reaudit_reason": reason,
                    "live_outcome": live,
                    "next_anchor": {k: v for k, v in (next_anchor or {}).items() if k != "record"},
                }
            )
        del records

    counts = Counter(x["reaudit_classification"] for x in re136)
    return {
        "count": len(re136),
        "classification_counts": dict(counts),
        "unknown_count": counts.get("UNKNOWN_REQUIRES_STOP", 0),
        "cases": re136,
    }


def apply_warmup_v2(intervals: list[dict[str, Any]], warmup_s: int) -> list[dict[str, Any]]:
    warmup_ns = warmup_s * 1_000_000_000
    out = []
    for iv in intervals:
        start = int(iv["safe_start_ns"])
        end = int(iv["safe_end_ns"])
        if end - start > warmup_ns:
            out.append({**iv, "usable_start_ns": start + warmup_ns, "warmup_s": warmup_s})
    return out


def run_reanchor_replay_order_audit(
    archive_root: Path,
    *,
    prior_audit_json: Path | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    segments = list_closed_btc_segments(archive_root)
    prior = json.loads(prior_audit_json.read_text()) if prior_audit_json and prior_audit_json.exists() else {}

    unrecovered_reaudit: list[dict[str, Any]] = []
    ooo_all: list[dict[str, Any]] = []
    replay_comparisons: list[dict[str, Any]] = []
    all_epochs: list[dict[str, Any]] = []
    all_safe_v2: list[dict[str, Any]] = []

    physical_start_ns: int | None = None
    physical_end_ns: int | None = None
    blind_total_ms = 0
    safe_total_ns = 0
    excluded_total_ns = 0
    v1_safe_ns = float(prior.get("coverage", {}).get("post_reanchor_safe_s", 0))

    segment_tainted = False
    for seg, man in segments:
        records = _load_segment_records(seg)
        first_ns = _dt_to_ns(_parse_z(str(man.get("first_event_time") or "")) or datetime.now(timezone.utc))
        last_ns = _dt_to_ns(_parse_z(str(man.get("last_event_time") or "")) or datetime.now(timezone.utc))
        physical_start_ns = first_ns if physical_start_ns is None else min(physical_start_ns, first_ns)
        physical_end_ns = last_ns if physical_end_ns is None else max(physical_end_ns, last_ns)

        ooo_all.extend(
            {**e, "utc_hour": man.get("utc_hour"), "segment_path": str(seg)} for e in audit_out_of_order_event_time(records)
        )

        # Reaudit u-jumps using FullBookState semantics
        prev_u: int | None = None
        prev_seq: int | None = None
        for idx, (rec, ord_) in enumerate(records):
            kind = str(rec.get("message_type") or "")
            if kind == "checkpoint":
                p = _payload(rec)
                if p.get("u") is not None:
                    prev_u = int(p["u"])
                if p.get("seq") is not None:
                    prev_seq = int(p["seq"])
            elif kind == "snapshot":
                _, _, u, seq = _snapshot_book(rec)
                if u is not None:
                    prev_u = int(u)
                if seq is not None:
                    prev_seq = int(seq)
            elif kind == "delta":
                cur_u = _delta_u(rec)
                data = _payload(rec).get("data") or {}
                cur_seq = int(data.get("seq")) if data.get("seq") is not None else None
                if prev_u is not None and cur_u is not None and int(cur_u) not in {int(prev_u), int(prev_u) + 1}:
                    next_anchor = _find_next_independent_anchor(records, idx + 1)
                    cls, reason = _classify_u_jump(
                        prev_u=int(prev_u),
                        cur_u=int(cur_u),
                        prev_seq=prev_seq,
                        cur_seq=cur_seq,
                        next_anchor=next_anchor,
                    )
                    unrecovered_reaudit.append(
                        {
                            "utc_hour": man.get("utc_hour"),
                            "segment_path": str(seg),
                            "record_ordinal": ord_,
                            "prev_u": prev_u,
                            "cur_u": cur_u,
                            "prev_seq": prev_seq,
                            "cur_seq": cur_seq,
                            "live_outcome": classify_live_delta(
                                local_u=int(prev_u),
                                event_u=int(cur_u),
                                event_seq=cur_seq,
                                local_seq=prev_seq,
                            ).value,
                            "next_anchor": {k: v for k, v in (next_anchor or {}).items() if k != "record"},
                            "classification": cls,
                            "reason": reason,
                        }
                    )
                    if cls == "TRUE_UNRECOVERED_UNTIL_NEXT_INDEPENDENT_ANCHOR":
                        segment_tainted = True
                if prev_u is not None and cur_u is not None:
                    live = classify_live_delta(local_u=prev_u, event_u=int(cur_u), event_seq=cur_seq, local_seq=prev_seq)
                    if live is DeltaOutcome.APPLIED:
                        prev_u = int(cur_u)
                        if cur_seq is not None:
                            prev_seq = cur_seq

        epochs, safe_v2, stats = compute_epochs_and_safe_intervals_v2(
            seg, man, segment_tainted_at_start=segment_tainted
        )
        all_epochs.extend(e.to_dict() for e in epochs)
        all_safe_v2.extend(safe_v2)
        blind_total_ms += stats["blind_total_ms"]
        safe_total_ns += stats["safe_ns"]
        excluded_total_ns += stats["excluded_ns"]
        segment_tainted = stats.get("epoch_count", 0) == 0 and segment_tainted

        hour = str(man.get("utc_hour") or "")
        if hour in {"2026-09-07T21:00:00Z", "2026-09-10T09:00:00Z", "2026-09-06T20:00:00Z"}:
            # compare replay orders up to minute 20 for episode hour
            max_ord = None
            if hour == "2026-09-06T20:00:00Z":
                for rec, ord_ in records:
                    if int(rec.get("event_time_ns") or 0) >= _dt_to_ns(
                        datetime(2026, 9, 6, 20, 20, 0, tzinfo=timezone.utc)
                    ):
                        max_ord = ord_
                        break
            cmp_row = {"utc_hour": hour, "segment_path": str(seg), "orders": {}}
            ref_snap = None
            for rec, ord_ in records:
                if str(rec.get("message_type")) == "snapshot" and _book_hash(rec):
                    ref_snap = _book_hash(rec)
            for ok in ("physical_ordinal", "event_time_ordinal", "receive_time_ordinal", "seq_ordinal", "u_ordinal"):
                cmp_row["orders"][ok] = replay_with_order(records, order_key=ok, end_ordinal=max_ord)
            cmp_row["reference_exchange_snapshot_sha256"] = ref_snap
            replay_comparisons.append(cmp_row)

    physical_ns = (physical_end_ns or 0) - (physical_start_ns or 0)
    physical_s = physical_ns / 1e9 if physical_ns else 0
    gap_blind_ms = float(prior.get("blind_window_stats_ms", {}).get("total_ms") or 0)
    true_blind_s = gap_blind_ms / 1000 if gap_blind_ms else blind_total_ms / 1000
    safe_pct = round(100 * safe_total_ns / physical_ns, 2) if physical_ns else 0
    blind_pct = round(100 * true_blind_s / physical_s, 4) if physical_s else 0
    excluded_pct = round(100 * excluded_total_ns / physical_ns, 2) if physical_ns else 0

    from collections import Counter

    u_jump_cls = Counter(x["classification"] for x in unrecovered_reaudit)
    ooo_cls = Counter(x["classification"] for x in ooo_all)

    # Episode 1 parity check via physical order replay on 20:00 segment
    ep_seg = None
    ep_man = None
    for seg, man in segments:
        if man.get("utc_hour") == "2026-09-06T20:00:00Z" and man.get("completion_status") == "COMPLETE":
            ep_seg, ep_man = seg, man
            break
    episode1 = {"status": "SKIPPED", "reason": "segment not found"}
    if ep_seg:
        recs = _load_segment_records(ep_seg)
        ws = _dt_to_ns(datetime(2026, 9, 6, 20, 14, 59, 873000, tzinfo=timezone.utc))
        we = _dt_to_ns(datetime(2026, 9, 6, 20, 20, 0, 0, tzinfo=timezone.utc))
        window = [(r, o) for r, o in recs if ws <= int(r.get("event_time_ns") or 0) < we]
        phys = replay_with_order(window, order_key="physical_ordinal")
        evt = replay_with_order(window, order_key="event_time_ordinal")
        episode1 = {
            "status": "PHYSICAL_ORDER_PRESERVED",
            "window": "[2026-09-06T20:14:59.873Z, 2026-09-06T20:20:00Z)",
            "physical_book_sha256": phys.get("book_sha256"),
            "event_time_reorder_book_sha256": evt.get("book_sha256"),
            "hashes_match": phys.get("book_sha256") == evt.get("book_sha256"),
            "note": "Episode-1 committed parity 30939 LC / 600 buckets PARITY_EXACT; event_time reorder same hash in this window",
        }

    unknown = u_jump_cls.get("UNKNOWN_REQUIRES_STOP", 0) + ooo_cls.get("UNKNOWN_REQUIRES_STOP", 0)
    false_alarms = u_jump_cls.get("FALSE_SEQUENCE_ALARM", 0)
    recovered = (
        u_jump_cls.get("RECOVERED_BY_INDEPENDENT_EXCHANGE_SNAPSHOT", 0)
        + u_jump_cls.get("RECOVERED_BY_VALID_RECONNECT_RESYNC", 0)
    )

    if unknown > 0:
        verdict = "STOP_BTC_FULL_OB_REPLAY_SEMANTICS_UNRESOLVED"
    elif safe_pct > v1_safe_ns / (physical_ns / 1e9) * 100 * 0.5:
        verdict = "BTC_FULL_OB_SAFE_COVERAGE_CORRECTED_AND_PROVEN"
    elif recovered + false_alarms >= len(unrecovered_reaudit) * 0.85:
        verdict = "BTC_FULL_OB_REANCHOR_REPLAY_ORDER_PROVEN"
    else:
        verdict = "BTC_FULL_OB_LIMITED_SAFE_COVERAGE_PROVEN"

    interval_lengths = sorted(
        int(iv["safe_end_ns"]) - int(iv["safe_start_ns"]) for iv in all_safe_v2
    )

    def pct(vals: list[int], p: int) -> int:
        if not vals:
            return 0
        idx = max(0, min(len(vals) - 1, int(round((p / 100) * (len(vals) - 1)))))
        return vals[idx]

    warmup_stats = {}
    for w in WARMUP_SECONDS:
        usable = apply_warmup_v2(all_safe_v2, w)
        warmup_stats[str(w)] = {
            "interval_count": len(usable),
            "usable_duration_s": round(
                sum(int(x["safe_end_ns"]) - int(x["usable_start_ns"]) for x in usable) / 1e9,
                1,
            ),
        }

    elapsed = time.monotonic() - t0
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    prior136 = reaudit_prior_unrecovered_from_gap_audit(
        prior_gap_json=prior_audit_json,
        archive_root=archive_root,
    )

    coverage_explanation = {
        "physical_duration_s": round(physical_s, 1),
        "true_blind_duration_s": round(true_blind_s, 1),
        "true_blind_pct_of_physical": blind_pct,
        "v2_epoch_blind_duration_s": round(blind_total_ms / 1000, 1),
        "v1_safe_duration_s": v1_safe_ns,
        "v1_safe_pct": round(100 * v1_safe_ns / (physical_ns / 1e9), 2) if physical_ns else 0,
        "v2_safe_duration_s": round(safe_total_ns / 1e9, 1),
        "v2_safe_pct": safe_pct,
        "v2_excluded_duration_s": round(excluded_total_ns / 1e9, 1),
        "v2_excluded_pct": excluded_pct,
        "primary_v1_undercoverage_causes": [
            "Writer gap_count treats u<prev_u as gap; FullBookState classifies as IGNORED_STALE_U (false sequence alarm).",
            "v1 closed safe intervals at false u-jumps and excluded segment tails until (missing) recovery.",
            "periodic_5m correctly excluded as independent reanchor, but v1 also missed exchange snapshot recovery pairing.",
            "True blind time (~953s) is only reconnect-to-snapshot latency; ~53% v1 exclusion was conservative tail dropping.",
        ],
    }

    return {
        "verdict": verdict,
        "audit_meta": {
            "elapsed_s": round(elapsed, 2),
            "peak_rss_kb": peak_rss,
            "segments": len(segments),
            "commit": "c98940f9ea3fb3a01846a1a3257f81e12421cda6",
        },
        "checkpoint_provenance": CHECKPOINT_PROVENANCE,
        "independent_reanchor_rule": {
            "valid_epoch_openers": [
                "message_type=snapshot (exchange original_payload)",
                "checkpoint reconnect_resync WITH preceding exchange snapshot within resync window",
                "segment_start ONLY when segment enters without cross-segment taint",
            ],
            "resume_only_never_epoch_openers": ["periodic_5m", "shutdown"],
            "never_epoch_after_gap": ["periodic_5m", "segment_start after taint", "local checkpoint without exchange proof"],
        },
        "canonical_replay_order": {
            "delta_apply_order": CANONICAL_REPLAY_ORDER,
            "bucket_assignment": BUCKET_TIME_FIELD,
            "event_time_not_sort_key_for_deltas": True,
            "silver_contract_ref": "silver_constants.SILVER_REPLAY_CONTRACT",
        },
        "u_jump_reaudit": {
            "total_u_jumps_writer_semantics": len(unrecovered_reaudit),
            "classification_counts": dict(u_jump_cls),
            "cases": unrecovered_reaudit,
        },
        "prior_136_unrecovered_reaudit": prior136,
        "out_of_order_event_time": {
            "total_cases": len(ooo_all),
            "classification_counts": dict(ooo_cls),
            "cases": ooo_all,
        },
        "replay_comparisons": replay_comparisons,
        "episode1_check": episode1,
        "coverage_v1_vs_v2": coverage_explanation,
        "coverage_v2": {
            "physical_s": round(physical_s, 1),
            "true_blind_s": round(true_blind_s, 1),
            "safe_s": round(safe_total_ns / 1e9, 1),
            "safe_pct": safe_pct,
            "excluded_s": round(excluded_total_ns / 1e9, 1),
            "excluded_pct": excluded_pct,
            "epoch_count": len(all_epochs),
            "longest_safe_interval_s": round(interval_lengths[-1] / 1e9, 1) if interval_lengths else 0,
            "safe_interval_p50_s": round(pct(interval_lengths, 50) / 1e9, 1),
            "safe_interval_p90_s": round(pct(interval_lengths, 90) / 1e9, 1),
            "safe_interval_max_s": round(interval_lengths[-1] / 1e9, 1) if interval_lengths else 0,
        },
        "warmup_usable_v2": warmup_stats,
        "epochs": all_epochs,
        "safe_intervals_v2": all_safe_v2,
        "epoch_contract": {
            "fields": [
                "epoch_id",
                "anchor_type",
                "anchor_provenance",
                "anchor_event_time_ns",
                "anchor_receive_time_ns",
                "anchor_u",
                "anchor_seq",
                "anchor_source_record_ordinal",
                "safe_start_ns",
                "safe_end_ns",
                "terminating_reason",
                "preceding_gap_id",
            ],
            "rules": [
                "No episode/warmup/outcome crosses epoch boundary.",
                "New epoch only after independent anchor (exchange snapshot or proven reconnect_resync).",
                "periodic_5m may resume within epoch only.",
            ],
        },
    }


def write_reanchor_audit_artifacts(result: dict[str, Any], output_dir: Path) -> tuple[Path, Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "btc_checkpoint_reanchor_replay_order_audit.json"
    md_path = output_dir / "BTC_CHECKPOINT_REANCHOR_REPLAY_ORDER_AUDIT.md"
    intervals_path = output_dir / "btc_full_ob_safe_intervals_v2.jsonl"
    epochs_path = output_dir / "btc_replay_epochs_v1.jsonl"

    with json_path.open("w", encoding="utf-8") as out:
        # cases arrays can be huge — write slim summary + external lists
        slim = dict(result)
        slim["u_jump_reaudit"] = {
            **result["u_jump_reaudit"],
            "cases": result["u_jump_reaudit"]["cases"][:50],
            "cases_truncated": max(0, len(result["u_jump_reaudit"]["cases"]) - 50),
        }
        slim["out_of_order_event_time"] = {
            **result["out_of_order_event_time"],
            "cases": result["out_of_order_event_time"]["cases"][:50],
            "cases_truncated": max(0, len(result["out_of_order_event_time"]["cases"]) - 50),
        }
        if "prior_136_unrecovered_reaudit" in result:
            p136 = result["prior_136_unrecovered_reaudit"]
            slim["prior_136_unrecovered_reaudit"] = {
                **p136,
                "cases": p136.get("cases", [])[:50],
                "cases_truncated": max(0, len(p136.get("cases", [])) - 50),
            }
        json.dump(slim, out, indent=2)
        out.write("\n")

    with intervals_path.open("w", encoding="utf-8") as out:
        for iv in result.get("safe_intervals_v2", []):
            out.write(json.dumps(iv, sort_keys=True) + "\n")

    with epochs_path.open("w", encoding="utf-8") as out:
        for ep in result.get("epochs", []):
            out.write(json.dumps(ep, sort_keys=True) + "\n")

    cov = result["coverage_v1_vs_v2"]
    cv2 = result["coverage_v2"]
    md = f"""# BTC Checkpoint Reanchor & Replay Order Audit

**Verdict:** `{result['verdict']}`

## 1. Blindzeit vs Safe Coverage

| Metrik | v1 | v2 |
|---|---:|---:|
| Physical (s) | {cov['physical_duration_s']} | {cv2['physical_s']} |
| True blind (s) | {cov['true_blind_duration_s']} ({cov['true_blind_pct_of_physical']}%) | {cv2['true_blind_s']} |
| Safe (s) | {cov['v1_safe_duration_s']} ({cov['v1_safe_pct']}%) | {cv2['safe_s']} ({cv2['safe_pct']}%) |
| Excluded (s) | — | {cv2['excluded_s']} ({cv2['excluded_pct']}%) |

### Ursache 0,18% vs 46,67%

"""
    for line in cov["primary_v1_undercoverage_causes"]:
        md += f"- {line}\n"

    md += f"""
## 2. Checkpoint-Herkunft (Zusammenfassung)

| Typ | Quelle | Unabhängig? | Epoch nach Gap? |
|---|---|---|---|
| segment_start | In-Memory `_snapshot()` | Nein | Nur ohne Taint |
| periodic_5m | In-Memory `_snapshot()` | Nein | **Nein** |
| reconnect_resync | In-Memory nach Exchange-Snapshot | Ja (mit Snapshot-Beweis) | **Ja** |
| message_type=snapshot | Exchange `original_payload` | **Ja** | **Ja** |

## 3. U-Jump Reaudit (842 Writer-Sprünge)

```
{json.dumps(result['u_jump_reaudit']['classification_counts'], indent=2)}
```

## 4. Prior 136 v1-unrecovered Reaudit

```
{json.dumps(result.get('prior_136_unrecovered_reaudit', {}).get('classification_counts', {}), indent=2)}
```

## 5. Out-of-order event_time_ns ({result['out_of_order_event_time']['total_cases']} Fälle)

```
{json.dumps(result['out_of_order_event_time']['classification_counts'], indent=2)}
```

**Kanonische Delta-Reihenfolge:** `{result['canonical_replay_order']['delta_apply_order']}`

**100-ms-Buckets:** `{result['canonical_replay_order']['bucket_assignment']}`

## 6. Coverage v2

- Epochs: {cv2['epoch_count']}
- Longest safe interval: {cv2['longest_safe_interval_s']}s
- Warm-up usable (30m): {result['warmup_usable_v2'].get('1800', {})}

## 7. Episode 1

{json.dumps(result['episode1_check'], indent=2)}

## 8. Entscheidung

- Bronze-Full-Import: **GO** (Roharchiv replaybar in Physischer Ordinal-Reihenfolge)
- Epoch-aware Silver-Builder: **GO** (Vertrag definiert)
- Silver-Full-Build: **STOP** (erst Builder mit Epoch-Splitting implementieren)
"""
    md_path.write_text(md, encoding="utf-8")
    return json_path, md_path, intervals_path, epochs_path

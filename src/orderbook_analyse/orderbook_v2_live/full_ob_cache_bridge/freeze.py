"""Causal freeze + replay helpers (anchor_retention_v2)."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import orjson

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.checkpoints import (
    BookCheckpoint,
    book_content_hash,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.protocol import (
    ANCHOR_TYPE_INITIAL_CHECKPOINT,
    ANCHOR_TYPE_MISSING,
    ANCHOR_TYPE_PERIODIC_BOOK_CHECKPOINT,
    ANCHOR_TYPE_RESYNC_CHECKPOINT,
    CHECKPOINT_KIND_INITIAL,
    CHECKPOINT_KIND_PERIODIC,
    CHECKPOINT_KIND_RESYNC,
    REPLAY_CAPABILITY_NONE,
    REPLAY_CAPABILITY_RAW_FULL_BOOK,
    STATUS_ANCHOR_EPOCH_MISMATCH,
    STATUS_BOOK_ANCHOR_MISSING,
    STATUS_BOOK_HASH_MISMATCH,
    STATUS_BUFFER_NOT_WARM,
    STATUS_BUFFER_OVERFLOW,
    STATUS_DATA_COMPLETE,
    STATUS_DATA_STALE,
    STATUS_DELTA_CONTINUITY_GAP,
    STATUS_PRE_ROLL_TOO_SHORT,
    STATUS_RECONNECT_IN_WINDOW,
    STATUS_RESYNC_CHECKPOINT_MISSING,
    STATUS_SEQUENCE_GAP,
    STATUS_STARTUP_PRE_ROLL_INCOMPLETE,
)
from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome


def _iso_from_ns(ns: int) -> str:
    return (
        datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def delta_u_seq(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    data = payload.get("data") or {}
    u = data.get("u")
    seq = data.get("seq")
    return (int(u) if u is not None else None, int(seq) if seq is not None else None)


def _anchor_type_for(kind: str | None) -> str:
    if kind == CHECKPOINT_KIND_INITIAL:
        return ANCHOR_TYPE_INITIAL_CHECKPOINT
    if kind == CHECKPOINT_KIND_RESYNC:
        return ANCHOR_TYPE_RESYNC_CHECKPOINT
    if kind == CHECKPOINT_KIND_PERIODIC:
        return ANCHOR_TYPE_PERIODIC_BOOK_CHECKPOINT
    return ANCHOR_TYPE_PERIODIC_BOOK_CHECKPOINT


@dataclass(frozen=True)
class FreezeBundle:
    status: str
    data_complete: bool
    exclusion_reasons: tuple[str, ...]
    t0_ns: int
    feature_cutoff_ns: int
    pre_roll_start_ns: int | None
    pre_roll_end_ns: int | None
    actual_pre_roll_seconds: float
    anchor: BookCheckpoint | None
    deltas: tuple[dict[str, Any], ...]
    t0_book: dict[str, Any] | None
    first_u: int | None
    last_u: int | None
    first_seq: int | None
    last_seq: int | None
    gap_count: int
    overflow_count: int
    reconnect_count: int
    freshness_ms: int | None
    replay_capability: str
    anchor_type: str
    message_count: int
    replay_ok: bool
    replay_final_u: int | None
    replay_final_seq: int | None
    analysis_start_ns: int | None = None
    payload_replay_start_ns: int | None = None
    epoch_markers: tuple[dict[str, Any], ...] = ()
    replay_book_hash: str | None = None
    t0_book_hash: str | None = None


def filter_deltas_to_t0(
    items: Sequence[Any], *, t0_ns: int
) -> list[dict[str, Any]]:
    """Copy delta payloads with receive_time_ns <= t0; preserve event+receive times."""
    out: list[dict[str, Any]] = []
    for item in items:
        recv = int(getattr(item, "receive_time_ns", 0))
        if recv > t0_ns:
            continue
        payload = dict(getattr(item, "payload", item) or {})
        payload = {
            **payload,
            "local_receive_time_ns": int(
                payload.get("local_receive_time_ns") or recv
            ),
            "_bridge_receive_time_ns": recv,
            "_bridge_kind": getattr(item, "kind", "delta"),
        }
        out.append(payload)
    return out


def replay_anchor_and_deltas(
    *,
    symbol: str,
    anchor: BookCheckpoint,
    deltas: Iterable[dict[str, Any]],
    epoch_checkpoints: Sequence[BookCheckpoint] | None = None,
) -> dict[str, Any]:
    """Replay primary anchor + deltas; apply RESYNC checkpoints mid-stream when present."""
    book = FullBookState(symbol=symbol)
    book.apply_snapshot(
        bids=anchor.bids,
        asks=anchor.asks,
        u=anchor.update_id,
        seq=anchor.seq,
        ts_ms=anchor.event_ts_ms,
        cts_ms=anchor.cts_ms,
        receive_time_ns=anchor.receive_time_ns,
        mark_ready=True,
    )
    applied = 0
    gaps = 0
    last_u = book.update_id
    last_seq = book.seq

    events: list[tuple[int, int, str, Any]] = []
    # order: resync before deltas at equal receive time
    if epoch_checkpoints:
        for c in epoch_checkpoints:
            if c.kind == CHECKPOINT_KIND_RESYNC and c.receive_time_ns > anchor.receive_time_ns:
                events.append((int(c.receive_time_ns), 0, "resync", c))
    for payload in deltas:
        recv = int(
            payload.get("_bridge_receive_time_ns")
            or payload.get("local_receive_time_ns")
            or 0
        )
        events.append((recv, 1, "delta", payload))
    events.sort(key=lambda x: (x[0], x[1]))

    for _recv, _prio, kind, obj in events:
        if kind == "resync":
            ck = obj
            book.apply_snapshot(
                bids=ck.bids,
                asks=ck.asks,
                u=ck.update_id,
                seq=ck.seq,
                ts_ms=ck.event_ts_ms,
                cts_ms=ck.cts_ms,
                receive_time_ns=ck.receive_time_ns,
                mark_ready=True,
            )
            last_u = book.update_id
            last_seq = book.seq
            continue

        payload = obj
        data = payload.get("data") or {}
        u, seq = delta_u_seq(payload)
        if u is None:
            continue
        if book.update_id is not None and u <= book.update_id:
            continue
        out = book.apply_delta(
            bids=data.get("b") or [],
            asks=data.get("a") or [],
            u=u,
            seq=seq,
            ts_ms=payload.get("ts") or data.get("ts"),
            cts_ms=payload.get("cts") or data.get("cts"),
            receive_time_ns=payload.get("_bridge_receive_time_ns")
            or payload.get("local_receive_time_ns"),
            enforce_continuity=True,
        )
        if out is DeltaOutcome.GAP:
            gaps += 1
            return {
                "ok": False,
                "status": STATUS_DELTA_CONTINUITY_GAP,
                "applied": applied,
                "gaps": gaps,
                "final_u": book.update_id,
                "final_seq": book.seq,
                "gap_u": u,
                "best_bid": book.best_bid(),
                "best_ask": book.best_ask(),
                "bid_levels": len(book.bids),
                "ask_levels": len(book.asks),
                "book_content_hash": None,
                "crossed": False,
            }
        if out is DeltaOutcome.APPLIED:
            applied += 1
            last_u = book.update_id
            last_seq = book.seq

    snap = book.copy_consistent_snapshot()
    bids, asks = snap.full_levels()
    bb = book.best_bid()
    ba = book.best_ask()
    crossed = bb is not None and ba is not None and float(bb) >= float(ba)
    return {
        "ok": gaps == 0 and not crossed,
        "status": STATUS_DATA_COMPLETE if gaps == 0 and not crossed else STATUS_DELTA_CONTINUITY_GAP,
        "applied": applied,
        "gaps": gaps,
        "final_u": last_u,
        "final_seq": last_seq,
        "best_bid": bb,
        "best_ask": ba,
        "bid_levels": len(book.bids),
        "ask_levels": len(book.asks),
        "book_content_hash": book_content_hash(bids, asks),
        "crossed": crossed,
        "bids": bids,
        "asks": asks,
    }


def _empty_bundle(
    *,
    status: str,
    reasons: list[str],
    t0_ns: int,
    t0_book_snap: dict[str, Any] | None,
    overflow_count: int,
    reconnect_count: int,
    gap_count_runtime: int,
    analysis_start_ns: int | None = None,
    freshness_ms: int | None = None,
    deltas: Sequence[dict[str, Any]] = (),
    anchor: BookCheckpoint | None = None,
) -> FreezeBundle:
    first_recv = int(deltas[0]["_bridge_receive_time_ns"]) if deltas else None
    last_recv = int(deltas[-1]["_bridge_receive_time_ns"]) if deltas else None
    return FreezeBundle(
        status=status,
        data_complete=False,
        exclusion_reasons=tuple(dict.fromkeys(reasons)),
        t0_ns=t0_ns,
        feature_cutoff_ns=t0_ns,
        pre_roll_start_ns=analysis_start_ns if analysis_start_ns is not None else first_recv,
        pre_roll_end_ns=last_recv if last_recv is not None else t0_ns,
        actual_pre_roll_seconds=(
            max(0.0, (last_recv - (analysis_start_ns or first_recv)) / 1e9)
            if last_recv is not None and (analysis_start_ns or first_recv) is not None
            else 0.0
        ),
        anchor=anchor,
        deltas=tuple(deltas),
        t0_book=t0_book_snap,
        first_u=delta_u_seq(deltas[0])[0] if deltas else None,
        last_u=delta_u_seq(deltas[-1])[0] if deltas else None,
        first_seq=delta_u_seq(deltas[0])[1] if deltas else None,
        last_seq=delta_u_seq(deltas[-1])[1] if deltas else None,
        gap_count=int(gap_count_runtime),
        overflow_count=int(overflow_count),
        reconnect_count=int(reconnect_count),
        freshness_ms=freshness_ms,
        replay_capability=REPLAY_CAPABILITY_NONE,
        anchor_type=_anchor_type_for(anchor.kind) if anchor else ANCHOR_TYPE_MISSING,
        message_count=len(deltas),
        replay_ok=False,
        replay_final_u=None,
        replay_final_seq=None,
        analysis_start_ns=analysis_start_ns,
        payload_replay_start_ns=anchor.receive_time_ns if anchor else None,
    )


def build_freeze_bundle(
    *,
    symbol: str,
    t0_ns: int,
    requested_pre_roll_seconds: float,
    min_pre_roll_seconds: float,
    max_pre_roll_seconds: float,
    ring_items: Sequence[Any],
    overflow_count: int,
    reconnect_count: int,
    reconnect_in_window: bool,
    gap_count_runtime: int,
    last_receive_ns: int | None,
    stale_after_ms: int,
    anchor: BookCheckpoint | None,
    t0_book_snap: dict[str, Any] | None,
    available_checkpoints: Sequence[BookCheckpoint] | None = None,
    reconnect_mark_ns: int | None = None,
) -> FreezeBundle:
    """Build freeze bundle under anchor_retention_v2 rules."""
    reasons: list[str] = []
    req = max(0.0, min(float(requested_pre_roll_seconds), float(max_pre_roll_seconds)))
    analysis_start_ns = t0_ns - int(req * 1_000_000_000)

    all_deltas = filter_deltas_to_t0(ring_items, t0_ns=t0_ns)
    if not all_deltas:
        return _empty_bundle(
            status=STATUS_BUFFER_NOT_WARM,
            reasons=[STATUS_BUFFER_NOT_WARM],
            t0_ns=t0_ns,
            t0_book_snap=t0_book_snap,
            overflow_count=overflow_count,
            reconnect_count=reconnect_count,
            gap_count_runtime=gap_count_runtime,
            analysis_start_ns=analysis_start_ns,
        )

    buffer_start = int(all_deltas[0]["_bridge_receive_time_ns"])
    buffer_end = int(all_deltas[-1]["_bridge_receive_time_ns"])

    # Select / validate anchor relative to analysis_start (not oldest ring delta).
    ck_list = list(available_checkpoints or ([] if anchor is None else [anchor]))
    if anchor is None and ck_list:
        # Prefer latest checkpoint at or before analysis start.
        for ck in ck_list:
            if ck.receive_time_ns <= analysis_start_ns:
                anchor = ck
    if anchor is not None and anchor.receive_time_ns > analysis_start_ns:
        reasons.append(STATUS_BOOK_ANCHOR_MISSING)
        return _empty_bundle(
            status=STATUS_BOOK_ANCHOR_MISSING,
            reasons=reasons,
            t0_ns=t0_ns,
            t0_book_snap=t0_book_snap,
            overflow_count=overflow_count,
            reconnect_count=reconnect_count,
            gap_count_runtime=gap_count_runtime,
            analysis_start_ns=analysis_start_ns,
            deltas=all_deltas,
        )

    if anchor is None:
        return _empty_bundle(
            status=STATUS_BOOK_ANCHOR_MISSING,
            reasons=[STATUS_BOOK_ANCHOR_MISSING],
            t0_ns=t0_ns,
            t0_book_snap=t0_book_snap,
            overflow_count=overflow_count,
            reconnect_count=reconnect_count,
            gap_count_runtime=gap_count_runtime,
            analysis_start_ns=analysis_start_ns,
            deltas=all_deltas,
        )

    # Payload: deltas from anchor receive time through T0 (includes analysis prefix).
    deltas = [
        d
        for d in all_deltas
        if int(d["_bridge_receive_time_ns"]) >= int(anchor.receive_time_ns)
    ]
    if not deltas:
        return _empty_bundle(
            status=STATUS_PRE_ROLL_TOO_SHORT,
            reasons=[STATUS_PRE_ROLL_TOO_SHORT],
            t0_ns=t0_ns,
            t0_book_snap=t0_book_snap,
            overflow_count=overflow_count,
            reconnect_count=reconnect_count,
            gap_count_runtime=gap_count_runtime,
            analysis_start_ns=analysis_start_ns,
            anchor=anchor,
        )

    # Startup / incomplete coverage of requested analysis window.
    if buffer_start > analysis_start_ns:
        reasons.append(STATUS_STARTUP_PRE_ROLL_INCOMPLETE)

    analysis_end = buffer_end
    actual_analysis = max(0.0, (analysis_end - max(buffer_start, analysis_start_ns)) / 1e9)
    status = STATUS_DATA_COMPLETE
    if actual_analysis + 1e-6 < min(req, min_pre_roll_seconds):
        reasons.append(STATUS_PRE_ROLL_TOO_SHORT)
        status = STATUS_PRE_ROLL_TOO_SHORT
    # Full requested window required for DATA_COMPLETE under v2.
    if buffer_start > analysis_start_ns + 1_000_000:  # >1ms tolerance
        status = STATUS_STARTUP_PRE_ROLL_INCOMPLETE

    freshness_ms = None
    if last_receive_ns is not None:
        freshness_ms = max(0, int((t0_ns - last_receive_ns) / 1_000_000))
        if freshness_ms > stale_after_ms:
            reasons.append(STATUS_DATA_STALE)
            status = STATUS_DATA_STALE

    if overflow_count > 0:
        reasons.append(STATUS_BUFFER_OVERFLOW)
        status = STATUS_BUFFER_OVERFLOW

    # Resync / reconnect handling.
    rmark = int(reconnect_mark_ns or 0)
    resync_in_span = [
        c
        for c in ck_list
        if c.kind == CHECKPOINT_KIND_RESYNC
        and analysis_start_ns < c.receive_time_ns <= t0_ns
    ]
    if reconnect_in_window or (rmark and analysis_start_ns <= rmark <= t0_ns):
        if not resync_in_span:
            reasons.append(STATUS_RESYNC_CHECKPOINT_MISSING)
            reasons.append(STATUS_RECONNECT_IN_WINDOW)
            return _empty_bundle(
                status=STATUS_RESYNC_CHECKPOINT_MISSING,
                reasons=reasons,
                t0_ns=t0_ns,
                t0_book_snap=t0_book_snap,
                overflow_count=overflow_count,
                reconnect_count=reconnect_count,
                gap_count_runtime=gap_count_runtime,
                analysis_start_ns=analysis_start_ns,
                freshness_ms=freshness_ms,
                deltas=deltas,
                anchor=anchor,
            )

    if gap_count_runtime > 0:
        reasons.append(STATUS_SEQUENCE_GAP)
        status = STATUS_DELTA_CONTINUITY_GAP

    # Epoch markers for payload.
    epoch_markers = []
    for c in ck_list:
        if anchor.receive_time_ns <= c.receive_time_ns <= t0_ns and c.kind in (
            CHECKPOINT_KIND_INITIAL,
            CHECKPOINT_KIND_RESYNC,
        ):
            epoch_markers.append(
                {
                    "kind": c.kind,
                    "epoch_id": c.epoch_id,
                    "receive_time_ns": c.receive_time_ns,
                    "u": c.update_id,
                    "seq": c.seq,
                    "book_content_hash": c.book_content_hash,
                }
            )

    # Multi-epoch: each RESYNC in window must be playable; primary anchor epoch must match
    # the epoch covering analysis_start.
    if resync_in_span:
        # Anchor must belong to the epoch active at analysis_start (before first resync).
        first_resync = min(resync_in_span, key=lambda c: c.receive_time_ns)
        if anchor.receive_time_ns > analysis_start_ns:
            reasons.append(STATUS_BOOK_ANCHOR_MISSING)
            status = STATUS_BOOK_ANCHOR_MISSING
        if first_resync.epoch_id < anchor.epoch_id:
            reasons.append(STATUS_ANCHOR_EPOCH_MISMATCH)
            status = STATUS_ANCHOR_EPOCH_MISMATCH

    replay = replay_anchor_and_deltas(
        symbol=symbol,
        anchor=anchor,
        deltas=deltas,
        epoch_checkpoints=ck_list,
    )
    if not replay.get("ok"):
        gap_status = (
            STATUS_DELTA_CONTINUITY_GAP
            if replay.get("status") == STATUS_DELTA_CONTINUITY_GAP
            else STATUS_SEQUENCE_GAP
        )
        reasons.append(gap_status)
        status = gap_status

    t0_hash = None
    if t0_book_snap and t0_book_snap.get("b") is not None and t0_book_snap.get("a") is not None:
        t0_hash = book_content_hash(t0_book_snap.get("b") or [], t0_book_snap.get("a") or [])

    if t0_book_snap and replay.get("ok"):
        tu = t0_book_snap.get("u")
        if tu is not None and replay.get("final_u") is not None:
            if int(tu) != int(replay["final_u"]):
                reasons.append(STATUS_DELTA_CONTINUITY_GAP)
                status = STATUS_DELTA_CONTINUITY_GAP
        ts = t0_book_snap.get("seq")
        if ts is not None and replay.get("final_seq") is not None:
            if int(ts) != int(replay["final_seq"]):
                reasons.append(STATUS_DELTA_CONTINUITY_GAP)
                status = STATUS_DELTA_CONTINUITY_GAP
        if t0_hash and replay.get("book_content_hash") and t0_hash != replay["book_content_hash"]:
            reasons.append(STATUS_BOOK_HASH_MISMATCH)
            status = STATUS_BOOK_HASH_MISMATCH

    reasons = list(dict.fromkeys(reasons))
    data_complete = status == STATUS_DATA_COMPLETE and not reasons
    if data_complete:
        replay_capability = REPLAY_CAPABILITY_RAW_FULL_BOOK
    else:
        if status == STATUS_DATA_COMPLETE and reasons:
            status = reasons[0]
        data_complete = False
        replay_capability = (
            REPLAY_CAPABILITY_RAW_FULL_BOOK
            if replay.get("ok")
            and STATUS_BOOK_ANCHOR_MISSING not in reasons
            and STATUS_BOOK_HASH_MISMATCH not in reasons
            else REPLAY_CAPABILITY_NONE
        )

    first_u, first_seq = delta_u_seq(deltas[0]) if deltas else (None, None)
    last_u, last_seq = delta_u_seq(deltas[-1]) if deltas else (None, None)
    return FreezeBundle(
        status=status if not data_complete else STATUS_DATA_COMPLETE,
        data_complete=data_complete,
        exclusion_reasons=tuple(reasons),
        t0_ns=t0_ns,
        feature_cutoff_ns=t0_ns,
        pre_roll_start_ns=analysis_start_ns,
        pre_roll_end_ns=buffer_end,
        actual_pre_roll_seconds=actual_analysis,
        anchor=anchor,
        deltas=tuple(deltas),
        t0_book=t0_book_snap,
        first_u=first_u,
        last_u=last_u,
        first_seq=first_seq,
        last_seq=last_seq,
        gap_count=int(gap_count_runtime) + int(replay.get("gaps") or 0),
        overflow_count=int(overflow_count),
        reconnect_count=int(reconnect_count),
        freshness_ms=freshness_ms,
        replay_capability=replay_capability,
        anchor_type=_anchor_type_for(anchor.kind),
        message_count=len(deltas),
        replay_ok=bool(replay.get("ok")),
        replay_final_u=replay.get("final_u"),
        replay_final_seq=replay.get("final_seq"),
        analysis_start_ns=analysis_start_ns,
        payload_replay_start_ns=anchor.receive_time_ns,
        epoch_markers=tuple(epoch_markers),
        replay_book_hash=replay.get("book_content_hash"),
        t0_book_hash=t0_hash,
    )


def manifest_from_bundle(
    *,
    bundle: FreezeBundle,
    protocol_version: str,
    request_id: str,
    forecast_id_seed: str,
    symbol: str,
    collector_instance_id: str,
    payload_sha256: str,
    dump_relpath: str,
) -> dict[str, Any]:
    body = {
        "protocol_version": protocol_version,
        "request_id": request_id,
        "forecast_id_seed": forecast_id_seed,
        "symbol": symbol,
        "collector_instance_id": collector_instance_id,
        "t0_utc": _iso_from_ns(bundle.t0_ns),
        "feature_cutoff_utc": _iso_from_ns(bundle.feature_cutoff_ns),
        "pre_roll_start_utc": (
            _iso_from_ns(bundle.pre_roll_start_ns)
            if bundle.pre_roll_start_ns is not None
            else None
        ),
        "pre_roll_end_utc": (
            _iso_from_ns(bundle.pre_roll_end_ns)
            if bundle.pre_roll_end_ns is not None
            else None
        ),
        "actual_pre_roll_seconds": bundle.actual_pre_roll_seconds,
        "message_count": bundle.message_count,
        "anchor_time": (
            _iso_from_ns(bundle.anchor.receive_time_ns) if bundle.anchor else None
        ),
        "anchor_type": bundle.anchor_type,
        "analysis_start_ns": bundle.analysis_start_ns,
        "payload_replay_start_ns": bundle.payload_replay_start_ns,
        "first_u": bundle.first_u,
        "last_u": bundle.last_u,
        "first_seq": bundle.first_seq,
        "last_seq": bundle.last_seq,
        "gap_count": bundle.gap_count,
        "overflow_count": bundle.overflow_count,
        "reconnect_count": bundle.reconnect_count,
        "freshness_ms": bundle.freshness_ms,
        "replay_capability": bundle.replay_capability,
        "data_complete": bundle.data_complete,
        "status": bundle.status,
        "exclusion_reasons": list(bundle.exclusion_reasons),
        "payload_sha256": payload_sha256,
        "dump_relpath": dump_relpath,
        "created_at_ns": time.time_ns(),
    }
    raw = orjson.dumps(body, option=orjson.OPT_SORT_KEYS)
    body["manifest_sha256"] = sha256_bytes(raw)
    return body


def canonical_manifest_hash(manifest: dict[str, Any]) -> str:
    trimmed = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    return sha256_bytes(orjson.dumps(trimmed, option=orjson.OPT_SORT_KEYS))

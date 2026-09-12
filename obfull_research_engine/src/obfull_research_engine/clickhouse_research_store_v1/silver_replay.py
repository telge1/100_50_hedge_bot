"""Causal Full-OB silver replay from bronze envelopes (FullBookState semantics)."""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome

from .silver_constants import BUCKET_MS, NEAR_BPS, SILVER_REPLAY_CONTRACT, SILVER_SCHEMA_VERSION

_BOOK_PACK = struct.Struct("<Bdd")


def book_map_sha256(bids: dict[float, float], asks: dict[float, float]) -> str:
    """Same canonical hash as drilldown.aggregation_100ms.book_map_sha256."""
    h = hashlib.sha256()
    bid_parts = [_BOOK_PACK.pack(0, float(p), float(q)) for p, q in sorted(bids.items()) if q and float(q) > 0]
    ask_parts = [_BOOK_PACK.pack(1, float(p), float(q)) for p, q in sorted(asks.items()) if q and float(q) > 0]
    if bid_parts:
        h.update(b"".join(bid_parts))
    if ask_parts:
        h.update(b"".join(ask_parts))
    return h.hexdigest()


def _floor_bucket(dt: datetime, bucket_ms: int) -> datetime:
    """Same semantics as drilldown.aggregation_100ms._floor_bucket."""
    dt = dt.astimezone(timezone.utc)
    ms = int(dt.timestamp() * 1000)
    floored = ms - (ms % bucket_ms)
    return datetime.fromtimestamp(floored / 1000.0, tz=timezone.utc)


def _near_band_depths(mid: float, bids: dict[float, float], asks: dict[float, float]) -> dict[str, float]:
    """Same near-band notional helper as drilldown.aggregation_100ms._near_band_depths."""
    if mid <= 0:
        return {}
    bands = [(0, 2), (2, 5), (5, 10), (10, 25), (25, 50)]
    bid_acc = {f"{lo}_{hi}": 0.0 for lo, hi in bands}
    ask_acc = {f"{lo}_{hi}": 0.0 for lo, hi in bands}
    for px, qty in bids.items():
        if qty <= 0:
            continue
        bps = (mid - px) / mid * 1e4
        if bps < 0 or bps >= 50:
            continue
        for lo, hi in bands:
            if lo <= bps < hi:
                bid_acc[f"{lo}_{hi}"] += px * qty
                break
    for px, qty in asks.items():
        if qty <= 0:
            continue
        bps = (px - mid) / mid * 1e4
        if bps < 0 or bps >= 50:
            continue
        for lo, hi in bands:
            if lo <= bps < hi:
                ask_acc[f"{lo}_{hi}"] += px * qty
                break
    out: dict[str, float] = {}
    for lo, hi in bands:
        k = f"{lo}_{hi}"
        b, a = bid_acc[k], ask_acc[k]
        out[f"bid_depth_notional_usdt_bps_{k}"] = b
        out[f"ask_depth_notional_usdt_bps_{k}"] = a
    return out


class SilverReplayError(RuntimeError):
    pass


def contract_hash() -> str:
    return hashlib.sha256(SILVER_REPLAY_CONTRACT.encode("utf-8")).hexdigest()


def make_silver_build_id(
    *,
    schema_version: str,
    bronze_import_id: str,
    symbol: str,
    window_start: str,
    window_end: str,
    replay_contract_hash: str,
    anchor_record_id: str,
) -> str:
    material = "|".join(
        [
            schema_version,
            bronze_import_id,
            symbol.upper(),
            window_start,
            window_end,
            replay_contract_hash,
            anchor_record_id,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def change_type(old_size: float, new_size: float) -> str | None:
    old_pos = old_size > 0
    new_pos = new_size > 0
    if not old_pos and new_pos:
        return "ADD"
    if old_pos and not new_pos:
        return "DELETE"
    if old_pos and new_pos and float(old_size) != float(new_size):
        return "UPDATE"
    return None


def _as_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value)


def _ns_to_dt(ns: int) -> datetime:
    sec, rem = divmod(int(ns), 1_000_000_000)
    return datetime.fromtimestamp(sec, tz=timezone.utc).replace(microsecond=rem // 1000)


def _dt_to_ns(dt: datetime) -> int:
    dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp()) * 1_000_000_000 + dt.microsecond * 1000


def _dt_to_ms(dt: datetime) -> int:
    dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp()) * 1000 + dt.microsecond // 1000


@dataclass
class BronzeRecord:
    record_id: str
    symbol: str
    message_type: str
    event_time_ns: int
    receive_time_ns: int
    update_id: int
    seq: int
    update_id_present: int
    seq_present: int
    source_segment_sha256: str
    record_ordinal: int
    payload_sha256: str
    original_payload: dict[str, Any]


@dataclass
class SilverReplayResult:
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    level_changes: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    anchor_record_id: str = ""
    anchor_time_ns: int | None = None
    replay_coverage_start_ns: int | None = None
    replay_coverage_end_ns: int | None = None
    source_record_count: int = 0
    unique_record_count: int = 0
    delta_message_count: int = 0
    skipped_pre_anchor_deltas: int = 0
    replay_epoch_count: int = 0
    gap_count: int = 0
    reset_count: int = 0
    start_book_hash: str = ""
    end_book_hash: str = ""
    epochs_seen: set[int] = field(default_factory=set)


def dedupe_bronze_by_record_id(rows: list[BronzeRecord]) -> list[BronzeRecord]:
    """Keep first occurrence per record_id in source order."""
    seen: set[str] = set()
    out: list[BronzeRecord] = []
    for r in rows:
        if r.record_id in seen:
            continue
        seen.add(r.record_id)
        out.append(r)
    return out


def sort_bronze_source_order(rows: list[BronzeRecord]) -> list[BronzeRecord]:
    return sorted(rows, key=lambda r: (r.source_segment_sha256, r.record_ordinal))


def assert_strict_ordinals(rows: list[BronzeRecord]) -> None:
    if not rows:
        return
    prev = None
    for r in rows:
        if prev is not None:
            if r.source_segment_sha256 == prev.source_segment_sha256:
                if r.record_ordinal <= prev.record_ordinal:
                    raise SilverReplayError(
                        f"STOP_BRONZE_INPUT_INVALID: non-increasing record_ordinal "
                        f"{prev.record_ordinal} -> {r.record_ordinal}"
                    )
        prev = r


def is_full_book_anchor(payload: dict[str, Any], message_type: str) -> bool:
    if message_type == "checkpoint":
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        return len(bids) > 0 and len(asks) > 0
    if message_type == "snapshot":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        bids = data.get("b") or []
        asks = data.get("a") or []
        return len(bids) > 0 and len(asks) > 0
    return False


def find_first_full_anchor(rows: list[BronzeRecord]) -> BronzeRecord | None:
    for r in rows:
        if r.message_type in ("checkpoint", "snapshot") and is_full_book_anchor(
            r.original_payload, r.message_type
        ):
            return r
    return None


def _emit_side_changes(
    *,
    side: str,
    book: dict[float, float],
    levels: list,
    event_time_ns: int,
    receive_time_ns: int,
    replay_epoch: int,
    apply_order_start: int,
    message_order: int,
    update_id: int,
    seq: int,
    source_record_id: str,
    source_record_ordinal: int,
    symbol: str,
    silver_build_id: str,
    created_at_ms: int,
) -> tuple[list[dict[str, Any]], int]:
    out: list[dict[str, Any]] = []
    apply_order = apply_order_start
    level_order = 0
    for item in levels or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            px, qty = float(item[0]), float(item[1])
        else:
            continue
        old = float(book.get(px, 0.0) or 0.0)
        new = float(qty)
        ct = change_type(old, new if new > 0 else 0.0)
        effective_new = new if new > 0 else 0.0
        if ct is None:
            continue
        apply_order += 1
        level_order += 1
        out.append(
            {
                "symbol": symbol,
                "event_time": 0,
                "event_time_ns": event_time_ns,
                "receive_time_ns": receive_time_ns,
                "replay_epoch": replay_epoch,
                "apply_order": apply_order,
                "message_order": message_order,
                "level_order": level_order,
                "side": side,
                "price": px,
                "old_size": old,
                "new_size": effective_new,
                "change_type": ct,
                "update_id": update_id,
                "seq": seq,
                "source_record_id": source_record_id,
                "source_record_ordinal": source_record_ordinal,
                "silver_build_id": silver_build_id,
                "created_at": 0,
                "created_at_ms": created_at_ms,
            }
        )
    return out, apply_order


def _metric_row(
    *,
    state: FullBookState,
    bucket_start: datetime,
    replay_epoch: int,
    symbol: str,
    silver_build_id: str,
    created_at_ms: int,
) -> dict[str, Any]:
    bb = state.best_bid()
    ba = state.best_ask()
    mid = state.mid()
    spread = (ba - bb) if bb is not None and ba is not None else None
    bands = _near_band_depths(mid or 0.0, state.bids, state.asks) if mid else {}
    return {
        "symbol": symbol,
        "bucket_start": 0,
        "bucket_start_ms": _dt_to_ms(bucket_start),
        "replay_epoch": replay_epoch,
        "best_bid": bb,
        "best_ask": ba,
        "mid": mid,
        "spread": spread,
        "bid_level_count": len(state.bids),
        "ask_level_count": len(state.asks),
        "bid_depth_near": float(bands.get(f"bid_depth_notional_usdt_bps_{NEAR_BPS}") or 0.0),
        "ask_depth_near": float(bands.get(f"ask_depth_notional_usdt_bps_{NEAR_BPS}") or 0.0),
        "last_update_id": state.update_id,
        "last_seq": state.seq,
        "book_hash": book_map_sha256(state.bids, state.asks),
        "silver_build_id": silver_build_id,
        "created_at": 0,
        "created_at_ms": created_at_ms,
    }


def replay_bronze_to_silver(
    *,
    rows: list[BronzeRecord],
    symbol: str,
    window_start_ns: int,
    window_end_ns: int,
    silver_build_id: str,
    created_at_ms: int,
) -> SilverReplayResult:
    """Replay using FullBookState; source order already sorted+deduped.

    ``window_start_ns``/``window_end_ns`` define the *analysis* window for metrics and
    level_changes. Warm-up events before ``window_start_ns`` are applied but not emitted.
    """
    assert_strict_ordinals(rows)
    result = SilverReplayResult(
        source_record_count=len(rows),
        unique_record_count=len(rows),
    )
    anchor = find_first_full_anchor(rows)
    if anchor is None:
        raise SilverReplayError(
            "STOP_SILVER_ANCHOR_MISSING: no full checkpoint/snapshot in bronze input; "
            "bronze must include a COMPLETE full-book checkpoint with "
            f"event_time_ns < {window_start_ns}"
        )

    for r in rows:
        if r.record_ordinal < anchor.record_ordinal and r.message_type == "delta":
            result.skipped_pre_anchor_deltas += 1

    state = FullBookState(symbol=symbol.upper())
    epoch = 0
    apply_order = 0
    message_order = 0
    coverage_start_ns = int(anchor.event_time_ns)
    result.anchor_record_id = anchor.record_id
    result.anchor_time_ns = coverage_start_ns
    result.replay_coverage_start_ns = coverage_start_ns
    result.replay_coverage_end_ns = int(window_end_ns)

    payload = anchor.original_payload
    if anchor.message_type == "checkpoint":
        bids, asks = payload.get("bids") or [], payload.get("asks") or []
        u, seq = payload.get("u"), payload.get("seq")
        recv = payload.get("receive_time")
        ts_ms = None if payload.get("event_time") is None else int(payload["event_time"]) // 1_000_000
    else:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        bids, asks = data.get("b") or [], data.get("a") or []
        u, seq = data.get("u"), data.get("seq")
        recv = anchor.receive_time_ns
        ts_ms = None
        if payload.get("ts") is not None:
            ts_ms = int(payload["ts"])
        elif data.get("ts") is not None:
            ts_ms = int(data["ts"])

    state.apply_snapshot(
        bids=bids,
        asks=asks,
        u=u,
        seq=seq,
        ts_ms=ts_ms if ts_ms is not None else int(anchor.event_time_ns // 1_000_000),
        receive_time_ns=recv if recv is not None else anchor.receive_time_ns,
        mark_ready=True,
    )
    epoch += 1
    result.epochs_seen.add(epoch)
    result.reset_count += 1
    apply_order += 1
    start_hash = book_map_sha256(state.bids, state.asks)
    result.start_book_hash = start_hash
    bb, ba = state.best_bid(), state.best_ask()
    archive_hash = str(payload.get("book_sha256") or "") or start_hash
    ck_payload = json.dumps(
        {"bids": bids, "asks": asks, "u": u, "seq": seq, "book_sha256": archive_hash},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    result.checkpoints.append(
        {
            "symbol": symbol.upper(),
            "checkpoint_time": 0,
            "checkpoint_time_ns": int(anchor.event_time_ns),
            "replay_epoch": epoch,
            "source_record_id": anchor.record_id,
            "source_record_ordinal": anchor.record_ordinal,
            "source_segment_sha256": anchor.source_segment_sha256,
            "update_id": int(u or 0),
            "seq": int(seq or 0),
            "bid_level_count": len(state.bids),
            "ask_level_count": len(state.asks),
            "best_bid": float(bb or 0.0),
            "best_ask": float(ba or 0.0),
            "book_hash": start_hash,
            "checkpoint_payload": ck_payload,
            "silver_build_id": silver_build_id,
            "created_at": 0,
            "created_at_ms": created_at_ms,
        }
    )

    post = [r for r in rows if r.record_ordinal > anchor.record_ordinal]
    # Metrics only for analysis window; start emitting after analysis_start.
    last_emitted_bucket_end_ns = int(window_start_ns)

    def emit_due_buckets(before_ns: int, current_epoch: int) -> None:
        nonlocal last_emitted_bucket_end_ns
        b = _floor_bucket(_ns_to_dt(max(window_start_ns, last_emitted_bucket_end_ns)), BUCKET_MS)
        while True:
            end_b = b + timedelta(milliseconds=BUCKET_MS)
            end_ns = _dt_to_ns(end_b)
            start_ns = _dt_to_ns(b)
            if start_ns < window_start_ns:
                b = end_b
                continue
            if end_ns > before_ns:
                break
            if end_ns > window_end_ns:
                break
            if end_ns <= last_emitted_bucket_end_ns:
                b = end_b
                continue
            result.metrics.append(
                _metric_row(
                    state=state,
                    bucket_start=b,
                    replay_epoch=current_epoch,
                    symbol=symbol.upper(),
                    silver_build_id=silver_build_id,
                    created_at_ms=created_at_ms,
                )
            )
            last_emitted_bucket_end_ns = end_ns
            b = end_b

    for r in post:
        if r.event_time_ns >= window_end_ns:
            break
        if r.event_time_ns < coverage_start_ns:
            continue
        emit_due_buckets(r.event_time_ns, epoch)

        if r.message_type in ("checkpoint", "snapshot"):
            if not is_full_book_anchor(r.original_payload, r.message_type):
                continue
            p = r.original_payload
            if r.message_type == "checkpoint":
                bids, asks = p.get("bids") or [], p.get("asks") or []
                u, seq = p.get("u"), p.get("seq")
                recv = p.get("receive_time")
                ts_ms = None if p.get("event_time") is None else int(p["event_time"]) // 1_000_000
            else:
                data = p.get("data") if isinstance(p.get("data"), dict) else {}
                bids, asks = data.get("b") or [], data.get("a") or []
                u, seq = data.get("u"), data.get("seq")
                recv = r.receive_time_ns
                ts_ms = int(p["ts"]) if p.get("ts") is not None else int(r.event_time_ns // 1_000_000)
            state.apply_snapshot(
                bids=bids,
                asks=asks,
                u=u,
                seq=seq,
                ts_ms=ts_ms,
                receive_time_ns=recv if recv is not None else r.receive_time_ns,
                mark_ready=True,
            )
            epoch += 1
            result.epochs_seen.add(epoch)
            result.reset_count += 1
            apply_order += 1
            message_order += 1
            bh = book_map_sha256(state.bids, state.asks)
            result.checkpoints.append(
                {
                    "symbol": symbol.upper(),
                    "checkpoint_time": 0,
                    "checkpoint_time_ns": int(r.event_time_ns),
                    "replay_epoch": epoch,
                    "source_record_id": r.record_id,
                    "source_record_ordinal": r.record_ordinal,
                    "source_segment_sha256": r.source_segment_sha256,
                    "update_id": int(u or 0),
                    "seq": int(seq or 0),
                    "bid_level_count": len(state.bids),
                    "ask_level_count": len(state.asks),
                    "best_bid": float(state.best_bid() or 0.0),
                    "best_ask": float(state.best_ask() or 0.0),
                    "book_hash": bh,
                    "checkpoint_payload": json.dumps(
                        {"bids": bids, "asks": asks, "u": u, "seq": seq},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "silver_build_id": silver_build_id,
                    "created_at": 0,
                    "created_at_ms": created_at_ms,
                }
            )
            continue

        if r.message_type != "delta":
            continue

        in_analysis = r.event_time_ns >= window_start_ns
        if in_analysis:
            result.delta_message_count += 1
        message_order += 1
        data = r.original_payload.get("data") if isinstance(r.original_payload.get("data"), dict) else {}
        b_lvls = data.get("b") or []
        a_lvls = data.get("a") or []
        u = data.get("u")
        seq = data.get("seq")
        ts = r.original_payload.get("ts") or data.get("ts")

        bid_changes: list[dict[str, Any]] = []
        ask_changes: list[dict[str, Any]] = []
        if in_analysis:
            bid_changes, apply_order = _emit_side_changes(
                side="bid",
                book=state.bids,
                levels=b_lvls,
                event_time_ns=r.event_time_ns,
                receive_time_ns=r.receive_time_ns,
                replay_epoch=epoch,
                apply_order_start=apply_order,
                message_order=message_order,
                update_id=int(u or 0),
                seq=int(seq or 0),
                source_record_id=r.record_id,
                source_record_ordinal=r.record_ordinal,
                symbol=symbol.upper(),
                silver_build_id=silver_build_id,
                created_at_ms=created_at_ms,
            )
            ask_changes, apply_order = _emit_side_changes(
                side="ask",
                book=state.asks,
                levels=a_lvls,
                event_time_ns=r.event_time_ns,
                receive_time_ns=r.receive_time_ns,
                replay_epoch=epoch,
                apply_order_start=apply_order,
                message_order=message_order,
                update_id=int(u or 0),
                seq=int(seq or 0),
                source_record_id=r.record_id,
                source_record_ordinal=r.record_ordinal,
                symbol=symbol.upper(),
                silver_build_id=silver_build_id,
                created_at_ms=created_at_ms,
            )
            result.level_changes.extend(bid_changes)
            result.level_changes.extend(ask_changes)

        outcome = state.apply_delta(
            bids=b_lvls,
            asks=a_lvls,
            u=u,
            seq=seq,
            ts_ms=int(ts) if ts is not None else int(r.event_time_ns // 1_000_000),
            cts_ms=r.original_payload.get("cts") or data.get("cts"),
            receive_time_ns=r.receive_time_ns,
            enforce_continuity=True,
        )
        if outcome is DeltaOutcome.GAP or outcome is DeltaOutcome.U_RESET:
            result.gap_count += 1
        if outcome is not DeltaOutcome.APPLIED:
            n = len(bid_changes) + len(ask_changes)
            if n:
                result.level_changes = result.level_changes[:-n]
                apply_order -= n
            continue

    emit_due_buckets(window_end_ns, epoch)
    result.end_book_hash = book_map_sha256(state.bids, state.asks)
    result.replay_epoch_count = len(result.epochs_seen) if result.epochs_seen else 0
    return result


def bronze_row_from_ch(row: tuple[Any, ...]) -> BronzeRecord:
    (
        record_id,
        symbol,
        message_type,
        event_time_ns,
        receive_time_ns,
        update_id,
        seq,
        update_id_present,
        seq_present,
        source_segment_sha256,
        record_ordinal,
        payload_sha256,
        original_payload,
    ) = row
    payload_txt = _as_text(original_payload)
    payload_obj = json.loads(payload_txt) if payload_txt else {}
    return BronzeRecord(
        record_id=_as_text(record_id),
        symbol=_as_text(symbol).upper(),
        message_type=_as_text(message_type).lower(),
        event_time_ns=int(event_time_ns),
        receive_time_ns=int(receive_time_ns or 0),
        update_id=int(update_id or 0),
        seq=int(seq or 0),
        update_id_present=int(update_id_present or 0),
        seq_present=int(seq_present or 0),
        source_segment_sha256=_as_text(source_segment_sha256),
        record_ordinal=int(record_ordinal),
        payload_sha256=_as_text(payload_sha256),
        original_payload=payload_obj if isinstance(payload_obj, dict) else {},
    )

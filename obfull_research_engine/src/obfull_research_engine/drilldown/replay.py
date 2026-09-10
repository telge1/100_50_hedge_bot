"""Full-OB + public trade replay for causal drilldown windows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.config import DEFAULT_ARCHIVE_ROOT
from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.replay import iter_records
from orderbook_analyse.research.general_market_behavior_v1.coverage import list_hour_segments
from orderbook_analyse.research.general_market_behavior_v1.modalities import TradeRow, load_trades
from orderbook_analyse.research.general_market_behavior_v1.state_builder import _event_dt_from_record

from .aggregation_100ms import _near_band_depths, book_map_sha256
from .event_order import sort_timeline

# Process-local cache: decompress each hourly segment at most once
_SEGMENT_RECORD_CACHE: dict[str, list[dict[str, Any]]] = {}

INITIAL_BOOK_EVENT_TIME_SEMANTICS = "event_time_strict_lt_window_start"
CUTOFF_EVENT_TIME_SEMANTICS = "event_time_strict_lt_cutoff"


def clear_segment_cache() -> None:
    _SEGMENT_RECORD_CACHE.clear()


def _hour_floor(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def segments_covering(symbol: str, start: datetime, end: datetime, archive_root: Path | None = None) -> list[Path]:
    root = archive_root or DEFAULT_ARCHIVE_ROOT
    all_segs = list_hour_segments(root, symbol)
    needed = set()
    h = _hour_floor(start)
    while h < end:
        needed.add(h)
        h += timedelta(hours=1)
    needed.add(_hour_floor(start - timedelta(seconds=1)))

    chosen: list[Path] = []
    for seg in all_segs:
        name = seg.name
        try:
            part = name.split("_")[1]
            hour = datetime.strptime(part, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if hour in needed:
            chosen.append(seg)
    return sorted(chosen, key=lambda p: p.name)


def cached_records(segment: Path) -> list[dict[str, Any]]:
    key = str(segment.resolve())
    if key not in _SEGMENT_RECORD_CACHE:
        _SEGMENT_RECORD_CACHE[key] = list(iter_records(segment))
    return _SEGMENT_RECORD_CACHE[key]


def _changed_levels(before: dict[float, float], after: dict[float, float]) -> int:
    keys = set(before) | set(after)
    n = 0
    for px in keys:
        if float(before.get(px, 0.0) or 0.0) != float(after.get(px, 0.0) or 0.0):
            n += 1
    return n


def sample_live_fullbook(
    state: FullBookState,
    *,
    cutoff: datetime,
    label: str,
    last_event_ts: datetime | None,
    last_event_type: str | None,
    checkpoint_or_snapshot_id: str | None,
    replay_epoch: int,
    semantics: str = CUTOFF_EVENT_TIME_SEMANTICS,
    include_depth_bands: bool = True,
) -> dict[str, Any]:
    """Metrics from the live FullBookState. Not via build_states_100ms / _book_at."""
    bb = max(state.bids) if state.bids else None
    ba = min(state.asks) if state.asks else None
    mid = ((bb + ba) / 2.0) if bb is not None and ba is not None else None
    spread_price = (ba - bb) if bb is not None and ba is not None else None
    spread_bps = ((ba - bb) / mid * 1e4) if mid and bb is not None and ba is not None else None
    bands = _near_band_depths(mid or 0.0, state.bids, state.asks) if include_depth_bands and mid else {}
    crossed = bool(bb is not None and ba is not None and bb >= ba)
    return {
        "cutoff_ts": cutoff,
        "label": label,
        "event_time_semantics": semantics,
        "best_bid": bb,
        "best_ask": ba,
        "mid": mid,
        "spread": spread_price,
        "spread_bps": spread_bps,
        "bid_depth_0_2bps": bands.get("bid_depth_notional_usdt_bps_0_2"),
        "ask_depth_0_2bps": bands.get("ask_depth_notional_usdt_bps_0_2"),
        "depth_imbalance": bands.get("depth_imbalance_bps_0_2"),
        "update_id": state.update_id,
        "sequence_id": state.seq,
        "replay_epoch": replay_epoch,
        "last_applied_event_ts": last_event_ts,
        "last_event_type": last_event_type,
        "checkpoint_or_snapshot_id": checkpoint_or_snapshot_id,
        "n_bid_levels": len(state.bids),
        "n_ask_levels": len(state.asks),
        "book_sha256": book_map_sha256(state.bids, state.asks),
        "bid_depth_2_5bps": bands.get("bid_depth_notional_usdt_bps_2_5"),
        "ask_depth_2_5bps": bands.get("ask_depth_notional_usdt_bps_2_5"),
        "bid_depth_5_10bps": bands.get("bid_depth_notional_usdt_bps_5_10"),
        "ask_depth_5_10bps": bands.get("ask_depth_notional_usdt_bps_5_10"),
        "crossed": crossed,
        "book_valid": bool(bb is not None and ba is not None and not crossed),
        "source": "live_FullBookState",
    }


def replay_window(
    *,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    archive_root: Path | None = None,
    trades: list[TradeRow] | None = None,
    reference_cutoffs: list[datetime] | None = None,
) -> dict[str, Any]:
    """Single-pass replay [window_start, window_end) with checkpoint warmup before start.

    In-window checkpoint/snapshot events are emitted as explicit ``book_resets``
    (full bids/asks replacement) and as ``book_snapshots_by_time``. They are not
    expanded into synthetic level-deltas.

    Warmup applies ``event_time < window_start``. The frozen initial book therefore
    contains strictly ``event_time < window_start``.

    Direct FullBookState samples at ``reference_cutoffs`` include only events with
    ``event_time < cutoff``. Immediately-after-reset samples include the reset at
    that timestamp and are labelled separately.
    """
    window_start = window_start.astimezone(timezone.utc)
    window_end = window_end.astimezone(timezone.utc)
    segments = segments_covering(symbol, window_start, window_end, archive_root)
    if not segments:
        return {
            "ok": False,
            "error": "NO_SEGMENTS",
            "window_start": window_start,
            "window_end": window_end,
        }

    bytes_read = 0
    records: list[tuple[str, dict[str, Any]]] = []
    for seg in segments:
        try:
            bytes_read += seg.stat().st_size
        except OSError:
            pass
        for rec in cached_records(seg):
            records.append((str(seg), rec))

    state = FullBookState(symbol=symbol)
    anchored = False
    epoch = 0
    events: list[dict[str, Any]] = []
    level_changes: list[dict[str, Any]] = []
    book_resets: list[dict[str, Any]] = []
    book_snapshots_by_time: list[tuple[datetime, dict[float, float], dict[float, float]]] = []
    checkpoint_before_after: list[dict[str, Any]] = []
    direct_reference: list[dict[str, Any]] = []
    sequence_gaps = 0
    prev_u: int | None = None
    initial_bids: dict[float, float] = {}
    initial_asks: dict[float, float] = {}
    initial_update_id: Any = None
    initial_sequence_id: Any = None
    initial_replay_epoch: Any = None
    initial_checkpoint_id: Any = None
    warmup_edt: datetime | None = None
    saw_window = False
    apply_order = 0
    last_event_ts: datetime | None = None
    last_event_type: str | None = None
    last_checkpoint_id: str | None = None

    remaining_cutoffs: list[tuple[datetime, str]] = []
    for raw in reference_cutoffs or []:
        if raw is None:
            continue
        if isinstance(raw, (tuple, list)) and len(raw) >= 2:
            ts_raw, label = raw[0], str(raw[1])
        else:
            ts_raw, label = raw, "cutoff"
        ts = ts_raw.astimezone(timezone.utc) if getattr(ts_raw, "tzinfo", None) else ts_raw.replace(tzinfo=timezone.utc)
        remaining_cutoffs.append((ts, label))
    remaining_cutoffs.sort(key=lambda item: (item[0], item[1]))
    cutoff_i = 0

    def capture_due(exclusive_upper: datetime) -> None:
        nonlocal cutoff_i
        while cutoff_i < len(remaining_cutoffs) and remaining_cutoffs[cutoff_i][0] <= exclusive_upper:
            cutoff, label = remaining_cutoffs[cutoff_i]
            cutoff_i += 1
            direct_reference.append(
                sample_live_fullbook(
                    state,
                    cutoff=cutoff,
                    label=label,
                    last_event_ts=last_event_ts,
                    last_event_type=last_event_type,
                    checkpoint_or_snapshot_id=last_checkpoint_id,
                    replay_epoch=epoch,
                    semantics=CUTOFF_EVENT_TIME_SEMANTICS,
                    include_depth_bands=not str(label).startswith("bucket_end_"),
                )
            )

    def apply_snapshot_kind(kind: str, payload: dict, record: dict, edt: datetime) -> tuple[Any, Any, Any]:
        nonlocal epoch, prev_u, anchored
        if kind == "checkpoint":
            bids, asks = payload.get("bids") or [], payload.get("asks") or []
            u, seq = payload.get("u"), payload.get("seq")
            recv = payload.get("receive_time")
        else:
            data = payload.get("data") or {}
            bids, asks = data.get("b") or [], data.get("a") or []
            u, seq = data.get("u"), data.get("seq")
            recv = record.get("receive_time_ns")
        state.apply_snapshot(
            bids=bids,
            asks=asks,
            u=u,
            seq=seq,
            ts_ms=int(edt.timestamp() * 1000),
            receive_time_ns=recv,
            mark_ready=True,
        )
        anchored = True
        epoch += 1
        prev_u = state.update_id
        return u, seq, recv

    def emit_level(
        side: str,
        px: float,
        old_q: float,
        new_q: float,
        edt2: datetime,
        rec: dict,
        u2,
        seq2,
        order: int,
    ) -> None:
        delta_q = new_q - old_q
        if delta_q == 0:
            return
        level_changes.append(
            {
                "event_time": edt2,
                "received_at": rec.get("receive_time_ns"),
                "event_type": "level_change",
                "symbol": symbol,
                "side": side,
                "price": float(px),
                "old_size": float(old_q),
                "new_size": float(new_q),
                "size_delta": float(delta_q),
                "notional_delta": float(delta_q) * float(px),
                "u": u2,
                "pu": None,
                "seq": seq2,
                "epoch": epoch,
                "apply_order": order,
                "source": "full_ob",
                "source_event_id": f"lvl:{side}:{px}:{u2}:{seq2}:{edt2.isoformat()}",
                "ordering_flag": None,
            }
        )

    def emit_reset(*, kind: str, edt: datetime, u, seq, recv, order: int, source_event_id: str) -> None:
        bids_copy = dict(state.bids)
        asks_copy = dict(state.asks)
        rec = {
            "event_time": edt,
            "event_type": kind,
            "bids": bids_copy,
            "asks": asks_copy,
            "update_id": u if u is not None else state.update_id,
            "sequence_id": seq if seq is not None else state.seq,
            "replay_epoch": epoch,
            "source": kind,
            "source_event_id": source_event_id,
            "checkpoint_or_snapshot_id": source_event_id,
            "apply_order": order,
            "received_at": recv,
        }
        book_resets.append(rec)
        book_snapshots_by_time.append((edt, bids_copy, asks_copy))

    for _seg, record in records:
        kind = str(record.get("message_type") or "")
        if kind not in {"checkpoint", "snapshot", "delta"}:
            continue
        payload = record.get("original_payload") if isinstance(record.get("original_payload"), dict) else {}
        edt = _event_dt_from_record(kind, payload, record)
        if edt is None:
            continue
        if edt >= window_end:
            break

        if edt < window_start:
            # Keep book current through deltas as well (not only checkpoints).
            if kind in {"checkpoint", "snapshot"}:
                apply_snapshot_kind(kind, payload, record, edt)
                warmup_edt = edt
                last_event_ts = edt
                last_event_type = kind
                last_checkpoint_id = f"warmup:{kind}:{state.update_id}:{state.seq}"
            elif kind == "delta" and anchored:
                data = payload.get("data") or {}
                state.apply_delta(
                    bids=data.get("b") or [],
                    asks=data.get("a") or [],
                    u=data.get("u"),
                    seq=data.get("seq"),
                    ts_ms=payload.get("ts") or data.get("ts"),
                    cts_ms=payload.get("cts") or data.get("cts"),
                    receive_time_ns=record.get("receive_time_ns"),
                    enforce_continuity=False,
                )
                prev_u = state.update_id
                last_event_ts = edt
                last_event_type = "delta"
            continue

        # entering window: freeze initial book once
        if not saw_window:
            if not anchored:
                return {
                    "ok": False,
                    "error": "NO_WARMUP_CHECKPOINT",
                    "window_start": window_start,
                    "window_end": window_end,
                    "segments": [str(s) for s in segments],
                }
            initial_bids = dict(state.bids)
            initial_asks = dict(state.asks)
            initial_update_id = state.update_id
            initial_sequence_id = state.seq
            initial_replay_epoch = epoch
            initial_checkpoint_id = last_checkpoint_id or f"warmup:{state.update_id}:{state.seq}"
            saw_window = True
            events.append(
                {
                    "event_time": warmup_edt or window_start,
                    "received_at": None,
                    "event_type": "checkpoint",
                    "symbol": symbol,
                    "side": None,
                    "price": None,
                    "old_size": None,
                    "new_size": None,
                    "size_delta": None,
                    "notional_delta": None,
                    "u": state.update_id,
                    "pu": None,
                    "seq": state.seq,
                    "epoch": epoch,
                    "source": "full_ob",
                    "source_event_id": f"warmup:{state.update_id}:{state.seq}",
                    "ordering_flag": None,
                }
            )
            capture_due(window_start)

        capture_due(edt)

        if kind in {"checkpoint", "snapshot"}:
            before = sample_live_fullbook(
                state,
                cutoff=edt,
                label=f"immediately_before_{kind}",
                last_event_ts=last_event_ts,
                last_event_type=last_event_type,
                checkpoint_or_snapshot_id=last_checkpoint_id,
                replay_epoch=epoch,
                semantics=CUTOFF_EVENT_TIME_SEMANTICS,
            )
            bids_before = dict(state.bids)
            asks_before = dict(state.asks)
            u, seq, recv = apply_snapshot_kind(kind, payload, record, edt)
            apply_order += 1
            source_event_id = f"{kind}:{u}:{seq}:{edt.isoformat()}"
            last_event_ts = edt
            last_event_type = kind
            last_checkpoint_id = source_event_id
            emit_reset(
                kind=kind,
                edt=edt,
                u=u,
                seq=seq,
                recv=recv,
                order=apply_order,
                source_event_id=source_event_id,
            )
            after = sample_live_fullbook(
                state,
                cutoff=edt,
                label=f"immediately_after_{kind}",
                last_event_ts=last_event_ts,
                last_event_type=last_event_type,
                checkpoint_or_snapshot_id=source_event_id,
                replay_epoch=epoch,
                semantics="includes_reset_at_cutoff",
            )
            direct_reference.append(before)
            direct_reference.append(after)
            bid_depth_before = before.get("bid_depth_0_2bps") or 0.0
            ask_depth_before = before.get("ask_depth_0_2bps") or 0.0
            bid_depth_after = after.get("bid_depth_0_2bps") or 0.0
            ask_depth_after = after.get("ask_depth_0_2bps") or 0.0
            checkpoint_before_after.append(
                {
                    "event_time": edt,
                    "event_type": kind,
                    "source_event_id": source_event_id,
                    "update_id": u if u is not None else state.update_id,
                    "sequence_id": seq if seq is not None else state.seq,
                    "replay_epoch": epoch,
                    "before": before,
                    "snapshot": after,
                    "after": after,
                    "changed_bid_levels": _changed_levels(bids_before, state.bids),
                    "changed_ask_levels": _changed_levels(asks_before, state.asks),
                    "best_bid_before": before.get("best_bid"),
                    "best_ask_before": before.get("best_ask"),
                    "best_bid_after": after.get("best_bid"),
                    "best_ask_after": after.get("best_ask"),
                    "top_of_book_changed": (
                        before.get("best_bid") != after.get("best_bid")
                        or before.get("best_ask") != after.get("best_ask")
                    ),
                    "bid_depth_0_2bps_before": before.get("bid_depth_0_2bps"),
                    "ask_depth_0_2bps_before": before.get("ask_depth_0_2bps"),
                    "bid_depth_0_2bps_after": after.get("bid_depth_0_2bps"),
                    "ask_depth_0_2bps_after": after.get("ask_depth_0_2bps"),
                    "bid_depth_0_2bps_diff": bid_depth_after - bid_depth_before,
                    "ask_depth_0_2bps_diff": ask_depth_after - ask_depth_before,
                }
            )
            events.append(
                {
                    "event_time": edt,
                    "received_at": recv,
                    "event_type": kind,
                    "symbol": symbol,
                    "side": None,
                    "price": None,
                    "old_size": None,
                    "new_size": None,
                    "size_delta": None,
                    "notional_delta": None,
                    "u": u,
                    "pu": None,
                    "seq": seq,
                    "epoch": epoch,
                    "apply_order": apply_order,
                    "source": "full_ob",
                    "source_event_id": source_event_id,
                    "ordering_flag": None,
                }
            )
            continue

        if not anchored:
            continue
        apply_order += 1
        data = payload.get("data") or {}
        b_lvls = data.get("b") or []
        a_lvls = data.get("a") or []
        u = data.get("u")
        seq = data.get("seq")
        for px_raw, q_raw in b_lvls:
            px, nq = float(px_raw), float(q_raw)
            emit_level("bid", px, float(state.bids.get(px, 0.0)), nq, edt, record, u, seq, apply_order)
        for px_raw, q_raw in a_lvls:
            px, nq = float(px_raw), float(q_raw)
            emit_level("ask", px, float(state.asks.get(px, 0.0)), nq, edt, record, u, seq, apply_order)
        if prev_u is not None and u is not None:
            try:
                if int(u) not in {int(prev_u), int(prev_u) + 1}:
                    sequence_gaps += 1
            except (TypeError, ValueError):
                sequence_gaps += 1
        state.apply_delta(
            bids=b_lvls,
            asks=a_lvls,
            u=u,
            seq=seq,
            ts_ms=payload.get("ts") or data.get("ts"),
            cts_ms=payload.get("cts") or data.get("cts"),
            receive_time_ns=record.get("receive_time_ns"),
            enforce_continuity=False,
        )
        prev_u = state.update_id
        last_event_ts = edt
        last_event_type = "delta"
        events.append(
            {
                "event_time": edt,
                "received_at": record.get("receive_time_ns"),
                "event_type": "delta",
                "symbol": symbol,
                "side": None,
                "price": None,
                "old_size": None,
                "new_size": None,
                "size_delta": None,
                "notional_delta": None,
                "u": u,
                "pu": None,
                "seq": seq,
                "epoch": epoch,
                "apply_order": apply_order,
                "source": "full_ob",
                "source_event_id": f"delta:{u}:{seq}:{edt.isoformat()}",
                "ordering_flag": None,
            }
        )

    if not saw_window:
        # empty window but had warmup — still valid empty timeline
        if not anchored:
            return {
                "ok": False,
                "error": "NO_WARMUP_CHECKPOINT",
                "window_start": window_start,
                "window_end": window_end,
                "segments": [str(s) for s in segments],
            }
        initial_bids = dict(state.bids)
        initial_asks = dict(state.asks)
        initial_update_id = state.update_id
        initial_sequence_id = state.seq
        initial_replay_epoch = epoch
        initial_checkpoint_id = last_checkpoint_id
        capture_due(window_start)

    capture_due(window_end)

    if trades is None:
        trades = load_trades(symbol, window_start, window_end)
    else:
        trades = [t for t in trades if window_start <= t.trade_ts < window_end]

    for t in trades:
        events.append(
            {
                "event_time": t.trade_ts,
                "received_at": None,
                "event_type": "trade",
                "symbol": symbol,
                "side": t.side,
                "price": float(t.price),
                "old_size": None,
                "new_size": float(t.size),
                "size_delta": float(t.size),
                "notional_delta": float(t.notional),
                "u": None,
                "pu": None,
                "seq": None,
                "epoch": epoch,
                "source": "public_trades",
                "source_event_id": str(t.trade_id),
                "ordering_flag": None,
                "trade_side": t.side,
            }
        )

    timeline_events = events  # deltas/trades/checkpoints only
    timeline_events, ordering_confidence = sort_timeline(timeline_events)
    # level_changes sorted separately (not merged into primary timeline artifact)
    level_changes.sort(key=lambda e: (e["event_time"], e.get("apply_order", 0)))
    book_resets.sort(key=lambda e: (e["event_time"], e.get("apply_order", 0)))
    timeline = timeline_events  # compact

    book_ok = bool(state.book_ready and state.bids and state.asks)
    crossed = bool(book_ok and max(state.bids) >= min(state.asks))

    return {
        "ok": True,
        "window_start": window_start,
        "window_end": window_end,
        "segments": [str(s) for s in segments],
        "bytes_read": bytes_read,
        "timeline": timeline,
        "level_changes": level_changes,
        "book_resets": book_resets,
        "book_snapshots_by_time": book_snapshots_by_time,
        "checkpoint_before_after": checkpoint_before_after,
        "direct_reference": direct_reference,
        "initial_book_event_time_semantics": INITIAL_BOOK_EVENT_TIME_SEMANTICS,
        "cutoff_event_time_semantics": CUTOFF_EVENT_TIME_SEMANTICS,
        "trades": trades,
        "ordering_confidence": ordering_confidence,
        "sequence_gaps": sequence_gaps,
        "final_bids": dict(state.bids),
        "final_asks": dict(state.asks),
        "initial_bids": initial_bids,
        "initial_asks": initial_asks,
        "initial_update_id": initial_update_id,
        "initial_sequence_id": initial_sequence_id,
        "initial_replay_epoch": initial_replay_epoch,
        "initial_checkpoint_id": initial_checkpoint_id,
        "book_valid": book_ok and not crossed,
        "warmup_event_time": warmup_edt,
        "replay_epoch": epoch,
    }

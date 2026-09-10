"""100ms causal aggregates from timelines.

Timestamp contract
------------------
Bucket ``[bucket_start, bucket_end_exclusive)``.
The finished state is available at ``available_at = bucket_end_exclusive``.
It represents every book event with ``event_time < bucket_end_exclusive``.
An event exactly at ``bucket_end_exclusive`` belongs to the next bucket.

Initial book semantics: ``replay_window`` warmup applies events with
``event_time < window_start``. The initial book therefore contains
``event_time < evidence_start``, not ``<=``.

Checkpoint / snapshot resets are applied as full book replacements. They are
never expanded into synthetic level-deltas (that would pollute wall/removal
stats). ``_book_at`` does **not** apply resets and is not a golden oracle.
"""

from __future__ import annotations

import hashlib
import struct
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from orderbook_analyse.research.general_market_behavior_v1.features import band_depths

EVENT_TIME_SEMANTICS = "event_time_strict_lt"
MISSING_APPLY_ORDER = 10**9
RESET_KINDS = {"checkpoint", "snapshot", "checkpoint_reset", "snapshot_reset", "book_reset"}


def _near_band_depths(mid: float, bids: dict[float, float], asks: dict[float, float]) -> dict[str, float]:
    """Depth bands via single O(n) scan (no full-book sort)."""
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
        s = b + a
        out[f"depth_imbalance_bps_{k}"] = ((b - a) / s) if s > 1e-12 else 0.0
    return out


def _floor_bucket(dt: datetime, bucket_ms: int) -> datetime:
    dt = dt.astimezone(timezone.utc)
    ms = int(dt.timestamp() * 1000)
    floored = ms - (ms % bucket_ms)
    return datetime.fromtimestamp(floored / 1000.0, tz=timezone.utc)


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return pd.to_datetime(value, utc=True).to_pydatetime()


def _apply_change(bids: dict, asks: dict, e: dict) -> None:
    side = e["side"]
    px = float(e["price"])
    nq = float(e["new_size"])
    book = bids if side == "bid" else asks
    if nq <= 0:
        book.pop(px, None)
    else:
        book[px] = nq


_BOOK_PACK = struct.Struct("<Bdd")


def book_map_sha256(bids: dict[float, float], asks: dict[float, float]) -> str:
    """Canonical SHA-256 of the full price→qty maps. Empty/zero sizes omitted."""
    h = hashlib.sha256()
    bid_parts = [_BOOK_PACK.pack(0, float(p), float(q)) for p, q in sorted(bids.items()) if q and float(q) > 0]
    ask_parts = [_BOOK_PACK.pack(1, float(p), float(q)) for p, q in sorted(asks.items()) if q and float(q) > 0]
    if bid_parts:
        h.update(b"".join(bid_parts))
    if ask_parts:
        h.update(b"".join(ask_parts))
    return h.hexdigest()


def first_diverging_level(
    left_bids: dict[float, float],
    left_asks: dict[float, float],
    right_bids: dict[float, float],
    right_asks: dict[float, float],
) -> dict[str, Any] | None:
    for side, a, b in (("bid", left_bids, right_bids), ("ask", left_asks, right_asks)):
        keys = sorted(set(float(x) for x in a) | set(float(x) for x in b))
        for px in keys:
            lq = float(a.get(px) or 0.0)
            rq = float(b.get(px) or 0.0)
            if lq != rq:
                return {"side": side, "price": px, "left_qty": lq, "right_qty": rq}
    return None


def _as_level_map(levels: Any) -> dict[float, float]:
    out: dict[float, float] = {}
    if levels is None:
        return out
    if isinstance(levels, dict):
        items = levels.items()
    else:
        items = levels
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            px, qty = item[0], item[1]
        else:
            px, qty = item, levels[item]  # type: ignore[index]
        q = float(qty)
        if q > 0:
            out[float(px)] = q
    return out


def _apply_reset(bids: dict[float, float], asks: dict[float, float], reset: dict[str, Any]) -> None:
    bids.clear()
    asks.clear()
    bids.update(_as_level_map(reset.get("bids")))
    asks.update(_as_level_map(reset.get("asks")))


def _event_update_id(event: dict[str, Any]) -> Any:
    if event.get("update_id") is not None:
        return event.get("update_id")
    return event.get("u")


def _event_sequence_id(event: dict[str, Any]) -> Any:
    if event.get("sequence_id") is not None:
        return event.get("sequence_id")
    return event.get("seq")


def _event_epoch(event: dict[str, Any]) -> Any:
    if event.get("replay_epoch") is not None:
        return event.get("replay_epoch")
    return event.get("epoch")


def _event_checkpoint_id(event: dict[str, Any]) -> Any:
    for key in ("checkpoint_or_snapshot_id", "source_event_id", "source_checkpoint_id"):
        if event.get(key) not in (None, ""):
            return event.get(key)
    return None


def normalize_book_resets(
    book_resets: list[dict[str, Any]] | None,
    book_snapshots_by_time: list | None,
) -> list[dict[str, Any]]:
    """Prefer explicit reset events. Fall back to snapshot tuples. Never both."""
    if book_resets:
        return list(book_resets)
    out: list[dict[str, Any]] = []
    for i, item in enumerate(book_snapshots_by_time or []):
        if item is None:
            continue
        if isinstance(item, dict):
            rec = dict(item)
            rec.setdefault("apply_order", i)
            rec.setdefault("event_type", rec.get("event_type") or "snapshot_reset")
            out.append(rec)
            continue
        event_time = item[0]
        bids = item[1]
        asks = item[2]
        extra = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
        out.append(
            {
                "event_time": event_time,
                "event_type": extra.get("event_type") or "snapshot_reset",
                "bids": bids,
                "asks": asks,
                "update_id": extra.get("update_id", extra.get("u")),
                "sequence_id": extra.get("sequence_id", extra.get("seq")),
                "replay_epoch": extra.get("replay_epoch", extra.get("epoch")),
                "source": extra.get("source") or extra.get("event_type") or "snapshot",
                "source_event_id": extra.get("source_event_id"),
                "checkpoint_or_snapshot_id": extra.get("checkpoint_or_snapshot_id") or extra.get("source_event_id"),
                "apply_order": extra.get("apply_order", i),
            }
        )
    return out


def merge_book_events(
    level_changes: list[dict[str, Any]],
    book_resets: list[dict[str, Any]],
) -> list[tuple[datetime, int, int, int, str, dict[str, Any]]]:
    """Stable merge: (event_time, apply_order, kind_rank, orig_index, kind, payload).

    Resets sort before level-changes when apply_order is missing and timestamps tie.
    Replay always stamps apply_order so archive order is preserved.
    """
    items: list[tuple[datetime, int, int, int, str, dict[str, Any]]] = []
    for i, event in enumerate(level_changes or []):
        items.append(
            (
                _as_dt(event["event_time"]),
                int(event.get("apply_order", MISSING_APPLY_ORDER)),
                1,
                i,
                "change",
                event,
            )
        )
    for i, event in enumerate(book_resets or []):
        items.append(
            (
                _as_dt(event["event_time"]),
                int(event.get("apply_order", MISSING_APPLY_ORDER)),
                0,
                i,
                "reset",
                event,
            )
        )
    items.sort()
    return items


def reconstruct_book_asof_exclusive(
    *,
    initial_bids: dict[float, float],
    initial_asks: dict[float, float],
    level_changes: list[dict[str, Any]],
    until: datetime,
    book_resets: list[dict[str, Any]] | None = None,
    book_snapshots_by_time: list | None = None,
    evidence_start: datetime | None = None,
    initial_update_id: Any = None,
    initial_sequence_id: Any = None,
    initial_replay_epoch: Any = None,
    initial_checkpoint_id: Any = None,
) -> dict[str, Any]:
    """Checkpoint-capable book for ``event_time < until``. Not ``_book_at``."""
    until = _as_dt(until)
    ev_start = _as_dt(evidence_start) if evidence_start is not None else None
    bids = dict(initial_bids)
    asks = dict(initial_asks)
    resets = normalize_book_resets(book_resets, book_snapshots_by_time)
    last_u = initial_update_id
    last_seq = initial_sequence_id
    last_epoch = initial_replay_epoch
    last_cp = initial_checkpoint_id
    last_ts = None
    last_type = "initial_book"
    for et, _order, _kr, _idx, kind, payload in merge_book_events(level_changes, resets):
        if et >= until:
            break
        if ev_start is not None and et < ev_start:
            continue
        if kind == "reset":
            _apply_reset(bids, asks, payload)
            last_type = str(payload.get("event_type") or "snapshot_reset")
            cp = _event_checkpoint_id(payload)
            if cp is not None:
                last_cp = cp
        else:
            _apply_change(bids, asks, payload)
            last_type = str(payload.get("event_type") or "level_change")
        last_ts = et
        u = _event_update_id(payload)
        seq = _event_sequence_id(payload)
        epoch = _event_epoch(payload)
        if u is not None:
            last_u = u
        if seq is not None:
            last_seq = seq
        if epoch is not None:
            last_epoch = epoch
    bb = max(bids) if bids else None
    ba = min(asks) if asks else None
    return {
        "bids": bids,
        "asks": asks,
        "best_bid": bb,
        "best_ask": ba,
        "last_update_id": last_u,
        "sequence_id": last_seq,
        "replay_epoch": last_epoch,
        "checkpoint_or_snapshot_id": last_cp,
        "last_applied_event_ts": last_ts,
        "last_event_type": last_type,
        "until_exclusive": until,
        "event_time_semantics": EVENT_TIME_SEMANTICS,
        "state_source": "checkpoint_capable_event_stream",
    }


def last_complete_state(states: list[dict[str, Any]], asof: datetime) -> dict[str, Any] | None:
    """Last 100ms row whose finished state is available at or before ``asof``."""
    asof = _as_dt(asof)
    last = None
    for row in states:
        avail = row.get("available_at") or row.get("bucket_end_exclusive")
        if avail is None:
            continue
        if _as_dt(avail) <= asof:
            last = row
    return last


def _state_source(*, had_reset: bool, had_change: bool, reset_types: set[str], book_carried: bool) -> str:
    if had_reset and had_change:
        return "MIXED"
    if had_reset:
        if reset_types == {"checkpoint"} or reset_types == {"checkpoint_reset"}:
            return "CHECKPOINT_RESET"
        if reset_types == {"snapshot"} or reset_types == {"snapshot_reset"}:
            return "SNAPSHOT_RESET"
        return "BOOK_RESET"
    if had_change:
        return "LEVEL_CHANGES"
    if book_carried:
        return "CARRIED_FORWARD"
    return "LEVEL_CHANGES"


def build_states_100ms(
    *,
    window_start: datetime,
    window_end: datetime,
    timeline: list[dict[str, Any]],
    level_changes: list[dict[str, Any]],
    trades: list[Any],
    book_snapshots_by_time: list[tuple[datetime, dict[float, float], dict[float, float]]] | None,
    initial_bids: dict[float, float],
    initial_asks: dict[float, float],
    bucket_ms: int = 100,
    ordering_confidence: str = "ORDERING_DETERMINISTIC_CONTRACT",
    book_resets: list[dict[str, Any]] | None = None,
    evidence_start: datetime | None = None,
    initial_update_id: Any = None,
    initial_sequence_id: Any = None,
    initial_replay_epoch: Any = None,
    initial_checkpoint_id: Any = None,
) -> list[dict[str, Any]]:
    """Build dense 100ms buckets; carried-forward book marked explicitly.

    ``timeline`` and snapshot/reset parameters are consumed. Snapshot/checkpoint
    resets replace the whole book at ``event_time`` and are not turned into
    synthetic level-deltas.
    """
    window_start = window_start.astimezone(timezone.utc)
    window_end = window_end.astimezone(timezone.utc)
    ev_start = _as_dt(evidence_start) if evidence_start is not None else window_start
    bids = dict(initial_bids)
    asks = dict(initial_asks)
    resets = normalize_book_resets(book_resets, book_snapshots_by_time)
    book_events = merge_book_events(level_changes, resets)
    ei = 0

    timeline_rows = list(timeline or [])
    timeline_rows.sort(key=lambda e: _as_dt(e["event_time"]))
    timeline_times = [_as_dt(e["event_time"]) for e in timeline_rows]
    tli = 0

    trade_rows = list(trades or [])
    trade_rows.sort(key=lambda t: _as_dt(t.trade_ts if hasattr(t, "trade_ts") else t["event_time"]))
    trade_times = [_as_dt(t.trade_ts if hasattr(t, "trade_ts") else t["event_time"]) for t in trade_rows]
    ti = 0

    rows: list[dict[str, Any]] = []
    b = _floor_bucket(window_start, bucket_ms)
    last_bands: dict[str, float] = {}
    last_bb = last_ba = last_mid = last_spread = None
    last_u = initial_update_id
    last_seq = initial_sequence_id
    last_epoch = initial_replay_epoch
    last_cp = initial_checkpoint_id
    last_event_type = "initial_book"
    last_n_bid = last_n_ask = 0
    last_book_sha: str | None = None
    last_spread_px = None

    while b < window_end:
        b_end = b + timedelta(milliseconds=bucket_ms)
        effective_start = max(b, ev_start)
        bid_added = ask_added = bid_removed = ask_removed = 0.0
        bid_upd = ask_upd = 0
        book_carried = True
        had_reset = False
        had_change = False
        reset_types: set[str] = set()
        first_applied = None
        last_applied = None
        timeline_events_in_bucket = 0

        while tli < len(timeline_rows):
            tet = timeline_times[tli]
            if tet < effective_start:
                tli += 1
                continue
            if tet >= b_end:
                break
            timeline_events_in_bucket += 1
            tli += 1

        while ei < len(book_events):
            et, _order, _kr, _idx, kind, payload = book_events[ei]
            if ev_start is not None and et < ev_start:
                ei += 1
                continue
            if et < b:
                if kind == "reset":
                    _apply_reset(bids, asks, payload)
                    rt = str(payload.get("event_type") or "snapshot_reset")
                    last_event_type = rt
                    cp = _event_checkpoint_id(payload)
                    if cp is not None:
                        last_cp = cp
                else:
                    _apply_change(bids, asks, payload)
                    last_event_type = str(payload.get("event_type") or "level_change")
                u = _event_update_id(payload)
                seq = _event_sequence_id(payload)
                epoch = _event_epoch(payload)
                if u is not None:
                    last_u = u
                if seq is not None:
                    last_seq = seq
                if epoch is not None:
                    last_epoch = epoch
                book_carried = False
                ei += 1
                continue
            if et >= b_end:
                break
            if kind == "reset":
                _apply_reset(bids, asks, payload)
                rt = str(payload.get("event_type") or "snapshot_reset")
                last_event_type = rt
                reset_types.add(rt)
                had_reset = True
                book_carried = False
                cp = _event_checkpoint_id(payload)
                if cp is not None:
                    last_cp = cp
            else:
                sd = float(payload.get("size_delta") or 0.0)
                notion = abs(float(payload.get("notional_delta") or 0.0))
                if payload.get("side") == "bid":
                    bid_upd += 1
                    bid_added += notion if sd > 0 else 0.0
                    bid_removed += notion if sd < 0 else 0.0
                else:
                    ask_upd += 1
                    ask_added += notion if sd > 0 else 0.0
                    ask_removed += notion if sd < 0 else 0.0
                _apply_change(bids, asks, payload)
                last_event_type = str(payload.get("event_type") or "level_change")
                had_change = True
                book_carried = False
            if first_applied is None:
                first_applied = et
            last_applied = et
            u = _event_update_id(payload)
            seq = _event_sequence_id(payload)
            epoch = _event_epoch(payload)
            if u is not None:
                last_u = u
            if seq is not None:
                last_seq = seq
            if epoch is not None:
                last_epoch = epoch
            ei += 1

        taker_buy = taker_sell = 0.0
        trade_count = 0
        largest = 0.0
        while ti < len(trade_rows):
            tt = trade_times[ti]
            if tt < effective_start:
                ti += 1
                continue
            if tt >= b_end:
                break
            t = trade_rows[ti]
            side = t.side if hasattr(t, "side") else t.get("side")
            notion = float(t.notional if hasattr(t, "notional") else t.get("notional_delta") or 0)
            if str(side).lower() == "buy":
                taker_buy += notion
            else:
                taker_sell += notion
            trade_count += 1
            largest = max(largest, notion)
            ti += 1

        if (not book_carried) or last_mid is None:
            bb = max(bids) if bids else None
            ba = min(asks) if asks else None
            mid = ((bb + ba) / 2.0) if bb is not None and ba is not None else None
            spread_px = (ba - bb) if bb is not None and ba is not None else None
            spread_bps = ((ba - bb) / mid * 1e4) if mid and bb is not None and ba is not None else None
            bands = _near_band_depths(mid or 0.0, bids, asks) if mid else {}
            n_bid = len(bids)
            n_ask = len(asks)
            book_sha = book_map_sha256(bids, asks)
            last_bb, last_ba, last_mid, last_spread, last_bands = bb, ba, mid, spread_bps, bands
            last_n_bid, last_n_ask, last_book_sha, last_spread_px = n_bid, n_ask, book_sha, spread_px
        else:
            bb, ba, mid, spread_bps, bands = last_bb, last_ba, last_mid, last_spread, last_bands
            n_bid, n_ask, book_sha, spread_px = last_n_bid, last_n_ask, last_book_sha, last_spread_px

        book_valid = bool(bb is not None and ba is not None and bb < ba)
        crossed = bool(bb is not None and ba is not None and bb >= ba)
        rows.append(
            {
                "bucket_start": b,
                "bucket_end_exclusive": b_end,
                "available_at": b_end,
                "state_asof_exclusive": b_end,
                "effective_bucket_start": effective_start,
                "best_bid": bb,
                "best_ask": ba,
                "mid": mid,
                "mid_price": mid,
                "spread": spread_px,
                "spread_bps": spread_bps,
                "n_bid_levels": n_bid,
                "n_ask_levels": n_ask,
                "book_map_sha256": book_sha,
                "bid_depth_2bps": bands.get("bid_depth_notional_usdt_bps_0_2"),
                "ask_depth_2bps": bands.get("ask_depth_notional_usdt_bps_0_2"),
                "bid_depth_notional_usdt_bps_0_2": bands.get("bid_depth_notional_usdt_bps_0_2"),
                "ask_depth_notional_usdt_bps_0_2": bands.get("ask_depth_notional_usdt_bps_0_2"),
                "depth_imbalance_bps_0_2": bands.get("depth_imbalance_bps_0_2"),
                "bid_depth_5bps": bands.get("bid_depth_notional_usdt_bps_2_5"),
                "ask_depth_5bps": bands.get("ask_depth_notional_usdt_bps_2_5"),
                "bid_depth_10bps": bands.get("bid_depth_notional_usdt_bps_5_10"),
                "ask_depth_10bps": bands.get("ask_depth_notional_usdt_bps_5_10"),
                "depth_imbalance_10bps": bands.get("depth_imbalance_bps_5_10"),
                "bid_depth_notional_usdt_bps_2_5": bands.get("bid_depth_notional_usdt_bps_2_5"),
                "ask_depth_notional_usdt_bps_2_5": bands.get("ask_depth_notional_usdt_bps_2_5"),
                "depth_imbalance_bps_2_5": bands.get("depth_imbalance_bps_2_5"),
                "bid_depth_notional_usdt_bps_5_10": bands.get("bid_depth_notional_usdt_bps_5_10"),
                "ask_depth_notional_usdt_bps_5_10": bands.get("ask_depth_notional_usdt_bps_5_10"),
                "depth_imbalance_bps_5_10": bands.get("depth_imbalance_bps_5_10"),
                "bid_added_notional": bid_added,
                "ask_added_notional": ask_added,
                "bid_removed_notional": bid_removed,
                "ask_removed_notional": ask_removed,
                "bid_update_count": bid_upd,
                "ask_update_count": ask_upd,
                "taker_buy_notional": taker_buy,
                "taker_sell_notional": taker_sell,
                "taker_delta_notional": taker_buy - taker_sell,
                "trade_count": trade_count,
                "largest_trade_notional": largest,
                "book_valid": int(book_valid and not crossed),
                "crossed": crossed,
                "trade_source_valid": 1,
                "sequence_valid": 1,
                "ordering_confidence": ordering_confidence,
                "coverage_status": "DENSE_100MS",
                "book_carried_forward": int(book_carried),
                "first_applied_event_ts": first_applied,
                "last_applied_event_ts": last_applied,
                "last_update_id": last_u,
                "sequence_id": last_seq,
                "replay_epoch": last_epoch,
                "checkpoint_or_snapshot_id": last_cp,
                "state_source": _state_source(
                    had_reset=had_reset,
                    had_change=had_change,
                    reset_types=reset_types,
                    book_carried=book_carried,
                ),
                "last_event_type": last_event_type,
                "timeline_events_in_bucket": timeline_events_in_bucket,
                "event_time_semantics": EVENT_TIME_SEMANTICS,
            }
        )
        b = b_end
    return rows


def aggregate_100ms_to_1s(states_100ms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not states_100ms:
        return []
    df = pd.DataFrame(states_100ms)
    df["bucket_start"] = pd.to_datetime(df["bucket_start"], utc=True)
    df["sec"] = df["bucket_start"].dt.floor("s")
    rows = []
    for sec, g in df.groupby("sec", sort=True):
        last = g.iloc[-1]
        rows.append(
            {
                "state_ts": sec,
                "taker_buy_notional_usdt": float(g["taker_buy_notional"].sum()),
                "taker_sell_notional_usdt": float(g["taker_sell_notional"].sum()),
                "taker_delta_notional_usdt": float(g["taker_delta_notional"].sum()),
                "trade_count": int(g["trade_count"].sum()),
                "bid_liquidity_added_usdt": float(g["bid_added_notional"].sum()),
                "ask_liquidity_added_usdt": float(g["ask_added_notional"].sum()),
                "bid_liquidity_removed_usdt": float(g["bid_removed_notional"].sum()),
                "ask_liquidity_removed_usdt": float(g["ask_removed_notional"].sum()),
                "bid_update_count": int(g["bid_update_count"].sum()),
                "ask_update_count": int(g["ask_update_count"].sum()),
                "mid_price": float(last["mid_price"]) if pd.notna(last["mid_price"]) else None,
                "spread_bps": float(last["spread_bps"]) if pd.notna(last["spread_bps"]) else None,
                "bid_depth_notional_usdt_bps_0_2": float(last["bid_depth_2bps"])
                if pd.notna(last["bid_depth_2bps"])
                else None,
                "ask_depth_notional_usdt_bps_0_2": float(last["ask_depth_2bps"])
                if pd.notna(last["ask_depth_2bps"])
                else None,
            }
        )
    return rows

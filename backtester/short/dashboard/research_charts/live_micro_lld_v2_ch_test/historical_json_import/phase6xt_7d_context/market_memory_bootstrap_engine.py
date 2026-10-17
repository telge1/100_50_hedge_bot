"""SSOT-driven bootstrap / catch-up engine for MarketMemoryState (research only)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from research_charts.dynamic_big_picture_v2 import origin_logical_v2_key
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close

from market_memory_state import MarketMemoryState, _iso, _parse_utc

_EPS = 0.00012
_PDS = (1, 10)
_SIDES = ("lower", "upper")


def unique_dcs(start: datetime, end: datetime) -> list[datetime]:
    t = causal_15m_decision_close(start)
    out: list[datetime] = []
    while t <= end:
        out.append(t)
        t += timedelta(minutes=15)
    return out


def _member_origin_id(m: dict) -> str:
    return origin_logical_v2_key(m["pool_id"])


def _sort_key_event(ev: dict) -> tuple:
    return (
        _parse_utc(ev["known_at"]),
        _parse_utc(ev["decision_close"]),
        ev["pool_detail"],
        ev["side"],
        ev.get("origin_id") or "",
        ev.get("pool_id") or "",
        ev.get("event_kind") or "",
    )


def classify_lower_spatial(
    bottom: float,
    main_last_step_bottom: float | None,
) -> str | None:
    if main_last_step_bottom is None:
        return None
    if bottom > main_last_step_bottom + _EPS:
        return "new_spatial_step"
    return "within_zone_or_ambiguous"


def extract_birth_events(
    prev_ids: set[str],
    members: list[dict],
    pool_detail: int,
    decision_close: str,
) -> list[dict]:
    cur = {m["pool_id"]: m for m in members}
    births = []
    for pid in sorted(set(cur) - prev_ids):
        m = cur[pid]
        ka = m.get("known_at") or decision_close
        if isinstance(ka, datetime):
            ka = _iso(ka)
        side = m.get("side") or ("lower" if "lower" in pid else "upper")
        births.append(
            {
                "event_kind": "pool_birth",
                "pool_detail": pool_detail,
                "decision_close": decision_close,
                "known_at": str(ka).replace("+00:00", "Z") if "Z" not in str(ka) else str(ka),
                "pool_id": pid,
                "origin_id": _member_origin_id(m),
                "side": side,
                "bottom": float(m["lower_bound"]),
                "top": float(m["upper_bound"]),
                "strength": float(m["strength"]) if m.get("strength") is not None else None,
            }
        )
    return births


def extract_death_events(
    prev_ids: set[str],
    members: list[dict],
    pool_detail: int,
    decision_close: str,
) -> list[dict]:
    cur_ids = {m["pool_id"] for m in members}
    return [
        {
            "event_kind": "pool_loss",
            "pool_detail": pool_detail,
            "decision_close": decision_close,
            "known_at": decision_close,
            "pool_id": pid,
            "origin_id": origin_logical_v2_key(pid),
            "side": "lower" if "lower" in pid else "upper" if "upper" in pid else "unknown",
        }
        for pid in sorted(prev_ids - cur_ids)
    ]


def reference_origin_map(members: list[dict], side: str) -> dict[str, dict]:
    """Independent SSOT snapshot: active origins on side at decision_close."""
    out: dict[str, dict] = {}
    for m in members:
        if m.get("side") != side:
            continue
        oid = _member_origin_id(m)
        row = {
            "origin_id": oid,
            "bottom": float(m["lower_bound"]),
            "top": float(m["upper_bound"]),
            "strength": float(m["strength"]) if m.get("strength") is not None else None,
            "pool_ids": [m["pool_id"]],
        }
        if oid not in out:
            out[oid] = row
        elif m["pool_id"] not in out[oid]["pool_ids"]:
            out[oid]["pool_ids"].append(m["pool_id"])
    for oid in out:
        out[oid]["pool_ids"] = sorted(out[oid]["pool_ids"])
    return out


def apply_ssot_event(
    state: MarketMemoryState,
    ev: dict,
    seen_origin: dict[int, set[str]],
    main_last_step_bottom: dict[int, float | None],
    main_step_seq: dict[int, int],
    spatial_override: dict[tuple[int, str, str], dict] | None = None,
) -> None:
    pd = ev["pool_detail"]
    dc = ev["decision_close"]
    if ev["event_kind"] == "pool_loss":
        state.record_pool_loss(pd, ev["pool_id"], ev["origin_id"], ev.get("side", "lower"), dc)
        return

    ka = ev["known_at"]
    if _parse_utc(ka) > _parse_utc(dc):
        return
    oid = ev["origin_id"]
    is_new = oid not in seen_origin[pd]
    if oid in state.origins[pd] or oid in state.upper_origins[pd]:
        is_new = False
    if is_new:
        seen_origin[pd].add(oid)
    elif oid not in seen_origin[pd]:
        seen_origin[pd].add(oid)

    spatial_kind = None
    step_id = None
    if ev["side"] == "lower":
        key = (pd, oid, dc)
        if spatial_override and key in spatial_override:
            spatial_kind = spatial_override[key].get("kind")
            step_id = spatial_override[key].get("step_id")
        elif is_new:
            sealed_window = _parse_utc(dc) >= datetime(2026, 7, 1, tzinfo=timezone.utc)
            if spatial_override and sealed_window:
                spatial_kind = "within_zone_or_ambiguous"
            else:
                spatial_kind = classify_lower_spatial(ev["bottom"], main_last_step_bottom[pd])
            if spatial_kind == "new_spatial_step":
                main_step_seq[pd] += 1
                step_id = f"bootstrap:PD{pd}:S{main_step_seq[pd]}"
        if spatial_kind == "new_spatial_step" and step_id and is_new:
            main_last_step_bottom[pd] = ev["bottom"]

        state.apply_lower_pool_event(
            pool_detail=pd,
            origin_id=oid,
            pool_id=ev["pool_id"],
            known_at=ka,
            decision_close=dc,
            bottom=ev["bottom"],
            top=ev["top"],
            strength=ev["strength"],
            is_new_origin=is_new,
            spatial_kind=spatial_kind,
            step_id=step_id,
        )
    else:
        state.apply_upper_pool_event(
            pool_detail=pd,
            origin_id=oid,
            pool_id=ev["pool_id"],
            known_at=ka,
            decision_close=dc,
            bottom=ev["bottom"],
            top=ev["top"],
            strength=ev["strength"],
            is_new_origin=is_new,
        )


def _sync_staircase_to_memory(
    memory: MarketMemoryState,
    seen_origin: dict[int, set[str]],
    main_last_step_bottom: dict[int, float | None],
    main_step_seq: dict[int, int],
    main_step_initialized: dict[int, bool],
) -> None:
    for pd in _PDS:
        memory.seen_origins[pd] = set(seen_origin[pd])
        memory.main_last_step_bottom[pd] = main_last_step_bottom[pd]
        memory.main_step_seq[pd] = main_step_seq[pd]
        memory.main_step_initialized[pd] = main_step_initialized[pd]


def walk_ssot(
    dcs: list[datetime],
    load_members_fn: Callable,
    client,
    db: str,
    memory: MarketMemoryState | None = None,
    spatial_override: dict | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    snapshot_at: list[datetime] | None = None,
    bars_5m: dict[datetime, dict] | None = None,
    price_processor: Any | None = None,
    bar_cursor: datetime | None = None,
) -> tuple[MarketMemoryState | None, dict[str, Any], list[dict]]:
    """
    Chronological SSOT walk.
    If memory is None, only builds reference birth ledger (independent full replay).
    """
    prev_ids: dict[int, set[str]] = {pd: set() for pd in _PDS}
    if memory:
        seen_origin = {pd: set(memory.seen_origins[pd]) for pd in _PDS}
        main_last_step_bottom = {pd: memory.main_last_step_bottom.get(pd) for pd in _PDS}
        main_step_initialized = {pd: bool(memory.main_step_initialized.get(pd)) for pd in _PDS}
        main_step_seq = {pd: int(memory.main_step_seq.get(pd, 0)) for pd in _PDS}
    else:
        seen_origin: dict[int, set[str]] = {pd: set() for pd in _PDS}
        main_last_step_bottom: dict[int, float | None] = {pd: None for pd in _PDS}
        main_step_initialized: dict[int, bool] = {pd: False for pd in _PDS}
        main_step_seq: dict[int, int] = {pd: 0 for pd in _PDS}
    birth_ledger: dict[int, dict[str, dict]] = {pd: {} for pd in _PDS}
    all_events: list[dict] = []
    cache: dict = getattr(walk_ssot, "_shared_member_cache", {})
    walk_ssot._shared_member_cache = cache
    processed = 0
    snapshots: dict[str, dict] = {}
    snap_set = {_iso(x) for x in (snapshot_at or [])}
    bar_times = sorted(bars_5m.keys()) if bars_5m else []
    bar_idx = 0
    if bar_cursor is None and dcs:
        if memory and price_processor and price_processor.watermark_5m:
            bar_cursor = _parse_utc(price_processor.watermark_5m)
        elif memory and memory.watermark_utc:
            bar_cursor = _parse_utc(memory.watermark_utc)
        else:
            bar_cursor = dcs[0] - timedelta(minutes=15)

    def _process_5m_until(until: datetime) -> None:
        nonlocal bar_idx, bar_cursor
        if not bars_5m or not price_processor or not memory:
            return
        while bar_idx < len(bar_times) and bar_times[bar_idx] <= until:
            t = bar_times[bar_idx]
            if bar_cursor is not None and t > bar_cursor:
                price_processor.process_bar(memory, t, bars_5m[t])
            bar_idx += 1
        bar_cursor = until

    for i, dc in enumerate(dcs):
        dc_s = _iso(dc)
        _process_5m_until(dc)
        for pd in _PDS:
            members = load_members_fn(cache, dc, pd, client, db)
            if memory and not main_step_initialized[pd] and not memory.seen_origins[pd]:
                price_support = None
                for m in members:
                    if m.get("side") != "lower":
                        continue
                    t = float(m["upper_bound"])
                    if price_support is None or t > price_support:
                        price_support = t
                main_last_step_bottom[pd] = price_support
                main_step_initialized[pd] = True
                memory.main_last_step_bottom[pd] = price_support
                memory.main_step_initialized[pd] = True

            births = extract_birth_events(prev_ids[pd], members, pd, dc_s)
            deaths = extract_death_events(prev_ids[pd], members, pd, dc_s)
            for ev in births + deaths:
                all_events.append(ev)
                if memory:
                    apply_ssot_event(
                        memory,
                        ev,
                        seen_origin,
                        main_last_step_bottom,
                        main_step_seq,
                        spatial_override,
                    )
                if (
                    ev["event_kind"] == "pool_birth"
                    and ev["side"] in _SIDES
                    and _parse_utc(ev["known_at"]) <= _parse_utc(ev["decision_close"])
                ):
                    oid = ev["origin_id"]
                    if oid not in birth_ledger[pd]:
                        birth_ledger[pd][oid] = {
                            "first_causal_available_at": ev["known_at"],
                            "birth_decision_close": ev["decision_close"],
                            "bottom": ev["bottom"],
                            "top": ev["top"],
                            "strength": ev["strength"],
                            "side": ev["side"],
                        }
            prev_ids[pd] = {m["pool_id"] for m in members}
            if memory:
                memory.note_active_pools(pd, prev_ids[pd])
                memory.processed_publications += 1
                _sync_staircase_to_memory(
                    memory, seen_origin, main_last_step_bottom, main_step_seq, main_step_initialized
                )

        processed += 1
        if memory:
            memory._advance_asof(dc_s)
            memory.watermark_utc = dc_s
            if price_processor:
                memory.price_processor = price_processor.to_dict()
            if dc_s in snap_set:
                snapshots[dc_s] = memory.checkpoint()
        if progress_cb and (i == 0 or i % 96 == 0):
            progress_cb(i + 1, len(dcs))

    if memory and price_processor:
        price_processor.flush_pending_drop(memory)
        memory.price_processor = price_processor.to_dict()

    meta = {
        "decision_closes_processed": len(dcs),
        "events_total": len(all_events),
        "birth_ledger": birth_ledger,
        "snapshots": snapshots,
        "bar_cursor_end": _iso(bar_cursor) if bar_cursor else None,
    }
    return memory, meta, all_events


def ledger_from_memory(state: MarketMemoryState) -> dict[int, dict[str, dict]]:
    out: dict[int, dict[str, dict]] = {pd: {} for pd in _PDS}
    for pd in _PDS:
        for side_key, store in (("lower", state.origins[pd]), ("upper", state.upper_origins[pd])):
            for oid, rec in store.items():
                out[pd][oid] = {
                    "first_causal_available_at": rec.first_causal_available_at,
                    "birth_decision_close": rec.birth_decision_close,
                    "bottom": rec.bottom,
                    "top": rec.top,
                    "strength": rec.strength,
                    "side": side_key,
                }
    return out


def parity_diff(
    reference: dict[int, dict[str, dict]],
    incremental: dict[int, dict[str, dict]],
) -> dict[str, Any]:
    diffs = []
    for pd in _PDS:
        ref_ids = set(reference[pd])
        inc_ids = set(incremental[pd])
        for oid in sorted(ref_ids - inc_ids):
            diffs.append({"pd": pd, "origin_id": oid, "issue": "missing_in_incremental"})
        for oid in sorted(inc_ids - ref_ids):
            diffs.append({"pd": pd, "origin_id": oid, "issue": "extra_in_incremental"})
        for oid in sorted(ref_ids & inc_ids):
            r, g = reference[pd][oid], incremental[pd][oid]
            for field in ("first_causal_available_at", "birth_decision_close", "bottom", "top"):
                rv, gv = r.get(field), g.get(field)
                if field in ("bottom", "top"):
                    try:
                        if abs(float(rv) - float(gv)) <= 1e-9:
                            continue
                    except (TypeError, ValueError):
                        pass
                if rv != gv:
                    diffs.append(
                        {
                            "pd": pd,
                            "origin_id": oid,
                            "issue": f"field_mismatch:{field}",
                            "reference": r.get(field),
                            "incremental": g.get(field),
                        }
                    )
    return {"match": len(diffs) == 0, "diff_count": len(diffs), "diffs": diffs[:200]}


def events_fingerprint(events: list[dict]) -> str:
    h = hashlib.sha256()
    for ev in sorted(events, key=_sort_key_event):
        h.update(json.dumps(ev, sort_keys=True, default=str).encode())
    return h.hexdigest()

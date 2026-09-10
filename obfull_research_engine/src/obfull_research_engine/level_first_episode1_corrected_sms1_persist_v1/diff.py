"""Classify old sms1 derived events vs new corrected events. Old is not an oracle."""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from ..timeparse import format_utc_z
from .io_zst import body_rows, read_jsonl_zst
from . import SMS1_EP1_REFILLS, SMS1_EP1_WALLS
from ..paths import ENGINE_ROOT

CLASSES = (
    "UNCHANGED",
    "TIMESTAMP_SHIFT",
    "EPOCH_CORRECTION",
    "BOOK_STATE_CORRECTION",
    "ADDED_EVENT",
    "REMOVED_EVENT",
    "ATTRIBUTE_CHANGE",
)


def _load_old(rel: str) -> list[dict[str, Any]]:
    path = ENGINE_ROOT / rel
    if not path.is_file():
        return []
    return body_rows(read_jsonl_zst(path))


def _num(v: Any) -> Any:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _eq_num(a: Any, b: Any, eps: float = 1e-6) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= eps
    except (TypeError, ValueError):
        return a == b


def _classify_pair(*, old: dict[str, Any] | None, new: dict[str, Any] | None, event_type: str) -> dict[str, Any]:
    if old is None and new is not None:
        klass = "ADDED_EVENT"
    elif old is not None and new is None:
        klass = "REMOVED_EVENT"
    else:
        assert old is not None and new is not None
        if event_type == "WALL":
            old_ts = old.get("first_seen_ts")
            new_ts = new.get("first_seen_ts")
        else:
            old_ts = old.get("event_time") or old.get("first_seen_ts") or old.get("first_touch_ts")
            new_ts = new.get("event_time") or new.get("first_seen_ts")
        old_avail = old.get("available_at")
        new_avail = new.get("available_at")
        old_epoch = old.get("replay_epoch")
        new_epoch = new.get("replay_epoch")
        old_px = _num(old.get("price"))
        new_px = _num(new.get("price"))
        old_side = old.get("side")
        new_side = new.get("side")
        size_keys = (
            "notional",
            "initial_notional",
            "removed_notional",
            "refilled_notional",
            "refilled_same_level_notional",
            "new_size",
            "change",
            "best_bid",
            "best_ask",
            "wall_status",
            "refill_type",
        )
        book_diff = False
        schema_only = False
        for k in size_keys:
            if k not in old and k not in new:
                continue
            if k not in old or k not in new:
                schema_only = True
                continue
            if not _eq_num(old.get(k), new.get(k)) and old.get(k) != new.get(k):
                book_diff = True
                break
        if old.get("size") != new.get("size") and (old.get("size") is None) != (new.get("size") is None):
            schema_only = True
        ts_diff = False
        if old_ts and new_ts:
            try:
                ts_diff = _as_dt(old_ts) != _as_dt(new_ts)
            except Exception:  # noqa: BLE001
                ts_diff = str(old_ts) != str(new_ts)
        epoch_diff = old_epoch is not None and new_epoch is not None and old_epoch != new_epoch
        epoch_filled = old_epoch is None and new_epoch is not None
        avail_filled = old_avail is None and new_avail is not None
        if not ts_diff and not book_diff and not epoch_diff and old_px == new_px and old_side == new_side:
            if epoch_filled or avail_filled or schema_only:
                klass = "ATTRIBUTE_CHANGE"
            else:
                klass = "UNCHANGED"
        elif book_diff:
            klass = "BOOK_STATE_CORRECTION"
        elif ts_diff and not book_diff:
            klass = "TIMESTAMP_SHIFT"
        elif epoch_diff and not book_diff:
            klass = "EPOCH_CORRECTION"
        else:
            klass = "ATTRIBUTE_CHANGE"
    src_old = old or {}
    src_new = new or {}
    return {
        "class": klass,
        "event_type": event_type,
        "event_id_old": src_old.get("event_id") or src_old.get("wall_id"),
        "event_id_new": src_new.get("event_id") or src_new.get("wall_id"),
        "old_event_time": src_old.get("event_time") or src_old.get("first_seen_ts"),
        "new_event_time": src_new.get("event_time") or src_new.get("first_seen_ts"),
        "old_available_at": src_old.get("available_at"),
        "new_available_at": src_new.get("available_at"),
        "old_replay_epoch": src_old.get("replay_epoch"),
        "new_replay_epoch": src_new.get("replay_epoch"),
        "price": src_new.get("price") if src_new else src_old.get("price"),
        "side": src_new.get("side") if src_new else src_old.get("side"),
        "old_size": src_old.get("size") or src_old.get("notional") or src_old.get("initial_notional") or src_old.get("best_bid"),
        "new_size": src_new.get("size") or src_new.get("notional") or src_new.get("initial_notional") or src_new.get("best_bid"),
        "underlying_100ms_cutoff": src_new.get("underlying_100ms_cutoff") or src_new.get("available_at"),
        "causal_raw_event": src_new.get("causal_source_event_id") or src_new.get("causal_event_time"),
        "explanation": None,
    }


def diff_walls(new_walls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old = _load_old(SMS1_EP1_WALLS)
    old_by = {(str(r.get("side")), float(r["price"])): r for r in old if r.get("price") is not None}
    new_by = {(str(r.get("side")), float(r["price"])): r for r in new_walls if r.get("price") is not None}
    keys = sorted(set(old_by) | set(new_by))
    out = []
    for k in keys:
        rec = _classify_pair(old=old_by.get(k), new=new_by.get(k), event_type="WALL")
        rec["explanation"] = _explain_wall(rec, old_by.get(k), new_by.get(k))
        out.append(rec)
    return out


def _explain_wall(rec: dict[str, Any], old: dict[str, Any] | None, new: dict[str, Any] | None) -> str:
    if rec["class"] == "UNCHANGED":
        return "Same wall identity, timestamps, and notionals. Touch book ToB unchanged on this identity-reset episode."
    if rec["class"] == "ADDED_EVENT":
        return "Wall present on corrected touch book, absent from old sms1 walls."
    if rec["class"] == "REMOVED_EVENT":
        return "Wall present on old _book_at touch book, absent from corrected reconstruct_book_asof_exclusive touch book."
    if rec["class"] == "ATTRIBUTE_CHANGE":
        return (
            "Core wall payload matches; new rows add available_at / replay_epoch / state_source "
            "from the checkpoint-capable stream. Old sms1 walls had no availability contract."
        )
    if rec["class"] == "EPOCH_CORRECTION":
        return "Wall identity unchanged; replay_epoch now taken from the reset stream at touch instead of a missing/final stamp."
    if rec["class"] == "TIMESTAMP_SHIFT":
        return "Wall first_seen/event_time shifted because availability is now bucket_end_exclusive / touch as-of."
    if rec["class"] == "BOOK_STATE_CORRECTION":
        return "Touch/detection book from reconstruct_book_asof_exclusive changed notional or wall_status versus snapshot-blind _book_at."
    return rec["class"]


def diff_refills(new_refills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old = _load_old(SMS1_EP1_REFILLS)
    def key(r: dict[str, Any]) -> tuple:
        src = str(r.get("source") or "")
        rtype = str(r.get("refill_type") or r.get("event_type") or "")
        et = str(r.get("event_time") or "")
        side = str(r.get("side") or "")
        eid = str(r.get("event_id") or "")
        px = r.get("original_price") if r.get("original_price") is not None else r.get("price")
        return (src, rtype, et, side, str(px), eid)

    old_by = {key(r): r for r in old}
    new_by = {key(r): r for r in new_refills}
    keys = sorted(set(old_by) | set(new_by))
    out = []
    for k in keys:
        etype = "WALL_REFILL" if (new_by.get(k) or old_by.get(k) or {}).get("source") == "detect_refills" else "WALL_LEVEL_CHANGE"
        rec = _classify_pair(old=old_by.get(k), new=new_by.get(k), event_type=etype)
        rec["explanation"] = _explain_refill(rec)
        out.append(rec)
    return out


def _explain_refill(rec: dict[str, Any]) -> str:
    if rec["class"] == "UNCHANGED":
        return "Same level-change/refill identity. Resets on this episode did not invent extra level-deltas."
    if rec["class"] == "ATTRIBUTE_CHANGE":
        return (
            "Same refill/level-change payload; new rows persist available_at = bucket_end_exclusive "
            "and per-event replay_epoch. Old refill_removal had neither."
        )
    if rec["class"] == "TIMESTAMP_SHIFT":
        return "Event assigned to a different 100ms bucket after exclusive-end / available_at correction."
    if rec["class"] == "EPOCH_CORRECTION":
        return "Refill/level-change now carries the replay_epoch of its bucket instead of the final persist stamp."
    if rec["class"] == "ADDED_EVENT":
        return "Present only after rebuild from the corrected sms1 stream."
    if rec["class"] == "REMOVED_EVENT":
        return "Present only on old sms1 persist; not produced from the corrected stream."
    if rec["class"] == "BOOK_STATE_CORRECTION":
        return "Size/notional/refill_type changed because the wall set or mid_at_touch came from the corrected book."
    return rec["class"]


def diff_touch_detection(
    *,
    new_touch: dict[str, Any],
    new_det: dict[str, Any],
    old_touch: dict[str, Any],
    old_det: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    for event_type, old, new in (("TOUCH", old_touch, new_touch), ("DETECTION", old_det, new_det)):
        rec = _classify_pair(old=old, new=new, event_type=event_type)
        if rec["class"] == "UNCHANGED" and old.get("replay_epoch") != new.get("replay_epoch"):
            rec["class"] = "EPOCH_CORRECTION"
        if rec["class"] == "ATTRIBUTE_CHANGE" and old.get("best_bid") == new.get("best_bid") and old.get("best_ask") == new.get("best_ask"):
            rec["explanation"] = (
                "ToB matches snapshot-blind _book_at on this identity-reset episode. "
                "New event persists available_at, replay_epoch, book_map_sha256, and the checkpoint-capable stream id. "
                "Old persist had no touch/detection table; comparison uses a reconstructed _book_at snapshot."
            )
        elif rec["class"] == "EPOCH_CORRECTION":
            rec["explanation"] = "ToB unchanged; replay_epoch now taken from the reset-aware stream instead of missing/final stamp."
        elif rec["class"] == "BOOK_STATE_CORRECTION":
            rec["explanation"] = "Touch/detection book differs versus _book_at because checkpoints are applied as full replacements."
        elif rec["class"] == "TIMESTAMP_SHIFT":
            rec["explanation"] = "Event timestamp/available_at shifted onto the exclusive-end contract."
        else:
            rec["explanation"] = rec["explanation"] or rec["class"]
        rec["causal_raw_event"] = new.get("causal_event_time") or new.get("causal_source_event_id")
        rec["underlying_100ms_cutoff"] = new.get("underlying_100ms_cutoff")
        rows.append(rec)
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {k: 0 for k in CLASSES}
    for r in rows:
        counts[str(r.get("class") or "ATTRIBUTE_CHANGE")] = counts.get(str(r.get("class")), 0) + 1
    counts["TOTAL"] = len(rows)
    return counts

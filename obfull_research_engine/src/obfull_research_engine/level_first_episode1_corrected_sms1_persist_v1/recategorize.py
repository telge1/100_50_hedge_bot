"""Field-level old/new classification. A row may carry several atomic categories."""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from .diff import _eq_num, _load_old, _num
from . import SMS1_EP1_REFILLS, SMS1_EP1_WALLS

TIME_FIELDS = {
    "event_time",
    "available_at",
    "event_available_at",
    "state_available_at",
    "detected_at",
    "first_seen_ts",
    "last_seen_ts",
    "last_change_ts",
    "first_removal_ts",
    "last_removal_ts",
    "removed_ts",
    "underlying_100ms_cutoff",
    "underlying_100ms_available_at",
    "first_touch_ts",
}
EPOCH_FIELDS = {"replay_epoch"}
# Content fields: BOOK_STATE_CORRECTION only when both old and new have values and differ.
# Newly filled metadata (including book_map_sha256 first appearance) → ATTRIBUTE_CHANGE.
BOOK_CONTENT_FIELDS = {
    "price",
    "size",
    "qty",
    "side",
    "best_bid",
    "best_ask",
    "notional",
    "initial_notional",
    "new_size",
    "old_size",
    "previous_size",
    "change",
    "removed_notional",
    "refilled_notional",
    "refilled_same_level_notional",
    "mid",
    "spread",
    "n_bid_levels",
    "n_ask_levels",
}
BOOK_HASH_FIELDS = {"book_map_sha256"}
BOOK_FIELDS = BOOK_CONTENT_FIELDS | BOOK_HASH_FIELDS


def _ts_eq(a: Any, b: Any) -> bool:
    if a in (None, "") and b in (None, ""):
        return True
    if a in (None, "") or b in (None, ""):
        return False
    try:
        return _as_dt(a) == _as_dt(b)
    except Exception:  # noqa: BLE001
        return str(a) == str(b)


def classify_fields(*, old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    if old is None and new is not None:
        return {
            "record_classes": ["ADDED_EVENT"],
            "field_changes": [{"field": "*", "class": "ADDED_EVENT"}],
        }
    if old is not None and new is None:
        return {
            "record_classes": ["REMOVED_EVENT"],
            "field_changes": [{"field": "*", "class": "REMOVED_EVENT"}],
        }
    assert old is not None and new is not None
    keys = sorted(set(old) | set(new))
    changes: list[dict[str, Any]] = []
    for key in keys:
        if key in {"event_id", "wall_id", "source_thresholds_config_hash", "state_source", "book_stream"}:
            ov, nv = old.get(key), new.get(key)
            if ov != nv and not (ov in (None, "") and nv not in (None, "")):
                changes.append({"field": key, "class": "ATTRIBUTE_CHANGE", "old": ov, "new": nv})
            elif ov in (None, "") and nv not in (None, ""):
                changes.append({"field": key, "class": "ATTRIBUTE_CHANGE", "old": ov, "new": nv})
            continue
        ov, nv = old.get(key), new.get(key)
        if key in TIME_FIELDS:
            if _ts_eq(ov, nv):
                continue
            changes.append({"field": key, "class": "TIMESTAMP_SHIFT", "old": ov, "new": nv})
            continue
        if key in EPOCH_FIELDS:
            if ov == nv:
                continue
            changes.append({"field": key, "class": "EPOCH_CORRECTION", "old": ov, "new": nv})
            continue
        if key in BOOK_FIELDS:
            if ov in (None, "") and nv not in (None, ""):
                # Newly available field (e.g. first book_map_sha256) is metadata, not a book correction.
                changes.append({"field": key, "class": "ATTRIBUTE_CHANGE", "old": ov, "new": nv})
                continue
            if _eq_num(ov, nv) or ov == nv:
                continue
            if ov not in (None, "") and nv not in (None, ""):
                changes.append({"field": key, "class": "BOOK_STATE_CORRECTION", "old": ov, "new": nv})
            else:
                changes.append({"field": key, "class": "ATTRIBUTE_CHANGE", "old": ov, "new": nv})
            continue
        if ov == nv or _eq_num(ov, nv):
            continue
        if ov in (None, "") and nv not in (None, ""):
            if key in EPOCH_FIELDS:
                klass = "EPOCH_CORRECTION"
            elif key in TIME_FIELDS:
                klass = "TIMESTAMP_SHIFT"
            else:
                klass = "ATTRIBUTE_CHANGE"
            changes.append({"field": key, "class": klass, "old": ov, "new": nv})
            continue
        changes.append({"field": key, "class": "ATTRIBUTE_CHANGE", "old": ov, "new": nv})
    classes = sorted({c["class"] for c in changes}) if changes else ["UNCHANGED"]
    return {"record_classes": classes, "field_changes": changes}


def recategorize_walls_and_refills(new_walls: list[dict[str, Any]], new_level_changes: list[dict[str, Any]]) -> dict[str, Any]:
    old_walls = _load_old(SMS1_EP1_WALLS)
    old_refills = _load_old(SMS1_EP1_REFILLS)
    wall_old = {(str(r.get("side")), _num(r.get("price"))): r for r in old_walls if r.get("price") is not None}
    wall_new = {(str(r.get("side")), _num(r.get("price"))): r for r in new_walls if r.get("price") is not None}
    records = []
    field_counts = {
        "UNCHANGED": 0,
        "TIMESTAMP_SHIFT": 0,
        "EPOCH_CORRECTION": 0,
        "BOOK_STATE_CORRECTION": 0,
        "ADDED_EVENT": 0,
        "REMOVED_EVENT": 0,
        "ATTRIBUTE_CHANGE": 0,
    }
    record_counts = dict(field_counts)
    multi = 0
    examples: dict[str, list] = {k: [] for k in field_counts}

    def _add(kind: str, old, new):
        nonlocal multi
        rec = classify_fields(old=old, new=new)
        rec["dataset"] = kind
        rec["price"] = (new or old or {}).get("price")
        rec["side"] = (new or old or {}).get("side")
        rec["event_id"] = (new or old or {}).get("event_id") or (new or old or {}).get("wall_id")
        classes = rec["record_classes"]
        if len(classes) > 1:
            multi += 1
        if classes == ["UNCHANGED"]:
            record_counts["UNCHANGED"] += 1
        else:
            seen = set()
            for c in classes:
                record_counts[c] = record_counts.get(c, 0) + 1
                seen.add(c)
        for ch in rec["field_changes"]:
            field_counts[ch["class"]] = field_counts.get(ch["class"], 0) + 1
            bucket = examples[ch["class"]]
            if len(bucket) < 3:
                bucket.append({"dataset": kind, "field": ch["field"], "old": ch.get("old"), "new": ch.get("new"), "event_id": rec["event_id"]})
        records.append(rec)

    wall_keys = sorted(set(wall_old) | set(wall_new), key=str)
    n_walls_paired = 0
    n_walls_added = 0
    n_walls_removed = 0
    for k in wall_keys:
        o, n = wall_old.get(k), wall_new.get(k)
        if o is not None and n is not None:
            n_walls_paired += 1
        elif o is None and n is not None:
            n_walls_added += 1
        elif o is not None and n is None:
            n_walls_removed += 1
        _add("WALL", o, n)

    def lk(r: dict[str, Any]) -> tuple:
        return (
            str(r.get("event_time") or ""),
            str(r.get("side") or ""),
            str(r.get("price") if r.get("price") is not None else r.get("original_price") or ""),
            str(r.get("source_event_id") or r.get("event_id") or ""),
        )

    old_lc = {lk(r): r for r in old_refills if str(r.get("source") or "") != "detect_refills"}
    new_lc = {lk(r): r for r in new_level_changes}
    lc_keys = sorted(set(old_lc) | set(new_lc), key=str)
    n_lc_paired = n_lc_added = n_lc_removed = 0
    for k in lc_keys:
        o, n = old_lc.get(k), new_lc.get(k)
        if o is not None and n is not None:
            n_lc_paired += 1
        elif o is None and n is not None:
            n_lc_added += 1
        elif o is not None and n is None:
            n_lc_removed += 1
        _add("LEVEL_CHANGE", o, n)

    n_old = len(wall_old) + len(old_lc)
    n_new = len(wall_new) + len(new_lc)
    n_paired = n_walls_paired + n_lc_paired
    n_added = n_walls_added + n_lc_added
    n_removed = n_walls_removed + n_lc_removed
    assert n_old == n_paired + n_removed
    assert n_new == n_paired + n_added
    assert len(records) == n_paired + n_added + n_removed

    return {
        "n_records": len(records),
        "n_old_unique": n_old,
        "n_new_unique": n_new,
        "n_paired": n_paired,
        "n_added": n_added,
        "n_removed": n_removed,
        "n_walls_old": len(wall_old),
        "n_walls_new": len(wall_new),
        "n_level_changes_old": len(old_lc),
        "n_level_changes_new": len(new_lc),
        "n_records_with_multiple_classes": multi,
        "record_class_counts": record_counts,
        "field_change_counts": field_counts,
        "examples": {k: v for k, v in examples.items() if v},
        "note": (
            "replay_epoch fill/change → EPOCH_CORRECTION; causal time fields → TIMESTAMP_SHIFT; "
            "price/size/side/notional changes when both sides present → BOOK_STATE_CORRECTION; "
            "newly filled hash/qty/metadata → ATTRIBUTE_CHANGE. "
            "A record may belong to several classes when several fields change. "
            "n_records = n_paired + n_added + n_removed = n_old_unique + n_added = n_new_unique + n_removed."
        ),
    }

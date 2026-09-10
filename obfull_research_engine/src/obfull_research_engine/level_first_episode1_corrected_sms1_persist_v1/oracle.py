"""Independent derived-event oracle. Does not call production wall/refill/touch helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt, book_map_sha256
from ..timeparse import format_utc_z
from . import DETECTION, FIRST_TOUCH
from .classify_levels import classify_level_kind, is_size_increase, is_size_reduction
from .io_zst import body_rows, read_jsonl_zst
from .persist import event_available_at, pairs_to_map, ts_cell
from .reader import load_table, reconstruct_from_persist
from .time_contract import last_state_available_at

# Reconstruct is book reconstruction from persist, not wall/refill derivation.


class IndependentBook:
    def __init__(self, bids: dict[float, float], asks: dict[float, float], epoch=None):
        self.bids = {float(p): float(q) for p, q in dict(bids).items() if float(q) > 0}
        self.asks = {float(p): float(q) for p, q in dict(asks).items() if float(q) > 0}
        self.epoch = epoch
        self.update_id = None
        self.seq = None
        self.last_event_ts = None

    def apply_reset(self, bids, asks, epoch=None, update_id=None, seq=None, ts=None):
        self.bids = {float(p): float(q) for p, q in dict(bids).items() if float(q) > 0}
        self.asks = {float(p): float(q) for p, q in dict(asks).items() if float(q) > 0}
        if epoch is not None:
            self.epoch = epoch
        if update_id is not None:
            self.update_id = update_id
        if seq is not None:
            self.seq = seq
        if ts is not None:
            self.last_event_ts = ts

    def apply_change(self, side, price, new_size, epoch=None, update_id=None, seq=None, ts=None):
        book = self.bids if side == "bid" else self.asks
        if float(new_size) <= 0:
            book.pop(float(price), None)
        else:
            book[float(price)] = float(new_size)
        if epoch is not None:
            self.epoch = epoch
        if update_id is not None:
            self.update_id = update_id
        if seq is not None:
            self.seq = seq
        if ts is not None:
            self.last_event_ts = ts

    def size(self, side: str, price: float) -> float:
        book = self.bids if side == "bid" else self.asks
        return float(book.get(float(price)) or 0.0)

    def top(self):
        return (max(self.bids) if self.bids else None, min(self.asks) if self.asks else None)


def _load_init(directory: Path) -> dict[str, Any]:
    rows = load_table(directory, "initial_book")
    return rows[0] if rows else {}


def _events(directory: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resets = []
    for rec in load_table(directory, "book_resets"):
        resets.append(
            {
                **rec,
                "_et": _as_dt(rec["event_time"]),
                "bids": pairs_to_map(rec.get("bids")),
                "asks": pairs_to_map(rec.get("asks")),
            }
        )
    changes = []
    for rec in load_table(directory, "level_changes"):
        changes.append({**rec, "_et": _as_dt(rec["event_time"])})
    resets.sort(key=lambda r: (r["_et"], int(r.get("apply_order") or 0)))
    changes.sort(key=lambda r: (r["_et"], int(r.get("apply_order") or 0)))
    return resets, changes


def _advance(book: IndependentBook, resets, changes, until: datetime) -> IndependentBook:
    timeline = [("r", r) for r in resets] + [("c", c) for c in changes]
    timeline.sort(
        key=lambda item: (
            item[1]["_et"],
            int(item[1].get("apply_order") or 0),
            0 if item[0] == "r" else 1,
        )
    )
    for kind, rec in timeline:
        if rec["_et"] >= until:
            continue
        if kind == "r":
            book.apply_reset(
                rec["bids"],
                rec["asks"],
                epoch=rec.get("replay_epoch"),
                update_id=rec.get("update_id"),
                seq=rec.get("sequence_id"),
                ts=rec["_et"],
            )
        else:
            book.apply_change(
                rec.get("side"),
                rec.get("price"),
                rec.get("new_size") or 0.0,
                epoch=rec.get("replay_epoch", rec.get("epoch")),
                update_id=rec.get("u"),
                seq=rec.get("seq"),
                ts=rec["_et"],
            )
    return book


def _oracle_walls(book: IndependentBook, *, threshold: float, event_time: datetime, states) -> list[dict[str, Any]]:
    out = []
    for side, levels in (("bid", book.bids), ("ask", book.asks)):
        for px, qty in levels.items():
            notional = float(px) * float(qty)
            if notional + 1e-9 >= threshold:
                out.append(
                    {
                        "side": side,
                        "price": float(px),
                        "qty": float(qty),
                        "notional": notional,
                        "replay_epoch": book.epoch,
                        "event_time": format_utc_z(event_time),
                        "event_available_at": format_utc_z(event_time),
                        "state_available_at": last_state_available_at(states, event_time),
                    }
                )
    return out


def _oracle_refills(
    changes: list[dict[str, Any]],
    resets: list[dict[str, Any]],
    *,
    window_ms: int,
    nearby_bps: float,
    partial_ratio: float,
    causal_end: datetime,
    mid_hint: float | None,
) -> dict[str, list[dict[str, Any]]]:
    from collections import defaultdict

    win = timedelta(milliseconds=int(window_ms))
    reductions = []
    increases_by_side: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in changes:
        if c["_et"] >= causal_end:
            continue
        kind = classify_level_kind(c.get("old_size"), c.get("new_size"))
        if is_size_reduction(kind):
            reductions.append(c)
        elif is_size_increase(kind):
            increases_by_side[str(c.get("side") or "")].append(c)
    reductions.sort(key=lambda c: (c["_et"], int(c.get("apply_order") or 0)))
    for side in increases_by_side:
        increases_by_side[side].sort(key=lambda c: (c["_et"], int(c.get("apply_order") or 0)))
    reset_times = [r["_et"] for r in resets if r["_et"] < causal_end]
    reset_times.sort()
    confirmed = []
    candidates = []
    pointers = {side: 0 for side in increases_by_side}

    def _reset_between(a: datetime, b: datetime) -> bool:
        for rt in reset_times:
            if a < rt <= b:
                return True
            if rt > b:
                break
        return False

    for rem in reductions:
        px = float(rem["price"])
        side = str(rem.get("side") or "")
        cand = increases_by_side.get(side) or []
        i = pointers.get(side, 0)
        while i < len(cand) and cand[i]["_et"] <= rem["_et"]:
            i += 1
        pointers[side] = i
        best = None
        j = i
        while j < len(cand):
            add = cand[j]
            if add["_et"] - rem["_et"] > win:
                break
            apx = float(add["price"])
            mid = mid_hint or px
            dist = abs(apx - px) / mid * 1e4 if mid else 0.0
            exact = abs(apx - px) <= 1e-9
            nearby = (not exact) and dist <= float(nearby_bps)
            if exact or nearby:
                lat = (add["_et"] - rem["_et"]).total_seconds() * 1000.0
                if best is None or lat < best[0]:
                    best = (lat, add, exact, nearby, dist)
            j += 1
        if best is None:
            continue
        _, add, exact, nearby, dist = best
        old = float(rem.get("old_size") or 0.0)
        new = float(rem.get("new_size") or 0.0)
        notion_before = abs(old * px)
        dropped = abs(float(rem.get("notional_delta") or ((old - new) * px)))
        depletion = new <= 0.0 or (notion_before > 0 and dropped + 1e-12 >= float(partial_ratio) * notion_before)
        reasons = []
        if old <= 0:
            reasons.append("NO_PRIOR_VISIBILITY")
        if not depletion:
            reasons.append("DEPLETION_THRESHOLD_NOT_MET")
        if nearby and not exact:
            reasons.append("NEARBY_NOT_EXACT")
        if rem.get("replay_epoch", rem.get("epoch")) != add.get("replay_epoch", add.get("epoch")):
            reasons.append("EPOCH_CHANGE")
        if _reset_between(rem["_et"], add["_et"]):
            reasons.append("RESET_IN_WINDOW")
        key = {
            "side": side,
            "price": px,
            "removal_event_id": rem.get("source_event_id"),
            "add_event_id": add.get("source_event_id"),
            "event_time": ts_cell(add["_et"]),
        }
        candidates.append(key)
        if not reasons:
            confirmed.append(key)
    return {"candidates": candidates, "confirmed": confirmed}


def _set_key_wall(row: dict[str, Any]) -> tuple:
    return (str(row.get("side")), round(float(row["price"]), 8))


def _set_key_refill(row: dict[str, Any]) -> tuple:
    return (str(row.get("removal_event_id")), str(row.get("add_event_id")), str(row.get("side")), str(row.get("price")))


def _causing_events(changes, resets, until: datetime, limit: int = 40) -> list[dict[str, Any]]:
    rows = []
    for rec in resets:
        if rec["_et"] < until:
            rows.append(
                {
                    "kind": "book_reset",
                    "event_time": ts_cell(rec["_et"]),
                    "event_type": rec.get("event_type"),
                    "replay_epoch": rec.get("replay_epoch"),
                    "update_id": rec.get("update_id"),
                    "source_event_id": rec.get("source_event_id"),
                    "identity_reset": rec.get("identity_reset"),
                }
            )
    for rec in changes:
        if rec["_et"] < until:
            rows.append(
                {
                    "kind": "level_change",
                    "event_time": ts_cell(rec["_et"]),
                    "side": rec.get("side"),
                    "price": rec.get("price"),
                    "old_size": rec.get("old_size"),
                    "new_size": rec.get("new_size"),
                    "level_kind": rec.get("level_kind") or classify_level_kind(rec.get("old_size"), rec.get("new_size")),
                    "u": rec.get("u"),
                    "seq": rec.get("seq"),
                    "replay_epoch": rec.get("replay_epoch"),
                    "source_event_id": rec.get("source_event_id"),
                }
            )
    rows.sort(key=lambda r: r["event_time"])
    return rows[-limit:]


def audit_directory(directory: Path, *, cfg: dict[str, Any], first_touch=FIRST_TOUCH, detection=DETECTION) -> dict[str, Any]:
    init = _load_init(directory)
    states = load_table(directory, "states_100ms")
    walls_prod = load_table(directory, "walls")
    touches_prod = load_table(directory, "touches")
    dets_prod = load_table(directory, "detections")
    confirmed_prod = load_table(directory, "confirmed_refills")
    candidates_prod = load_table(directory, "refill_candidates")
    changes_prod = load_table(directory, "level_changes")
    resets, changes = _events(directory)

    book_chk = IndependentBook(pairs_to_map(init.get("bids")), pairs_to_map(init.get("asks")), epoch=init.get("replay_epoch"))
    timeline = [("r", r) for r in resets] + [("c", c) for c in changes]
    timeline.sort(
        key=lambda item: (
            item[1]["_et"],
            int(item[1].get("apply_order") or 0),
            0 if item[0] == "r" else 1,
        )
    )
    level_fp = []
    level_ok = 0
    epoch_as_delta = 0
    for kind, rec in timeline:
        if rec["_et"] >= detection:
            break
        if kind == "r":
            book_chk.apply_reset(rec["bids"], rec["asks"], epoch=rec.get("replay_epoch"), ts=rec["_et"])
            continue
        side = str(rec.get("side") or "")
        px = rec.get("price")
        expected_old = book_chk.size(side, float(px)) if px is not None else None
        got_old = float(rec.get("old_size") or 0.0)
        if px is None or abs((expected_old or 0.0) - got_old) > 1e-9:
            if len(level_fp) < 8:
                level_fp.append(
                    {
                        "event_time": rec.get("event_time"),
                        "side": side,
                        "price": px,
                        "persist_old_size": rec.get("old_size"),
                        "oracle_old_size": expected_old,
                    }
                )
        else:
            level_ok += 1
        book_chk.apply_change(side, px, rec.get("new_size") or 0.0, epoch=rec.get("replay_epoch"), ts=rec["_et"])

    touch_book = IndependentBook(pairs_to_map(init.get("bids")), pairs_to_map(init.get("asks")), epoch=init.get("replay_epoch"))
    _advance(touch_book, resets, changes, first_touch)
    det_book = IndependentBook(pairs_to_map(init.get("bids")), pairs_to_map(init.get("asks")), epoch=init.get("replay_epoch"))
    _advance(det_book, resets, changes, detection)

    persist_touch = reconstruct_from_persist(directory, first_touch)
    persist_det = reconstruct_from_persist(directory, detection)
    look_ahead_touch = persist_touch.get("last_applied_event_ts") is not None and _as_dt(persist_touch["last_applied_event_ts"]) >= first_touch
    look_ahead_det = persist_det.get("last_applied_event_ts") is not None and _as_dt(persist_det["last_applied_event_ts"]) >= detection

    threshold = float(cfg["wall_large_notional_usdt"])
    oracle_walls = _oracle_walls(touch_book, threshold=threshold, event_time=first_touch, states=states)
    prod_wall_keys = {_set_key_wall(w) for w in walls_prod if w.get("price") is not None}
    oracle_wall_keys = {_set_key_wall(w) for w in oracle_walls}
    wall_fp = sorted(prod_wall_keys - oracle_wall_keys)
    wall_fn = sorted(oracle_wall_keys - prod_wall_keys)

    wall_attr_fp = []
    oracle_by = {_set_key_wall(w): w for w in oracle_walls}
    for w in walls_prod:
        if w.get("price") is None:
            continue
        key = _set_key_wall(w)
        ow = oracle_by.get(key)
        if ow is None:
            continue
        qty_ok = abs(float(w.get("qty") or w.get("size") or 0.0) - ow["qty"]) <= 1e-8
        notional_ok = abs(float(w.get("notional") or w.get("initial_notional") or 0.0) - ow["notional"]) <= 1e-4
        epoch_ok = w.get("replay_epoch") == ow.get("replay_epoch")
        early = False
        avail = w.get("event_available_at") or w.get("available_at")
        if avail and _as_dt(avail) < first_touch:
            early = True
        if not (qty_ok and notional_ok and epoch_ok) or early:
            wall_attr_fp.append(
                {
                    "key": key,
                    "qty_ok": qty_ok,
                    "notional_ok": notional_ok,
                    "epoch_ok": epoch_ok,
                    "early_available": early,
                }
            )

    bb, ba = touch_book.top()
    mid = ((bb + ba) / 2.0) if bb is not None and ba is not None else None
    refill_oracle = _oracle_refills(
        changes,
        resets,
        window_ms=int(cfg["refill_window_ms"]),
        nearby_bps=float(cfg["nearby_refill_max_bps"]),
        partial_ratio=float(cfg["wall_partial_consume_min_ratio"]),
        causal_end=detection,
        mid_hint=mid,
    )
    prod_conf = {_set_key_refill(r) for r in confirmed_prod}
    ora_conf = {_set_key_refill(r) for r in refill_oracle["confirmed"]}
    refill_fp = sorted(prod_conf - ora_conf)
    refill_fn = sorted(ora_conf - prod_conf)

    touch_fp = []
    touch_fn = []
    if len(touches_prod) != 1:
        if len(touches_prod) > 1:
            touch_fp.append({"reason": "extra_touch", "n": len(touches_prod)})
        if len(touches_prod) < 1:
            touch_fn.append({"reason": "missing_touch"})
    elif touches_prod:
        t = touches_prod[0]
        if _as_dt(t["event_time"]) != first_touch:
            touch_fp.append({"reason": "touch_time_mismatch", "got": t.get("event_time")})
        if abs(float(t.get("best_bid") or 0) - float(bb or 0)) > 1e-9 or abs(float(t.get("best_ask") or 0) - float(ba or 0)) > 1e-9:
            touch_fp.append({"reason": "tob_mismatch", "prod": (t.get("best_bid"), t.get("best_ask")), "oracle": (bb, ba)})
        if look_ahead_touch:
            touch_fp.append({"reason": "look_ahead"})
        avail = t.get("event_available_at")
        if avail and _as_dt(avail) < first_touch:
            touch_fp.append({"reason": "event_available_at_before_touch"})

    dbb, dba = det_book.top()
    det_fp = []
    det_fn = []
    if len(dets_prod) != 1:
        if len(dets_prod) > 1:
            det_fp.append({"reason": "extra_detection", "n": len(dets_prod)})
        if len(dets_prod) < 1:
            det_fn.append({"reason": "missing_detection"})
    elif dets_prod:
        d = dets_prod[0]
        if _as_dt(d["event_time"]) != detection:
            det_fp.append({"reason": "detection_time_mismatch", "got": d.get("event_time")})
        if abs(float(d.get("best_bid") or 0) - float(dbb or 0)) > 1e-9 or abs(float(d.get("best_ask") or 0) - float(dba or 0)) > 1e-9:
            det_fp.append({"reason": "tob_mismatch", "prod": (d.get("best_bid"), d.get("best_ask")), "oracle": (dbb, dba)})
        if look_ahead_det:
            det_fp.append({"reason": "look_ahead"})
        avail = d.get("event_available_at") or d.get("detected_at")
        inputs = [first_touch]
        if states:
            last = last_state_available_at(states, detection)
            if last:
                inputs.append(_as_dt(last))
        if walls_prod:
            inputs.extend(_as_dt(w.get("event_available_at") or first_touch) for w in walls_prod if w.get("event_available_at"))
        max_in = max(inputs)
        if avail and _as_dt(avail) < max_in:
            det_fp.append({"reason": "detection_before_input_availability", "event_available_at": avail, "max_input": ts_cell(max_in)})

    causality = []
    for rec in walls_prod + confirmed_prod + candidates_prod + touches_prod + dets_prod:
        avail = rec.get("event_available_at") or rec.get("available_at")
        et = rec.get("event_time")
        if avail and et and _as_dt(avail) < _as_dt(et) and rec.get("event_type") not in {"TOUCH", "DETECTION", "WALL"}:
            causality.append({"kind": "event_available_at_lt_event_time", "event_type": rec.get("event_type"), "event_id": rec.get("event_id") or rec.get("wall_id")})

    fp = len(wall_fp) + len(wall_attr_fp) + len(refill_fp) + len(touch_fp) + len(det_fp) + len(level_fp)
    fn = len(wall_fn) + len(refill_fn) + len(touch_fn) + len(det_fn)

    return {
        "derived_false_positives": fp,
        "derived_false_negatives": fn,
        "causality_violations": len(causality) + (1 if look_ahead_touch or look_ahead_det else 0),
        "refill_misclassifications": len(refill_fp) + len(refill_fn),
        "walls": {
            "prod": len(prod_wall_keys),
            "oracle": len(oracle_wall_keys),
            "false_positives": [list(x) for x in wall_fp],
            "false_negatives": [list(x) for x in wall_fn],
            "attribute_false_positives": wall_attr_fp[:10],
        },
        "level_changes": {
            "n_prod": len(changes_prod),
            "n_old_size_ok": level_ok,
            "false_positives": level_fp,
            "epoch_as_delta": epoch_as_delta,
        },
        "refills": {
            "prod_confirmed": len(prod_conf),
            "oracle_confirmed": len(ora_conf),
            "prod_candidates": len(candidates_prod),
            "oracle_candidates": len(refill_oracle["candidates"]),
            "false_positives": [list(x) for x in refill_fp[:20]],
            "false_negatives": [list(x) for x in refill_fn[:20]],
        },
        "touches": {"false_positives": touch_fp, "false_negatives": touch_fn},
        "detections": {"false_positives": det_fp, "false_negatives": det_fn},
        "look_ahead_touch": look_ahead_touch,
        "look_ahead_detection": look_ahead_det,
        "causality_examples": causality[:10],
        "touch_raw_trace": {
            "event_time": format_utc_z(first_touch),
            "best_bid": bb,
            "best_ask": ba,
            "replay_epoch": touch_book.epoch,
            "n_bid_levels": len(touch_book.bids),
            "n_ask_levels": len(touch_book.asks),
            "book_map_sha256": book_map_sha256(touch_book.bids, touch_book.asks),
            "persist_book_map_sha256": persist_touch.get("book_map_sha256") if persist_touch.get("bids") is None else book_map_sha256(persist_touch.get("bids") or {}, persist_touch.get("asks") or {}),
            "last_applied_event_ts": ts_cell(persist_touch.get("last_applied_event_ts")),
            "causing_events_tail": _causing_events(changes, resets, first_touch),
        },
        "detection_raw_trace": {
            "event_time": format_utc_z(detection),
            "best_bid": dbb,
            "best_ask": dba,
            "replay_epoch": det_book.epoch,
            "n_bid_levels": len(det_book.bids),
            "n_ask_levels": len(det_book.asks),
            "book_map_sha256": book_map_sha256(det_book.bids, det_book.asks),
            "last_applied_event_ts": ts_cell(persist_det.get("last_applied_event_ts")),
            "causing_events_tail": _causing_events(changes, resets, detection),
        },
        "oracle_uses_production_walls": False,
        "oracle_uses_production_refill_detect": False,
    }


def body_sha256_file(path: Path) -> str:
    import hashlib

    rows = body_rows(read_jsonl_zst(path)) if path.is_file() else []
    raw = ("\n".join(__import__("json").dumps(r, sort_keys=True, separators=(",", ":"), default=str) for r in rows) + ("\n" if rows else "")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

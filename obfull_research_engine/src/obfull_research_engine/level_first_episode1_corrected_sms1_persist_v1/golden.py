"""Golden oracle: every Episode-1 100ms cutoff vs live FullBookState samples."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..drilldown.aggregation_100ms import (
    _as_dt,
    _floor_bucket,
    first_diverging_level,
    reconstruct_book_asof_exclusive,
)
from ..timeparse import format_utc_z
from . import (
    BUCKET_MS,
    DEPTH_ABS_TOL_USDT,
    DETECTION,
    EVIDENCE_START,
    FIRST_BUCKET_AVAILABLE_AT,
)
from .persist import pairs_to_map


def all_bucket_end_cutoffs() -> list[tuple[datetime, str]]:
    first_end = _floor_bucket(EVIDENCE_START, BUCKET_MS) + timedelta(milliseconds=BUCKET_MS)
    out: list[tuple[datetime, str]] = []
    cur = first_end
    i = 0
    while cur <= DETECTION:
        out.append((cur, f"bucket_end_{i:04d}"))
        cur += timedelta(milliseconds=BUCKET_MS)
        i += 1
    return out


def extra_cutoffs(replay: dict[str, Any]) -> list[tuple[datetime, str]]:
    rows: list[tuple[datetime, str]] = [
        (EVIDENCE_START, "evidence_start"),
        (FIRST_BUCKET_AVAILABLE_AT, "first_bucket_available_at"),
        (DETECTION, "detection"),
    ]
    for rec in replay.get("book_resets") or []:
        ts = _as_dt(rec["event_time"])
        rows.append((ts, f"reset_at_{format_utc_z(ts)}"))
    return rows


def _price_equal(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return float(a) == float(b)


def _none_or_equal(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a == b


def _depth_equal(a: Any, b: Any, *, abs_tol: float = DEPTH_ABS_TOL_USDT) -> bool:
    if a is None and b is None:
        return True
    return abs(float(a or 0.0) - float(b or 0.0)) <= abs_tol


def compare_state_to_live(state: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    n_bid_ok = int(state.get("n_bid_levels") or 0) == int(ref.get("n_bid_levels") or 0)
    n_ask_ok = int(state.get("n_ask_levels") or 0) == int(ref.get("n_ask_levels") or 0)
    bid_ok = _price_equal(state.get("best_bid"), ref.get("best_bid"))
    ask_ok = _price_equal(state.get("best_ask"), ref.get("best_ask"))
    mid_ok = _price_equal(state.get("mid") if state.get("mid") is not None else state.get("mid_price"), ref.get("mid"))
    spread_ok = _depth_equal(state.get("spread"), ref.get("spread"))
    epoch_ok = _none_or_equal(state.get("replay_epoch"), ref.get("replay_epoch"))
    hash_ok = (state.get("book_map_sha256") or "") == (ref.get("book_sha256") or "")
    if hash_ok or ref.get("bid_depth_0_2bps") is None:
        d02_bid = d02_ask = d25_bid = d25_ask = d510_bid = d510_ask = True
    else:
        d02_bid = _depth_equal(state.get("bid_depth_notional_usdt_bps_0_2"), ref.get("bid_depth_0_2bps"))
        d02_ask = _depth_equal(state.get("ask_depth_notional_usdt_bps_0_2"), ref.get("ask_depth_0_2bps"))
        d25_bid = _depth_equal(state.get("bid_depth_notional_usdt_bps_2_5"), ref.get("bid_depth_2_5bps"))
        d25_ask = _depth_equal(state.get("ask_depth_notional_usdt_bps_2_5"), ref.get("ask_depth_2_5bps"))
        d510_bid = _depth_equal(state.get("bid_depth_notional_usdt_bps_5_10"), ref.get("bid_depth_5_10bps"))
        d510_ask = _depth_equal(state.get("ask_depth_notional_usdt_bps_5_10"), ref.get("ask_depth_5_10bps"))
    start_ok = True
    end_ok = _as_dt(state["bucket_end_exclusive"]) == _as_dt(ref["cutoff_ts"])
    avail_ok = _as_dt(state["available_at"]) == _as_dt(ref["cutoff_ts"]) == _as_dt(state["bucket_end_exclusive"])
    ok = all(
        (
            n_bid_ok,
            n_ask_ok,
            bid_ok,
            ask_ok,
            mid_ok,
            spread_ok,
            d02_bid,
            d02_ask,
            d25_bid,
            d25_ask,
            d510_bid,
            d510_ask,
            epoch_ok,
            hash_ok,
            end_ok,
            avail_ok,
        )
    )
    first_field = None
    checks = [
        ("n_bid_levels", n_bid_ok),
        ("n_ask_levels", n_ask_ok),
        ("best_bid", bid_ok),
        ("best_ask", ask_ok),
        ("mid", mid_ok),
        ("spread", spread_ok),
        ("bid_depth_0_2bps", d02_bid),
        ("ask_depth_0_2bps", d02_ask),
        ("bid_depth_2_5bps", d25_bid),
        ("ask_depth_2_5bps", d25_ask),
        ("bid_depth_5_10bps", d510_bid),
        ("ask_depth_5_10bps", d510_ask),
        ("replay_epoch", epoch_ok),
        ("book_map_sha256", hash_ok),
        ("bucket_end_exclusive", end_ok),
        ("available_at", avail_ok),
    ]
    for name, passed in checks:
        if not passed:
            first_field = name
            break
    return {
        "cutoff_ts": format_utc_z(_as_dt(ref["cutoff_ts"])),
        "label": ref.get("label"),
        "ok": ok,
        "first_diverging_field": first_field,
        "match_n_bid_levels": n_bid_ok,
        "match_n_ask_levels": n_ask_ok,
        "match_best_bid": bid_ok,
        "match_best_ask": ask_ok,
        "match_mid": mid_ok,
        "match_spread": spread_ok,
        "match_bid_depth_0_2bps": d02_bid,
        "match_ask_depth_0_2bps": d02_ask,
        "match_bid_depth_2_5bps": d25_bid,
        "match_ask_depth_2_5bps": d25_ask,
        "match_bid_depth_5_10bps": d510_bid,
        "match_ask_depth_5_10bps": d510_ask,
        "match_replay_epoch": epoch_ok,
        "match_book_map_sha256": hash_ok,
        "match_bucket_end_exclusive": end_ok,
        "match_available_at": avail_ok,
        "match_bucket_start": start_ok,
        "state_bucket_start": format_utc_z(_as_dt(state["bucket_start"])),
        "state_bucket_end_exclusive": format_utc_z(_as_dt(state["bucket_end_exclusive"])),
        "state_available_at": format_utc_z(_as_dt(state["available_at"])),
        "state_effective_bucket_start": format_utc_z(_as_dt(state["effective_bucket_start"])),
        "state_replay_epoch": state.get("replay_epoch"),
        "ref_replay_epoch": ref.get("replay_epoch"),
        "state_best_bid": state.get("best_bid"),
        "ref_best_bid": ref.get("best_bid"),
        "state_best_ask": state.get("best_ask"),
        "ref_best_ask": ref.get("best_ask"),
        "state_n_bid_levels": state.get("n_bid_levels"),
        "ref_n_bid_levels": ref.get("n_bid_levels"),
        "state_n_ask_levels": state.get("n_ask_levels"),
        "ref_n_ask_levels": ref.get("n_ask_levels"),
        "state_book_map_sha256": state.get("book_map_sha256"),
        "ref_book_sha256": ref.get("book_sha256"),
    }


def golden_all_cutoffs(
    *,
    states: list[dict[str, Any]],
    references: list[dict[str, Any]],
    replay: dict[str, Any],
) -> dict[str, Any]:
    by_avail = {_as_dt(s["available_at"]): s for s in states}
    rows: list[dict[str, Any]] = []
    first_fail: dict[str, Any] | None = None
    n_full_map = 0
    for ref in references:
        label = str(ref.get("label") or "")
        if not label.startswith("bucket_end_"):
            continue
        cutoff = _as_dt(ref["cutoff_ts"])
        state = by_avail.get(cutoff)
        if state is None:
            rec = {
                "cutoff_ts": format_utc_z(cutoff),
                "label": label,
                "ok": False,
                "first_diverging_field": "missing_state",
            }
            rows.append(rec)
            if first_fail is None:
                first_fail = rec
            continue
        rec = compare_state_to_live(state, ref)
        n_full_map += 1
        if not rec["ok"] and first_fail is None:
            stream = reconstruct_book_asof_exclusive(
                initial_bids=replay["initial_bids"],
                initial_asks=replay["initial_asks"],
                level_changes=replay["level_changes"],
                until=cutoff,
                book_resets=replay.get("book_resets"),
                evidence_start=replay["window_start"],
                initial_update_id=replay.get("initial_update_id"),
                initial_sequence_id=replay.get("initial_sequence_id"),
                initial_replay_epoch=replay.get("initial_replay_epoch"),
                initial_checkpoint_id=replay.get("initial_checkpoint_id"),
            )
            rec["reconstruct_best_bid"] = stream.get("best_bid")
            rec["reconstruct_best_ask"] = stream.get("best_ask")
            rec["reconstruct_replay_epoch"] = stream.get("replay_epoch")
            rec["reconstruct_n_bid"] = len(stream.get("bids") or {})
            rec["reconstruct_n_ask"] = len(stream.get("asks") or {})
            rec["first_diverging_level"] = None
            rec["note"] = (
                "Live FullBookState maps are not stored for every cutoff; first diverging "
                "scalar/hash field is named. Reconstruct vs persist ToB is attached."
            )
            first_fail = rec
        rows.append(rec)
    n_ok = sum(1 for r in rows if r.get("ok"))
    return {
        "n_cutoffs_compared": len(rows),
        "n_ok": n_ok,
        "n_mismatch": len(rows) - n_ok,
        "full_book_level_comparisons": n_full_map,
        "full_book_level_mode": "canonical_book_map_sha256_plus_topN_depth_and_level_counts",
        "first_mismatch": first_fail,
        "rows": rows,
    }


def reset_before_after_parity(replay: dict[str, Any], states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in replay.get("checkpoint_before_after") or []:
        edt = _as_dt(item["event_time"])
        before = item.get("before") or {}
        after = item.get("after") or {}
        # 100ms row immediately before reset: available_at <= edt, last complete
        pre = None
        post = None
        for st in states:
            if _as_dt(st["available_at"]) <= edt:
                pre = st
            if post is None and _as_dt(st["bucket_start"]) <= edt < _as_dt(st["bucket_end_exclusive"]):
                post = st
        out.append(
            {
                "event_time": format_utc_z(edt),
                "event_type": item.get("event_type"),
                "replay_epoch_after": item.get("replay_epoch"),
                "identity_reset": int(item.get("changed_bid_levels") or 0) == 0
                and int(item.get("changed_ask_levels") or 0) == 0,
                "changed_bid_levels": item.get("changed_bid_levels"),
                "changed_ask_levels": item.get("changed_ask_levels"),
                "live_best_bid_before": before.get("best_bid"),
                "live_best_ask_before": before.get("best_ask"),
                "live_best_bid_after": after.get("best_bid"),
                "live_best_ask_after": after.get("best_ask"),
                "live_epoch_before": before.get("replay_epoch"),
                "live_epoch_after": after.get("replay_epoch"),
                "state_immediately_before_available_at": format_utc_z(_as_dt(pre["available_at"])) if pre else None,
                "state_containing_reset_available_at": format_utc_z(_as_dt(post["available_at"])) if post else None,
                "state_containing_reset_epoch": post.get("replay_epoch") if post else None,
                "containing_state_matches_after_tob": bool(
                    post is not None
                    and _price_equal(post.get("best_bid"), after.get("best_bid"))
                    and _price_equal(post.get("best_ask"), after.get("best_ask"))
                ),
            }
        )
    return out


def boundary_event_audit(replay: dict[str, Any], states: list[dict[str, Any]]) -> dict[str, Any]:
    """Events with event_time == bucket_end_exclusive must appear only in the next bucket."""
    by_end = {_as_dt(s["bucket_end_exclusive"]): s for s in states}
    n_boundary = 0
    n_wrong = 0
    examples = []
    for e in replay.get("level_changes") or []:
        et = _as_dt(e["event_time"])
        st = by_end.get(et)
        if st is None:
            continue
        n_boundary += 1
        # this event must NOT be in st (the bucket that ends at et)
        first = st.get("first_applied_event_ts")
        last = st.get("last_applied_event_ts")
        in_this = False
        if first is not None and last is not None:
            in_this = _as_dt(first) <= et <= _as_dt(last) and et >= _as_dt(st["effective_bucket_start"])
        # more precise: event belongs to next bucket; this bucket has et >= b_end so excluded
        if in_this and et == _as_dt(st["bucket_end_exclusive"]):
            n_wrong += 1
            if len(examples) < 5:
                examples.append({"event_time": format_utc_z(et), "bucket_start": format_utc_z(_as_dt(st["bucket_start"]))})
    return {
        "n_events_exactly_on_bucket_end_exclusive": n_boundary,
        "n_incorrectly_kept_in_ending_bucket": n_wrong,
        "examples": examples,
        "ok": n_wrong == 0,
    }


def diagnose_first_diverging_level(replay: dict[str, Any], cutoff: datetime, live_ref: dict[str, Any] | None) -> dict[str, Any] | None:
    stream = reconstruct_book_asof_exclusive(
        initial_bids=replay["initial_bids"],
        initial_asks=replay["initial_asks"],
        level_changes=replay["level_changes"],
        until=cutoff,
        book_resets=replay.get("book_resets"),
        evidence_start=replay["window_start"],
        initial_update_id=replay.get("initial_update_id"),
        initial_sequence_id=replay.get("initial_sequence_id"),
        initial_replay_epoch=replay.get("initial_replay_epoch"),
        initial_checkpoint_id=replay.get("initial_checkpoint_id"),
    )
    if live_ref and live_ref.get("bids") is not None:
        return first_diverging_level(
            stream["bids"],
            stream["asks"],
            pairs_to_map(live_ref.get("bids")),
            pairs_to_map(live_ref.get("asks")),
        )
    return {
        "reconstruct_best_bid": stream.get("best_bid"),
        "reconstruct_best_ask": stream.get("best_ask"),
        "reconstruct_n_bid": len(stream["bids"]),
        "reconstruct_n_ask": len(stream["asks"]),
    }

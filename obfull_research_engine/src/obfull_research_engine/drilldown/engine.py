"""Orchestrate event-level candidate drilldown V1."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.research.general_market_behavior_v1.modalities import load_trades

from ..episodes.state_loader import load_states_for_interval
from .aggregation_100ms import build_states_100ms
from .attribution import attribute_removals, attribution_summary
from .event_order import sort_timeline
from .refill import detect_refills
from .replay import replay_window
from .selector import select_candidates
from .support import assess_support
from .validation import causality_check, compare_parity_to_state, quality_checks
from .walls import analyze_walls
from .windowing import group_candidates, merge_replay_windows


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def config_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _book_at(
    initial_bids: dict[float, float],
    initial_asks: dict[float, float],
    level_changes: list[dict[str, Any]],
    until: pd.Timestamp,
) -> tuple[dict[float, float], dict[float, float]]:
    """Level-change-only reconstruction. Ignores checkpoint/snapshot resets.

    Not a golden oracle. Do not use for touch book, detection book, wall
    trigger book, OI price, or spatial coverage when resets exist in-window.
    Use ``reconstruct_book_asof_exclusive`` (checkpoint-capable stream) instead.
    """
    bids = dict(initial_bids)
    asks = dict(initial_asks)
    until = pd.to_datetime(until, utc=True).to_pydatetime()
    # level_changes are already time-sorted by replay_window
    for e in level_changes:
        et = e["event_time"]
        if hasattr(et, "to_pydatetime"):
            et = et.to_pydatetime()
        if et >= until:
            break
        side = e["side"]
        px = float(e["price"])
        nq = float(e["new_size"])
        book = bids if side == "bid" else asks
        if nq <= 0:
            book.pop(px, None)
        else:
            book[px] = nq
    return bids, asks


def run_drilldown(
    *,
    candidates: pd.DataFrame,
    cfg: dict[str, Any],
    symbol: str,
    interval_start: datetime,
    interval_end: datetime,
    max_candidates: int,
    candidate_type: str | None = None,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    selected, not_selected, sel_manifest = select_candidates(
        candidates,
        cfg=cfg,
        max_candidates=max_candidates,
        candidate_type=candidate_type,
        candidate_id=candidate_id,
    )
    groups = group_candidates(selected, grouping_window_seconds=int(cfg["grouping_window_seconds"]))
    spans = merge_replay_windows(selected)

    # Load 1s states for parity (read-only partitions)
    state_df, state_meta = load_states_for_interval(symbol=symbol, start=interval_start, end=interval_end)

    all_timeline: list[dict[str, Any]] = []
    all_100ms: list[dict[str, Any]] = []
    all_attr: list[dict[str, Any]] = []
    all_refill: list[dict[str, Any]] = []
    all_walls: list[dict[str, Any]] = []
    support_rows: list[dict[str, Any]] = []
    parity_reports: list[dict[str, Any]] = []
    bytes_read = 0
    blocked: list[dict[str, Any]] = []

    # Preload trades for union window once
    if selected.empty:
        return {
            "selected": selected,
            "not_selected": not_selected,
            "selection_manifest": sel_manifest,
            "groups": groups,
            "timeline": pd.DataFrame(),
            "states_100ms": pd.DataFrame(),
            "attribution": pd.DataFrame(),
            "refills": pd.DataFrame(),
            "walls": pd.DataFrame(),
            "support": pd.DataFrame(),
            "parity": {"ok": True, "compared_rows": 0},
            "resources": {"bytes_read": 0, "n_spans": 0},
            "blocked": blocked,
            "state_meta": state_meta,
        }

    union_start = min(pd.to_datetime(selected["context_start_drilldown"], utc=True))
    union_end = max(pd.to_datetime(selected["detection_available_at_effective"], utc=True))
    trades_all = load_trades(symbol, union_start.to_pydatetime(), union_end.to_pydatetime())

    print(f"Merged replay spans: {len(spans)}", flush=True)
    for i, span in enumerate(spans):
        print(
            f"  span[{i}] {span['start']} -> {span['end']} candidates={len(span['candidate_ids'])}",
            flush=True,
        )
    # Map candidate_id -> span replay cache
    span_cache: dict[int, dict[str, Any]] = {}
    for i, span in enumerate(spans):
        print(f"Replaying span {i+1}/{len(spans)}…", flush=True)
        rep = replay_window(
            symbol=symbol,
            window_start=span["start"].to_pydatetime(),
            window_end=span["end"].to_pydatetime(),
            trades=[t for t in trades_all if span["start"].to_pydatetime() <= t.trade_ts < span["end"].to_pydatetime()],
        )
        span_cache[i] = rep
        print(
            f"  replay done ok={rep.get('ok')} levels={len(rep.get('level_changes') or [])} "
            f"timeline={len(rep.get('timeline') or [])}",
            flush=True,
        )
        if not rep.get("ok"):
            for cid in span["candidate_ids"]:
                blocked.append({"candidate_id": cid, "status": "DRILLDOWN_BLOCKED_COVERAGE", "error": rep.get("error")})
            continue
        bytes_read += int(rep.get("bytes_read") or 0)

        print(f"  building 100ms…", flush=True)
        states = build_states_100ms(
            window_start=rep["window_start"],
            window_end=rep["window_end"],
            timeline=rep["timeline"],
            level_changes=rep["level_changes"],
            trades=rep["trades"],
            book_snapshots_by_time=rep.get("book_snapshots_by_time"),
            initial_bids=rep["initial_bids"],
            initial_asks=rep["initial_asks"],
            bucket_ms=int(cfg["aggregation_bucket_ms"]),
            ordering_confidence=rep["ordering_confidence"],
            book_resets=rep.get("book_resets"),
            evidence_start=rep.get("window_start"),
            initial_update_id=rep.get("initial_update_id"),
            initial_sequence_id=rep.get("initial_sequence_id"),
            initial_replay_epoch=rep.get("initial_replay_epoch"),
            initial_checkpoint_id=rep.get("initial_checkpoint_id"),
        )
        print(f"  100ms rows={len(states)}", flush=True)
        # Compact timeline: trades + deltas only (no per-level explosion in artifact)
        for e in rep["timeline"]:
            if e.get("event_type") in {"trade", "delta", "checkpoint", "snapshot"}:
                e2 = dict(e)
                e2["replay_span_id"] = i
                all_timeline.append(e2)
        for r in states:
            r = dict(r)
            r["replay_span_id"] = i
            all_100ms.append(r)

        # parity on full seconds inside span
        ws = pd.to_datetime(rep["window_start"], utc=True)
        we = pd.to_datetime(rep["window_end"], utc=True)
        if ws != ws.floor("s"):
            sec_start = ws.floor("s") + pd.Timedelta(seconds=1)
        else:
            sec_start = ws
        sec_end = we.floor("s")
        sub_states = [
            r
            for r in states
            if sec_start <= pd.to_datetime(r["bucket_start"], utc=True) < sec_end
        ]
        state_slice = state_df[
            (state_df["state_ts"] >= sec_start) & (state_df["state_ts"] < sec_end)
        ]
        parity_reports.append(
            compare_parity_to_state(
                sub_states,
                state_slice,
                abs_notional_tol=float(cfg["parity_abs_notional_tol"]),
                rel_tol=float(cfg["parity_rel_tol"]),
                price_abs_tol=float(cfg["parity_price_abs_tol"]),
                count_tol=int(cfg["parity_count_tol"]),
            )
        )
        # attribution deferred to per-candidate slices (avoid O(span) blowup)
    # Per-candidate analyses using owning span
    cid_to_span = {}
    for i, span in enumerate(spans):
        for cid in span["candidate_ids"]:
            cid_to_span[cid] = i

    print(f"Per-candidate analysis for {len(selected)} candidates…", flush=True)
    # Index level_changes by span with pre-parsed timestamps
    span_level_times: dict[int, list] = {}
    for si, rep in span_cache.items():
        if not rep.get("ok"):
            continue
        times = []
        for e in rep["level_changes"]:
            et = e["event_time"]
            times.append(et.to_pydatetime() if hasattr(et, "to_pydatetime") else et)
        span_level_times[si] = times

    # Precompute book snapshots at unique trigger timestamps (avoid O(candidates * levels))
    books_at_trigger: dict[pd.Timestamp, tuple[dict[float, float], dict[float, float]]] = {}
    for _, cand in selected.iterrows():
        cid = str(cand["candidate_id"])
        if cid in {b["candidate_id"] for b in blocked}:
            continue
        si = cid_to_span.get(cid)
        if si is None or not span_cache.get(si, {}).get("ok"):
            continue
        trigger = pd.to_datetime(cand["trigger_ts"], utc=True)
        if trigger not in books_at_trigger:
            rep = span_cache[si]
            books_at_trigger[trigger] = _book_at(
                rep["initial_bids"], rep["initial_asks"], rep["level_changes"], trigger
            )
    print(f"  books_at_trigger cached={len(books_at_trigger)}", flush=True)

    span_100ms: dict[int, list[tuple[Any, Any]]] = {}
    for r in all_100ms:
        si = r.get("replay_span_id")
        if si is None:
            continue
        span_100ms.setdefault(int(si), []).append((pd.to_datetime(r["bucket_start"], utc=True), r))
    span_timeline: dict[int, list[tuple[Any, Any]]] = {}
    for si, rep in span_cache.items():
        if not rep.get("ok"):
            continue
        rows = []
        for e in rep["timeline"]:
            if e.get("event_type") == "level_change":
                continue
            et = e.get("event_time")
            ts = et.to_pydatetime() if hasattr(et, "to_pydatetime") else pd.to_datetime(et, utc=True)
            rows.append((ts, e))
        span_timeline[si] = rows

    n_sel = int(len(selected))
    for cand_i, (_, cand) in enumerate(selected.iterrows(), start=1):
        cid = str(cand["candidate_id"])
        if cid in {b["candidate_id"] for b in blocked}:
            continue
        si = cid_to_span.get(cid)
        if si is None or not span_cache.get(si, {}).get("ok"):
            blocked.append({"candidate_id": cid, "status": "DRILLDOWN_BLOCKED_COVERAGE", "error": "missing_span"})
            continue
        if cand_i == 1 or cand_i % 50 == 0:
            print(f"  candidate {cand_i}/{n_sel}", flush=True)
        rep = span_cache[si]
        trigger = pd.to_datetime(cand["trigger_ts"], utc=True)
        det = pd.to_datetime(cand["detection_available_at_effective"], utc=True)
        ctx = pd.to_datetime(cand["context_start_drilldown"], utc=True)
        focus_start = max(ctx, trigger - pd.Timedelta(seconds=2))
        fs = focus_start.to_pydatetime()
        de = det.to_pydatetime()
        times = span_level_times[si]
        lv = [e for e, et in zip(rep["level_changes"], times) if fs <= et < de]
        tr = [t for t in rep["trades"] if fs <= t.trade_ts < de]
        ref = detect_refills(
            lv,
            refill_window_ms=int(cfg["refill_window_ms"]),
            nearby_max_bps=float(cfg["nearby_refill_max_bps"]),
            causal_end=det,
            max_removals=2000,
        )
        for r in ref:
            r["candidate_id"] = cid
            all_refill.append(r)

        bids_t, asks_t = books_at_trigger[trigger]
        mid = None
        if bids_t and asks_t:
            mid = (max(bids_t) + min(asks_t)) / 2.0
        walls = analyze_walls(
            level_changes=lv,
            bids_at_trigger=bids_t,
            asks_at_trigger=asks_t,
            mid_at_trigger=mid or 0.0,
            causal_end=det,
            large_notional=float(cfg["wall_large_notional_usdt"]),
            migration_max_bps=float(cfg["wall_migration_max_bps"]),
            migration_max_ms=int(cfg["wall_migration_max_ms"]),
            partial_ratio=float(cfg["wall_partial_consume_min_ratio"]),
            removed_ratio=float(cfg["wall_removed_min_ratio"]),
        )
        for w in walls:
            w["candidate_id"] = cid
            all_walls.append(w)

        attr_c = attribute_removals(
            lv,
            tr,
            tolerance_ms=int(cfg["trade_match_tolerance_ms"]),
        )
        for a in attr_c:
            a["candidate_id"] = cid
            a["replay_span_id"] = si
            all_attr.append(a)

        window_events = [dict(e) for ts, e in span_timeline.get(si, []) if ctx <= ts < det]
        _, local_order = sort_timeline(window_events)
        if local_order == "EMPTY":
            local_order = "ORDERING_DETERMINISTIC_CONTRACT"
        st100 = []
        for ts, r in span_100ms.get(int(si), []):
            if ctx <= ts < det:
                rr = dict(r)
                rr["ordering_confidence"] = local_order
                st100.append(rr)
        support_rows.append(
            assess_support(
                candidate_type=str(cand["candidate_type"]),
                candidate_id=cid,
                attribution_rows=attr_c,
                refill_rows=ref,
                wall_rows=walls,
                states_100ms=st100,
                trigger_ts=trigger,
                causal_end=det,
            )
        )

        caus = causality_check(
            [e for ts, e in span_timeline.get(si, []) if ctx <= ts < det],
            det,
        )
        if not caus["ok"]:
            blocked.append({"candidate_id": cid, "status": "CAUSALITY_VIOLATION", "error": caus})

    # Merge parity
    parity = {
        "span_reports": parity_reports,
        "compared_rows": sum(p.get("compared_rows", 0) for p in parity_reports),
        "exact_matches": sum(p.get("exact_matches", 0) for p in parity_reports),
        "tolerance_matches": sum(p.get("tolerance_matches", 0) for p in parity_reports),
        "mismatches": sum(p.get("mismatches", 0) for p in parity_reports),
        "hard_trade_mismatches": sum(p.get("hard_trade_mismatches", 0) for p in parity_reports),
        "soft_book_mismatches": sum(p.get("soft_book_mismatches", 0) for p in parity_reports),
        "book_flow_parity": "PROXY_LIMITED"
        if any(p.get("soft_book_mismatches", 0) for p in parity_reports)
        else "MATCHED",
        "max_abs_error": max((p.get("max_abs_error") or 0) for p in parity_reports) if parity_reports else 0,
        "max_rel_error": max((p.get("max_rel_error") or 0) for p in parity_reports) if parity_reports else 0,
        "mismatch_reasons": [],
        "ok": all(p.get("ok", False) for p in parity_reports) if parity_reports else True,
    }
    reasons: list[str] = []
    for p in parity_reports:
        reasons.extend(p.get("mismatch_reasons") or [])
    parity["mismatch_reasons"] = reasons[:100]

    q = quality_checks(states_100ms=all_100ms, attribution_rows=all_attr, selected=selected)

    return {
        "selected": selected,
        "not_selected": not_selected,
        "selection_manifest": sel_manifest,
        "groups": groups,
        "timeline": pd.DataFrame(all_timeline),
        "states_100ms": pd.DataFrame(all_100ms),
        "attribution": pd.DataFrame(all_attr),
        "attribution_summary": attribution_summary(all_attr),
        "refills": pd.DataFrame(all_refill),
        "walls": pd.DataFrame(all_walls),
        "support": pd.DataFrame(support_rows),
        "parity": parity,
        "quality": q,
        "blocked": blocked,
        "resources": {
            "bytes_read": bytes_read,
            "n_spans": len(spans),
            "n_selected": int(len(selected)),
            "n_groups": int(len(groups)),
            "union_start": str(union_start),
            "union_end": str(union_end),
        },
        "state_meta": state_meta,
        "causality_proof": {
            "future_returns_loaded": False,
            "outcomes_loaded": False,
            "mfe_mae": False,
            "post_trigger_window": False,
            "causal_end_policy": cfg.get("causal_end_policy"),
            "trigger_mutated": False,
        },
    }


def content_hash_bundle(result: dict[str, Any]) -> str:
    parts = []
    for key in ("selected", "groups", "support", "attribution", "states_100ms"):
        df = result.get(key)
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            parts.append(f"{key}:empty")
            continue
        if key == "selected":
            cols = [c for c in ["candidate_id", "candidate_type", "trigger_ts"] if c in df.columns]
            sub = df[cols].copy()
        elif key == "groups":
            cols = [c for c in ["group_id", "first_trigger_ts", "primary_candidate_id", "trigger_count"] if c in df.columns]
            sub = df[cols].copy()
        elif key == "support":
            cols = [c for c in ["candidate_id", "support_status"] if c in df.columns]
            sub = df[cols].copy()
        elif key == "attribution":
            cols = [c for c in ["event_time", "side", "price", "attribution_class", "matched_trade_notional"] if c in df.columns]
            sub = df[cols].copy() if cols else df.head(0)
        else:
            cols = [c for c in ["bucket_start", "taker_buy_notional", "taker_sell_notional", "bid_removed_notional", "ask_removed_notional"] if c in df.columns]
            sub = df[cols].copy() if cols else df.head(0)
        for c in sub.columns:
            if pd.api.types.is_datetime64_any_dtype(sub[c]):
                sub[c] = pd.to_datetime(sub[c], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        parts.append(json.dumps(sub.to_dict(orient="records"), sort_keys=True, default=str))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()

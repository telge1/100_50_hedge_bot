#!/usr/bin/env python3
"""Phase 3B — causal two-pool market structure detector (reads Phase 3A only)."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


def parse_origins(s: str | None) -> list[str]:
    if not s:
        return []
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return []


def stream_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_lower_events(path: Path) -> dict[str, list[dict[str, Any]]]:
    by_bar: dict[str, list[dict[str, Any]]] = {}
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            et = row.get("event_type") or ""
            if et not in (
                "POOL_NEW",
                "POOL_REACHED_BY_PRICE",
                "POOL_DISAPPEARED",
                "POOL_DENSITY_INCREASED",
                "POOL_DENSITY_DECREASED",
            ):
                continue
            t = row["candle_close_utc"]
            by_bar.setdefault(t, []).append(row)
    return by_bar


def gap_directly_below_pool(
    pool_bottom: float, gaps_lower: list[dict], min_pct: float
) -> dict[str, Any] | None:
    """Pool-to-pool gap whose top edge is at or below the pool lower bound."""
    best = None
    for g in gaps_lower:
        gap_top = float(g["gap_top_price"])
        gap_pct = float(g["gap_pct"])
        if gap_top <= pool_bottom + 1e-9 and gap_pct >= min_pct:
            if best is None or gap_top > float(best["gap_top_price"]):
                best = g
    return best


def gap_under_pool_zone(
    pool_bottom: float, pool_top: float, gaps_lower: list[dict], min_pct: float
) -> dict[str, Any] | None:
    """Prefer gap immediately under pool bottom; fallback to largest qualifying gap below top."""
    direct = gap_directly_below_pool(pool_bottom, gaps_lower, min_pct)
    if direct:
        return direct
    best = None
    for g in gaps_lower:
        gap_top = float(g["gap_top_price"])
        gap_pct = float(g["gap_pct"])
        if gap_top <= pool_top + 1e-9 and gap_pct >= min_pct:
            if best is None or float(g["gap_pct"]) > float(best["gap_pct"]):
                best = g
    return best


def pool_in_snapshot(pools: list[dict], track_id: str) -> dict | None:
    for p in pools:
        if p.get("pool_track_id") == track_id:
            return p
    return None


def origins_disjoint(a: list[str], b: list[str]) -> bool:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return True
    inter = sa & sb
    return len(inter) < min(len(sa), len(sb)) * 0.5


def upper_tp_candidates(
    upper_pools: list[dict], close: float, min_depth: int = 2
) -> list[dict[str, Any]]:
    out = []
    for u in upper_pools:
        if int(u.get("depth") or 0) < min_depth:
            continue
        bot = float(u["bottom_price"])
        if bot <= close:
            continue
        out.append(
            {
                "pool_track_id": u.get("pool_track_id"),
                "bottom_price": bot,
                "top_price": float(u["top_price"]),
                "depth": u.get("depth"),
                "origins": u.get("origins") or [],
                "distance_above_close_pct": (bot - close) / close * 100,
            }
        )
    out.sort(key=lambda x: x["bottom_price"])
    return out


@dataclass
class Episode:
    episode_id: int
    event_a_utc: str
    event_a_track_id: str
    event_a_bottom: float
    event_a_top: float
    event_a_depth: int
    event_a_origins: list[str]
    event_a_gap: dict[str, Any]
    pools_at_a: set[str]
    phase: str = "WATCH_REVERSAL"
    recovery_utc: str | None = None
    event_b_utc: str | None = None
    event_b_track_id: str | None = None
    event_b_bottom: float | None = None
    event_b_top: float | None = None
    event_b_origins: list[str] = field(default_factory=list)
    event_b_gap: dict[str, Any] | None = None
    long_candidate_utc: str | None = None
    tp_uppers: list[dict] = field(default_factory=list)
    identity_notes: list[str] = field(default_factory=list)
    first_seen_bars: dict[str, int] = field(default_factory=dict)


@dataclass
class DetectorState:
    market_state: str = "SHORT_SEARCH_ALLOWED"
    episodes: list[Episode] = field(default_factory=list)
    event_a_tracks_used: set[str] = field(default_factory=set)
    event_b_tracks_used: set[str] = field(default_factory=set)
    last_event_a_bar: str | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)
    timeline_rows: list[dict[str, Any]] = field(default_factory=list)
    reach_rows: list[dict[str, Any]] = field(default_factory=list)
    confirm_rows: list[dict[str, Any]] = field(default_factory=list)
    _next_episode_id: int = 1
    _pool_first_seen: dict[str, str] = field(default_factory=dict)


def _emit_transition(
    st: DetectorState,
    as_of: str,
    prev: str,
    new: str,
    trigger: str,
    evidence: dict[str, Any],
) -> None:
    st.transitions.append(
        {
            "as_of_close_utc": as_of,
            "previous_state": prev,
            "new_state": new,
            "trigger": trigger,
            **evidence,
        }
    )


def _global_state(st: DetectorState) -> str:
    if any(e.phase == "LONG_CANDIDATE" for e in st.episodes if e.long_candidate_utc):
        return "LONG_CANDIDATE"
    if any(e.phase == "WATCH_REVERSAL" for e in st.episodes):
        return "WATCH_REVERSAL"
    if any(e.identity_notes for e in st.episodes):
        return "UNRESOLVED"
    return "SHORT_SEARCH_ALLOWED"


def try_event_a(
    st: DetectorState,
    snap: dict,
    prev_snap: dict | None,
    events: list[dict],
    cfg: dict,
) -> None:
    min_gap = float(cfg["min_pool_gap_pct"])
    lowers = snap.get("lower_pools") or []
    gaps_lower = snap.get("gaps_lower") or []
    ohlc = snap["ohlc"]
    as_of = snap["candle_close_utc"]
    if st.last_event_a_bar == as_of:
        return

    best_candidate: tuple[float, dict, dict, dict] | None = None

    for ev in events:
        if ev.get("event_type") != "POOL_REACHED_BY_PRICE":
            continue
        track_id = ev.get("pool_track_id") or ""
        pool = pool_in_snapshot(lowers, track_id)
        if not pool:
            st.transitions.append(
                {
                    "as_of_close_utc": as_of,
                    "previous_state": st.market_state,
                    "new_state": "UNRESOLVED",
                    "trigger": "REACH_WITHOUT_VISIBLE_POOL",
                    "pool_track_id": track_id,
                }
            )
            continue
        if prev_snap and not pool_in_snapshot(prev_snap.get("lower_pools") or [], track_id):
            # Pool first visible same bar as reach — flag but allow if POOL_NEW not same bar only
            same_bar_new = any(
                e.get("event_type") == "POOL_NEW" and e.get("pool_track_id") == track_id
                for e in events
            )
            if same_bar_new:
                continue

        bot = float(pool["bottom_price"])
        top = float(pool["top_price"])
        if track_id in st.event_a_tracks_used:
            continue

        gap = gap_directly_below_pool(bot, gaps_lower, min_gap)
        if not gap:
            continue

        # OHLC corroboration (touch zone)
        if float(ohlc["h"]) < bot or float(ohlc["l"]) > top:
            continue
        close = float(ohlc["c"])
        if close < bot * 0.9985:
            continue

        score = float(gap["gap_pct"]) + (top - close) / close * 100
        cand = (score, ev, pool, gap)
        if best_candidate is None or cand[0] > best_candidate[0]:
            best_candidate = cand

    if not best_candidate:
        return

    _score, ev, pool, gap = best_candidate
    track_id = ev.get("pool_track_id") or ""
    bot = float(pool["bottom_price"])
    top = float(pool["top_price"])
    close = float(ohlc["c"])

    ep = Episode(
            episode_id=st._next_episode_id,
            event_a_utc=as_of,
            event_a_track_id=track_id,
            event_a_bottom=bot,
            event_a_top=top,
            event_a_depth=int(pool.get("depth") or ev.get("depth") or 0),
            event_a_origins=list(pool.get("origins") or parse_origins(ev.get("origins_json"))),
            event_a_gap={
                "gap_bottom_price": float(gap["gap_bottom_price"]),
                "gap_top_price": float(gap["gap_top_price"]),
                "gap_pct": float(gap["gap_pct"]),
                "large_gap_ge_1pct": float(gap["gap_pct"]) >= float(
                    cfg.get("min_pool_gap_highlight_pct", 1.0)
                ),
            },
            pools_at_a={p["pool_track_id"] for p in lowers},
        )
    st._next_episode_id += 1
    st.episodes.append(ep)
    st.event_a_tracks_used.add(track_id)
    st.last_event_a_bar = as_of
    st.reach_rows.append(
            {
                "episode_id": ep.episode_id,
                "reach_utc": as_of,
                "pool_track_id": track_id,
                "lower_bottom": bot,
                "lower_top": top,
                "depth": ep.event_a_depth,
                "origins": json.dumps(ep.event_a_origins),
                "gap_below_bottom": ep.event_a_gap["gap_bottom_price"],
                "gap_below_top": ep.event_a_gap["gap_top_price"],
                "gap_pct": ep.event_a_gap["gap_pct"],
                "candle_high": ohlc["h"],
                "candle_low": ohlc["l"],
                "candle_close": ohlc["c"],
            }
    )
    prev = st.market_state
    st.market_state = _global_state(st)
    if prev != st.market_state:
        _emit_transition(
            st,
            as_of,
            prev,
            st.market_state,
            "EVENT_A_LOWER_REACH_WITH_GAP",
            {
                "episode_id": ep.episode_id,
                "pool_track_id": track_id,
                "pool_bounds": [bot, top],
                "gap_bounds": [
                    ep.event_a_gap["gap_bottom_price"],
                    ep.event_a_gap["gap_top_price"],
                ],
                "gap_pct": ep.event_a_gap["gap_pct"],
                "origins": ep.event_a_origins,
            },
        )


def try_event_b(st: DetectorState, snap: dict, events: list[dict], cfg: dict) -> None:
    min_gap = float(cfg["min_pool_gap_pct"])
    as_of = snap["candle_close_utc"]
    close = float(snap["ohlc"]["c"])
    lowers = snap.get("lower_pools") or []
    gaps_lower = snap.get("gaps_lower") or []
    uppers = snap.get("upper_pools") or []

    open_eps = [
        e
        for e in st.episodes
        if not e.long_candidate_utc and as_of > e.event_a_utc
    ]
    if not open_eps:
        return
    open_eps.sort(key=lambda e: e.event_a_utc, reverse=True)
    ep = open_eps[0]

    if ep.recovery_utc is None and close > ep.event_a_top:
        ep.recovery_utc = as_of

    if ep.recovery_utc is None:
        return

    for ev in events:
            if ev.get("event_type") != "POOL_NEW":
                continue
            track_id = ev.get("pool_track_id") or ""
            if track_id in ep.pools_at_a:
                continue
            if track_id in st.event_b_tracks_used:
                continue
            pool = pool_in_snapshot(lowers, track_id)
            if not pool:
                continue
            bot = float(pool["bottom_price"])
            top = float(pool["top_price"])
            origins = list(pool.get("origins") or parse_origins(ev.get("origins_json")))

            if bot < ep.event_a_top - 1e-6:
                continue
            if not origins_disjoint(ep.event_a_origins, origins):
                ep.identity_notes.append(f"origins_overlap_{as_of}")
                continue
            if close < bot:
                continue

            gap = gap_under_pool_zone(bot, top, gaps_lower, min_gap)
            if not gap:
                continue

            tps = upper_tp_candidates(uppers, close, int(cfg.get("min_depth", 2)))
            if not tps:
                ep.identity_notes.append(f"no_upper_tp_{as_of}")
                continue

            ep.event_b_utc = as_of
            ep.event_b_track_id = track_id
            ep.event_b_bottom = bot
            ep.event_b_top = top
            ep.event_b_origins = origins
            ep.event_b_gap = {
                "gap_bottom_price": float(gap["gap_bottom_price"]),
                "gap_top_price": float(gap["gap_top_price"]),
                "gap_pct": float(gap["gap_pct"]),
            }
            ep.long_candidate_utc = as_of
            ep.phase = "LONG_CANDIDATE"
            ep.tp_uppers = tps
            st.event_b_tracks_used.add(track_id)

            st.confirm_rows.append(
                {
                    "episode_id": ep.episode_id,
                    "event_a_utc": ep.event_a_utc,
                    "recovery_utc": ep.recovery_utc,
                    "confirmation_close_utc": as_of,
                    "earliest_entry_open_utc": _next_bar_open(as_of),
                    "second_pool_track_id": track_id,
                    "second_lower_bottom": bot,
                    "second_lower_top": top,
                    "second_origins": json.dumps(origins),
                    "first_lower_bottom": ep.event_a_bottom,
                    "first_lower_top": ep.event_a_top,
                    "gap_under_second_bottom": ep.event_b_gap["gap_bottom_price"],
                    "gap_under_second_top": ep.event_b_gap["gap_top_price"],
                    "gap_pct": ep.event_b_gap["gap_pct"],
                    "close_at_confirmation": close,
                    "tp_upper_count": len(tps),
                    "research_note": (
                        "Confirmation on same bar as POOL_NEW; no extra persistence bars required in v1."
                    ),
                }
            )

            prev = st.market_state
            st.market_state = _global_state(st)
            if prev != st.market_state:
                _emit_transition(
                    st,
                    as_of,
                    prev,
                    st.market_state,
                    "EVENT_B_SECOND_LOWER_WITH_GAP_AND_TP",
                    {
                        "episode_id": ep.episode_id,
                        "first_pool": [ep.event_a_bottom, ep.event_a_top],
                        "second_pool": [bot, top],
                        "gap_under_second": ep.event_b_gap,
                        "tp_upper_count": len(tps),
                        "origins_second": origins,
                    },
                )
            return


def _next_bar_open(close_utc: str) -> str:
    from datetime import datetime, timedelta, timezone

    s = close_utc.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return (dt + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_detector(phase3a_dir: Path, cfg: dict) -> DetectorState:
    jsonl = phase3a_dir / "landscape_timeline.jsonl"
    lower_lc = phase3a_dir / "lower_pool_lifecycle.csv"
    events_by_bar = load_lower_events(lower_lc)

    st = DetectorState()
    prev_snap: dict | None = None

    for snap in stream_jsonl(jsonl):
        as_of = snap["candle_close_utc"]
        events = events_by_bar.get(as_of, [])

        for p in snap.get("lower_pools") or []:
            tid = p.get("pool_track_id")
            if tid and tid not in st._pool_first_seen:
                st._pool_first_seen[tid] = as_of

        try_event_a(st, snap, prev_snap, events, cfg)
        try_event_b(st, snap, events, cfg)

        st.market_state = _global_state(st)
        active_watch = [
            e.episode_id for e in st.episodes if e.phase == "WATCH_REVERSAL" and not e.long_candidate_utc
        ]
        active_long = [e.episode_id for e in st.episodes if e.long_candidate_utc]

        st.timeline_rows.append(
            {
                "as_of_close_utc": as_of,
                "market_state": st.market_state,
                "close": snap["ohlc"]["c"],
                "active_watch_episodes": json.dumps(active_watch),
                "active_long_episodes": json.dumps(active_long),
                "episode_count": len(st.episodes),
            }
        )
        prev_snap = snap

    return st


def blocked_short_candidates(
    st: DetectorState, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    blocked = []
    for c in candidates:
        sig = c.get("signal_close_utc") or ""
        for ep in st.episodes:
            if not ep.event_a_utc:
                continue
            if sig < ep.event_a_utc:
                continue
            if ep.long_candidate_utc and sig >= ep.long_candidate_utc:
                continue
            blocked.append(
                {
                    "candidate_id": c.get("candidate_id"),
                    "signal_close_utc": sig,
                    "blocked_by_episode_id": ep.episode_id,
                    "watch_from_utc": ep.event_a_utc,
                    "watch_until_utc": ep.long_candidate_utc or "",
                    "block_reason": "BLOCKED_BY_MARKET_STRUCTURE",
                    "state_at_block": "WATCH_REVERSAL",
                }
            )
            break
    return blocked

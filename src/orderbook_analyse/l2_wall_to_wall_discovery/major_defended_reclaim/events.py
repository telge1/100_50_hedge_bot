"""Causal major-wall → defense → reclaim event machine."""

from __future__ import annotations

import bisect
from collections import defaultdict, deque
from typing import Any

import pandas as pd

from orderbook_analyse.l2_wall_attack_discovery.models import tick_size
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim import (
    BAND_TICKS,
    BREAK_HOLD_S,
    BREAK_TICKS,
    MISSING,
    PERCENTILE_GATE,
    PERSISTENCE_GATE,
    PERSISTENCE_LOOKBACK_MS,
    REL_SIZE_GATE,
    REMAINING_RATIO_GATE,
    REPLENISH_RATIO_GATE,
    REPLENISH_WINDOW_MS,
    RECLAIM_HOLD_S,
    TEST_TOUCH_TICKS,
    WARMUP_GENUINE_S,
)
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.rich_samples import RichSample
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.util import (
    band_around,
    bisect_trades,
    in_band,
    percentile_rank_sorted,
)


ATTACK_SIDE = {"BID": "Sell", "ASK": "Buy"}


class Funnel:
    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)

    def hit(self, stage: str, n: int = 1) -> None:
        self.counts[stage] += n

    def rows(self) -> list[dict[str, Any]]:
        order = [
            "samples_seen",
            "warmup_excluded",
            "seq_gap_excluded",
            "wall_visible",
            "fail_rel_size",
            "fail_percentile",
            "fail_persistence",
            "major_wall_ok",
            "wall_tested",
            "missing_public_trade_confirmation",
            "fail_consumed_or_pulled",
            "fail_sustained_break",
            "wall_defended_not_consumed",
            "fail_reclaim",
            "reclaim_confirmed",
            "entry_emitted",
        ]
        out = []
        for k in order:
            out.append({"stage": k, "n": self.counts.get(k, 0)})
        for k, v in sorted(self.counts.items()):
            if k not in order:
                out.append({"stage": k, "n": v})
        return out


def _persistence_ratio(
    history: deque[tuple[int, float | None, float | None]],
    *,
    now_ms: int,
    anchor: float,
    tick: float,
) -> float | None:
    """Fraction of samples in prior 10s where a wall exists in ±2 ticks of anchor."""
    low, high = band_around(anchor, tick, BAND_TICKS)
    window = [(t, p) for t, p, _n in history if now_ms - PERSISTENCE_LOOKBACK_MS <= t < now_ms]
    if len(window) < 4:
        return None
    present = 0
    for _t, p in window:
        if p is not None and in_band(p, low, high):
            present += 1
    return present / len(window)


def _aggressive_notional(
    trades: pd.DataFrame,
    ts_index: list[int],
    *,
    start_ms: int,
    end_ms: int,
    side: str,
    band_low: float,
    band_high: float,
) -> float | None:
    if trades is None or trades.empty:
        return None
    i0, i1 = bisect_trades(ts_index, start_ms, end_ms)
    if i1 <= i0:
        return 0.0
    sub = trades.iloc[i0:i1]
    want = ATTACK_SIDE[side]
    hit = sub[(sub["side"] == want) & (sub["price"] >= band_low) & (sub["price"] <= band_high)]
    return float(hit["notional"].sum()) if len(hit) else 0.0


def detect_defended_reclaim_events(
    samples: list[RichSample],
    trades: pd.DataFrame,
    *,
    symbol: str,
    event_start_ms: int,
    event_end_ms: int,
    funnel: Funnel,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (events, major_wall_candidates)."""
    tick = tick_size(symbol)
    trade_ts = [] if trades is None or trades.empty else trades["ts_ms"].astype(int).tolist()
    # causal size histories per side (prior wall notionals; capped ~1h at 250ms)
    size_hist: dict[str, deque[float]] = {
        "BID": deque(maxlen=14_400),
        "ASK": deque(maxlen=14_400),
    }
    sorted_snap: dict[str, tuple[int, list[float]] | None] = {"BID": None, "ASK": None}
    # recent (ts, wall_price, wall_notional) for persistence
    persist_hist: dict[str, deque] = {
        "BID": deque(maxlen=80),
        "ASK": deque(maxlen=80),
    }
    genuine_seconds: set[int] = set()
    candidates: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    # active tests: wall_side -> state
    active: dict[str, dict[str, Any]] = {}
    emitted_keys: set[str] = set()
    eid = 0

    by_ts = {s.ts_ms: s for s in samples}
    ts_list = [s.ts_ms for s in samples]

    for si, s in enumerate(samples):
        funnel.hit("samples_seen")
        if s.warmup:
            funnel.hit("warmup_excluded")
            continue
        if s.seq_gap:
            funnel.hit("seq_gap_excluded")
            continue
        if s.genuine:
            genuine_seconds.add(s.ts_ms // 1000)
        # need 300 genuine historical seconds as warmup beyond sample warmup flag
        if len(genuine_seconds) < WARMUP_GENUINE_S:
            funnel.hit("insufficient_genuine_warmup")
            # still update histories after basic warmup path
        in_event_window = event_start_ms <= s.ts_ms < event_end_ms

        for side, wall in (("BID", s.bid_wall), ("ASK", s.ask_wall)):
            if wall is None:
                persist_hist[side].append((s.ts_ms, None, None))
                continue
            funnel.hit("wall_visible")
            # update persistence history with current observation (after measuring prior)
            pers = _persistence_ratio(persist_hist[side], now_ms=s.ts_ms, anchor=wall.price, tick=tick)
            rel = wall.relative_size
            # expensive percentile only if relative-size gate already passes;
            # refresh sorted snapshot every ~500 samples (still prior-only causal)
            pct = None
            if rel is not None and rel >= REL_SIZE_GATE and size_hist[side]:
                snap = sorted_snap[side]
                if snap is None or (s.ts_ms - snap[0]) >= 500 * 250:
                    sorted_snap[side] = (s.ts_ms, sorted(size_hist[side]))
                    snap = sorted_snap[side]
                pct = percentile_rank_sorted(snap[1], wall.notional)
            cand = {
                "symbol": symbol,
                "ts_ms": s.ts_ms,
                "side": side,
                "wall_price": wall.price,
                "wall_notional": wall.notional,
                "wall_relative_size": rel,
                "causal_wall_size_percentile": pct,
                "wall_persistence_ratio": pers,
                "median_notional_same_side": wall.median_notional,
                "n_levels": wall.n_levels,
                "band_low": wall.band_low,
                "band_high": wall.band_high,
                "source": wall.source,
                "is_major": False,
                "fail_gate": None,
            }
            # gates (report funnel even outside event window for diagnostics on in-window only)
            if rel is None or rel < REL_SIZE_GATE:
                cand["fail_gate"] = "rel_size"
                if in_event_window:
                    funnel.hit("fail_rel_size")
            elif pct is None or pct < PERCENTILE_GATE:
                cand["fail_gate"] = "percentile"
                if in_event_window:
                    funnel.hit("fail_percentile")
            elif pers is None or pers < PERSISTENCE_GATE:
                cand["fail_gate"] = "persistence"
                if in_event_window:
                    funnel.hit("fail_persistence")
            elif len(genuine_seconds) < WARMUP_GENUINE_S:
                cand["fail_gate"] = "warmup"
            elif s.seq_gap:
                cand["fail_gate"] = "seq_gap"
            else:
                cand["is_major"] = True
                if in_event_window:
                    funnel.hit("major_wall_ok")
                    candidates.append(cand)

            # advance histories AFTER using prior-only percentile
            size_hist[side].append(wall.notional)
            persist_hist[side].append((s.ts_ms, wall.price, wall.notional))

            if not in_event_window or not cand["is_major"]:
                continue

            # --- wall test ---
            touch_low = wall.band_low - TEST_TOUCH_TICKS * tick
            touch_high = wall.band_high + TEST_TOUCH_TICKS * tick
            # tighter: price within 1 tick of wall band edge toward mid
            if side == "BID":
                tested = s.mid <= wall.band_high + tick and s.mid >= wall.price - tick
            else:
                tested = s.mid >= wall.band_low - tick and s.mid <= wall.price + tick

            st = active.get(side)
            if st is None and tested:
                # look for aggressive trades in a short window around contact
                agg = _aggressive_notional(
                    trades,
                    trade_ts,
                    start_ms=s.ts_ms - 1000,
                    end_ms=s.ts_ms + 1000,
                    side=side,
                    band_low=wall.band_low - tick,
                    band_high=wall.band_high + tick,
                )
                if agg is None:
                    funnel.hit("missing_public_trade_confirmation")
                    continue
                if agg <= 0:
                    funnel.hit("missing_public_trade_confirmation")
                    continue
                funnel.hit("wall_tested")
                active[side] = {
                    "phase": "TESTING",
                    "wall_side": side,
                    "direction": "LONG" if side == "BID" else "SHORT",
                    "anchor": wall.price,
                    "band_low": wall.band_low,
                    "band_high": wall.band_high,
                    "wall_notional_at_test": wall.notional,
                    "wall_relative_size": rel,
                    "causal_wall_size_percentile": pct,
                    "wall_persistence_ratio": pers,
                    "wall_test_at": s.ts_ms,
                    "aggressive_notional_at_wall": agg,
                    "peak_notional_after_test": wall.notional,
                    "min_notional_after_test": wall.notional,
                    "defended_at": None,
                    "break_streak_ms": 0,
                    "last_ts": s.ts_ms,
                }
                st = active[side]

            if st is None:
                continue

            # track wall size in band after test
            cur_wall = s.bid_wall if side == "BID" else s.ask_wall
            cur_n = None
            if cur_wall is not None and in_band(cur_wall.price, st["band_low"] - tick, st["band_high"] + tick):
                cur_n = cur_wall.notional
            elif cur_wall is None:
                # pulled from book
                cur_n = 0.0

            if cur_n is not None:
                st["min_notional_after_test"] = min(st["min_notional_after_test"], cur_n)
                st["peak_notional_after_test"] = max(st["peak_notional_after_test"], cur_n)

            # sustained break check
            if side == "BID":
                broken = s.mid < st["band_low"] - BREAK_TICKS * tick
            else:
                broken = s.mid > st["band_high"] + BREAK_TICKS * tick
            dt = max(0, s.ts_ms - st["last_ts"])
            if broken:
                st["break_streak_ms"] += dt
            else:
                st["break_streak_ms"] = 0
            st["last_ts"] = s.ts_ms

            if st["break_streak_ms"] >= BREAK_HOLD_S * 1000:
                funnel.hit("fail_sustained_break")
                del active[side]
                continue

            # pulled before remaining evidence
            if cur_n == 0.0 and st["phase"] == "TESTING":
                # allow brief pull if replenished within 3s — check later
                st["pulled_at"] = s.ts_ms

            if st["phase"] == "TESTING":
                base = st["wall_notional_at_test"]
                remaining = (cur_n / base) if base > 0 and cur_n is not None else None
                # replenishment: peak after a dip within 3s
                repl = None
                if base > 0 and st["peak_notional_after_test"] is not None:
                    # if min dipped then peak recovered
                    if st["min_notional_after_test"] < base * 0.9:
                        repl = st["peak_notional_after_test"] / base
                    else:
                        repl = remaining
                elapsed = s.ts_ms - st["wall_test_at"]
                defended = False
                if remaining is not None and remaining >= REMAINING_RATIO_GATE:
                    defended = True
                if repl is not None and repl >= REPLENISH_RATIO_GATE and elapsed <= REPLENISH_WINDOW_MS + 2000:
                    defended = True
                # fully consumed
                if remaining is not None and remaining < 0.05 and elapsed > REPLENISH_WINDOW_MS:
                    funnel.hit("fail_consumed_or_pulled")
                    del active[side]
                    continue
                if defended and elapsed >= 250:
                    st["phase"] = "DEFENDED"
                    st["defended_at"] = s.ts_ms
                    st["remaining_wall_notional_ratio"] = remaining
                    st["wall_replenishment_ratio"] = repl
                    funnel.hit("wall_defended_not_consumed")

            if st["phase"] != "DEFENDED":
                # timeout test without defense
                if s.ts_ms - st["wall_test_at"] > 30_000:
                    funnel.hit("fail_defense_timeout")
                    del active[side]
                continue

            # --- reclaim ---
            if side == "BID":
                reclaim_edge = st["band_high"] + tick
                on_reclaim = s.mid > reclaim_edge
            else:
                reclaim_edge = st["band_low"] - tick
                on_reclaim = s.mid < reclaim_edge

            if "reclaim_streak_ms" not in st:
                st["reclaim_streak_ms"] = 0
                st["reclaim_last"] = s.ts_ms
            if on_reclaim:
                st["reclaim_streak_ms"] += max(0, s.ts_ms - st["reclaim_last"])
            else:
                st["reclaim_streak_ms"] = 0
            st["reclaim_last"] = s.ts_ms

            if st["reclaim_streak_ms"] < RECLAIM_HOLD_S * 1000:
                if s.ts_ms - st["defended_at"] > 120_000:
                    funnel.hit("fail_reclaim")
                    del active[side]
                continue

            # confirmed
            reclaim_confirmed_at = s.ts_ms
            decision_at = reclaim_confirmed_at
            # first sample strictly after decision_at
            entry = None
            for s2 in samples[si + 1 :]:
                if s2.ts_ms > decision_at:
                    entry = s2
                    break
            if entry is None:
                funnel.hit("fail_entry_after_decision")
                del active[side]
                continue

            key = f"{symbol}:{side}:{st['wall_test_at']}:{st['anchor']}"
            if key in emitted_keys:
                del active[side]
                continue
            emitted_keys.add(key)
            eid += 1
            funnel.hit("reclaim_confirmed")
            funnel.hit("entry_emitted")
            events.append(
                {
                    "event_id": f"mdr_{symbol}_{eid}",
                    "symbol": symbol,
                    "direction": st["direction"],
                    "wall_side": side,
                    "wall_anchor_price": st["anchor"],
                    "wall_band_low": st["band_low"],
                    "wall_band_high": st["band_high"],
                    "wall_notional": st["wall_notional_at_test"],
                    "wall_relative_size": st["wall_relative_size"],
                    "causal_wall_size_percentile": st["causal_wall_size_percentile"],
                    "wall_persistence_ratio": st["wall_persistence_ratio"],
                    "wall_test_at": st["wall_test_at"],
                    "aggressive_notional_at_wall": st["aggressive_notional_at_wall"],
                    "remaining_wall_notional_ratio": st.get("remaining_wall_notional_ratio"),
                    "wall_replenishment_ratio": st.get("wall_replenishment_ratio"),
                    "wall_defended_at": st["defended_at"],
                    "reclaim_confirmed_at": reclaim_confirmed_at,
                    "decision_at": decision_at,
                    "entry_at": entry.ts_ms,
                    "entry_price": entry.mid,
                    "entry_latency_ms": entry.ts_ms - decision_at,
                    "quality_status": "OK",
                    "wall_source": "per_level_mutable_book",
                }
            )
            del active[side]

    return events, candidates

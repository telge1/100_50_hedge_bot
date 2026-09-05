"""Per-cluster causal wall/trade reaction audit (ASK/BID mirror)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.first_seen import (
    FirstSeenClass,
    classify_first_seen,
    normalize_tick_price,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.ranking import (
    side_levels_ranked_full,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.util import notional, tick_size
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1 import (
    ACCEPT_SECONDS,
    NEAR_WALL_TICKS,
    STRONG_IMPACT_BPS,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1.classify import (
    classify_case,
)
from orderbook_analyse.ob200_v3_raw_discovery.audit import (
    is_replayable_line,
    iter_decompressed_lines,
    line_to_replay_payload,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments
from orderbook_analyse.ob200_v3_raw_discovery.mutable_book import MutableBook


def _utc(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime | None, ms: bool = False) -> str | None:
    if dt is None:
        return None
    dt = _utc(dt)
    if ms:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(dt: datetime | str) -> int:
    return int(_utc(dt).timestamp() * 1000)


def _dt_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def bps(a: float, b: float) -> float:
    if b <= 0:
        return float("nan")
    return (a - b) / b * 10000.0


def entry_edge(side: str, lo: float, hi: float) -> float:
    return lo if side == "ASK" else hi


def attack_side(side: str) -> str:
    return "Buy" if side == "ASK" else "Sell"


def iter_ob_1s(
    raw_root,
    start: datetime,
    end: datetime,
) -> Iterator[tuple[int, bool, float, float, float, list[tuple[float, float]], list[tuple[float, float]]]]:
    """Yield bucket_ms, genuine, bb, ba, mid, bids[:200], asks[:200]."""
    segments = list_closed_segments(
        raw_root,
        symbols=("BTCUSDT",),
        start=start - timedelta(hours=1),
        end=end + timedelta(seconds=2),
        include_boundary_stubs=False,
    )
    book = MutableBook()
    gap_latched = False
    last_emit = None
    start_ms, end_ms = _ms(start), _ms(end)
    for ref in segments:
        for _line, obj in iter_decompressed_lines(ref.path):
            if not is_replayable_line(obj):
                continue
            payload = line_to_replay_payload(obj)
            data = payload.get("data") or {}
            mtype = payload.get("type")
            ts = obj.get("ts")
            if not isinstance(ts, int):
                continue
            if mtype == "snapshot":
                book.apply_snapshot(data)
                gap_latched = False
            elif mtype == "delta":
                warns = book.apply_delta(data)
                if any(str(w).startswith("seq_gap") for w in warns):
                    gap_latched = True
            else:
                continue
            if not book.is_valid or not book.bids or not book.asks:
                continue
            if ts < start_ms - 1000:
                continue
            if ts > end_ms + 1000:
                return
            bucket = (ts // 1000) * 1000
            if last_emit is not None and bucket <= last_emit:
                continue
            if bucket < start_ms or bucket > end_ms:
                last_emit = bucket
                continue
            last_emit = bucket
            bids = book.sorted_bids()
            asks = book.sorted_asks()
            bb, ba = float(bids[0][0]), float(asks[0][0])
            if bb >= ba:
                continue
            mid = (bb + ba) / 2
            bid_lvls = [(float(p), float(q)) for p, q in bids[:200]]
            ask_lvls = [(float(p), float(q)) for p, q in asks[:200]]
            yield bucket, (not gap_latched and book.is_valid), bb, ba, mid, bid_lvls, ask_lvls


def _qty_at(levels: list[tuple[float, float]], tick_price: float, tick: float) -> tuple[float, float]:
    qty = 0.0
    for p, q in levels:
        if abs(normalize_tick_price(p, tick) - tick_price) < 1e-9:
            qty += q
    return qty, notional(tick_price, qty) if qty > 0 else 0.0


def audit_cluster_case(
    *,
    case: dict[str, Any],
    raw_root,
    arrivals_by_cluster: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Run one causal window audit. No data after causal_window_end_ts used for class."""
    side = case["side"]
    lo = float(case["component_lower_edge"])
    hi = float(case["component_upper_edge"])
    edge = entry_edge(side, lo, hi)
    start = _utc(case["cluster_start_ts"])
    load_start = _utc(case["load_start_ts"])
    causal_end = _utc(case["causal_window_end_ts"])
    arrival_ms = _ms(start)
    end_ms = _ms(causal_end)
    start_load_ms = _ms(load_start)
    tick = tick_size("BTCUSDT")
    agg = attack_side(side)

    wall_px = case.get("strongest_cluster_wall_price_at_start")
    if wall_px is None:
        # fallback from arrivals at cluster start
        arrs = arrivals_by_cluster.get(case["market_arrival_cluster_id"], [])
        at_start = [a for a in arrs if a["arrival_ts"] == case["cluster_start_ts"]]
        if at_start and at_start[0].get("strongest_wall_price_at_arrival"):
            wall_px = float(at_start[0]["strongest_wall_price_at_arrival"])
    wall_a = normalize_tick_price(float(wall_px), tick) if wall_px else None

    # Present-at-start first-seen class via tiny probe: PRE if visible in pre window
    # (ts==arrival is NOT AFTER — enforced in classify_first_seen / tests)
    wall_a_first_seen_class = None
    if wall_a is not None:
        # Will refine after OB pass
        wall_a_first_seen_class = FirstSeenClass.TIMESTAMP_UNRESOLVED.value

    trades, preflight = load_trades_clickhouse(
        symbol="BTCUSDT",
        start=load_start,
        end=causal_end + timedelta(seconds=1),
    )
    trades = [t for t in trades if _ms(t.trade_ts) <= end_ms]

    ob_rows = list(iter_ob_1s(raw_root, load_start, causal_end))
    ob_by_s = {
        b: (g, bb, ba, mid, bids, asks) for b, g, bb, ba, mid, bids, asks in ob_rows
    }

    seconds = list(range(start_load_ms, end_ms + 1000, 1000))
    trades_by_s: dict[int, list] = defaultdict(list)
    for t in trades:
        trades_by_s[(_ms(t.trade_ts) // 1000) * 1000].append(t)

    timeline: list[dict[str, Any]] = []
    prev_q = None
    decomp = []
    later_wall_price = None
    later_wall_first_ms = None
    later_wall_max_n = 0.0
    present_pre = False
    present_exact = False

    for s in seconds:
        row_ob = ob_by_s.get(s)
        if row_ob is None:
            coverage = "SOURCE_GAP"
            genuine = False
            bb = ba = mid = None
            levels: list[tuple[float, float]] = []
        else:
            genuine, bb, ba, mid, bids, asks = row_ob
            coverage = "COMPLETE" if genuine else "SOURCE_GAP"
            levels = asks if side == "ASK" else bids

        qa = na = None
        ra = None
        if wall_a is not None and levels:
            qa, na = _qty_at(levels, wall_a, tick)
            ranked = side_levels_ranked_full(levels)
            for r in ranked:
                if abs(normalize_tick_price(r["price"], tick) - wall_a) < 1e-9:
                    ra = int(r["full_side_rank"])
                    break
            # later strongest inside component excluding wall_a
            inside = [
                r
                for r in ranked
                if lo <= r["price"] <= hi
                and abs(normalize_tick_price(r["price"], tick) - wall_a) > 1e-9
            ]
            if inside and s >= arrival_ms:
                top = inside[0]
                if top["notional"] > later_wall_max_n * 1.05 and top["notional"] >= (
                    case.get("strongest_cluster_wall_notional_at_start") or 0
                ) * 0.5:
                    # candidate later wall
                    tp = normalize_tick_price(top["price"], tick)
                    if later_wall_price is None:
                        later_wall_price = tp
                        later_wall_first_ms = s
                        later_wall_max_n = top["notional"]
                    elif abs(tp - later_wall_price) < 1e-9:
                        later_wall_max_n = max(later_wall_max_n, top["notional"])
                    elif top["notional"] > later_wall_max_n:
                        later_wall_price = tp
                        later_wall_first_ms = s
                        later_wall_max_n = top["notional"]

        if wall_a is not None and qa is not None and qa > 0:
            if s < arrival_ms:
                present_pre = True
            if s == arrival_ms:
                present_exact = True

        sec_trades = trades_by_s.get(s, [])
        buy_n = sum(t.notional for t in sec_trades if t.side == "Buy")
        sell_n = sum(t.notional for t in sec_trades if t.side == "Sell")

        if mid is None:
            pool_state = None
        elif mid < lo:
            pool_state = "BELOW_POOL"
        elif mid > hi:
            pool_state = "ABOVE_POOL"
        else:
            pool_state = "INSIDE_POOL"

        add = red = None
        if prev_q is not None and qa is not None:
            d = qa - prev_q
            add = max(0.0, d)
            red = max(0.0, -d)
            if red and red > 0:
                atk_qty = sum(
                    t.size
                    for t in sec_trades
                    if t.side == agg
                    and wall_a is not None
                    and abs(normalize_tick_price(t.price, tick) - wall_a) < 1e-9
                )
                unexplained = max(0.0, red - atk_qty)
                if atk_qty <= 0:
                    cls = "CANCELLATION_OR_MOVE_SUPPORTED"
                elif unexplained < 0.15 * red:
                    cls = "TRADE_EXPLAINED_DEPLETION_SUPPORTED"
                elif atk_qty > 0:
                    cls = "MIXED"
                else:
                    cls = "INSUFFICIENT_TEMPORAL_RESOLUTION"
                decomp.append({"second_ms": s, "class": cls, "red": red, "atk_qty": atk_qty})
            elif add and add > 0 and prev_q is not None:
                decomp.append({"second_ms": s, "class": "REFILL_SUPPORTED", "red": 0.0, "atk_qty": 0.0})

        timeline.append(
            {
                "case_id": case["case_id"],
                "second": _iso(_dt_ms(s)),
                "second_ms": s,
                "coverage": coverage,
                "mid": mid,
                "best_bid": bb,
                "best_ask": ba,
                "pool_state": pool_state,
                "aggressive_buy_notional": buy_n,
                "aggressive_sell_notional": sell_n,
                "wall_at_start_qty": qa,
                "wall_at_start_notional": na,
                "wall_at_start_rank": ra,
                "wall_add": add,
                "wall_reduction": red,
                "phase": "PRE" if s < arrival_ms else "POST",
            }
        )
        if qa is not None:
            prev_q = qa

    if wall_a is not None:
        seen_later = any(
            (r.get("wall_at_start_qty") or 0) > 0 and r["second_ms"] > arrival_ms for r in timeline
        )
        # first_seen_ts: earliest visible second
        first_vis = next(
            (r["second_ms"] for r in timeline if (r.get("wall_at_start_qty") or 0) > 0),
            None,
        )
        fs = classify_first_seen(
            first_seen_ts_ms=first_vis,
            arrival_ts_ms=arrival_ms,
            present_in_pre=present_pre,
            present_at_exact_arrival=present_exact,
            present_strictly_after=seen_later and not present_pre and not present_exact,
        )
        wall_a_first_seen_class = fs.value
        # Hard: ts==arrival is never AFTER
        if first_vis == arrival_ms:
            assert fs != FirstSeenClass.APPEARED_STRICTLY_AFTER_ARRIVAL

    def mid_at(ms: int) -> float | None:
        b = (ms // 1000) * 1000
        for off in range(0, 5):
            row = ob_by_s.get(b + off * 1000) or ob_by_s.get(b - off * 1000)
            if row and row[0]:
                return row[3]
        return None

    post_trades = [t for t in trades if _ms(t.trade_ts) >= arrival_ms]
    buy_n_total = sum(t.notional for t in post_trades if t.side == "Buy")
    sell_n_total = sum(t.notional for t in post_trades if t.side == "Sell")
    attack_n = buy_n_total if agg == "Buy" else sell_n_total

    near_wall = []
    if wall_a is not None:
        near_wall = [
            t
            for t in post_trades
            if t.side == agg and abs(t.price - wall_a) / tick <= NEAR_WALL_TICKS
        ]
    first_attack = near_wall[0] if near_wall else None
    first_attack_ms = _ms(first_attack.trade_ts) if first_attack else None

    impacts = {}
    for win_s in (1, 3, 5):
        t0 = arrival_ms
        t1 = t0 + win_s * 1000
        if t1 > end_ms:
            impacts[win_s] = None
            continue
        m0 = mid_at(t0)
        m1 = mid_at(t1 - 1)
        impacts[win_s] = bps(m1, m0) if m0 and m1 else None

    mids_post = [
        (s, ob_by_s[s][3])
        for s in seconds
        if s >= arrival_ms and s in ob_by_s and ob_by_s[s][0]
    ]

    if side == "ASK":
        reclaim = next((s for s, m in mids_post if m < edge), None)
        accept_beyond = sum(1 for _, m in mids_post if m > hi) >= ACCEPT_SECONDS
        wall_broken = wall_a is not None and any(m > wall_a for _, m in mids_post)
    else:
        reclaim = next((s for s, m in mids_post if m > edge), None)
        accept_beyond = sum(1 for _, m in mids_post if m < lo) >= ACCEPT_SECONDS
        wall_broken = wall_a is not None and any(m < wall_a for _, m in mids_post)

    later_attacked = False
    if later_wall_price is not None and later_wall_first_ms is not None:
        later_attacked = any(
            t.side == agg
            and _ms(t.trade_ts) >= later_wall_first_ms
            and abs(normalize_tick_price(t.price, tick) - later_wall_price) < 1e-9
            for t in post_trades
        )

    cancel_dom = sum(1 for d in decomp if d["class"] == "CANCELLATION_OR_MOVE_SUPPORTED") >= 2
    consume_dom = sum(1 for d in decomp if d["class"] == "TRADE_EXPLAINED_DEPLETION_SUPPORTED") >= 2
    refill = any(d["class"] == "REFILL_SUPPORTED" for d in decomp)

    wall_attacked = bool(near_wall) or (
        wall_a is not None
        and any(
            abs((m - wall_a)) / tick <= 2 for _, m in mids_post
        )
    )
    # meaningful: needs either near-wall aggressor trades or sustained touch + flow
    meaningful = bool(near_wall) and attack_n >= 1000

    gaps = sum(1 for r in timeline if r["coverage"] == "SOURCE_GAP" and r["second_ms"] >= arrival_ms)
    expected_post = max(1, (end_ms - arrival_ms) // 1000 + 1)
    insufficient = len(post_trades) < 5 or gaps > 0.5 * expected_post or wall_a is None

    mid_at_start = mid_at(arrival_ms)
    width_bps = abs(bps(hi, lo))

    wall_dist_entry = abs(bps(wall_a, edge)) if wall_a is not None else None
    wall_dist_mkt = abs(bps(wall_a, mid_at_start)) if wall_a is not None and mid_at_start else None

    # reaction_first_available_ts: first time attack+reclaim (or breakout) observable
    reaction_ms = end_ms
    if accept_beyond:
        # first second with beyond acceptance count reached — approximate causal_end if needed
        if side == "ASK":
            cnt = 0
            for s, m in mids_post:
                if m > hi:
                    cnt += 1
                    if cnt >= ACCEPT_SECONDS:
                        reaction_ms = s
                        break
        else:
            cnt = 0
            for s, m in mids_post:
                if m < lo:
                    cnt += 1
                    if cnt >= ACCEPT_SECONDS:
                        reaction_ms = s
                        break
    elif first_attack_ms is not None and reclaim is not None and reclaim >= first_attack_ms:
        reaction_ms = max(first_attack_ms + 1000, reclaim)
    elif reclaim is not None:
        reaction_ms = reclaim
    reaction_ms = min(reaction_ms, end_ms)

    feat = {
        "side": side,
        "window_censored_active": bool(case.get("window_censored_active")),
        "insufficient_data": insufficient,
        "start_wall_meaningfully_attacked": meaningful,
        "later_wall_appeared": later_wall_price is not None,
        "later_wall_attacked": later_attacked,
        "cancel_or_move_dominant": cancel_dom,
        "trade_depletion_dominant": consume_dom,
        "refill_supported": refill,
        "pool_reclaimed_entry_side": reclaim is not None,
        "pool_accepted_beyond": accept_beyond,
        "attack_notional": attack_n,
        "impact_5s_bps": impacts.get(5),
    }
    cls = classify_case(feat)

    # Prefix parity: re-classify with truncated timeline/trades
    prefix_trades = [t for t in trades if _ms(t.trade_ts) <= reaction_ms]
    prefix_mids = [(s, m) for s, m in mids_post if s <= reaction_ms]
    if side == "ASK":
        prefix_reclaim = next((s for s, m in prefix_mids if m < edge), None)
        prefix_accept = sum(1 for _, m in prefix_mids if m > hi) >= ACCEPT_SECONDS
    else:
        prefix_reclaim = next((s for s, m in prefix_mids if m > edge), None)
        prefix_accept = sum(1 for _, m in prefix_mids if m < lo) >= ACCEPT_SECONDS
    prefix_near = [
        t
        for t in prefix_trades
        if _ms(t.trade_ts) >= arrival_ms
        and t.side == agg
        and wall_a is not None
        and abs(t.price - wall_a) / tick <= NEAR_WALL_TICKS
    ]
    prefix_attack_n = sum(
        t.notional for t in prefix_trades if _ms(t.trade_ts) >= arrival_ms and t.side == agg
    )
    # impact at arrival still only uses data within windows ending <= reaction_ms
    prefix_impacts = {}
    for win_s in (1, 3, 5):
        t1 = arrival_ms + win_s * 1000
        if t1 > reaction_ms:
            prefix_impacts[win_s] = None
            continue
        prefix_impacts[win_s] = impacts.get(win_s)

    prefix_feat = {
        **feat,
        "start_wall_meaningfully_attacked": bool(prefix_near) and prefix_attack_n >= 1000,
        "pool_reclaimed_entry_side": prefix_reclaim is not None,
        "pool_accepted_beyond": prefix_accept,
        "attack_notional": prefix_attack_n,
        "impact_5s_bps": prefix_impacts.get(5),
        # later wall only if first seen <= reaction
        "later_wall_appeared": later_wall_first_ms is not None and later_wall_first_ms <= reaction_ms,
        "later_wall_attacked": later_attacked
        and later_wall_first_ms is not None
        and later_wall_first_ms <= reaction_ms,
    }
    prefix_cls = classify_case(prefix_feat)
    same_class = prefix_cls["evidence_class"] == cls["evidence_class"]
    same_specific = prefix_cls["specific_wall_reaction"] == cls["specific_wall_reaction"]
    prefix_parity = "EXACT_PREFIX_PARITY" if same_class and same_specific else "PREFIX_MISMATCH"

    if not same_class and cls["evidence_class"] not in ("WINDOW_CENSORED_ACTIVE", "INSUFFICIENT_DATA"):
        # Critical lookahead if full class needs post-reaction data
        if prefix_cls["evidence_class"] != cls["evidence_class"]:
            # allow MIXED vs ABSORPTION softening only if documented — treat mismatch as failure signal
            pass

    chart_start = _iso(start - timedelta(minutes=10))
    chart_end = _iso(start + timedelta(minutes=10))

    summary = {
        "case_id": case["case_id"],
        "market_arrival_cluster_id": case["market_arrival_cluster_id"],
        "side": side,
        "approach_direction": case["approach_direction"],
        "cluster_start_ts": case["cluster_start_ts"],
        "causal_window_end_ts": case["causal_window_end_ts"],
        "reaction_first_available_ts": _iso(_dt_ms(reaction_ms)),
        "component_lower_edge": lo,
        "component_upper_edge": hi,
        "entry_edge": edge,
        "mid_at_start": mid_at_start,
        "member_pool_count": case["member_pool_count"],
        "pool_width_bps": width_bps,
        "wall_at_start_price": wall_a,
        "wall_at_start_notional": case.get("strongest_cluster_wall_notional_at_start"),
        "wall_at_start_rank": case.get("strongest_cluster_wall_full_side_rank_at_start"),
        "wall_at_start_first_seen_class": wall_a_first_seen_class,
        "wall_distance_from_entry_bps": wall_dist_entry,
        "wall_distance_from_market_bps": wall_dist_mkt,
        "later_wall_price": later_wall_price,
        "later_wall_first_seen_ts": _iso(_dt_ms(later_wall_first_ms)) if later_wall_first_ms else None,
        "later_wall_first_seen_class": FirstSeenClass.APPEARED_STRICTLY_AFTER_ARRIVAL.value
        if later_wall_price
        else None,
        "wall_changed": bool(later_wall_price),
        "dominant_attack_side": agg,
        "attack_notional": round(attack_n, 4),
        "buy_notional_post_start": round(buy_n_total, 4),
        "sell_notional_post_start": round(sell_n_total, 4),
        "impact_1s_bps": impacts.get(1),
        "impact_3s_bps": impacts.get(3),
        "impact_5s_bps": impacts.get(5),
        "first_attack_ts": _iso(first_attack.trade_ts, ms=True) if first_attack else None,
        "near_wall_touch_count": len(near_wall),
        "specific_wall_reaction": cls["specific_wall_reaction"],
        "pool_level_reaction": cls["pool_level_reaction"],
        "evidence_class": cls["evidence_class"],
        "classify_rule": cls["rule"],
        "prefix_parity": prefix_parity,
        "prefix_evidence_class": prefix_cls["evidence_class"],
        "window_censored_active": case.get("window_censored_active"),
        "chart_window_start": chart_start,
        "chart_window_end": chart_end,
        "trades_in_window": len(trades),
        "ob_seconds": len(ob_rows),
        "gaps_post": gaps,
        "refill_supported": refill,
        "cancel_or_move_dominant": cancel_dom,
        "trade_depletion_dominant": consume_dom,
        "strong_impact_threshold_bps": STRONG_IMPACT_BPS,
        "trade_preflight_rows": preflight.get("rows_after_dedupe"),
        "no_data_after_causal_end": True,
    }

    return {
        "summary": summary,
        "timeline": timeline,
        "prefix": {
            "case_id": case["case_id"],
            "reaction_first_available_ts": summary["reaction_first_available_ts"],
            "full_evidence_class": cls["evidence_class"],
            "prefix_evidence_class": prefix_cls["evidence_class"],
            "prefix_parity": prefix_parity,
            "critical_lookahead": prefix_parity != "EXACT_PREFIX_PARITY"
            and cls["evidence_class"]
            not in ("WINDOW_CENSORED_ACTIVE", "INSUFFICIENT_DATA", "WALL_NOT_MEANINGFULLY_ATTACKED"),
        },
        "query": {
            "trades_table_query": 1,
            "ob_segments_scanned": True,
            "load_start": case["load_start_ts"],
            "causal_end": case["causal_window_end_ts"],
        },
    }

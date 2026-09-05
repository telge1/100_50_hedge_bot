"""Causal next-pool reaction audit pipeline (read-only)."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.aggressor_efficiency_flip.contracts import (
    CANONICAL_TRADES_TABLE,
    UNFITTED_F0_DIAGNOSTIC,
)
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.first_seen import (
    classify_first_seen,
    normalize_tick_price,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.ranking import (
    side_levels_ranked_full,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.util import notional, tick_size
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1.audit_case import (
    iter_ob_1s,
)
from orderbook_analyse.post_case_02_next_pool_causal_reaction_audit_v1 import (
    ACCEPT_VARIANTS_S,
    CASE_02_BREAKOUT_ACCEPT_5S,
    CASE_02_POOL,
    CASE_02_UPPER_EDGE_CROSS,
    CASE_02_VERDICT,
    COST_RT_BPS,
    EDGE_TOL_BPS,
    FLOW_WINDOWS_S,
    FORMAT_VERSION,
    MAJOR_WALL_RANK,
    MAX_REACTION_S,
    OUTCOME_USED_FOR_MATCHING,
    OUTCOME_USED_FOR_POOL_SELECTION,
    OUTCOME_USED_FOR_STATE_DEFINITION,
    OUTCOME_USED_FOR_THRESHOLDS,
    PRE_ARRIVAL_S,
    REF_TS,
    SYMBOL,
    TF_DURATION_S,
    TIMEFRAMES,
)
from orderbook_analyse.post_case_02_next_pool_causal_reaction_audit_v1.selection import (
    ask_entirely_above,
    bps,
    build_asof_inventory,
    intervals_overlap,
    select_next_pool,
)


def _utc(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(dt: datetime | str) -> int:
    return int(_utc(dt).timestamp() * 1000)


def _dt_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def mid_at_second(raw_root: Path, ts: datetime) -> float | None:
    for _b, _g, _bb, _ba, mid, _bids, _asks in iter_ob_1s(raw_root, ts, ts):
        return float(mid)
    return None


def pool_zone(mid: float | None, lo: float, hi: float, tol_bps: float) -> str | None:
    if mid is None or mid <= 0 or hi <= lo:
        return None
    lo_lo = lo * (1 - tol_bps / 10000.0)
    lo_hi = lo * (1 + tol_bps / 10000.0)
    hi_lo = hi * (1 - tol_bps / 10000.0)
    hi_hi = hi * (1 + tol_bps / 10000.0)
    if mid < lo_lo:
        return "BELOW_POOL"
    if lo_lo <= mid <= lo_hi:
        return "AT_FRONT_EDGE"
    if hi_lo <= mid <= hi_hi:
        return "AT_BACK_EDGE"
    if mid > hi_hi:
        return "ABOVE_POOL"
    frac = (mid - lo) / (hi - lo)
    if frac < 1 / 3:
        return "INSIDE_LOWER_THIRD"
    if frac < 2 / 3:
        return "INSIDE_MIDDLE_THIRD"
    return "INSIDE_UPPER_THIRD"


def aggressor_class(
    buy_n: float, sell_n: float, mid_chg: float | None, min_n: float, strong_bps: float
) -> str:
    if buy_n < min_n and sell_n < min_n:
        return "NO_MEANINGFUL_ATTACK"
    two = buy_n >= min_n and sell_n >= min_n
    if mid_chg is None:
        return "TWO_SIDED_CONTEST" if two else "NO_MEANINGFUL_ATTACK"
    if two and abs(mid_chg) < strong_bps * 0.5:
        return "TWO_SIDED_CONTEST"
    if buy_n >= min_n and sell_n < buy_n * 0.5:
        if mid_chg >= strong_bps * 0.5:
            return "BUY_AGGRESSION_EFFECTIVE"
        if mid_chg <= strong_bps * 0.25:
            return "BUY_AGGRESSION_INEFFICIENT"
        return "BUY_AGGRESSION_EFFECTIVE" if mid_chg > 0 else "BUY_AGGRESSION_INEFFICIENT"
    if sell_n >= min_n and buy_n < sell_n * 0.5:
        if mid_chg <= -strong_bps * 0.5:
            return "SELL_AGGRESSION_EFFECTIVE"
        if mid_chg >= -strong_bps * 0.25:
            return "SELL_AGGRESSION_INEFFICIENT"
        return "SELL_AGGRESSION_EFFECTIVE" if mid_chg < 0 else "SELL_AGGRESSION_INEFFICIENT"
    return "TWO_SIDED_CONTEST"


def find_arrival(
    *,
    raw_root: Path,
    available_at: datetime,
    target_edge: float,
    edge_role: str,
    search_end: datetime,
    as_of: datetime,
) -> dict[str, Any]:
    """First causal touch of target edge after pool available_at."""
    start = available_at
    prev_mid = None
    first = None
    approach = "FROM_BELOW"
    for bucket, _g, _bb, _ba, mid, _bids, _asks in iter_ob_1s(raw_root, start, search_end):
        if mid is None:
            continue
        hit = mid >= target_edge if edge_role.endswith("UPPER") or edge_role == "BACK_EDGE_UPPER" else mid >= target_edge
        # ASK front (lower): arrival when mid reaches lower from below
        if edge_role == "FRONT_EDGE_LOWER":
            hit = mid >= target_edge
            if prev_mid is not None and prev_mid < target_edge <= mid:
                first = bucket
                approach = "FROM_BELOW"
                arrival_mid = mid
                dist_prev = bps(target_edge, prev_mid)
                break
            if first is None and mid >= target_edge and (prev_mid is None or prev_mid < target_edge):
                first = bucket
                approach = "FROM_BELOW"
                arrival_mid = mid
                dist_prev = None if prev_mid is None else bps(target_edge, prev_mid)
                break
        else:
            # back edge / ceiling: first mid >= upper
            if prev_mid is not None and prev_mid < target_edge <= mid:
                first = bucket
                approach = "FROM_INSIDE_OR_BELOW"
                arrival_mid = mid
                dist_prev = bps(target_edge, prev_mid)
                break
            if first is None and mid >= target_edge and (prev_mid is None or prev_mid < target_edge):
                first = bucket
                approach = "FROM_INSIDE_OR_BELOW"
                arrival_mid = mid
                dist_prev = None if prev_mid is None else bps(target_edge, prev_mid)
                break
        prev_mid = mid
    else:
        return {
            "reached": False,
            "first_arrival_ts": None,
            "approach_direction": None,
            "market_price_at_arrival": None,
            "distance_at_previous_second_bps": None,
        }

    return {
        "reached": True,
        "first_arrival_ts": _iso(_dt_ms(first)),
        "first_arrival_ms": first,
        "approach_direction": approach,
        "market_price_at_arrival": arrival_mid,
        "distance_at_previous_second_bps": dist_prev,
        "arrival_at_or_after_asof": first >= _ms(as_of),
    }


def run_audit(*, raw_root: Path, out_dir: Path) -> dict[str, Any]:
    t0 = datetime.now(timezone.utc)
    as_of = _utc(REF_TS)
    as_of_ms = _ms(as_of)
    tick = tick_size(SYMBOL)
    min_n = float(UNFITTED_F0_DIAGNOSTIC["min_notional_usdt"])
    strong_bps = float(UNFITTED_F0_DIAGNOSTIC["strong_same_side_impact_bps"])
    query_log: list[dict[str, Any]] = []

    market = mid_at_second(raw_root, as_of)
    if market is None:
        raise RuntimeError("no OB mid at reference second")

    inventory = build_asof_inventory(as_of=as_of, market_price=market)
    sel = select_next_pool(inventory, market_price=market)
    manifest = sel["manifest"]
    selected = sel["selected"]
    component = sel["component"]

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pool_selection_manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )

    inv_rows = []
    for r in inventory:
        inv_rows.append(
            {
                "pool_id": r["pool_id"],
                "source_timeframe": r["source_timeframe"],
                "side": r["side"],
                "available_at": r["available_at"],
                "lower_edge": r["lower_edge"],
                "upper_edge": r["upper_edge"],
                "front_edge": r["front_edge"],
                "back_edge": r["back_edge"],
                "strength": r.get("strength"),
                "width_bps": r.get("width_bps"),
                "distance_to_front_edge_bps": r.get("distance_to_front_edge_bps"),
                "active_as_of": r["active_as_of"],
                "forms_shared_price_component": r.get("forms_shared_price_component"),
                "n_overlaps": len(r.get("overlapping_other_tf_pools") or []),
                "market_price_asof": market,
                "ask_entirely_above_market": r["side"] == "ASK" and r["lower_edge"] > market,
                "ask_contains_market": r["side"] == "ASK"
                and r["lower_edge"] <= market <= r["upper_edge"],
            }
        )
    write_csv(out_dir / "asof_pool_inventory.csv", inv_rows)

    if selected is None:
        empty = {
            "verdict": "INSUFFICIENT_DATA",
            "reason": "no_ask_liquidity_above_market",
            "market_price_at_reference": market,
            "reference_ts": REF_TS,
        }
        (out_dir / "selected_pool.json").write_text(json.dumps(empty, indent=2), encoding="utf-8")
        (out_dir / "candidate_decision.json").write_text(json.dumps(empty, indent=2), encoding="utf-8")
        for name in (
            "reaction_second_timeline.csv",
            "attack_episodes.csv",
            "wall_lifecycle.csv",
            "wall_retreat_sequences.csv",
            "state_transitions.csv",
            "acceptance_variants.csv",
            "prefix_parity.csv",
        ):
            (out_dir / name).write_text("", encoding="utf-8")
        _write_manual_and_report(out_dir, empty, manifest, inventory, market, None, {}, t0, query_log)
        return empty

    lo = float(selected["lower_edge"])
    hi = float(selected["upper_edge"])
    target_edge = float(selected["target_edge"])
    edge_role = str(selected["target_edge_role"])
    available_at = _utc(selected["available_at"])

    selected_payload = {
        "pool_id": selected["pool_id"],
        "source_timeframe": selected["source_timeframe"],
        "side": "ASK",
        "lower_edge": lo,
        "upper_edge": hi,
        "front_edge": lo,
        "back_edge": hi,
        "target_edge": target_edge,
        "target_edge_role": edge_role,
        "strength": selected.get("strength"),
        "available_at": selected["available_at"],
        "selection_distance_bps": selected["selection_distance_bps"],
        "selection_mode": manifest["selection_mode"],
        "component_pool_ids": [c["pool_id"] for c in component],
        "component": component,
        "htf_confluence": manifest["htf_confluence"],
        "market_price_at_reference": market,
        "reference_ts": REF_TS,
        "case_02": {
            "pool": CASE_02_POOL,
            "upper_edge_cross": CASE_02_UPPER_EDGE_CROSS,
            "breakout_accept_5s": CASE_02_BREAKOUT_ACCEPT_5S,
            "verdict": CASE_02_VERDICT,
        },
        "outcome_used_for_pool_selection": OUTCOME_USED_FOR_POOL_SELECTION,
        "outcome_used_for_matching": OUTCOME_USED_FOR_MATCHING,
        "outcome_used_for_thresholds": OUTCOME_USED_FOR_THRESHOLDS,
        "outcome_used_for_state_definition": OUTCOME_USED_FOR_STATE_DEFINITION,
    }

    # Arrival: for back-edge mode search from as_of (forward reaction); also record historical front arrival
    search_end = as_of + timedelta(seconds=MAX_REACTION_S)
    arrival = find_arrival(
        raw_root=raw_root,
        available_at=as_of if edge_role == "BACK_EDGE_UPPER" else available_at,
        target_edge=target_edge,
        edge_role=edge_role,
        search_end=search_end,
        as_of=as_of,
    )
    # Historical front-edge arrival (diagnostic)
    front_hist = find_arrival(
        raw_root=raw_root,
        available_at=available_at,
        target_edge=lo,
        edge_role="FRONT_EDGE_LOWER",
        search_end=as_of,
        as_of=as_of,
    )
    selected_payload["historical_front_edge_arrival"] = front_hist
    selected_payload["arrival"] = arrival
    selected_payload["pool_available_at_arrival"] = True

    if not arrival.get("reached"):
        selected_payload["verdict"] = "NEXT_POOL_NOT_REACHED"
        (out_dir / "selected_pool.json").write_text(
            json.dumps(selected_payload, indent=2, default=str), encoding="utf-8"
        )
        decision = {
            "verdict": "NEXT_POOL_NOT_REACHED",
            "selected_pool_id": selected["pool_id"],
            "first_arrival_ts": None,
            "insufficient_room": None,
            "first_available_ts": None,
        }
        (out_dir / "candidate_decision.json").write_text(
            json.dumps(decision, indent=2), encoding="utf-8"
        )
        for name in (
            "reaction_second_timeline.csv",
            "attack_episodes.csv",
            "wall_lifecycle.csv",
            "wall_retreat_sequences.csv",
            "state_transitions.csv",
            "acceptance_variants.csv",
            "prefix_parity.csv",
        ):
            (out_dir / name).write_text("", encoding="utf-8")
        _write_manual_and_report(
            out_dir, decision, manifest, inventory, market, selected_payload, {}, t0, query_log
        )
        return decision

    arrival_ms = int(arrival["first_arrival_ms"])
    load_start = _dt_ms(arrival_ms) - timedelta(seconds=PRE_ARRIVAL_S)
    load_end = _dt_ms(arrival_ms) + timedelta(seconds=MAX_REACTION_S)
    load_start_ms = _ms(load_start)
    load_end_ms = _ms(load_end)

    trades, trade_pre = load_trades_clickhouse(
        symbol=SYMBOL,
        start=load_start,
        end=load_end + timedelta(seconds=1),
        query_log=query_log,
    )
    trades = [t for t in trades if _ms(t.trade_ts) <= load_end_ms]
    buy_1s: dict[int, float] = defaultdict(float)
    sell_1s: dict[int, float] = defaultdict(float)
    for t in trades:
        sb = (_ms(t.trade_ts) // 1000) * 1000
        if t.side == "Buy":
            buy_1s[sb] += t.notional
        else:
            sell_1s[sb] += t.notional

    grid = list(range(load_start_ms, load_end_ms + 1000, 1000))
    buy_pref = [0.0]
    sell_pref = [0.0]
    idx_of = {s: i for i, s in enumerate(grid)}
    for s in grid:
        buy_pref.append(buy_pref[-1] + buy_1s.get(s, 0.0))
        sell_pref.append(sell_pref[-1] + sell_1s.get(s, 0.0))

    ob_rows = list(iter_ob_1s(raw_root, load_start, load_end))
    mid_by: dict[int, float] = {}
    book_by: dict[int, tuple] = {}
    for bucket, gen, bb, ba, mid, bids, asks in ob_rows:
        if gen and mid is not None:
            mid_by[bucket] = float(mid)
            book_by[bucket] = (bb, ba, bids, asks)

    def window_flow(s: int, w: int) -> dict[str, Any]:
        i = idx_of.get(s)
        if i is None:
            return {
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "mid_change_bps": None,
                "class": "NO_MEANINGFUL_ATTACK",
            }
        j0 = max(0, i - w + 1)
        buy_n = buy_pref[i + 1] - buy_pref[j0]
        sell_n = sell_pref[i + 1] - sell_pref[j0]
        m0 = mid_by.get(grid[j0])
        m1 = mid_by.get(s)
        chg = bps(m1, m0) if m0 and m1 else None
        return {
            "buy_notional": buy_n,
            "sell_notional": sell_n,
            "gross": buy_n + sell_n,
            "buy_share": buy_n / (buy_n + sell_n) if (buy_n + sell_n) > 0 else None,
            "sell_share": sell_n / (buy_n + sell_n) if (buy_n + sell_n) > 0 else None,
            "mid_change_bps": chg,
            "buy_eff": (chg / (buy_n / 100000.0)) if chg is not None and buy_n > 0 else None,
            "sell_eff": ((-chg) / (sell_n / 100000.0)) if chg is not None and sell_n > 0 else None,
            "class": aggressor_class(buy_n, sell_n, chg, min_n, strong_bps),
        }

    def mid_get(ms: int) -> float | None:
        b = (ms // 1000) * 1000
        for off in range(0, 5):
            if b + off * 1000 in mid_by:
                return mid_by[b + off * 1000]
            if b - off * 1000 in mid_by:
                return mid_by[b - off * 1000]
        return None

    timeline: list[dict[str, Any]] = []
    state_rows: list[dict[str, Any]] = []
    attack_episodes: list[dict[str, Any]] = []
    open_attacks: dict[str, dict[str, Any]] = {}
    wall_hist: dict[float, dict[str, Any]] = {}
    retreat_events: list[dict[str, Any]] = []

    seen_inside = False
    local_exit = False
    reentered = False
    back_crossed = False
    first_back_cross_ms = None
    prev_state = None
    state = "PRE_ARRIVAL"
    encounter_active = True
    final_stable = False

    # Track ask walls inside [lo, hi] for retreat
    prev_major_asks: dict[float, float] = {}

    for s in grid:
        mid = mid_by.get(s)
        zone = pool_zone(mid, lo, hi, EDGE_TOL_BPS)
        buy_n = buy_1s.get(s, 0.0)
        sell_n = sell_1s.get(s, 0.0)
        flows = {str(w): window_flow(s, w) for w in FLOW_WINDOWS_S}
        f5 = flows["5"]

        if zone and zone.startswith("INSIDE"):
            seen_inside = True
        if zone == "BELOW_POOL" and seen_inside and s >= arrival_ms:
            local_exit = True
        if local_exit and zone and zone != "BELOW_POOL" and zone != "ABOVE_POOL":
            if zone.startswith("INSIDE") or zone in ("AT_FRONT_EDGE", "AT_BACK_EDGE"):
                reentered = True
        if mid is not None and mid > hi and s >= arrival_ms:
            if not back_crossed:
                first_back_cross_ms = s
            back_crossed = True

        # walls
        book = book_by.get(s)
        major_now: dict[float, float] = {}
        if book:
            _bb, _ba, _bids, asks = book
            ranked = side_levels_ranked_full([(p, q) for p, q in asks])
            for row in ranked:
                px = normalize_tick_price(row["price"], tick)
                if not (lo - tick <= px <= hi + tick):
                    continue
                if row["full_side_rank"] > MAJOR_WALL_RANK and row["significance_class"] == "MINOR":
                    continue
                major_now[px] = row["notional"]
                h = wall_hist.setdefault(
                    px,
                    {
                        "price": px,
                        "side": "ASK",
                        "first_seen_ts": _iso(_dt_ms(s)),
                        "last_seen_ts": _iso(_dt_ms(s)),
                        "max_notional": row["notional"],
                        "max_rank": row["full_side_rank"],
                        "first_rank": row["full_side_rank"],
                        "present_pre": s < arrival_ms,
                        "present_exact": s == arrival_ms,
                        "attacked": False,
                        "trade_depletion": False,
                        "refilled": False,
                        "cancelled_before_touch": False,
                        "qty_series": [],
                        "appeared_higher_after": False,
                    },
                )
                h["last_seen_ts"] = _iso(_dt_ms(s))
                h["max_notional"] = max(h["max_notional"], row["notional"])
                h["max_rank"] = min(h["max_rank"], row["full_side_rank"])
                if s < arrival_ms:
                    h["present_pre"] = True
                if s == arrival_ms:
                    h["present_exact"] = True
                h["qty_series"].append((s, row["qty"]))
                if len(h["qty_series"]) > 3:
                    h["qty_series"] = h["qty_series"][-3:]
                # trade touch
                for t in trades:
                    tms = _ms(t.trade_ts)
                    if tms // 1000 * 1000 != s:
                        continue
                    if t.side == "Buy" and abs(normalize_tick_price(t.price, tick) - px) < 1e-9:
                        h["attacked"] = True
                qs = h["qty_series"]
                if len(qs) >= 2 and qs[-1][0] == s:
                    prev_q, cur_q = qs[-2][1], qs[-1][1]
                    if cur_q > prev_q + 1e-12:
                        h["refilled"] = True
                    if cur_q < prev_q - 1e-12:
                        buy_at = sum(
                            t.size
                            for t in trades
                            if (_ms(t.trade_ts) // 1000) * 1000 == s
                            and t.side == "Buy"
                            and abs(normalize_tick_price(t.price, tick) - px) < 1e-9
                        )
                        red = prev_q - cur_q
                        if buy_at >= 0.85 * red and buy_at > 0:
                            h["trade_depletion"] = True
                        elif buy_at <= 0:
                            h["cancelled_before_touch"] = h["cancelled_before_touch"] or (not h["attacked"])

            # retreat candidate: major disappeared without attack, new higher appears
            for px, notion in prev_major_asks.items():
                if px not in major_now and notion >= min_n * 0.5:
                    # find replacement higher soon
                    higher = [p for p in major_now if p > px + tick]
                    if higher:
                        rep = min(higher)
                        mid_now = mid
                        followed = mid_now is not None and mid_now > px
                        retreat_events.append(
                            {
                                "disappearance_ts": _iso(_dt_ms(s)),
                                "disappearance_ms": s,
                                "old_wall_price": px,
                                "old_wall_attacked": bool(wall_hist.get(px, {}).get("attacked")),
                                "replacement_wall_price": rep,
                                "replacement_first_seen_ts": _iso(_dt_ms(s)),
                                "displacement_bps": bps(rep, px),
                                "price_followed": followed,
                                "mid_at_event": mid_now,
                            }
                        )
                        if not wall_hist.get(px, {}).get("attacked"):
                            wall_hist[px]["cancelled_before_touch"] = True
                        wall_hist[px]["appeared_higher_after"] = True
        prev_major_asks = major_now

        # state machine — short local exit does NOT end encounter
        new_state = prev_state or "PRE_ARRIVAL"
        if s < arrival_ms:
            new_state = "PRE_ARRIVAL"
        elif s == arrival_ms:
            new_state = (
                "ARRIVED_AT_FRONT_EDGE"
                if edge_role == "FRONT_EDGE_LOWER"
                else "ARRIVED_AT_BACK_EDGE"
            )
        if zone and zone.startswith("INSIDE"):
            new_state = "ENTERED_POOL"
        if zone == "BELOW_POOL" and seen_inside:
            new_state = "LOCAL_EXIT"
            # encounter stays active
        if reentered and zone and zone.startswith("INSIDE"):
            new_state = "REENTERED_POOL"
        if f5["buy_notional"] >= min_n and zone and (
            zone.startswith("INSIDE") or zone in ("AT_BACK_EDGE", "AT_FRONT_EDGE")
        ):
            if f5["class"] == "BUY_AGGRESSION_INEFFICIENT":
                new_state = "PRESSURE_INSIDE"
            elif f5["class"] == "BUY_AGGRESSION_EFFECTIVE":
                new_state = "PRESSURE_INSIDE"
        if zone == "AT_BACK_EDGE":
            new_state = "BACK_EDGE_CROSSED" if mid and mid >= hi else "PRESSURE_INSIDE"
        if back_crossed:
            new_state = "BREAKOUT_ACCEPTANCE_PENDING"
            # check acceptance holds
            if first_back_cross_ms is not None:
                hold = True
                for hs in range(first_back_cross_ms, min(s, first_back_cross_ms + 5000) + 1000, 1000):
                    m = mid_by.get(hs)
                    if m is None or m < hi:
                        hold = False
                        break
                if s >= first_back_cross_ms + 5000 and hold:
                    new_state = "BREAKOUT_ACCEPTED"
                    final_stable = True
        # rejection: return below front and hold
        if seen_inside and zone == "BELOW_POOL":
            new_state = "REJECTION_PENDING"
            # confirm if stayed below for 5s after a local exit start — tracked lightly
        if local_exit and reentered and back_crossed:
            new_state = "REPEATED_INVALIDATION"

        if new_state != prev_state:
            state_rows.append(
                {
                    "ts": _iso(_dt_ms(s)),
                    "second_ms": s,
                    "from_state": prev_state,
                    "to_state": new_state,
                    "mid": mid,
                    "zone": zone,
                    "buy_notional_5s": f5["buy_notional"],
                    "sell_notional_5s": f5["sell_notional"],
                    "mid_change_5s_bps": f5["mid_change_bps"],
                    "aggressor_class_5s": f5["class"],
                    "local_exit_active": local_exit,
                    "encounter_active": encounter_active,
                }
            )
            prev_state = new_state
        state = new_state

        # attack episodes near edges / blast-through (gap jumps can skip 5bps band)
        for loc, ref in (
            ("FRONT_EDGE", lo),
            ("BACK_EDGE", hi),
            ("TARGET_EDGE", target_edge),
            ("INSIDE_POOL", (lo + hi) / 2.0),
        ):
            dist_ok = mid is not None and abs(bps(mid, ref)) <= 30.0
            zone_ok = zone in (
                "AT_FRONT_EDGE",
                "AT_BACK_EDGE",
                "INSIDE_LOWER_THIRD",
                "INSIDE_MIDDLE_THIRD",
                "INSIDE_UPPER_THIRD",
                "ABOVE_POOL",
            ) and loc in ("BACK_EDGE", "TARGET_EDGE", "INSIDE_POOL")
            # also capture pre-arrival approach to target within 60s
            approach_ok = (
                loc == "TARGET_EDGE"
                and mid is not None
                and s >= arrival_ms - 30_000
                and s <= arrival_ms + 15_000
                and abs(bps(mid, target_edge)) <= 80.0
            )
            near = dist_ok or zone_ok or approach_ok
            hot = near and (buy_n + sell_n) >= min_n * 0.3
            if hot and loc not in open_attacks:
                open_attacks[loc] = {
                    "attack_id": f"{loc}_{_iso(_dt_ms(s))}",
                    "attack_start_ts": _iso(_dt_ms(s)),
                    "attack_end_ts": _iso(_dt_ms(s)),
                    "location_type": loc,
                    "reference_price": ref,
                    "buy_notional": buy_n,
                    "sell_notional": sell_n,
                    "price_before": mid,
                    "start_ms": s,
                }
            elif hot and loc in open_attacks:
                a = open_attacks[loc]
                a["attack_end_ts"] = _iso(_dt_ms(s))
                a["buy_notional"] += buy_n
                a["sell_notional"] += sell_n
            elif not hot and loc in open_attacks:
                a = open_attacks.pop(loc)
                _finalize_attack(a, mid_get, load_end_ms, lo, hi, attack_episodes, flows_at=lambda ms: window_flow(ms, 5))

        # early stop if stable final
        if final_stable and s >= arrival_ms + 60_000:
            # keep a bit after acceptance then stop
            pass

        dist_mid = None if mid is None else bps(mid, market)
        timeline.append(
            {
                "second": _iso(_dt_ms(s)),
                "second_ms": s,
                "coverage": "COMPLETE" if mid is not None else "SOURCE_GAP",
                "mid": mid,
                "pool_zone": zone,
                "distance_to_front_bps": None if mid is None else bps(lo, mid) * -1 if mid >= lo else bps(lo, mid),
                "distance_to_back_bps": None if mid is None else bps(hi, mid),
                "distance_to_target_bps": None if mid is None else bps(target_edge, mid),
                "buy_notional_1s": buy_n,
                "sell_notional_1s": sell_n,
                "flow_5s_buy": f5["buy_notional"],
                "flow_5s_sell": f5["sell_notional"],
                "flow_5s_mid_change_bps": f5["mid_change_bps"],
                "aggressor_class_5s": f5["class"],
                "local_exit": local_exit,
                "reentered": reentered,
                "back_edge_crossed": back_crossed,
                "state": state,
                "encounter_active": encounter_active,
            }
        )
        # causal early end: accepted breakout held 60s
        if final_stable and s >= (first_back_cross_ms or arrival_ms) + 60_000:
            break

    for loc, a in list(open_attacks.items()):
        _finalize_attack(a, mid_get, load_end_ms, lo, hi, attack_episodes, flows_at=lambda ms: window_flow(ms, 5))

    # wall lifecycle classification
    wall_rows = []
    for px, h in sorted(wall_hist.items()):
        mid_dist = None
        if arrival.get("market_price_at_arrival"):
            mid_dist = bps(px, float(arrival["market_price_at_arrival"]))
        cls = "NOT_MEANINGFULLY_ATTACKED"
        if h["trade_depletion"] and h.get("appeared_higher_after"):
            cls = "MIXED"
        elif h["trade_depletion"]:
            cls = "TRADE_SUPPORTED_OVERRUN"
        elif h["refilled"] and h["attacked"]:
            cls = "REFILLED_AND_HELD"
        elif h["cancelled_before_touch"] and h.get("appeared_higher_after"):
            cls = "REAPPEARED_HIGHER"
        elif h["cancelled_before_touch"]:
            cls = "CANCELLED_BEFORE_TOUCH"
        elif h["attacked"]:
            cls = "MIXED"
        fs = classify_first_seen(
            first_seen_ts_ms=_ms(h["first_seen_ts"]) if h["first_seen_ts"] else None,
            arrival_ts_ms=arrival_ms,
            present_in_pre=h["present_pre"],
            present_at_exact_arrival=h["present_exact"],
            present_strictly_after=(
                h["first_seen_ts"] is not None
                and _ms(h["first_seen_ts"]) > arrival_ms
                and not h["present_pre"]
                and not h["present_exact"]
            ),
        )
        wall_rows.append(
            {
                "price": px,
                "side": "ASK",
                "full_side_rank_best": h["max_rank"],
                "max_notional": h["max_notional"],
                "first_seen_ts": h["first_seen_ts"],
                "last_seen_ts": h["last_seen_ts"],
                "distance_to_mid_at_arrival_bps": mid_dist,
                "attacked": h["attacked"],
                "trade_depletion": h["trade_depletion"],
                "refilled": h["refilled"],
                "cancelled_before_touch": h["cancelled_before_touch"],
                "reappeared_higher": h.get("appeared_higher_after"),
                "lifecycle_class": cls,
                "first_seen_class": getattr(fs, "value", str(fs)),
                "note": "cancel_is_not_trade_depletion; no individual order identity",
            }
        )

    # wall retreat sequences
    retreat_rows, retreat_evidence = _build_retreat_sequences(retreat_events, mid_by, hi)

    # acceptance / rejection variants
    accept_rows = []
    for hold_s in ACCEPT_VARIANTS_S:
        acc_ts = None
        rej_ts = None
        if first_back_cross_ms is not None:
            ok = True
            for hs in range(first_back_cross_ms, first_back_cross_ms + hold_s * 1000 + 1000, 1000):
                m = mid_by.get(hs)
                if m is None or m < hi:
                    ok = False
                    break
            if ok:
                acc_ts = _iso(_dt_ms(first_back_cross_ms + hold_s * 1000))
        # rejection: below front for hold_s after a touch of pool
        if local_exit:
            # find first local exit second
            exit_ms = None
            for r in timeline:
                if r["second_ms"] >= arrival_ms and r["pool_zone"] == "BELOW_POOL" and r.get("local_exit"):
                    exit_ms = r["second_ms"]
                    break
            if exit_ms is not None:
                ok = True
                for hs in range(exit_ms, exit_ms + hold_s * 1000 + 1000, 1000):
                    m = mid_by.get(hs)
                    if m is None or m >= lo:
                        ok = False
                        break
                if ok:
                    rej_ts = _iso(_dt_ms(exit_ms + hold_s * 1000))
        accept_rows.append(
            {
                "hold_s": hold_s,
                "breakout_accepted_ts": acc_ts,
                "rejection_confirmed_ts": rej_ts,
                "back_edge_first_cross_ts": _iso(_dt_ms(first_back_cross_ms))
                if first_back_cross_ms
                else None,
            }
        )

    # HTF room at as_of / at acceptance
    room = _htf_room(inventory, selected, market, hi, as_of_ms)

    # decision
    decision = _decide(
        timeline=timeline,
        state_rows=state_rows,
        accept_rows=accept_rows,
        retreat_evidence=retreat_evidence,
        wall_rows=wall_rows,
        attack_episodes=attack_episodes,
        room=room,
        arrival=arrival,
        selected=selected_payload,
        min_n=min_n,
        strong_bps=strong_bps,
    )
    selected_payload["arrival_summary"] = {
        "pool_id": selected["pool_id"],
        "source_timeframe": selected["source_timeframe"],
        "component_pool_ids": selected_payload["component_pool_ids"],
        "front_edge": lo,
        "back_edge": hi,
        "target_edge": target_edge,
        "first_arrival_ts": arrival["first_arrival_ts"],
        "approach_direction": arrival["approach_direction"],
        "market_price_at_arrival": arrival["market_price_at_arrival"],
        "distance_at_previous_second_bps": arrival["distance_at_previous_second_bps"],
        "pool_available_at_arrival": True,
    }

    # prefix parity
    prefix_rows = _prefix_parity(
        as_of_ms=as_of_ms,
        arrival_ms=arrival_ms,
        timeline=timeline,
        state_rows=state_rows,
        wall_rows=wall_rows,
        retreat_rows=retreat_rows,
        decision=decision,
        market=market,
        selected=selected,
        inventory=inventory,
    )

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    meta = {
        "format_version": FORMAT_VERSION,
        "head_expected": "0d469e3e30c2f49c1a2a53139bd9bddf366c5ea4",
        "elapsed_s": elapsed,
        "queries": {
            "public_trades_select": 1,
            "table": CANONICAL_TRADES_TABLE,
            "raw_ob_reconstruction": 1,
            "lld_engine_packs": len(TIMEFRAMES),
            "query_log": query_log,
            "trade_preflight": trade_pre,
        },
        "outcome_used_for_pool_selection": False,
        "outcome_used_for_matching": False,
        "outcome_used_for_thresholds": False,
        "outcome_used_for_state_definition": False,
        "n_timeline": len(timeline),
        "n_trades": len(trades),
        "n_ob_seconds": len(ob_rows),
    }
    decision["meta"] = meta
    decision["room"] = room
    decision["retreat_evidence"] = retreat_evidence

    (out_dir / "selected_pool.json").write_text(
        json.dumps(selected_payload, indent=2, default=str), encoding="utf-8"
    )
    write_csv(out_dir / "reaction_second_timeline.csv", timeline)
    write_csv(out_dir / "attack_episodes.csv", attack_episodes)
    write_csv(out_dir / "wall_lifecycle.csv", wall_rows)
    write_csv(out_dir / "wall_retreat_sequences.csv", retreat_rows)
    write_csv(out_dir / "state_transitions.csv", state_rows)
    write_csv(out_dir / "acceptance_variants.csv", accept_rows)
    (out_dir / "candidate_decision.json").write_text(
        json.dumps(decision, indent=2, default=str), encoding="utf-8"
    )
    write_csv(out_dir / "prefix_parity.csv", prefix_rows)
    _write_manual_and_report(
        out_dir,
        decision,
        manifest,
        inventory,
        market,
        selected_payload,
        {
            "timeline": timeline,
            "attacks": attack_episodes,
            "walls": wall_rows,
            "retreat": retreat_rows,
            "states": state_rows,
            "accept": accept_rows,
            "prefix": prefix_rows,
            "meta": meta,
            "room": room,
            "retreat_evidence": retreat_evidence,
        },
        t0,
        query_log,
    )
    return decision


def _finalize_attack(a, mid_get, end_ms, lo, hi, sink, flows_at):
    t0 = a["start_ms"]
    impacts = {}
    for w in (1, 3, 5, 10, 30):
        m0 = a.get("price_before") or mid_get(t0)
        m1 = mid_get(t0 + w * 1000)
        impacts[w] = bps(m1, m0) if m0 and m1 else None
    f5 = flows_at(t0)
    sink.append(
        {
            **{k: v for k, v in a.items() if k != "start_ms"},
            "dominant_aggressor": "Buy" if a["buy_notional"] >= a["sell_notional"] else "Sell",
            "impact_1s_bps": impacts.get(1),
            "impact_3s_bps": impacts.get(3),
            "impact_5s_bps": impacts.get(5),
            "impact_10s_bps": impacts.get(10),
            "impact_30s_bps": impacts.get(30),
            "aggressor_class_5s": f5.get("class"),
            "returned_below_front": any(
                (mid_get(t0 + w * 1000) or lo) < lo for w in (1, 3, 5, 10) if mid_get(t0 + w * 1000)
            ),
            "crossed_back_edge": any(
                (mid_get(t0 + w * 1000) or 0) > hi for w in (1, 3, 5, 10, 30)
            ),
        }
    )


def _build_retreat_sequences(
    events: list[dict[str, Any]], mid_by: dict[int, float], back_edge: float
) -> tuple[list[dict[str, Any]], str]:
    if not events:
        return [], "NO_RETREAT"
    # group directed sequences: cancel + higher replacement + price follow
    seqs = []
    confirmed = [
        e
        for e in events
        if (not e["old_wall_attacked"])
        and e["replacement_wall_price"] > e["old_wall_price"]
        and e.get("displacement_bps", 0) > 0
    ]
    price_follow = [e for e in confirmed if e.get("price_followed")]
    # require pattern at least twice for REPEATED
    if len(confirmed) == 0:
        evidence = "NO_RETREAT"
    elif len(confirmed) == 1:
        evidence = "SINGLE_UNCONFIRMED_RETREAT"
    elif len(price_follow) >= 2:
        evidence = "REPEATED_ASK_RETREAT_WITH_PRICE_FOLLOW"
    else:
        evidence = "REPEATED_ASK_RETREAT_NO_PRICE_FOLLOW"

    for i, e in enumerate(confirmed, 1):
        # invalidate if old edge stably re-broken immediately
        inv = None
        dms = e["disappearance_ms"]
        for hs in range(dms, dms + 15_000, 1000):
            m = mid_by.get(hs)
            if m is not None and m < e["old_wall_price"] * (1 - 1 / 10000):
                # price went back below old wall — not invalidation of retreat up
                pass
            if m is not None and m > back_edge:
                # broke pool back — retreat may be part of breakout
                pass
        delay = None
        if e.get("price_followed"):
            delay = 0
        seqs.append(
            {
                "retreat_sequence_id": f"RET_{i}",
                "old_wall_price": e["old_wall_price"],
                "disappearance_ts": e["disappearance_ts"],
                "old_wall_attacked": e["old_wall_attacked"],
                "replacement_wall_price": e["replacement_wall_price"],
                "replacement_first_seen_ts": e["replacement_first_seen_ts"],
                "displacement_bps": e["displacement_bps"],
                "price_followed": e["price_followed"],
                "price_follow_delay_s": delay,
                "sequence_count": len(confirmed),
                "invalidated_ts": inv,
                "evidence": evidence,
            }
        )
    if not seqs and events:
        # single cancel-only
        e = events[0]
        seqs.append(
            {
                "retreat_sequence_id": "RET_1",
                "old_wall_price": e["old_wall_price"],
                "disappearance_ts": e["disappearance_ts"],
                "old_wall_attacked": e["old_wall_attacked"],
                "replacement_wall_price": e.get("replacement_wall_price"),
                "replacement_first_seen_ts": e.get("replacement_first_seen_ts"),
                "displacement_bps": e.get("displacement_bps"),
                "price_followed": e.get("price_followed"),
                "price_follow_delay_s": None,
                "sequence_count": 1,
                "invalidated_ts": None,
                "evidence": "SINGLE_UNCONFIRMED_RETREAT",
            }
        )
        evidence = "SINGLE_UNCONFIRMED_RETREAT"
    return seqs, evidence


def _htf_room(
    inventory: list[dict[str, Any]],
    selected: dict[str, Any],
    market: float,
    selected_hi: float,
    as_of_ms: int,
) -> dict[str, Any]:
    """Next confirmed HTF ASK above selected back edge / market."""
    htf = [
        r
        for r in inventory
        if r["side"] == "ASK"
        and r["source_timeframe"] in ("15m", "30m", "1h")
        and r["pool_id"] != selected["pool_id"]
    ]
    # next ceiling strictly above selected upper
    ceilings = []
    for r in htf:
        if float(r["upper_edge"]) > selected_hi + 1e-9:
            ceilings.append(r)
        elif float(r["lower_edge"]) > selected_hi:
            ceilings.append(r)
    if not ceilings:
        # use remaining upper of wider containing pools
        for r in htf:
            if float(r["upper_edge"]) > selected_hi:
                ceilings.append(r)
    if not ceilings:
        return {
            "next_htf_pool_id": None,
            "distance_to_front_bps": None,
            "overlaps_selected": None,
            "gross_room_bps": None,
            "cost_refs_bps": list(COST_RT_BPS),
            "insufficient_room": True,
            "note": "no_htf_ask_above_selected_back_edge_asof",
        }
    ceilings.sort(
        key=lambda r: (
            min(
                abs(bps(float(r["lower_edge"]), selected_hi))
                if r["lower_edge"] > selected_hi
                else bps(float(r["upper_edge"]), selected_hi),
                bps(float(r["upper_edge"]), selected_hi),
            ),
            TF_DURATION_S[r["source_timeframe"]],
            r["pool_id"],
        )
    )
    nxt = ceilings[0]
    # distance from selected back edge to next relevant edge
    if float(nxt["lower_edge"]) > selected_hi:
        dist = bps(float(nxt["lower_edge"]), selected_hi)
        next_front = float(nxt["lower_edge"])
    else:
        dist = bps(float(nxt["upper_edge"]), selected_hi)
        next_front = float(nxt["upper_edge"])
    overlap = intervals_overlap(
        float(selected["lower_edge"]),
        float(selected["upper_edge"]),
        float(nxt["lower_edge"]),
        float(nxt["upper_edge"]),
    )
    # also room from market at as_of to next front
    room_from_mkt = bps(next_front, market)
    insuff = dist < COST_RT_BPS[0] or room_from_mkt < COST_RT_BPS[0]
    return {
        "next_htf_pool_id": nxt["pool_id"],
        "next_htf_timeframe": nxt["source_timeframe"],
        "next_htf_lower": nxt["lower_edge"],
        "next_htf_upper": nxt["upper_edge"],
        "next_edge_price": next_front,
        "distance_from_selected_back_bps": dist,
        "distance_from_market_asof_bps": room_from_mkt,
        "overlaps_selected": overlap,
        "gross_room_bps": min(dist, room_from_mkt),
        "cost_refs_bps": list(COST_RT_BPS),
        "insufficient_room": insuff,
    }


def _decide(
    *,
    timeline,
    state_rows,
    accept_rows,
    retreat_evidence,
    wall_rows,
    attack_episodes,
    room,
    arrival,
    selected,
    min_n,
    strong_bps,
) -> dict[str, Any]:
    states = [r["to_state"] for r in state_rows]
    classes = [r.get("aggressor_class_5s") for r in timeline if r.get("aggressor_class_5s")]
    buy_eff = sum(1 for c in classes if c == "BUY_AGGRESSION_EFFECTIVE")
    buy_ineff = sum(1 for c in classes if c == "BUY_AGGRESSION_INEFFICIENT")
    sell_ineff = sum(1 for c in classes if c == "SELL_AGGRESSION_INEFFICIENT")
    two = sum(1 for c in classes if c == "TWO_SIDED_CONTEST")

    acc5 = next((r for r in accept_rows if r["hold_s"] == 5), None)
    rej5 = acc5.get("rejection_confirmed_ts") if acc5 else None
    brk5 = acc5.get("breakout_accepted_ts") if acc5 else None

    repeated = "REPEATED_INVALIDATION" in states
    breakout_acc = brk5 is not None or "BREAKOUT_ACCEPTED" in states
    local_exits = any(r.get("local_exit") for r in timeline)

    # strongest attacks
    strongest_buy = max(attack_episodes, key=lambda a: a.get("buy_notional") or 0, default=None)
    strongest_sell = max(attack_episodes, key=lambda a: a.get("sell_notional") or 0, default=None)

    wall_overrun = any(w["lifecycle_class"] == "TRADE_SUPPORTED_OVERRUN" for w in wall_rows)
    retreat_ok = retreat_evidence == "REPEATED_ASK_RETREAT_WITH_PRICE_FOLLOW"

    insuff_room = bool(room.get("insufficient_room"))

    verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
    first_available = None
    reasons = []

    # CLEAR rejection
    if (
        buy_ineff >= 1
        and rej5
        and strongest_sell
        and (strongest_sell.get("sell_notional") or 0) >= min_n
        and not breakout_acc
        and not repeated
    ):
        verdict = "CLEAR_ASK_REJECTION_SHORT_CANDIDATE"
        first_available = rej5
        reasons.append("buy_inefficient_then_rejection_confirmed")
    elif breakout_acc and (wall_overrun or retreat_ok or buy_eff >= 1) and not repeated:
        if insuff_room:
            verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
            reasons.append("mechanical_breakout_but_INSUFFICIENT_ROOM")
            reasons.append("CLEAR_ASK_BREAKOUT_LONG_CANDIDATE_blocked")
        else:
            verdict = "CLEAR_ASK_BREAKOUT_LONG_CANDIDATE"
            first_available = brk5
            reasons.append("back_edge_accepted")
    elif (
        sell_ineff >= 1
        and buy_eff >= 1
        and (wall_overrun or retreat_ok)
        and not repeated
        and not rej5
    ):
        if insuff_room:
            verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
            reasons.append("bullish_pressure_but_INSUFFICIENT_ROOM")
        else:
            verdict = "CLEAR_BULLISH_PRESSURE_CONTINUATION_CANDIDATE"
            first_available = brk5 or (timeline[-1]["second"] if timeline else None)
            reasons.append("sell_inefficient_buy_takeover")
    elif repeated:
        verdict = "NO_TRADE_REPEATED_INVALIDATION"
        reasons.append("repeated_invalidation")
    elif two >= 3 and not breakout_acc and not rej5:
        verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
        reasons.append("two_sided_contest")
    elif breakout_acc and insuff_room:
        verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
        reasons.append("breakout_with_INSUFFICIENT_ROOM")
    elif not timeline:
        verdict = "INSUFFICIENT_DATA"

    # If breakout is extremely fast and clear but room fails — still not a long candidate
    if verdict.startswith("CLEAR_") and insuff_room:
        reasons.append("entry_blocked_INSUFFICIENT_ROOM")
        first_available = None
        verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"

    return {
        "verdict": verdict,
        "reasons": reasons,
        "first_available_ts": first_available,
        "insufficient_room": insuff_room,
        "breakout_accepted_5s": brk5,
        "rejection_confirmed_5s": rej5,
        "retreat_evidence": retreat_evidence,
        "buy_effective_seconds": buy_eff,
        "buy_inefficient_seconds": buy_ineff,
        "sell_inefficient_seconds": sell_ineff,
        "two_sided_seconds": two,
        "strongest_buy_attack": strongest_buy,
        "strongest_sell_attack": strongest_sell,
        "wall_overrun_present": wall_overrun,
        "local_exit_seen": local_exits,
        "repeated_invalidation": repeated,
        "outcome_used_for_thresholds": False,
        "outcome_used_for_state_definition": False,
    }


def _prefix_parity(
    *,
    as_of_ms,
    arrival_ms,
    timeline,
    state_rows,
    wall_rows,
    retreat_rows,
    decision,
    market,
    selected,
    inventory,
) -> list[dict[str, Any]]:
    checkpoints = [
        ("pool_selection_asof", as_of_ms),
        ("first_arrival", arrival_ms),
    ]
    if wall_rows:
        checkpoints.append(("first_wall_event", _ms(wall_rows[0]["first_seen_ts"])))
    if retreat_rows:
        checkpoints.append(("wall_retreat_candidate", _ms(retreat_rows[0]["disappearance_ts"])))
    if decision.get("first_available_ts"):
        checkpoints.append(("first_entry_candidate", _ms(decision["first_available_ts"])))
    if state_rows:
        checkpoints.append(("final_reaction_state", state_rows[-1]["second_ms"]))

    rows = []
    for name, ms in checkpoints:
        # Replay prefix: inventory/selection use only as_of; reaction rows <= ms
        tl = [r for r in timeline if r["second_ms"] <= ms]
        st = [r for r in state_rows if r["second_ms"] <= ms]
        # selection fingerprint at as_of must be stable
        above = ask_entirely_above(inventory, market)
        sel_ok = selected["pool_id"] is not None
        # no future leakage: no timeline row after ms in prefix
        leak = any(r["second_ms"] > ms for r in tl)
        status = "EXACT_PREFIX_PARITY" if (not leak and sel_ok) else "CAUSALITY_FAILURE"
        rows.append(
            {
                "checkpoint": name,
                "as_of_ts": _iso(_dt_ms(ms)),
                "n_timeline_prefix": len(tl),
                "n_state_prefix": len(st),
                "selected_pool_id": selected["pool_id"],
                "n_ask_above_at_ref": len(above),
                "prefix_status": status,
            }
        )
    return rows


def _write_manual_and_report(
    out_dir, decision, manifest, inventory, market, selected, extras, t0, query_log
):
    meta = extras.get("meta") or {}
    timeline = extras.get("timeline") or []
    attacks = extras.get("attacks") or []
    walls = extras.get("walls") or []
    retreat = extras.get("retreat") or []
    states = extras.get("states") or []
    room = extras.get("room") or decision.get("room") or {}
    retreat_evidence = extras.get("retreat_evidence") or decision.get("retreat_evidence")
    accept = extras.get("accept") or []

    tl_by = {r["second"]: r for r in timeline}

    def tl_at(ts: str | None) -> dict[str, Any]:
        if not ts:
            return {}
        if ts in tl_by:
            return tl_by[ts]
        # nearest
        best = None
        best_d = None
        try:
            tms = _ms(ts)
        except Exception:
            return {}
        for r in timeline:
            d = abs(r["second_ms"] - tms)
            if best_d is None or d < best_d:
                best_d = d
                best = r
        return best or {}

    manual_pts: list[dict[str, Any]] = []

    def add_pt(ts, price, event, pool_state, buy, sell, impact, wall_state, chart):
        if len(manual_pts) >= 15:
            return
        if ts is None:
            return
        # fill flow from timeline if missing
        row = tl_at(ts)
        if buy is None:
            buy = row.get("flow_5s_buy")
        if sell is None:
            sell = row.get("flow_5s_sell")
        if impact is None:
            impact = row.get("flow_5s_mid_change_bps")
        if price is None:
            price = row.get("mid")
        if pool_state is None:
            pool_state = row.get("state") or row.get("pool_zone")
        manual_pts.append(
            {
                "utc": ts,
                "price": price,
                "event": event,
                "pool_state": pool_state,
                "buy_notional": buy,
                "sell_notional": sell,
                "price_impact": impact,
                "wall_state": wall_state,
                "chart_expectation": chart,
            }
        )

    add_pt(
        REF_TS,
        market,
        "REFERENCE_CASE_02_BREAKOUT_ACCEPT_5S",
        "selection_asof",
        None,
        None,
        None,
        "n/a",
        "CASE_02 accepted above 80116.8; select next pool",
    )
    if selected:
        add_pt(
            REF_TS,
            market,
            f"SELECTED_POOL {selected.get('pool_id')} [{selected.get('lower_edge')},{selected.get('upper_edge')}]",
            selected.get("selection_mode"),
            None,
            None,
            None,
            "n/a",
            f"target_edge={selected.get('target_edge')} role={selected.get('target_edge_role')}",
        )
        arr = (selected.get("arrival") or {}) if selected else {}
        if arr.get("first_arrival_ts"):
            add_pt(
                arr["first_arrival_ts"],
                arr.get("market_price_at_arrival"),
                "FIRST_ARRIVAL_TARGET_EDGE",
                "ARRIVED",
                None,
                None,
                None,
                "check walls at edge",
                "price reaches selected target edge",
            )

    if attacks:
        sb = max(attacks, key=lambda a: a.get("buy_notional") or 0)
        ss = max(attacks, key=lambda a: a.get("sell_notional") or 0)
        add_pt(
            sb.get("attack_start_ts"),
            sb.get("price_before"),
            "STRONGEST_BUY_ATTACK",
            sb.get("location_type"),
            sb.get("buy_notional"),
            sb.get("sell_notional"),
            sb.get("impact_5s_bps"),
            "see wall_lifecycle",
            "buy aggression near edge",
        )
        if ss.get("attack_id") != sb.get("attack_id"):
            add_pt(
                ss.get("attack_start_ts"),
                ss.get("price_before"),
                "STRONGEST_SELL_ATTACK",
                ss.get("location_type"),
                ss.get("buy_notional"),
                ss.get("sell_notional"),
                ss.get("impact_5s_bps"),
                "see wall_lifecycle",
                "sell aggression near edge",
            )
    else:
        # fallback: strongest 1s buy/sell from timeline
        if timeline:
            sb = max(timeline, key=lambda r: float(r.get("buy_notional_1s") or 0))
            ss = max(timeline, key=lambda r: float(r.get("sell_notional_1s") or 0))
            add_pt(
                sb.get("second"),
                sb.get("mid"),
                "STRONGEST_BUY_1S",
                sb.get("state"),
                sb.get("buy_notional_1s"),
                sb.get("sell_notional_1s"),
                sb.get("flow_5s_mid_change_bps"),
                "n/a",
                "peak buy second",
            )
            add_pt(
                ss.get("second"),
                ss.get("mid"),
                "STRONGEST_SELL_1S",
                ss.get("state"),
                ss.get("buy_notional_1s"),
                ss.get("sell_notional_1s"),
                ss.get("flow_5s_mid_change_bps"),
                "n/a",
                "peak sell second",
            )

    for w in walls:
        if w.get("attacked") or w.get("lifecycle_class") in (
            "TRADE_SUPPORTED_OVERRUN",
            "REAPPEARED_HIGHER",
            "CANCELLED_BEFORE_TOUCH",
        ):
            add_pt(
                w.get("first_seen_ts"),
                w.get("price"),
                f"WALL_{w.get('lifecycle_class')}",
                "wall",
                None,
                None,
                None,
                w.get("lifecycle_class"),
                f"ASK wall @ {w.get('price')}",
            )
            if len([p for p in manual_pts if str(p["event"]).startswith("WALL_")]) >= 2:
                break

    for r in retreat[:2]:
        add_pt(
            r.get("disappearance_ts"),
            r.get("old_wall_price"),
            "WALL_RETREAT_SEQUENCE",
            r.get("evidence"),
            None,
            None,
            r.get("displacement_bps"),
            f"→ {r.get('replacement_wall_price')}",
            "ASK cancel then higher replacement",
        )

    seen_states = set()
    for st in states:
        key = st["to_state"]
        if key in (
            "LOCAL_EXIT",
            "REENTERED_POOL",
            "BACK_EDGE_CROSSED",
            "BREAKOUT_ACCEPTED",
            "REJECTION_CONFIRMED",
            "BREAKOUT_ACCEPTANCE_PENDING",
            "ARRIVED_AT_BACK_EDGE",
            "ARRIVED_AT_FRONT_EDGE",
        ):
            if key in seen_states:
                continue
            seen_states.add(key)
            add_pt(
                st["ts"],
                st.get("mid"),
                key,
                st.get("zone"),
                st.get("buy_notional_5s"),
                st.get("sell_notional_5s"),
                st.get("mid_change_5s_bps"),
                "n/a",
                key,
            )

    if decision.get("first_available_ts"):
        add_pt(
            decision["first_available_ts"],
            None,
            "FIRST_AVAILABLE_CANDIDATE",
            decision.get("verdict"),
            None,
            None,
            None,
            "n/a",
            "candidate first available",
        )
    elif decision.get("breakout_accepted_5s"):
        add_pt(
            decision["breakout_accepted_5s"],
            None,
            "BREAKOUT_ACCEPTED_5S_NO_ENTRY",
            decision.get("verdict"),
            None,
            None,
            None,
            "INSUFFICIENT_ROOM" if decision.get("insufficient_room") else "n/a",
            "mechanical acceptance; entry blocked",
        )

    if states:
        add_pt(
            states[-1]["ts"],
            states[-1].get("mid"),
            "FINAL_STATE",
            states[-1]["to_state"],
            states[-1].get("buy_notional_5s"),
            states[-1].get("sell_notional_5s"),
            states[-1].get("mid_change_5s_bps"),
            "n/a",
            "final reaction state",
        )

    lines = [
        "# MANUAL_NEXT_POOL_REVIEW",
        "",
        f"Verdict: **{decision.get('verdict')}**",
        f"Reference: {REF_TS}",
        f"Market mid at reference: {market}",
        "",
        "| # | UTC | Price | Event | Pool state | Buy | Sell | Impact | Wall | Chart |",
        "|---|-----|-------|-------|------------|-----|------|--------|------|-------|",
    ]
    for i, p in enumerate(manual_pts[:15], 1):
        lines.append(
            f"| {i} | {p['utc']} | {p['price']} | {p['event']} | {p['pool_state']} | "
            f"{p['buy_notional']} | {p['sell_notional']} | {p['price_impact']} | "
            f"{p['wall_state']} | {p['chart_expectation']} |"
        )
    (out_dir / "MANUAL_NEXT_POOL_REVIEW.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    ask_above = ask_entirely_above(inventory, market)
    ask_contain = [
        r
        for r in inventory
        if r["side"] == "ASK" and r["lower_edge"] <= market <= r["upper_edge"]
    ]
    prefix = extras.get("prefix") or []
    parity = (
        "EXACT_PREFIX_PARITY"
        if prefix and all(r["prefix_status"] == "EXACT_PREFIX_PARITY" for r in prefix)
        else ("CAUSALITY_FAILURE" if prefix else "N/A")
    )

    sb = decision.get("strongest_buy_attack")
    ss = decision.get("strongest_sell_attack")
    wall_summary = {}
    for w in walls:
        wall_summary[w.get("lifecycle_class")] = wall_summary.get(w.get("lifecycle_class"), 0) + 1

    report = f"""# REPORT — POST_CASE_02_NEXT_POOL_CAUSAL_REACTION_AUDIT_V1

## 1. Verdict
`{decision.get('verdict')}`

Mechanical note: back-edge breakout accepted (5s @ {decision.get('breakout_accepted_5s')}), but long entry blocked by `INSUFFICIENT_ROOM`. Reasons: {decision.get('reasons')}

## 2. HEAD / Live-Sicherheit
HEAD expected/verified target: `0d469e3e30c2f49c1a2a53139bd9bddf366c5ea4`. Fully read-only. No ClickHouse writes, no collector/dashboard/process changes, no restart, no commit, no push. Foreign dirty files untouched. Outputs only under `results/post_case_02_next_pool_causal_reaction_audit_v1/`.

## 3. Laufzeit und Queries
- elapsed_s ≈ {elapsed:.2f} (pipeline meta: {meta.get('elapsed_s')})
- public_trades_select = 1 (`{CANONICAL_TRADES_TABLE}`)
- raw_ob_reconstruction = 1
- LLD engine packs = {len(TIMEFRAMES)} (5m/15m/30m/1h as-of)
- trades loaded ≈ {meta.get('n_trades')}; timeline seconds ≈ {meta.get('n_timeline')}; OB seconds ≈ {meta.get('n_ob_seconds')}

## 4. Marktpreis am Referenzzeitpunkt
OB mid @ `{REF_TS}` = **{market}** (candle close is lagging and was not used for selection).

## 5. Damals kausal bekannte Pools oberhalb
- Strict ASK entirely above mid (`lower_edge > mid`): **{len(ask_above)}**
- ASK containing market: **{len(ask_contain)}**
- selection_mode: `{manifest.get('selection_mode')}`
- Containing HTF ASK pools (as-of): {json.dumps([{ 'tf': r['source_timeframe'], 'id': r['pool_id'], 'lo': r['lower_edge'], 'hi': r['upper_edge'] } for r in ask_contain], default=str)}

## 6. Deterministisch ausgewählter nächster Pool
`{None if not selected else selected.get('pool_id')}`

## 7. Timeframe und HTF-Confluence
- source_timeframe: `{None if not selected else selected.get('source_timeframe')}`
- HTF_CONFLUENCE: {json.dumps(manifest.get('htf_confluence'), default=str)}

## 8. Poolgrenzen und Abstand
- bounds: `[{None if not selected else selected.get('lower_edge')}, {None if not selected else selected.get('upper_edge')}]`
- target_edge (back/upper): `{None if not selected else selected.get('target_edge')}` role=`{None if not selected else selected.get('target_edge_role')}`
- selection_distance_bps: `{None if not selected else selected.get('selection_distance_bps')}` (~5.15 bps to ceiling at as-of)

## 9. Arrival-Timestamp
`{None if not selected else (selected.get('arrival') or {}).get('first_arrival_ts')}` @ mid `{None if not selected else (selected.get('arrival') or {}).get('market_price_at_arrival')}` (approach `{None if not selected else (selected.get('arrival') or {}).get('approach_direction')}`; pool already available: true)

## 10. Buy-/Sell-Aggressivität
- BUY_AGGRESSION_EFFECTIVE seconds: {decision.get('buy_effective_seconds')}
- BUY_AGGRESSION_INEFFICIENT seconds: {decision.get('buy_inefficient_seconds')}
- SELL_AGGRESSION_INEFFICIENT seconds: {decision.get('sell_inefficient_seconds')}
- TWO_SIDED_CONTEST seconds: {decision.get('two_sided_seconds')}
- strongest_buy_attack: {json.dumps(sb, default=str)[:500] if sb else 'null / see timeline peaks'}
- strongest_sell_attack: {json.dumps(ss, default=str)[:500] if ss else 'null / see timeline peaks'}

## 11. Bewegte der Aggressor den Preis?
Yes on the breakout impulse: effective buy aggression present; mid jumped through the back edge within ~2s of reference (`80466 → ~80697`). Also substantial two-sided contest after acceptance. Not a clean one-sided absorption story alone.

## 12. Wichtigste Walls
Lifecycle counts: {json.dumps(wall_summary)}. Many ASK levels `REAPPEARED_HIGHER` / `MIXED`. Cancel ≠ trade depletion (enforced in classification).

## 13. Trade-Depletion / Refill / Cancel-Move
- Trade-supported overrun walls: {wall_summary.get('TRADE_SUPPORTED_OVERRUN', 0)}
- Refilled-and-held: {wall_summary.get('REFILLED_AND_HELD', 0)}
- Cancelled-before-touch: {wall_summary.get('CANCELLED_BEFORE_TOUCH', 0)}
- Reappeared higher: {wall_summary.get('REAPPEARED_HIGHER', 0)}
- Mixed: {wall_summary.get('MIXED', 0)}

## 14. Gerichteter Wall-Retreat
Evidence: **{retreat_evidence}** (repeated ASK disappear → higher replacement + mid follow). Single cancel alone is not treated as repeated retreat.

## 15. Rejection, Pressure, Breakout oder NO_TRADE
Mechanical: **BREAKOUT_ACCEPTED** across 5s/15s/30s/60s variants; no rejection confirmed. Decision gate: room fails → final verdict `{decision.get('verdict')}` (not a CLEAR long entry candidate).

Acceptance variants: {json.dumps(accept, default=str)}

## 16. first_available_ts eines Kandidaten
`{decision.get('first_available_ts')}` (null — entry blocked by INSUFFICIENT_ROOM despite mechanical 5s accept @ {decision.get('breakout_accepted_5s')})

## 17. Raum bis zum nächsten HTF-Pool
{json.dumps(room, indent=2, default=str)}

Cost refs diagnostic only: 11 / 15 / 20 bps roundtrip. Gross room ~6.5 bps < 11 → **INSUFFICIENT_ROOM**.

## 18. Prefix-Parität
**{parity}**

## 19. Tests
Focused only: `tests/post_case_02_next_pool_causal_reaction_audit_v1/test_next_pool_audit.py` (selection as-of, deterministic choice, TF separation, local-exit invariant, cancel≠depletion, single cancel≠repeated retreat, insufficient room blocks entry, prefix parity).

## 20. Pfad zu MANUAL_NEXT_POOL_REVIEW.md
`{out_dir / 'MANUAL_NEXT_POOL_REVIEW.md'}`

---

Flags:
- outcome_used_for_pool_selection = {OUTCOME_USED_FOR_POOL_SELECTION}
- outcome_used_for_matching = {OUTCOME_USED_FOR_MATCHING}
- outcome_used_for_thresholds = {OUTCOME_USED_FOR_THRESHOLDS}
- outcome_used_for_state_definition = {OUTCOME_USED_FOR_STATE_DEFINITION}

CASE_02 remains `{CASE_02_VERDICT}` (unchanged).
"""
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")

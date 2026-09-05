"""Read-only market → MicroEvidence adapter (no decision resolution).

Builds evidence + opposing pool geometry for `run_mechanical_audit`.
ASK/BID geometry comes exclusively from Entry Contract V2 `resolve_geometry`.
Thresholds/windows match CASE pipeline / V2 constants (unchanged).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.aggressor_efficiency_flip.contracts import UNFITTED_F0_DIAGNOSTIC
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.first_seen import (
    normalize_tick_price,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.ranking import (
    side_levels_ranked_full,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.util import tick_size
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2 import (
    ACCEPT_VARIANTS_S,
    EDGE_TOL_BPS,
    FLOW_WINDOWS_S,
    MAJOR_WALL_RANK,
    MAX_POST_S,
    PRE_S,
    TIMEFRAMES,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.status import atomic_write_json
from orderbook_analyse.liquidity_pool_entry_contract_v2.case_spec import CaseSpec
from orderbook_analyse.liquidity_pool_entry_contract_v2.geometry import (
    PoolGeometry,
    resolve_geometry,
)
from orderbook_analyse.liquidity_pool_signal import chart_lookback_start
from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import (
    pool_row_from_engine,
    run_chart_backend_lld,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1.audit_case import (
    iter_ob_1s,
)


class MarketEvidenceError(RuntimeError):
    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


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


def bps(a: float, b: float) -> float:
    if b <= 0:
        return float("nan")
    return (a - b) / b * 10000.0


def mid_at_or_before(raw_root: Path, symbol: str, ref: datetime) -> dict[str, Any]:
    # iter_ob_1s is BTCUSDT-scoped (matches EXP_* freeze symbols).
    if symbol.upper() != "BTCUSDT":
        return {"ok": False, "reason": f"unsupported_symbol:{symbol}", "symbol": symbol}
    last = None
    for bucket, gen, bb, ba, mid, _bids, _asks in iter_ob_1s(
        raw_root, ref - timedelta(seconds=30), ref
    ):
        if mid is not None:
            last = {
                "ob_timestamp": _iso(_dt_ms(bucket)),
                "ob_ms": bucket,
                "best_bid": bb,
                "best_ask": ba,
                "mid": float(mid),
                "genuine": bool(gen),
                "symbol": symbol,
            }
    if last is None:
        return {"ok": False, "reason": "no_ob_mid", "symbol": symbol}
    last["age_to_reference_s"] = (_ms(ref) - last["ob_ms"]) / 1000.0
    last["ok"] = True
    return last


def pool_zone(mid: float | None, geom: PoolGeometry, tol_bps: float) -> str | None:
    if mid is None or mid <= 0 or geom.upper <= geom.lower:
        return None
    front = geom.front_edge
    back = geom.back_edge
    lo = geom.lower
    hi = geom.upper
    f_lo = front * (1 - tol_bps / 10000.0)
    f_hi = front * (1 + tol_bps / 10000.0)
    b_lo = back * (1 - tol_bps / 10000.0)
    b_hi = back * (1 + tol_bps / 10000.0)
    if geom.pool_side == "BID":
        if mid > f_hi:
            return "ABOVE_FRONT"
        if f_lo <= mid <= f_hi:
            return "AT_FRONT_EDGE"
        if mid < b_lo:
            return "BELOW_BACK"
        if b_lo <= mid <= b_hi:
            return "AT_BACK_EDGE"
        frac = (hi - mid) / (hi - lo)
    else:
        if mid < f_lo:
            return "BELOW_FRONT"
        if f_lo <= mid <= f_hi:
            return "AT_FRONT_EDGE"
        if mid > b_hi:
            return "ABOVE_BACK"
        if b_lo <= mid <= b_hi:
            return "AT_BACK_EDGE"
        frac = (mid - lo) / (hi - lo)
    if frac < 1 / 3:
        return "INSIDE_FRONT_THIRD"
    if frac < 2 / 3:
        return "INSIDE_MIDDLE_THIRD"
    return "INSIDE_BACK_THIRD"


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
    if sell_n >= min_n and buy_n < sell_n * 0.5:
        if mid_chg <= -strong_bps * 0.5:
            return "SELL_EFFECTIVE_BREAK_ATTACK"
        if mid_chg >= -strong_bps * 0.25:
            return "SELL_INEFFICIENT_ABSORPTION"
        return "SELL_EFFECTIVE_BREAK_ATTACK" if mid_chg < 0 else "SELL_INEFFICIENT_ABSORPTION"
    if buy_n >= min_n and sell_n < buy_n * 0.5:
        if mid_chg >= strong_bps * 0.5:
            return "BUY_EFFECTIVE_BREAK_ATTACK"
        if mid_chg <= strong_bps * 0.25:
            return "BUY_INEFFICIENT_ABSORPTION"
        return "BUY_EFFECTIVE_BREAK_ATTACK" if mid_chg > 0 else "BUY_INEFFICIENT_ABSORPTION"
    return "TWO_SIDED_CONTEST"


def _beyond_back(mid: float, geom: PoolGeometry) -> bool:
    if geom.breakout_beyond_back == "below":
        return mid < geom.back_edge
    return mid > geom.back_edge


def _at_or_back_side_of_back(mid: float, geom: PoolGeometry) -> bool:
    """True if mid has returned to pool side of / across back (reclaim of breakout)."""
    if geom.breakout_beyond_back == "below":
        return mid >= geom.back_edge
    return mid <= geom.back_edge


def _reclaim_front(mid: float, geom: PoolGeometry) -> bool:
    if geom.reclaim_toward_front == "above":
        return mid > geom.front_edge
    return mid < geom.front_edge


def _front_hold_ok(mid: float | None, geom: PoolGeometry) -> bool:
    if mid is None:
        return False
    if geom.reclaim_toward_front == "above":
        return mid > geom.front_edge
    return mid < geom.front_edge


def build_market_evidence_bundle(
    *,
    case_spec: CaseSpec,
    repo_root: Path,
    raw_root: Path,
    out_dir: Path,
    query_log: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Load OB/trades/LLD read-only and emit evidence for run_mechanical_audit."""
    query_log = query_log if query_log is not None else []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    geom = resolve_geometry(
        pool_side=case_spec.pool_side,
        approach=case_spec.approach,
        lower=case_spec.pool_lower,
        upper=case_spec.pool_upper,
    )
    symbol = case_spec.symbol
    ref = _utc(case_spec.reference_ts)
    front = geom.front_edge
    back = geom.back_edge
    lo = geom.lower
    hi = geom.upper
    tick = tick_size(symbol)
    min_n = float(UNFITTED_F0_DIAGNOSTIC["min_notional_usdt"])
    strong_bps = float(UNFITTED_F0_DIAGNOSTIC["strong_same_side_impact_bps"])

    mid_info = mid_at_or_before(raw_root, symbol, ref)
    atomic_write_json(out_dir / "reference_mid.json", mid_info)
    if not mid_info.get("ok"):
        raise MarketEvidenceError(
            "EXP_ASK_REAL_DATA_EXECUTION_BLOCKED"
            if geom.pool_side == "ASK"
            else "EXP_BID_REAL_DATA_EXECUTION_BLOCKED",
            "no_ob_mid",
        )
    market = float(mid_info["mid"])

    # Causal opposing / HTF pools as-of reference (room gate input)
    geom_rows: list[dict[str, Any]] = []
    for tf in TIMEFRAMES:
        start = chart_lookback_start(ref, tf)
        bundle = run_chart_backend_lld(symbol=symbol, timeframe=tf, start=start, end=ref)
        query_log.append(
            {
                "kind": "lld_chart",
                "symbol": symbol,
                "timeframe": tf,
                "start": _iso(start),
                "end": _iso(ref),
            }
        )
        for p in bundle["engine_result"].pools:
            r = pool_row_from_engine(p, cfg=bundle["config"], as_of=ref, market_price=market)
            if not r["active_as_of"]:
                continue
            geom_rows.append(
                {
                    "pool_id": r["pool_id"],
                    "source_timeframe": r["source_timeframe"],
                    "side": r["side"],
                    "lower_edge": r["lower_edge"],
                    "upper_edge": r["upper_edge"],
                    "available_at": r["available_at"],
                }
            )
    atomic_write_json(
        out_dir / "causal_pool_geometry.json",
        {"n": len(geom_rows), "rows": geom_rows},
    )
    atomic_write_json(
        out_dir / "selected_pool.json",
        {
            "pool_id": case_spec.pool_id,
            "pool_side": geom.pool_side,
            "approach": geom.approach,
            "lower": lo,
            "upper": hi,
            "front_edge": front,
            "back_edge": back,
            "mid_at_reference": market,
            "available_at": case_spec.pool_first_available_ts,
        },
    )

    load_start = ref - timedelta(seconds=PRE_S)
    load_end = ref + timedelta(seconds=MAX_POST_S)
    trades, trade_pre = load_trades_clickhouse(
        symbol=symbol,
        start=load_start,
        end=load_end + timedelta(seconds=1),
        query_log=query_log,
    )
    end_ms = _ms(load_end)
    start_ms = _ms(load_start)
    ref_ms = _ms(ref)
    trades = [t for t in trades if _ms(t.trade_ts) <= end_ms]
    buy_1s: dict[int, float] = defaultdict(float)
    sell_1s: dict[int, float] = defaultdict(float)
    for t in trades:
        sb = (_ms(t.trade_ts) // 1000) * 1000
        if t.side == "Buy":
            buy_1s[sb] += t.notional
        else:
            sell_1s[sb] += t.notional

    grid = list(range(start_ms, end_ms + 1000, 1000))
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

    def mid_get(ms: int) -> float | None:
        b = (ms // 1000) * 1000
        for off in range(0, 5):
            if b + off * 1000 in mid_by:
                return mid_by[b + off * 1000]
            if b - off * 1000 in mid_by:
                return mid_by[b - off * 1000]
        return None

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
            "mid_change_bps": chg,
            "class": aggressor_class(buy_n, sell_n, chg, min_n, strong_bps),
        }

    # Front arrival (direction from approach)
    arrival_ms = None
    prev = None
    for s in grid:
        m = mid_by.get(s)
        if m is None:
            continue
        if prev is not None:
            if geom.approach == "FROM_ABOVE" and prev > front and m <= front:
                arrival_ms = s
                if s >= ref_ms - 2000:
                    break
            if geom.approach == "FROM_BELOW" and prev < front and m >= front:
                arrival_ms = s
                if s >= ref_ms - 2000:
                    break
        prev = m
    if arrival_ms is None:
        if lo <= market <= hi:
            arrival_ms = ref_ms
        else:
            raise MarketEvidenceError(
                "EXP_ASK_REAL_DATA_EXECUTION_BLOCKED"
                if geom.pool_side == "ASK"
                else "EXP_BID_REAL_DATA_EXECUTION_BLOCKED",
                "no_front_arrival",
            )

    timeline: list[dict[str, Any]] = []
    wall_hist: dict[float, dict[str, Any]] = {}
    prev_major: dict[float, float] = {}
    retreat_events: list[dict[str, Any]] = []
    first_back_cross_ms = None
    first_reclaim_front_ms = None
    seen_inside = False

    attack_side = geom.attack_aggressor  # Sell vs BID, Buy vs ASK
    wall_book_side = "BID" if geom.pool_side == "BID" else "ASK"

    for s in grid:
        if s < arrival_ms - PRE_S * 1000:
            continue
        mid = mid_by.get(s)
        zone = pool_zone(mid, geom, EDGE_TOL_BPS)
        buy_n = buy_1s.get(s, 0.0)
        sell_n = sell_1s.get(s, 0.0)
        flows = {str(w): window_flow(s, w) for w in FLOW_WINDOWS_S}
        f5 = flows["5"]
        if zone and zone.startswith("INSIDE"):
            seen_inside = True
        if mid is not None and _beyond_back(mid, geom) and s >= arrival_ms:
            if first_back_cross_ms is None:
                first_back_cross_ms = s
        if mid is not None and _reclaim_front(mid, geom) and seen_inside and s >= arrival_ms:
            if first_reclaim_front_ms is None and first_back_cross_ms is None:
                first_reclaim_front_ms = s
            elif first_back_cross_ms is not None and first_reclaim_front_ms is None:
                first_reclaim_front_ms = s

        book = book_by.get(s)
        major_now: dict[float, float] = {}
        if book:
            _bb, _ba, bids, asks = book
            levels = bids if wall_book_side == "BID" else asks
            ranked = side_levels_ranked_full([(p, q) for p, q in levels])
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
                        "side": wall_book_side,
                        "first_seen_ts": _iso(_dt_ms(s)),
                        "last_seen_ts": _iso(_dt_ms(s)),
                        "max_notional": row["notional"],
                        "max_rank": row["full_side_rank"],
                        "attacked": False,
                        "trade_depletion": False,
                        "refilled": False,
                        "cancelled_before_touch": False,
                        "reappeared_lower": False,
                        "reappeared_higher": False,
                        "qty_series": [],
                    },
                )
                h["last_seen_ts"] = _iso(_dt_ms(s))
                h["max_notional"] = max(h["max_notional"], row["notional"])
                h["max_rank"] = min(h["max_rank"], row["full_side_rank"])
                h["qty_series"].append((s, row["qty"]))
                if len(h["qty_series"]) > 3:
                    h["qty_series"] = h["qty_series"][-3:]
                for t in trades:
                    if (_ms(t.trade_ts) // 1000) * 1000 != s:
                        continue
                    if t.side == attack_side and abs(
                        normalize_tick_price(t.price, tick) - px
                    ) < 1e-9:
                        h["attacked"] = True
                qs = h["qty_series"]
                if len(qs) >= 2 and qs[-1][0] == s:
                    prev_q, cur_q = qs[-2][1], qs[-1][1]
                    if cur_q > prev_q + 1e-12:
                        h["refilled"] = True
                    if cur_q < prev_q - 1e-12:
                        atk_at = sum(
                            t.size
                            for t in trades
                            if (_ms(t.trade_ts) // 1000) * 1000 == s
                            and t.side == attack_side
                            and abs(normalize_tick_price(t.price, tick) - px) < 1e-9
                        )
                        red = prev_q - cur_q
                        if atk_at >= 0.85 * red and atk_at > 0:
                            h["trade_depletion"] = True
                        elif atk_at <= 0:
                            h["cancelled_before_touch"] = h["cancelled_before_touch"] or (
                                not h["attacked"]
                            )

            for px, notion in prev_major.items():
                if px not in major_now and notion >= min_n * 0.5:
                    lower = [p for p in major_now if p < px - tick]
                    higher = [p for p in major_now if p > px + tick]
                    if lower:
                        rep = max(lower)
                        followed = mid is not None and mid < px
                        retreat_events.append(
                            {
                                "disappearance_ts": _iso(_dt_ms(s)),
                                "old_wall_price": px,
                                "old_wall_attacked": bool(wall_hist.get(px, {}).get("attacked")),
                                "replacement_wall_price": rep,
                                "price_followed": followed,
                                "pattern": "RETREATED_LOWER",
                            }
                        )
                        wall_hist[px]["reappeared_lower"] = True
                    if higher:
                        rep = min(higher)
                        followed = mid is not None and mid > px
                        retreat_events.append(
                            {
                                "disappearance_ts": _iso(_dt_ms(s)),
                                "old_wall_price": px,
                                "old_wall_attacked": bool(wall_hist.get(px, {}).get("attacked")),
                                "replacement_wall_price": rep,
                                "price_followed": followed,
                                "pattern": "RETREATED_HIGHER",
                            }
                        )
                        wall_hist[px]["reappeared_higher"] = True
        prev_major = major_now

        timeline.append(
            {
                "second": _iso(_dt_ms(s)),
                "second_ms": s,
                "mid": mid,
                "pool_zone": zone,
                "buy_notional_1s": buy_n,
                "sell_notional_1s": sell_n,
                "aggressor_class_5s": f5["class"],
            }
        )

    # Acceptance / reclaim holds
    accept_rows = []
    for hold_s in ACCEPT_VARIANTS_S:
        beyond_ok = False
        if first_back_cross_ms is not None:
            beyond_ok = True
            for hs in range(first_back_cross_ms, first_back_cross_ms + hold_s * 1000 + 1000, 1000):
                m = mid_by.get(hs)
                if m is None or not _beyond_back(m, geom):
                    beyond_ok = False
                    break
        reclaim_ok = False
        if first_back_cross_ms is not None:
            reclaim_ms = None
            for hs in range(first_back_cross_ms, first_back_cross_ms + 180_000, 1000):
                m = mid_by.get(hs)
                if m is not None and _at_or_back_side_of_back(m, geom):
                    reclaim_ms = hs
                    break
            if reclaim_ms is not None:
                reclaim_ok = True
                for hs in range(reclaim_ms, reclaim_ms + hold_s * 1000 + 1000, 1000):
                    m = mid_by.get(hs)
                    if m is None or not _at_or_back_side_of_back(m, geom):
                        reclaim_ok = False
                        break
        front_hold = False
        if first_reclaim_front_ms is not None:
            front_hold = True
            for hs in range(
                first_reclaim_front_ms, first_reclaim_front_ms + hold_s * 1000 + 1000, 1000
            ):
                m = mid_by.get(hs)
                if not _front_hold_ok(m, geom):
                    front_hold = False
                    break
        accept_rows.append(
            {
                "hold_s": hold_s,
                "breakout_accepted_beyond_back": beyond_ok,
                "reclaim_across_back_held": reclaim_ok,
                "reclaim_front_held": front_hold,
            }
        )

    wall_rows = []
    for px, h in sorted(wall_hist.items()):
        cls = "STABLE_DEFENSE"
        adverse = (
            h.get("reappeared_lower")
            if geom.wall_retreat_adverse == "lower"
            else h.get("reappeared_higher")
        )
        if h["trade_depletion"]:
            cls = "TRADE_SUPPORTED_DEPLETION"
        elif h["cancelled_before_touch"] and adverse:
            cls = f"RETREATED_{geom.wall_retreat_adverse.upper()}"
        elif h["cancelled_before_touch"]:
            cls = "CANCEL_DOMINANT_REMOVAL"
        elif h["refilled"] and h["attacked"]:
            cls = "REFILLED"
        elif adverse:
            cls = f"REAPPEARED_{geom.wall_retreat_adverse.upper()}"
        elif h["attacked"]:
            cls = "MIXED"
        wall_rows.append({**{k: v for k, v in h.items() if k != "qty_series"}, "lifecycle_class": cls})

    post = [r for r in timeline if r["second_ms"] >= arrival_ms]
    sell_eff = sum(1 for r in post if r.get("aggressor_class_5s") == "SELL_EFFECTIVE_BREAK_ATTACK")
    sell_abs = sum(1 for r in post if r.get("aggressor_class_5s") == "SELL_INEFFICIENT_ABSORPTION")
    buy_eff = sum(1 for r in post if r.get("aggressor_class_5s") == "BUY_EFFECTIVE_BREAK_ATTACK")
    buy_abs = sum(1 for r in post if r.get("aggressor_class_5s") == "BUY_INEFFICIENT_ABSORPTION")
    two = sum(1 for r in post if r.get("aggressor_class_5s") == "TWO_SIDED_CONTEST")

    # Side-symmetric aggressor tallies
    if geom.pool_side == "BID":
        attack_eff_count = sell_eff
        attack_abs_count = sell_abs
        counter_count = buy_eff  # buy with +impact
    else:
        attack_eff_count = buy_eff
        attack_abs_count = buy_abs
        counter_count = sell_eff  # sell with -impact

    acc5 = next(r for r in accept_rows if r["hold_s"] == 5)
    acc15 = next(r for r in accept_rows if r["hold_s"] == 15)
    acc30 = next(r for r in accept_rows if r["hold_s"] == 30)

    trade_dep = any(w["lifecycle_class"] == "TRADE_SUPPORTED_DEPLETION" for w in wall_rows)
    adverse_pattern = (
        "RETREATED_LOWER" if geom.wall_retreat_adverse == "lower" else "RETREATED_HIGHER"
    )
    retreat_follow = (
        sum(
            1
            for e in retreat_events
            if e.get("pattern") == adverse_pattern
            and e.get("price_followed")
            and not e.get("old_wall_attacked")
        )
        >= 2
    )

    defense_ok = (
        seen_inside
        and attack_abs_count >= 1
        and counter_count >= 1
        and not (
            acc5["breakout_accepted_beyond_back"] and acc15["breakout_accepted_beyond_back"]
        )
        and not retreat_follow
    )
    breakout_ok = (
        first_back_cross_ms is not None
        and acc5["breakout_accepted_beyond_back"]
        and (trade_dep or retreat_follow or attack_eff_count >= 1)
        and not acc30["reclaim_across_back_held"]
        and attack_eff_count >= 1
    )
    breakout_contested = bool(
        first_back_cross_ms is not None
        and acc5["breakout_accepted_beyond_back"]
        and (
            acc30["reclaim_across_back_held"] or not acc30["breakout_accepted_beyond_back"]
        )
    )

    defense_first_ts = None
    defense_entry = None
    breakout_first_ts = None
    breakout_entry = None
    if defense_ok and first_reclaim_front_ms is not None and acc5.get("reclaim_front_held"):
        defense_first_ts = _iso(_dt_ms(first_reclaim_front_ms + 5000))
        defense_entry = mid_get(first_reclaim_front_ms + 5000)
    elif defense_ok and counter_count >= 1:
        counter_class = (
            "BUY_EFFECTIVE_BREAK_ATTACK"
            if geom.pool_side == "BID"
            else "SELL_EFFECTIVE_BREAK_ATTACK"
        )
        for r in post:
            if r.get("aggressor_class_5s") != counter_class or r.get("mid") is None:
                continue
            if geom.pool_side == "BID" and r["mid"] > front * 0.999:
                defense_first_ts = r["second"]
                defense_entry = r["mid"]
                break
            if geom.pool_side == "ASK" and r["mid"] < front * 1.001:
                defense_first_ts = r["second"]
                defense_entry = r["mid"]
                break
    if breakout_ok or (first_back_cross_ms and acc5["breakout_accepted_beyond_back"]):
        breakout_first_ts = (
            _iso(_dt_ms(first_back_cross_ms + 5000)) if first_back_cross_ms else None
        )
        breakout_entry = mid_get(first_back_cross_ms + 5000) if first_back_cross_ms else None

    evidence = {
        "seen_inside": seen_inside,
        "arrival_present": arrival_ms is not None,
        "defense_ok": bool(defense_ok),
        "breakout_ok": bool(breakout_ok),
        "breakout_contested": bool(breakout_contested),
        "defense_entry": defense_entry,
        "breakout_entry": breakout_entry,
        "defense_first_ts": defense_first_ts,
        "breakout_first_ts": breakout_first_ts,
        "attack_eff_count": int(attack_eff_count),
        "counter_count": int(counter_count),
        "two_sided_count": int(two),
    }

    diagnostics = {
        "arrival_ts": _iso(_dt_ms(arrival_ms)),
        "first_back_cross_ts": _iso(_dt_ms(first_back_cross_ms)) if first_back_cross_ms else None,
        "first_reclaim_front_ts": (
            _iso(_dt_ms(first_reclaim_front_ms)) if first_reclaim_front_ms else None
        ),
        "acceptance_variants": accept_rows,
        "aggressor_counts": {
            "sell_effective": sell_eff,
            "sell_inefficient_absorption": sell_abs,
            "buy_effective": buy_eff,
            "buy_inefficient_absorption": buy_abs,
            "two_sided": two,
            "attack_eff": attack_eff_count,
            "attack_abs": attack_abs_count,
            "counter": counter_count,
        },
        "wall_flags": {
            "trade_supported_depletion": trade_dep,
            "adverse_retreat_with_price_follow_repeated": retreat_follow,
            "n_retreat_events": len(retreat_events),
            "adverse_pattern": adverse_pattern,
        },
        "geometry": {
            "pool_side": geom.pool_side,
            "approach": geom.approach,
            "front_edge": front,
            "back_edge": back,
            "defense_trade_direction": geom.defense_trade_direction,
            "breakout_trade_direction": geom.breakout_trade_direction,
        },
        "n_trades": len(trades),
        "n_ob_seconds": len(ob_rows),
        "n_timeline": len(timeline),
        "trade_preflight": trade_pre,
        "reference_mid": mid_info,
    }
    atomic_write_json(out_dir / "market_evidence.json", evidence)
    atomic_write_json(out_dir / "market_diagnostics.json", diagnostics)

    return {
        "evidence": evidence,
        "pool_geometry_rows": geom_rows,
        "diagnostics": diagnostics,
        "query_log": query_log,
        "market_data_loaded": True,
        "outcomes_read": False,
    }

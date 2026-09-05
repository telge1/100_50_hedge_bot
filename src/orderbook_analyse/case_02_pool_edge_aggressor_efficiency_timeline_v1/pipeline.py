"""Causal second-by-second CASE_02 timeline (read-only diagnostic)."""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.aggressor_efficiency_flip.contracts import (
    CANONICAL_TRADES_TABLE,
    UNFITTED_F0_DIAGNOSTIC,
)
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.case_02_pool_edge_aggressor_efficiency_timeline_v1 import (
    ACCEPT_VARIANTS_S,
    APPROACH,
    ARRIVAL_TS,
    CLUSTER_ID,
    EDGE_TOL_BPS,
    FLOW_WINDOWS_S,
    FORMAT_VERSION,
    LOAD_START_TS,
    MAX_END_TS,
    OUTCOME_USED_FOR_EVENT_SELECTION,
    OUTCOME_USED_FOR_STATE_DEFINITION,
    OUTCOME_USED_FOR_THRESHOLDS,
    POOL_HI,
    POOL_LO,
    PRIMARY_EDGE_TOL_BPS,
    SIDE,
    START_WALL,
    SYMBOL,
    TIMEFRAME,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.first_seen import (
    FirstSeenClass,
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
from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import (
    chart_lookback_start,
    load_chart_candles,
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


def bps(a: float, b: float) -> float:
    if b <= 0:
        return float("nan")
    return (a - b) / b * 10000.0


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


def filter_as_of(rows: list[dict[str, Any]], *, ts_key: str, as_of_ms: int) -> list[dict[str, Any]]:
    """Hard causal cut: keep only rows with ts <= as_of."""
    out = []
    for r in rows:
        t = r.get(ts_key)
        if t is None:
            continue
        ms = t if isinstance(t, int) else _ms(t)
        if ms <= as_of_ms:
            out.append(r)
    return out


def pool_zone(mid: float | None, lo: float, hi: float, edge_tol_bps: float) -> str | None:
    if mid is None or mid <= 0:
        return None
    width = hi - lo
    if width <= 0:
        return None
    lo_lo = lo * (1 - edge_tol_bps / 10000.0)
    lo_hi = lo * (1 + edge_tol_bps / 10000.0)
    hi_lo = hi * (1 - edge_tol_bps / 10000.0)
    hi_hi = hi * (1 + edge_tol_bps / 10000.0)
    if mid < lo_lo:
        return "BELOW_POOL"
    if lo_lo <= mid <= lo_hi:
        return "AT_LOWER_EDGE"
    if hi_lo <= mid <= hi_hi:
        return "AT_UPPER_EDGE"
    if mid > hi_hi:
        return "ABOVE_POOL"
    # inside
    frac = (mid - lo) / width
    if frac < 1 / 3:
        return "INSIDE_LOWER_THIRD"
    if frac < 2 / 3:
        return "INSIDE_MIDDLE_THIRD"
    return "INSIDE_UPPER_THIRD"


def impact_label(
    *,
    buy_n: float,
    sell_n: float,
    mid_chg_bps: float | None,
    min_notional: float,
    strong_bps: float,
) -> str:
    if buy_n < min_notional and sell_n < min_notional:
        return "LOW_FLOW" if (buy_n + sell_n) > 0 else "INSUFFICIENT"
    two = buy_n >= min_notional and sell_n >= min_notional
    if mid_chg_bps is None:
        return "TWO_SIDED_HIGH_FLOW" if two else "INSUFFICIENT"
    labels = []
    if buy_n >= min_notional:
        if mid_chg_bps >= strong_bps:
            labels.append("STRONG_BUY_EFFECTIVE")
        elif mid_chg_bps <= strong_bps * 0.25:
            labels.append("STRONG_BUY_INEFFICIENT")
    if sell_n >= min_notional:
        # sell efficiency: negative price progress
        if mid_chg_bps <= -strong_bps:
            labels.append("STRONG_SELL_EFFECTIVE")
        elif mid_chg_bps >= -strong_bps * 0.25:
            labels.append("STRONG_SELL_INEFFICIENT")
    if two and len(labels) >= 1:
        labels.append("TWO_SIDED_HIGH_FLOW")
    if not labels:
        return "LOW_FLOW"
    # prefer most informative single label; keep combined for two-sided
    if "STRONG_SELL_INEFFICIENT" in labels and sell_n >= min_notional:
        return "STRONG_SELL_INEFFICIENT" if not two else "TWO_SIDED_HIGH_FLOW|STRONG_SELL_INEFFICIENT"
    if "STRONG_BUY_EFFECTIVE" in labels:
        return "STRONG_BUY_EFFECTIVE" if not two else "TWO_SIDED_HIGH_FLOW|STRONG_BUY_EFFECTIVE"
    return "|".join(labels)


def existing_aef_label(buy_n: float, sell_n: float, mid_chg_bps: float | None) -> str:
    thr = float(UNFITTED_F0_DIAGNOSTIC["strong_same_side_impact_bps"])
    mn = float(UNFITTED_F0_DIAGNOSTIC["min_notional_usdt"])
    return impact_label(
        buy_n=buy_n, sell_n=sell_n, mid_chg_bps=mid_chg_bps, min_notional=mn, strong_bps=thr
    )


def load_closed_emas(end: datetime) -> dict[str, list[tuple[int, float]]]:
    """EMA from chart candles; point available only after candle close (open+5m)."""
    look = chart_lookback_start(end, TIMEFRAME)
    packed, candles = load_chart_candles(SYMBOL, TIMEFRAME, start=look, end=end)
    from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import _ensure_dashboard_path

    _ensure_dashboard_path()
    from research_charts.service import _indicators_from_candles

    ind = _indicators_from_candles(
        packed, candles, ema={"enabled": True}, stochastic={"enabled": False}, liquidity=None
    )
    out: dict[str, list[tuple[int, float]]] = {}
    for s in ind.get("ema", {}).get("series") or []:
        period = int(s.get("period") or 0)
        key = f"ema{period}"
        pts: list[tuple[int, float]] = []
        for pt in s.get("data") or []:
            open_unix = int(pt["time"])
            # candle open at T closes at T+300s → available_ms = (T+300)*1000
            avail_ms = (open_unix + 300) * 1000
            pts.append((avail_ms, float(pt["value"])))
        pts.sort()
        out[key] = pts
    # EMA200: compute from closed candle closes if chart default lacks it
    if "ema200" not in out:
        closes: list[tuple[int, float]] = []
        for c in candles:
            open_ms = int(_utc(c.timestamp).timestamp()) * 1000
            avail_ms = open_ms + 300_000
            closes.append((avail_ms, float(c.close)))
        closes.sort()
        out["ema200"] = _ema_series(closes, 200)
    # ensure ema50 exists (often blue)
    if "ema50" not in out and "ema20" in out:
        closes = []
        for c in candles:
            open_ms = int(_utc(c.timestamp).timestamp()) * 1000
            closes.append((open_ms + 300_000, float(c.close)))
        out["ema50"] = _ema_series(sorted(closes), 50)
    return out


def _ema_series(points: list[tuple[int, float]], period: int) -> list[tuple[int, float]]:
    if len(points) < period:
        return []
    k = 2 / (period + 1)
    seed = sum(v for _, v in points[:period]) / period
    out = [(points[period - 1][0], seed)]
    ema = seed
    for t, v in points[period:]:
        ema = v * k + ema * (1 - k)
        out.append((t, ema))
    return out


def ema_as_of(series: list[tuple[int, float]], as_of_ms: int) -> float | None:
    val = None
    for t, v in series:
        if t <= as_of_ms:
            val = v
        else:
            break
    return val


@dataclass
class SecRow:
    second_ms: int
    mid: float | None
    bb: float | None
    ba: float | None
    genuine: bool
    asks: list[tuple[float, float]]
    bids: list[tuple[float, float]]


def build_second_book(
    ob_rows: list[tuple],
    start_ms: int,
    end_ms: int,
) -> list[SecRow]:
    by = {b: (g, bb, ba, mid, bids, asks) for b, g, bb, ba, mid, bids, asks in ob_rows}
    rows = []
    for s in range(start_ms, end_ms + 1000, 1000):
        if s in by:
            g, bb, ba, mid, bids, asks = by[s]
            rows.append(SecRow(s, mid, bb, ba, bool(g), asks, bids))
        else:
            rows.append(SecRow(s, None, None, None, False, [], []))
    return rows


def run_case_02(*, raw_root: Path, out_dir: Path) -> dict[str, Any]:
    t0 = datetime.now(timezone.utc)
    arrival = _utc(ARRIVAL_TS)
    load_start = _utc(LOAD_START_TS)
    max_end = _utc(MAX_END_TS)
    arrival_ms = _ms(arrival)
    end_ms = _ms(max_end)
    start_ms = _ms(load_start)
    tick = tick_size(SYMBOL)
    wall0 = normalize_tick_price(START_WALL, tick)
    lo, hi = POOL_LO, POOL_HI
    min_n = float(UNFITTED_F0_DIAGNOSTIC["min_notional_usdt"])
    strong_bps = float(UNFITTED_F0_DIAGNOSTIC["strong_same_side_impact_bps"])

    trades, trade_pre = load_trades_clickhouse(
        symbol=SYMBOL, start=load_start, end=max_end + timedelta(seconds=1)
    )
    trades = [t for t in trades if _ms(t.trade_ts) <= end_ms]
    trades_by_s: dict[int, list] = defaultdict(list)
    buy_1s: dict[int, float] = defaultdict(float)
    sell_1s: dict[int, float] = defaultdict(float)
    buy_c_1s: dict[int, int] = defaultdict(int)
    sell_c_1s: dict[int, int] = defaultdict(int)
    for t in trades:
        sb = (_ms(t.trade_ts) // 1000) * 1000
        trades_by_s[sb].append(t)
        if t.side == "Buy":
            buy_1s[sb] += t.notional
            buy_c_1s[sb] += 1
        else:
            sell_1s[sb] += t.notional
            sell_c_1s[sb] += 1

    # Prefix sums on 1s grid for O(1) window queries
    grid = list(range(start_ms, end_ms + 1000, 1000))
    buy_pref = [0.0]
    sell_pref = [0.0]
    buyc_pref = [0]
    sellc_pref = [0]
    idx_of = {s: i for i, s in enumerate(grid)}
    for s in grid:
        buy_pref.append(buy_pref[-1] + buy_1s.get(s, 0.0))
        sell_pref.append(sell_pref[-1] + sell_1s.get(s, 0.0))
        buyc_pref.append(buyc_pref[-1] + buy_c_1s.get(s, 0))
        sellc_pref.append(sellc_pref[-1] + sell_c_1s.get(s, 0))

    ob_rows = list(iter_ob_1s(raw_root, load_start, max_end))
    print(f"ob_seconds={len(ob_rows)} trades={len(trades)}", flush=True)
    secs = build_second_book(ob_rows, start_ms, end_ms)
    mid_by = {r.second_ms: r.mid for r in secs if r.genuine and r.mid is not None}

    emas = load_closed_emas(max_end)
    print("emas loaded", list(emas.keys()), flush=True)

    # Pre-arrival warmup notionals for descriptive quantiles only (not for live thresholds)
    pre_buy = [buy_1s.get(s, 0.0) for s in range(start_ms, arrival_ms, 1000)]
    pre_sell = [sell_1s.get(s, 0.0) for s in range(start_ms, arrival_ms, 1000)]

    def q80(xs: list[float]) -> float:
        ys = sorted(xs)
        if not ys:
            return min_n
        return ys[int(0.8 * (len(ys) - 1))]

    warmup_buy_q80 = q80(pre_buy)
    warmup_sell_q80 = q80(pre_sell)

    timeline: list[dict[str, Any]] = []
    seconds_inside = 0
    local_exit = False
    reentered = False
    upper_crossed = False
    first_upper_cross_ms = None
    seen_inside = False
    wall_hist: dict[float, dict[str, Any]] = {}
    state = "UNRESOLVED_TIMEOUT"
    state_rows: list[dict[str, Any]] = []
    prev_state = None
    attack_episodes: list[dict[str, Any]] = []
    open_attacks: dict[str, dict[str, Any]] = {}

    def mid_at(ms: int) -> float | None:
        b = (ms // 1000) * 1000
        for off in range(0, 5):
            m = mid_by.get(b + off * 1000) or mid_by.get(b - off * 1000)
            if m is not None:
                return m
        return None

    def window_flow(t0_ms: int, win_s: int) -> dict[str, Any]:
        t1 = t0_ms + win_s * 1000
        if t1 > end_ms or t0_ms not in idx_of:
            return {
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "total_notional": 0.0,
                "delta_notional": 0.0,
                "buy_share": None,
                "sell_share": None,
                "trade_count_buy": 0,
                "trade_count_sell": 0,
                "mid_change_bps": None,
                "max_up_move_bps": None,
                "max_down_move_bps": None,
                "buy_impact_bps_per_100k": None,
                "sell_impact_bps_per_100k": None,
                "diag_label": "INSUFFICIENT",
                "existing_aef_label": "INSUFFICIENT",
            }
        i0 = idx_of[t0_ms]
        # sum over seconds [t0, t1)
        last_s = t1 - 1000
        if last_s < t0_ms:
            return window_flow(t0_ms, 0) if False else {
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "total_notional": 0.0,
                "delta_notional": 0.0,
                "buy_share": None,
                "sell_share": None,
                "trade_count_buy": 0,
                "trade_count_sell": 0,
                "mid_change_bps": None,
                "max_up_move_bps": None,
                "max_down_move_bps": None,
                "buy_impact_bps_per_100k": None,
                "sell_impact_bps_per_100k": None,
                "diag_label": "INSUFFICIENT",
                "existing_aef_label": "INSUFFICIENT",
            }
        i1 = idx_of.get(last_s)
        if i1 is None:
            # clamp to last available
            i1 = len(grid) - 1
            while i1 >= 0 and grid[i1] >= t1:
                i1 -= 1
            if i1 < i0:
                buy_n = sell_n = 0.0
                bc = sc = 0
            else:
                buy_n = buy_pref[i1 + 1] - buy_pref[i0]
                sell_n = sell_pref[i1 + 1] - sell_pref[i0]
                bc = buyc_pref[i1 + 1] - buyc_pref[i0]
                sc = sellc_pref[i1 + 1] - sellc_pref[i0]
        else:
            buy_n = buy_pref[i1 + 1] - buy_pref[i0]
            sell_n = sell_pref[i1 + 1] - sell_pref[i0]
            bc = buyc_pref[i1 + 1] - buyc_pref[i0]
            sc = sellc_pref[i1 + 1] - sellc_pref[i0]
        m0 = mid_at(t0_ms)
        m1 = mid_at(t1 - 1)
        chg = bps(m1, m0) if m0 and m1 else None
        ups = downs = None
        if m0:
            path = [mid_by[s] for s in range(t0_ms, min(t1, end_ms + 1000), 1000) if s in mid_by]
            if path:
                ups = max(bps(m, m0) for m in path)
                downs = min(bps(m, m0) for m in path)
        buy_imp = (chg / buy_n * 100_000) if buy_n and chg is not None else None
        sell_imp = ((-chg) / sell_n * 100_000) if sell_n and chg is not None else None
        return {
            "buy_notional": buy_n,
            "sell_notional": sell_n,
            "total_notional": buy_n + sell_n,
            "delta_notional": buy_n - sell_n,
            "buy_share": buy_n / (buy_n + sell_n) if (buy_n + sell_n) else None,
            "sell_share": sell_n / (buy_n + sell_n) if (buy_n + sell_n) else None,
            "trade_count_buy": bc,
            "trade_count_sell": sc,
            "mid_change_bps": chg,
            "max_up_move_bps": ups,
            "max_down_move_bps": downs,
            "buy_impact_bps_per_100k": buy_imp,
            "sell_impact_bps_per_100k": sell_imp,
            "diag_label": impact_label(
                buy_n=buy_n, sell_n=sell_n, mid_chg_bps=chg, min_notional=min_n, strong_bps=strong_bps
            ),
            "existing_aef_label": existing_aef_label(buy_n, sell_n, chg),
        }

    for row in secs:
        s = row.second_ms
        mid = row.mid if row.genuine else None
        zone_primary = pool_zone(mid, lo, hi, PRIMARY_EDGE_TOL_BPS)
        zones = {f"zone_{int(t)}bps": pool_zone(mid, lo, hi, t) for t in EDGE_TOL_BPS}
        if mid is not None and lo <= mid <= hi:
            seconds_inside += 1
            seen_inside = True
        dist_lo = bps(mid, lo) if mid else None
        dist_hi = bps(mid, hi) if mid else None
        pen = max(0.0, bps(mid, lo)) if mid and mid >= lo else 0.0
        pen_frac = ((mid - lo) / (hi - lo)) if mid and hi > lo else None

        # exits / reentries (primary tol)
        if seen_inside and zone_primary == "BELOW_POOL":
            local_exit = True
        if local_exit and zone_primary in (
            "AT_LOWER_EDGE",
            "INSIDE_LOWER_THIRD",
            "INSIDE_MIDDLE_THIRD",
            "INSIDE_UPPER_THIRD",
            "AT_UPPER_EDGE",
            "ABOVE_POOL",
        ):
            reentered = True
        if mid is not None and mid > hi * (1 + PRIMARY_EDGE_TOL_BPS / 10000.0):
            if not upper_crossed:
                first_upper_cross_ms = s
            upper_crossed = True

        # walls inside pool (MAJOR/top only to limit cost)
        ranked = side_levels_ranked_full(row.asks) if row.asks else []
        inside_walls = [r for r in ranked if lo <= r["price"] <= hi][:30]
        for w in inside_walls:
            px = normalize_tick_price(w["price"], tick)
            h = wall_hist.setdefault(
                px,
                {
                    "price": px,
                    "first_seen_ts": None,
                    "last_seen_ts": None,
                    "max_notional": 0.0,
                    "max_rank": None,
                    "present_pre": False,
                    "present_exact": False,
                    "attacked": False,
                    "consumed_by_trades": False,
                    "refilled": False,
                    "cancelled_or_moved": False,
                    "price_traded_above": False,
                    "acceptance_above_wall": False,
                    "qty_series": [],
                },
            )
            h["last_seen_ts"] = _iso(_dt_ms(s))
            if h["first_seen_ts"] is None:
                h["first_seen_ts"] = _iso(_dt_ms(s))
            h["max_notional"] = max(h["max_notional"], float(w["notional"]))
            h["max_rank"] = (
                int(w["full_side_rank"])
                if h["max_rank"] is None
                else min(h["max_rank"], int(w["full_side_rank"]))
            )
            if s < arrival_ms and w["qty"] > 0:
                h["present_pre"] = True
            if s == arrival_ms and w["qty"] > 0:
                h["present_exact"] = True
            h["qty_series"].append((s, float(w["qty"]), float(w["notional"]), int(w["full_side_rank"])))

        # trade attacks on walls
        sec_tr = trades_by_s.get(s, [])
        buy_n = buy_1s.get(s, 0.0)
        sell_n = sell_1s.get(s, 0.0)
        for t in sec_tr:
            if t.side != "Buy":
                continue
            tp = normalize_tick_price(t.price, tick)
            if tp in wall_hist:
                wall_hist[tp]["attacked"] = True
        if mid is not None:
            for w in inside_walls:
                px = normalize_tick_price(w["price"], tick)
                if mid > px and px in wall_hist:
                    wall_hist[px]["price_traded_above"] = True
            if mid > wall0 and wall0 in wall_hist:
                wall_hist[wall0]["price_traded_above"] = True
        # wall qty decomp vs previous second for known levels
        for px, h in wall_hist.items():
            qs = h["qty_series"]
            if len(qs) >= 2 and qs[-1][0] == s:
                prev_q, cur_q = qs[-2][1], qs[-1][1]
                if cur_q > prev_q + 1e-12:
                    h["refilled"] = True
                if cur_q < prev_q - 1e-12:
                    buy_at = sum(
                        t.size
                        for t in sec_tr
                        if t.side == "Buy" and abs(normalize_tick_price(t.price, tick) - px) < 1e-9
                    )
                    red = prev_q - cur_q
                    if buy_at >= 0.85 * red and buy_at > 0:
                        h["consumed_by_trades"] = True
                    elif buy_at <= 0:
                        h["cancelled_or_moved"] = True
            # keep only last 2 samples
            if len(qs) > 2:
                h["qty_series"] = qs[-2:]
        flows = {str(w): window_flow(s, w) for w in FLOW_WINDOWS_S}
        f5 = flows["5"]

        ema9 = ema_as_of(emas.get("ema9", []), s)
        ema20 = ema_as_of(emas.get("ema20", []), s)
        ema50 = ema_as_of(emas.get("ema50", []), s)
        ema200 = ema_as_of(emas.get("ema200", []), s)

        # state machine (causal, no future)
        new_state = prev_state or "ARRIVED_AT_LOWER_EDGE"
        if s < arrival_ms:
            new_state = "PRE_ARRIVAL"
        elif s == arrival_ms or (prev_state in (None, "PRE_ARRIVAL") and zone_primary in ("AT_LOWER_EDGE", "INSIDE_LOWER_THIRD")):
            new_state = "ARRIVED_AT_LOWER_EDGE"
        if zone_primary and zone_primary.startswith("INSIDE"):
            new_state = "ENTERED_POOL"
        if zone_primary == "AT_LOWER_EDGE" and s > arrival_ms:
            new_state = "LOWER_EDGE_TEST"
        if zone_primary == "BELOW_POOL" and seen_inside:
            new_state = "LOCAL_EXIT_BELOW"
        if reentered and zone_primary and zone_primary != "BELOW_POOL" and local_exit:
            new_state = "REENTERED_POOL"
        if f5["sell_notional"] >= min_n and zone_primary in ("AT_LOWER_EDGE", "BELOW_POOL", "INSIDE_LOWER_THIRD"):
            lab = f5["diag_label"] or ""
            if "STRONG_SELL_EFFECTIVE" in lab:
                new_state = "SELL_ATTACK_EFFECTIVE"
            elif "STRONG_SELL_INEFFICIENT" in lab:
                new_state = "SELL_ATTACK_INEFFICIENT"
            else:
                new_state = "SELL_PRESSURE_AT_LOWER_EDGE"
        if f5["buy_notional"] >= min_n and zone_primary and zone_primary.startswith("INSIDE"):
            if "STRONG_BUY_EFFECTIVE" in (f5["diag_label"] or ""):
                new_state = "BUY_PRESSURE_INSIDE"
        # wall overrun
        if wall0 in wall_hist and wall_hist[wall0].get("price_traded_above"):
            if any(
                abs(normalize_tick_price(t.price, tick) - wall0) <= tick and t.side == "Buy"
                for t in sec_tr
            ):
                new_state = "INTERNAL_WALL_ATTACK"
            if mid is not None and mid > wall0:
                new_state = "INTERNAL_WALL_OVERRUN"
        if zone_primary == "AT_UPPER_EDGE":
            new_state = "UPPER_EDGE_ATTACK"
        if upper_crossed:
            new_state = "UPPER_EDGE_CROSSED"
            # acceptance pending/accepted handled in variants; keep crossed here
            new_state = "BREAKOUT_ACCEPTANCE_PENDING"

        if new_state != prev_state:
            state_rows.append(
                {
                    "ts": _iso(_dt_ms(s)),
                    "second_ms": s,
                    "from_state": prev_state,
                    "to_state": new_state,
                    "mid": mid,
                    "zone": zone_primary,
                    "buy_notional_5s": f5["buy_notional"],
                    "sell_notional_5s": f5["sell_notional"],
                    "mid_change_5s_bps": f5["mid_change_bps"],
                }
            )
            prev_state = new_state
        state = new_state

        # attack episode coalescing near lower edge / wall0 / upper edge
        for loc, ref, near in (
            ("LOWER_EDGE", lo, abs(bps(mid, lo)) <= PRIMARY_EDGE_TOL_BPS if mid else False),
            ("START_WALL", wall0, abs(bps(mid, wall0)) <= 3.0 if mid else False),
            ("UPPER_EDGE", hi, abs(bps(mid, hi)) <= PRIMARY_EDGE_TOL_BPS if mid else False),
        ):
            hot = near and (buy_n + sell_n) >= min_n * 0.3
            key = loc
            if hot and key not in open_attacks:
                open_attacks[key] = {
                    "attack_id": f"{loc}_{_iso(_dt_ms(s))}",
                    "attack_start_ts": _iso(_dt_ms(s)),
                    "attack_end_ts": _iso(_dt_ms(s)),
                    "location_type": loc,
                    "reference_price": ref,
                    "entering_side": "Buy" if buy_n >= sell_n else "Sell",
                    "dominant_aggressor": "Buy" if buy_n >= sell_n else "Sell",
                    "buy_notional": buy_n,
                    "sell_notional": sell_n,
                    "price_before": mid,
                    "start_ms": s,
                }
            elif hot and key in open_attacks:
                a = open_attacks[key]
                a["attack_end_ts"] = _iso(_dt_ms(s))
                a["buy_notional"] += buy_n
                a["sell_notional"] += sell_n
                a["dominant_aggressor"] = (
                    "Buy" if a["buy_notional"] >= a["sell_notional"] else "Sell"
                )
            elif not hot and key in open_attacks:
                a = open_attacks.pop(key)
                _finalize_attack(a, mid_at, end_ms, lo, hi, wall_hist, wall0, tick, attack_episodes)

        timeline.append(
            {
                "second": _iso(_dt_ms(s)),
                "second_ms": s,
                "coverage": "COMPLETE" if row.genuine and mid is not None else "SOURCE_GAP",
                "mid": mid,
                "best_bid": row.bb,
                "best_ask": row.ba,
                "pool_zone": zone_primary,
                **zones,
                "distance_to_lower_edge_bps": dist_lo,
                "distance_to_upper_edge_bps": dist_hi,
                "penetration_into_pool_bps": pen,
                "penetration_fraction": pen_frac,
                "seconds_inside_pool": seconds_inside,
                "seconds_since_arrival": max(0, (s - arrival_ms) // 1000),
                "local_exit_below": local_exit,
                "reentered_pool": reentered,
                "upper_edge_crossed": upper_crossed,
                "buy_notional_1s": buy_n,
                "sell_notional_1s": sell_n,
                "flow_5s_buy": f5["buy_notional"],
                "flow_5s_sell": f5["sell_notional"],
                "flow_5s_mid_change_bps": f5["mid_change_bps"],
                "flow_5s_buy_impact_per_100k": f5["buy_impact_bps_per_100k"],
                "flow_5s_sell_impact_per_100k": f5["sell_impact_bps_per_100k"],
                "diag_label_5s": f5["diag_label"],
                "existing_aef_label_5s": f5["existing_aef_label"],
                "ema9": ema9,
                "ema20": ema20,
                "ema50": ema50,
                "ema200": ema200,
                "ema20_below_price": (ema20 is not None and mid is not None and ema20 < mid),
                "state": state,
                "start_wall_visible": any(abs(normalize_tick_price(p, tick) - wall0) < 1e-9 for p, _ in row.asks),
                "n_ask_walls_inside_pool": len(inside_walls),
                "warmup_buy_q80_desc_only": warmup_buy_q80,
                "warmup_sell_q80_desc_only": warmup_sell_q80,
            }
        )

    # close open attacks
    for key, a in list(open_attacks.items()):
        _finalize_attack(a, mid_at, end_ms, lo, hi, wall_hist, wall0, tick, attack_episodes)

    # wall lifecycle table
    wall_rows = []
    # Precompute seconds mid above each candidate wall only for start wall + top majors
    focus_walls = [
        px
        for px, h in wall_hist.items()
        if abs(px - wall0) < 1e-9 or (h.get("max_rank") is not None and h["max_rank"] <= 5)
    ]
    above_runs = {px: 0 for px in focus_walls}
    acc_flags = {px: False for px in focus_walls}
    for s in sorted(mid_by):
        m = mid_by[s]
        for px in focus_walls:
            if m > px:
                above_runs[px] += 1
                if above_runs[px] >= 5:
                    acc_flags[px] = True
            else:
                above_runs[px] = 0

    for px, h in sorted(wall_hist.items()):
        h["acceptance_above_wall"] = acc_flags.get(px, False)
        first_ms = _ms(h["first_seen_ts"]) if h["first_seen_ts"] else None
        fs = classify_first_seen(
            first_seen_ts_ms=first_ms,
            arrival_ts_ms=arrival_ms,
            present_in_pre=h["present_pre"],
            present_at_exact_arrival=h["present_exact"],
            present_strictly_after=(
                first_ms is not None and first_ms > arrival_ms and not h["present_pre"] and not h["present_exact"]
            ),
        )
        wall_rows.append(
            {
                "price": px,
                "first_seen_ts": h["first_seen_ts"],
                "last_seen_ts": h["last_seen_ts"],
                "full_side_rank_best": h["max_rank"],
                "max_notional": h["max_notional"],
                "first_seen_class": fs.value,
                "is_start_wall": abs(px - wall0) < 1e-9,
                "attacked": h["attacked"],
                "consumed_by_trades": h["consumed_by_trades"],
                "refilled": h["refilled"],
                "cancelled_or_moved": h["cancelled_or_moved"],
                "price_traded_above": h["price_traded_above"],
                "acceptance_above_wall": h["acceptance_above_wall"],
                "visible_only_not_attacked": (not h["attacked"]) and h["first_seen_ts"] is not None,
            }
        )

    # acceptance variants
    accept_rows = []
    final_states = {}
    for hold_s in ACCEPT_VARIANTS_S:
        first_cross = first_upper_cross_ms
        acc_ts = None
        max_reclaim = None
        reentry_after = False
        if first_cross is not None:
            # need hold_s consecutive seconds with mid > hi
            run = 0
            start_run = None
            for s in range(first_cross, end_ms + 1000, 1000):
                m = mid_by.get(s)
                if m is not None and m > hi:
                    if run == 0:
                        start_run = s
                    run += 1
                    if run >= hold_s:
                        acc_ts = start_run + (hold_s - 1) * 1000
                        break
                else:
                    run = 0
                    start_run = None
            if acc_ts is not None:
                # max reclaim after acceptance: how far back toward/into pool
                after = [mid_by[s] for s in range(acc_ts, end_ms + 1000, 1000) if s in mid_by]
                if after:
                    min_after = min(after)
                    max_reclaim = bps(min_after, hi)
                    reentry_after = min_after <= hi
        diag = "BREAKOUT_ACCEPTED" if acc_ts is not None else (
            "BREAKOUT_ACCEPTANCE_PENDING" if first_cross is not None else (
                "REJECTION_CONFIRMED"
                if local_exit and not upper_crossed and any(
                    r["to_state"] == "LOCAL_EXIT_BELOW" for r in state_rows
                )
                else "UNRESOLVED_TIMEOUT"
            )
        )
        final_states[hold_s] = diag
        accept_rows.append(
            {
                "acceptance_hold_s": hold_s,
                "first_cross_ts": _iso(_dt_ms(first_cross)) if first_cross else None,
                "acceptance_first_available_ts": _iso(_dt_ms(acc_ts)) if acc_ts else None,
                "max_reclaim_bps_vs_upper": max_reclaim,
                "reentry_into_pool_after_accept": reentry_after,
                "diagnostic_state": diag,
            }
        )

    # Lower-edge reset analysis + long candidates
    resets = _lower_edge_resets(timeline, trades, lo, arrival_ms, end_ms, min_n, strong_bps, mid_at, emas)
    long_cands = _long_candidates(
        timeline,
        accept_rows,
        wall_rows,
        wall0,
        lo,
        hi,
        arrival_ms,
        min_n,
        strong_bps,
        first_upper_cross_ms,
    )

    # Prefix parity checks
    prefix_rows, causality_fail = _prefix_checks(
        timeline,
        long_cands,
        accept_rows,
        resets,
        first_upper_cross_ms,
    )

    # Manual review bullets
    manual = _manual_md(
        timeline,
        resets,
        wall_rows,
        accept_rows,
        long_cands,
        attack_episodes,
        first_upper_cross_ms,
    )

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    gaps = sum(1 for r in timeline if r["coverage"] != "COMPLETE")
    last_mid_ts = next((r["second"] for r in reversed(timeline) if r["mid"] is not None), None)

    manifest = {
        "format_version": FORMAT_VERSION,
        "case_id": "CASE_02",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "side": SIDE,
        "approach": APPROACH,
        "market_arrival_cluster_id": CLUSTER_ID,
        "arrival_ts": ARRIVAL_TS,
        "load_start_ts": LOAD_START_TS,
        "max_end_ts": MAX_END_TS,
        "pool": [POOL_LO, POOL_HI],
        "start_wall": START_WALL,
        "OUTCOME_USED_FOR_THRESHOLDS": OUTCOME_USED_FOR_THRESHOLDS,
        "OUTCOME_USED_FOR_STATE_DEFINITION": OUTCOME_USED_FOR_STATE_DEFINITION,
        "OUTCOME_USED_FOR_EVENT_SELECTION": OUTCOME_USED_FOR_EVENT_SELECTION,
        "edge_tol_bps_reported": list(EDGE_TOL_BPS),
        "primary_edge_tol_bps_reporting_only": PRIMARY_EDGE_TOL_BPS,
        "flow_windows_s": list(FLOW_WINDOWS_S),
        "acceptance_variants_s": list(ACCEPT_VARIANTS_S),
        "aef_thresholds_frozen": {
            "min_notional_usdt": min_n,
            "strong_same_side_impact_bps": strong_bps,
            "source": "UNFITTED_F0_DIAGNOSTIC",
        },
        "queries": {
            "public_trades_select": 1,
            "table": CANONICAL_TRADES_TABLE,
            "raw_ob_reconstruction": 1,
            "chart_candle_ema_pack": 1,
        },
        "coverage": {
            "trades": len(trades),
            "trade_preflight": trade_pre,
            "ob_seconds_emitted": len(ob_rows),
            "timeline_seconds": len(timeline),
            "gaps": gaps,
        },
        "elapsed_s": elapsed,
        "causality_failure": causality_fail,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "case_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    write_csv(out_dir / "second_timeline.csv", timeline)
    write_csv(out_dir / "edge_attack_episodes.csv", attack_episodes)
    write_csv(out_dir / "wall_lifecycle_inside_pool.csv", wall_rows)
    write_csv(out_dir / "state_transitions.csv", state_rows)
    write_csv(out_dir / "acceptance_variants.csv", accept_rows)
    write_csv(out_dir / "long_candidate_timestamps.csv", long_cands)
    write_csv(out_dir / "prefix_parity.csv", prefix_rows)
    (out_dir / "MANUAL_CASE_02_REVIEW.md").write_text(manual, encoding="utf-8")

    # Lower edge summary for report
    strongest_sell = max(resets, key=lambda r: r.get("sell_notional") or 0, default=None)
    report = _report_md(
        manifest,
        timeline,
        resets,
        strongest_sell,
        wall_rows,
        accept_rows,
        long_cands,
        prefix_rows,
        causality_fail,
        final_states,
        last_mid_ts,
    )
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")
    return {
        "manifest": manifest,
        "causality_failure": causality_fail,
        "n_timeline": len(timeline),
        "long_cands": long_cands,
        "accept_rows": accept_rows,
    }


def _finalize_attack(a, mid_at, end_ms, lo, hi, wall_hist, wall0, tick, sink):
    t0 = _ms(a["attack_start_ts"])
    impacts = {}
    prices = {}
    for w in (1, 3, 5, 10):
        t1 = t0 + w * 1000
        if t1 > end_ms:
            impacts[w] = None
            prices[w] = None
            continue
        m0 = a.get("price_before") or mid_at(t0)
        m1 = mid_at(t1 - 1)
        prices[w] = m1
        impacts[w] = bps(m1, m0) if m0 and m1 else None
    # wall stats at start
    ref = float(a["reference_price"])
    wh = None
    if a["location_type"] == "START_WALL":
        wh = wall_hist.get(wall0)
    elif a["location_type"] in ("LOWER_EDGE", "UPPER_EDGE"):
        # nearest wall to ref
        if wall_hist:
            nearest = min(wall_hist.keys(), key=lambda p: abs(p - ref))
            if abs(nearest - ref) / ref * 10000 <= 5:
                wh = wall_hist[nearest]
    returned = False
    progressed = False
    crossed = False
    for w, m in prices.items():
        if m is None:
            continue
        if m < lo:
            returned = True
        if m > lo + (hi - lo) * 0.15:
            progressed = True
        if m > hi:
            crossed = True
    sink.append(
        {
            **{k: v for k, v in a.items() if k != "start_ms"},
            "price_after_1s": prices.get(1),
            "price_after_3s": prices.get(3),
            "price_after_5s": prices.get(5),
            "price_after_10s": prices.get(10),
            "impact_1s_bps": impacts.get(1),
            "impact_3s_bps": impacts.get(3),
            "impact_5s_bps": impacts.get(5),
            "impact_10s_bps": impacts.get(10),
            "returned_below_lower_edge": returned,
            "progressed_deeper_into_pool": progressed,
            "crossed_upper_edge": crossed,
            "wall_present_at_start": wh is not None,
            "wall_notional_at_start": (wh or {}).get("max_notional"),
            "wall_rank_at_start": (wh or {}).get("max_rank"),
            "wall_visibility": "VISIBLE" if wh else "NONE",
            "refill_share": None,
            "cancel_or_move_share": None,
            "trade_depletion_share": None,
            "note": "shares_not_estimated_without_queue; see wall_lifecycle flags",
        }
    )


def _lower_edge_resets(timeline, trades, lo, arrival_ms, end_ms, min_n, strong_bps, mid_at, emas):
    resets = []
    in_reset = False
    cur = None
    for r in timeline:
        if r["second_ms"] < arrival_ms:
            continue
        z = r.get("pool_zone")
        at_edge = z in ("AT_LOWER_EDGE", "BELOW_POOL", "INSIDE_LOWER_THIRD")
        if at_edge and not in_reset:
            in_reset = True
            cur = {
                "reset_start_ts": r["second"],
                "start_ms": r["second_ms"],
                "sell_notional": 0.0,
                "buy_notional": 0.0,
                "max_down_bps": 0.0,
                "closed_below": False,
                "seconds_below": 0,
                "reentered": False,
            }
        if in_reset and cur is not None:
            cur["sell_notional"] += float(r.get("sell_notional_1s") or 0)
            cur["buy_notional"] += float(r.get("buy_notional_1s") or 0)
            mid = r.get("mid")
            if mid is not None:
                cur["max_down_bps"] = min(cur["max_down_bps"], bps(mid, lo))
                if mid < lo:
                    cur["closed_below"] = True
                    cur["seconds_below"] += 1
            if z and z.startswith("INSIDE") and cur["closed_below"]:
                cur["reentered"] = True
            # end reset when deep inside or above
            if z in ("INSIDE_MIDDLE_THIRD", "INSIDE_UPPER_THIRD", "AT_UPPER_EDGE", "ABOVE_POOL"):
                in_reset = False
                cur["reset_end_ts"] = r["second"]
                m0 = mid_at(cur["start_ms"])
                m5 = mid_at(cur["start_ms"] + 5000)
                chg = bps(m5, m0) if m0 and m5 else None
                if cur["sell_notional"] < min_n:
                    sell_lab = "NO_MEANINGFUL_SELL_ATTACK"
                else:
                    sell_lab = impact_label(
                        buy_n=cur["buy_notional"],
                        sell_n=cur["sell_notional"],
                        mid_chg_bps=chg,
                        min_notional=min_n,
                        strong_bps=strong_bps,
                    )
                ema20 = ema_as_of(emas.get("ema20", []), r["second_ms"])
                cur.update(
                    {
                        "sell_label": sell_lab,
                        "impact_5s_bps": chg,
                        "ema20_at_end": ema20,
                        "ema20_below_price": ema20 is not None and mid is not None and ema20 < mid,
                        "ema20_broken": ema20 is not None and mid is not None and mid < ema20,
                        "buy_took_over": cur["buy_notional"] > cur["sell_notional"],
                        "buy_efficient": chg is not None and chg >= strong_bps and cur["buy_notional"] >= min_n,
                    }
                )
                resets.append(cur)
                cur = None
    if in_reset and cur is not None:
        cur["reset_end_ts"] = timeline[-1]["second"]
        if cur["sell_notional"] < min_n:
            cur["sell_label"] = "NO_MEANINGFUL_SELL_ATTACK"
        else:
            cur["sell_label"] = "UNCLOSED_RESET"
        resets.append(cur)
    return resets


def _long_candidates(timeline, accept_rows, wall_rows, wall0, lo, hi, arrival_ms, min_n, strong_bps, first_upper):
    rows = []
    # A: lower-edge hold
    a_ts = None
    a_reasons = []
    a_missing = []
    for r in timeline:
        if r["second_ms"] < arrival_ms:
            continue
        if r.get("pool_zone") in ("BELOW_POOL", None):
            continue
        sell_ok = (r.get("flow_5s_sell") or 0) >= min_n
        ineff = "STRONG_SELL_INEFFICIENT" in str(r.get("diag_label_5s") or "")
        no_stable_break = not (
            r.get("local_exit_below") and (r.get("seconds_since_arrival") or 0) > 30 and r.get("pool_zone") == "BELOW_POOL"
        )
        buy_take = (r.get("flow_5s_buy") or 0) >= min_n and (r.get("flow_5s_mid_change_bps") or -999) > 0
        if sell_ok and ineff and no_stable_break and buy_take:
            a_ts = r["second"]
            a_reasons = [
                "in_or_above_pool",
                "meaningful_sell_at_lower_edge",
                "sell_impact_inefficient",
                "no_stable_break_below",
                "buy_takeover",
            ]
            break
    if a_ts is None:
        a_missing = ["concurrent_sell_inefficient_plus_buy_takeover_not_observed"]
    rows.append(
        {
            "candidate": "LONG_CANDIDATE_A",
            "eligible": a_ts is not None,
            "first_available_ts": a_ts,
            "reference_price": lo,
            "reasons": "|".join(a_reasons),
            "missing": "|".join(a_missing),
            "outcome_not_used_for_eligibility": True,
        }
    )

    # B: pressure inside
    b_ts = None
    b_reasons = []
    overrun = any(w.get("is_start_wall") and w.get("price_traded_above") for w in wall_rows)
    for r in timeline:
        if r["second_ms"] < arrival_ms:
            continue
        if not (r.get("pool_zone") or "").startswith("INSIDE") and r.get("pool_zone") not in (
            "AT_LOWER_EDGE",
            "AT_UPPER_EDGE",
        ):
            continue
        ema_hold = r.get("ema20_below_price") is True
        buy_prog = (r.get("flow_5s_buy") or 0) >= min_n and (r.get("flow_5s_mid_change_bps") or -999) >= strong_bps * 0.5
        if ema_hold and buy_prog and (overrun or r.get("state") in ("INTERNAL_WALL_OVERRUN", "BUY_PRESSURE_INSIDE")):
            b_ts = r["second"]
            b_reasons = ["stays_in_pool", "ema20_hold_price_above", "buy_progress", "internal_wall_overrun_or_pressure"]
            break
    rows.append(
        {
            "candidate": "LONG_CANDIDATE_B",
            "eligible": b_ts is not None,
            "first_available_ts": b_ts,
            "reference_price": wall0,
            "reasons": "|".join(b_reasons) if b_reasons else "",
            "missing": "" if b_ts else "pressure_inside_conditions_not_jointly_met",
            "outcome_not_used_for_eligibility": True,
        }
    )

    # C: confirmed breakout — use acceptance variants separately (report first 5s as primary flag)
    c_acc = next((a for a in accept_rows if a["acceptance_hold_s"] == 5), None)
    c_ts = c_acc["acceptance_first_available_ts"] if c_acc else None
    rows.append(
        {
            "candidate": "LONG_CANDIDATE_C",
            "eligible": c_ts is not None,
            "first_available_ts": c_ts,
            "reference_price": hi,
            "reasons": "upper_crossed_and_5s_acceptance" if c_ts else "",
            "missing": "" if c_ts else "5s_acceptance_not_reached_in_window",
            "outcome_not_used_for_eligibility": True,
            "note": "15/30/60s variants in acceptance_variants.csv — not collapsed to winner",
        }
    )
    return rows


def _prefix_checks(timeline, long_cands, accept_rows, resets, first_upper_cross_ms):
    rows = []
    fail = False

    def check(name, as_of_ts, full_value, compute_prefix):
        nonlocal fail
        as_of_ms = _ms(as_of_ts) if as_of_ts else None
        if as_of_ms is None:
            rows.append(
                {
                    "checkpoint": name,
                    "as_of_ts": None,
                    "prefix_parity": "SKIPPED_NO_TS",
                    "full_value": full_value,
                    "prefix_value": None,
                }
            )
            return
        pref = compute_prefix(as_of_ms)
        ok = pref == full_value
        if not ok:
            fail = True
        rows.append(
            {
                "checkpoint": name,
                "as_of_ts": as_of_ts,
                "prefix_parity": "EXACT_PREFIX_PARITY" if ok else "CAUSALITY_FAILURE",
                "full_value": full_value,
                "prefix_value": pref,
            }
        )

    # first lower edge test
    first_let = next((r for r in timeline if r.get("state") == "LOWER_EDGE_TEST" or r.get("pool_zone") == "AT_LOWER_EDGE"), None)
    if first_let:
        check(
            "first_lower_edge_test",
            first_let["second"],
            "AT_LOWER_EDGE_OR_TEST",
            lambda ms: "AT_LOWER_EDGE_OR_TEST"
            if any(
                x["second_ms"] <= ms
                and (x.get("pool_zone") == "AT_LOWER_EDGE" or x.get("state") == "LOWER_EDGE_TEST")
                for x in timeline
            )
            else "ABSENT",
        )

    strongest = max(resets, key=lambda r: r.get("sell_notional") or 0, default=None)
    if strongest:
        check(
            "strongest_sell_reset_label",
            strongest.get("reset_end_ts") or strongest.get("reset_start_ts"),
            strongest.get("sell_label"),
            lambda ms: next(
                (
                    r.get("sell_label")
                    for r in resets
                    if _ms(r.get("reset_end_ts") or r.get("reset_start_ts")) <= ms
                    and r.get("start_ms") == strongest.get("start_ms")
                ),
                None,
            ),
        )

    for cand in long_cands:
        check(
            cand["candidate"],
            cand.get("first_available_ts"),
            bool(cand.get("eligible")),
            lambda ms, c=cand: bool(c.get("first_available_ts")) and _ms(c["first_available_ts"]) <= ms,
        )

    if first_upper_cross_ms:
        check(
            "upper_edge_cross",
            _iso(_dt_ms(first_upper_cross_ms)),
            True,
            lambda ms: any(r["second_ms"] <= ms and r.get("upper_edge_crossed") for r in timeline),
        )

    for a in accept_rows:
        check(
            f"acceptance_{a['acceptance_hold_s']}s",
            a.get("acceptance_first_available_ts"),
            a.get("acceptance_first_available_ts") is not None,
            lambda ms, aa=a: aa.get("acceptance_first_available_ts") is not None
            and _ms(aa["acceptance_first_available_ts"]) <= ms,
        )

    return rows, fail


def _manual_md(timeline, resets, wall_rows, accept_rows, long_cands, attacks, first_upper):
    lines = [
        "# MANUAL_CASE_02_REVIEW",
        "",
        "Chart: Liquidity Location ON, Orderbook Walls ON, Trade Bubbles optional. UTC.",
        "",
        f"- Arrival: `{ARRIVAL_TS}` mid≈{_find_mid(timeline, ARRIVAL_TS)} pool `[{POOL_LO}, {POOL_HI}]` start-wall `{START_WALL}`",
        "",
        "## Lower-edge resets",
    ]
    for r in resets:
        lines.append(
            f"- `{r.get('reset_start_ts')}` → `{r.get('reset_end_ts')}` sell≈{r.get('sell_notional'):.0f} "
            f"label=`{r.get('sell_label')}` below={r.get('closed_below')} reenter={r.get('reentered')} "
            f"impact5s={r.get('impact_5s_bps')}"
        )
    if not resets:
        lines.append("- (none detected)")
    strongest_sell = max(resets, key=lambda r: r.get("sell_notional") or 0, default=None)
    strongest_buy = max(timeline, key=lambda r: r.get("buy_notional_1s") or 0)
    lines += [
        "",
        f"## Strongest sell attack: `{strongest_sell}`" if strongest_sell else "## Strongest sell attack: none",
        f"## Strongest buy second: `{strongest_buy.get('second')}` buy≈{strongest_buy.get('buy_notional_1s')} mid={strongest_buy.get('mid')} zone={strongest_buy.get('pool_zone')}",
        "",
        "## EMA20 tests (price vs last closed EMA20)",
    ]
    ema_tests = [r for r in timeline if r.get("ema20") is not None and r.get("mid") is not None and abs(bps(r["mid"], r["ema20"])) <= 3]
    for r in ema_tests[:: max(1, len(ema_tests)//10)][:12]:
        lines.append(
            f"- `{r['second']}` mid={r['mid']} ema20={r['ema20']} broken={r.get('mid') < r.get('ema20')} buy1s={r.get('buy_notional_1s')} sell1s={r.get('sell_notional_1s')}"
        )
    lines += ["", "## Internal wall attacks (start wall + MAJOR inside)"]
    for w in wall_rows:
        if w.get("is_start_wall") or (w.get("full_side_rank_best") or 99) <= 5:
            lines.append(
                f"- px={w['price']} first={w['first_seen_ts']} class={w['first_seen_class']} attacked={w['attacked']} "
                f"consumed={w['consumed_by_trades']} cancel/move={w['cancelled_or_moved']} above={w['price_traded_above']}"
            )
    lines += [
        "",
        f"## Upper-edge cross: `{_iso(_dt_ms(first_upper)) if first_upper else None}`",
        "",
        "## Acceptance variants",
    ]
    for a in accept_rows:
        lines.append(
            f"- {a['acceptance_hold_s']}s: cross=`{a['first_cross_ts']}` accept=`{a['acceptance_first_available_ts']}` state=`{a['diagnostic_state']}`"
        )
    lines += ["", "## LONG candidates"]
    for c in long_cands:
        lines.append(
            f"- {c['candidate']}: eligible={c['eligible']} first=`{c['first_available_ts']}` reasons=`{c['reasons']}` missing=`{c['missing']}`"
        )
    lines += ["", "## Attack episodes (sample)"]
    for a in attacks[:15]:
        lines.append(
            f"- {a.get('attack_id')} {a.get('location_type')} {a.get('attack_start_ts')} buy={a.get('buy_notional'):.0f} sell={a.get('sell_notional'):.0f} impact5={a.get('impact_5s_bps')}"
        )
    return "\n".join(lines) + "\n"


def _find_mid(timeline, ts):
    for r in timeline:
        if r["second"] == ts:
            return r.get("mid")
    return None


def _report_md(manifest, timeline, resets, strongest_sell, wall_rows, accept_rows, long_cands, prefix_rows, causality_fail, final_states, last_mid_ts):
    arrival_i = next(i for i, r in enumerate(timeline) if r["second"] == ARRIVAL_TS)
    # duration to first BREAKOUT_ACCEPTED 5s or end
    acc5 = next(a for a in accept_rows if a["acceptance_hold_s"] == 5)
    end_ts = acc5.get("acceptance_first_available_ts") or last_mid_ts or MAX_END_TS
    dur_s = (_ms(end_ts) - _ms(ARRIVAL_TS)) // 1000
    pref_ok = all(r.get("prefix_parity") in ("EXACT_PREFIX_PARITY", "SKIPPED_NO_TS") for r in prefix_rows)
    return f"""# REPORT — CASE_02_POOL_EDGE_AGGRESSOR_EFFICIENCY_TIMELINE_V1

## 1. Verdict
{'CAUSALITY_FAILURE' if causality_fail else 'CASE_02_POOL_EDGE_AGGRESSOR_EFFICIENCY_TIMELINE_V1_COMPLETE'}

## 2. Live safety / HEAD
Read-only. No commit/push. Event selection only (CASE_02). Thresholds not outcome-fitted.

## 3. Runtime / queries
elapsed_s={manifest['elapsed_s']:.1f}; trades_select=1; raw_ob=1; ema_pack=1

## 4. Coverage
trades={manifest['coverage']['trades']}; ob_seconds={manifest['coverage']['ob_seconds_emitted']}; timeline={manifest['coverage']['timeline_seconds']}; gaps={manifest['coverage']['gaps']}

## 5. Duration arrival → diagnostic end
{dur_s}s (to 5s-acceptance or last mid / max end)

## 6–9. Lower edge / aggressors
resets={len(resets)}
strongest_sell={strongest_sell}
(See second_timeline + edge_attack_episodes for Buy/Sell impact signs.)

## 10. EMA20
Uses last **closed** 5m EMA only (`avail = candle_open + 5m`). Visual EMA20 tests listed in MANUAL review; causal break only if mid < closed EMA20.

## 11. Internal walls
start_wall_rows={sum(1 for w in wall_rows if w.get('is_start_wall'))}; major_inside={sum(1 for w in wall_rows if (w.get('full_side_rank_best') or 99)<=5)}

## 12–14. Re-entry / upper / acceptance
local_exit observed in timeline flags; acceptance variants: {accept_rows}

## 15. LONG candidates
{long_cands}

## 16. Prefix
all_ok={pref_ok}; causality_failure={causality_fail}

## 17. Interpretation
Diagnostic reconstruction only. No trading edge, PnL, or parameter optimization.

## 18. Manual path
`results/case_02_pool_edge_aggressor_efficiency_timeline_v1/MANUAL_CASE_02_REVIEW.md`
"""

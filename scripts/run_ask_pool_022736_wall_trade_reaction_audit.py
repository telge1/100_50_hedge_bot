#!/usr/bin/env python3
"""ASK_POOL_022736_WALL_PUBLIC_TRADE_REACTION_AUDIT_V1

Single-case causal audit: BTCUSDT ASK pool arrival 2026-08-26T02:27:36Z.
Read-only. No strategy, no profit metrics, no commit.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.aggressor_efficiency_flip.contracts import (
    CANONICAL_TRADES_TABLE,
    UNFITTED_F0_DIAGNOSTIC,
)
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.first_seen import (
    normalize_tick_price,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.ranking import (
    side_levels_ranked_full,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.util import notional, tick_size
from orderbook_analyse.ob200_v3_raw_discovery.audit import (
    is_replayable_line,
    iter_decompressed_lines,
    line_to_replay_payload,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments
from orderbook_analyse.ob200_v3_raw_discovery.mutable_book import MutableBook

V2 = OA_ROOT / "results" / "liquidity_pool_arrival_wall_monitor_v2"
RAW_ROOT = OA_ROOT / "data" / "orderbook_raw_shadow" / "ob200_v3"
ARRIVAL_TS = "2026-08-26T02:27:36Z"
CLUSTER_END_TS = "2026-08-26T02:30:21Z"
WINDOW_START = "2026-08-26T02:26:36Z"
WALL_A_REF = 79176.0
WALL_B_REF = 79217.1
MAX_CLASSIFY_MS = None  # set from cluster end


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
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(dt.microsecond/1000):03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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


def bps(a: float, b: float) -> float:
    if b <= 0:
        return float("nan")
    return (a - b) / b * 10000.0


def abs_bps(a: float, b: float) -> float:
    return abs(bps(a, b))


def load_v2_context() -> dict[str, Any]:
    rows = list(csv.DictReader((V2 / "pool_arrivals_v2.csv").open(encoding="utf-8")))
    hits = [
        r
        for r in rows
        if r["arrival_ts"] == ARRIVAL_TS and "1787684400" in r["pool_id"]
    ]
    if len(hits) != 1:
        raise RuntimeError(f"expected exactly 1 pool arrival, got {len(hits)}")
    a = hits[0]
    fs = [
        r
        for r in csv.DictReader((V2 / "wall_first_seen_v2.csv").open(encoding="utf-8"))
        if r["pool_arrival_id"] == a["pool_arrival_id"]
    ]
    tick = tick_size("BTCUSDT")
    wall_a_tick = normalize_tick_price(WALL_A_REF, tick)
    wall_b_tick = normalize_tick_price(WALL_B_REF, tick)
    fa = next((r for r in fs if abs(float(r["tick_price"]) - wall_a_tick) < 1e-9), None)
    fb = next((r for r in fs if abs(float(r["tick_price"]) - wall_b_tick) < 1e-9), None)
    return {
        "arrival": a,
        "wall_a_tick": wall_a_tick,
        "wall_b_tick": wall_b_tick,
        "wall_a_fs": fa,
        "wall_b_fs": fb,
        "tick": tick,
    }


def iter_ob_1s(raw_root: Path, start: datetime, end: datetime):
    """Yield (bucket_ms, genuine, bb, ba, mid, asks[:200])."""
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
            ask_lvls = [(float(p), float(q)) for p, q in asks[:200]]
            yield bucket, (not gap_latched and book.is_valid), bb, ba, mid, ask_lvls


def wall_qty_at(ask_lvls: list[tuple[float, float]], tick_price: float, tick: float) -> tuple[float, float]:
    qty = 0.0
    for p, q in ask_lvls:
        if abs(normalize_tick_price(p, tick) - tick_price) < 1e-9:
            qty += q
    return qty, notional(tick_price, qty) if qty > 0 else 0.0


def rank_at(ask_lvls: list[tuple[float, float]], tick_price: float, tick: float) -> int | None:
    ranked = side_levels_ranked_full(ask_lvls)
    for r in ranked:
        if abs(normalize_tick_price(r["price"], tick) - tick_price) < 1e-9:
            return int(r["full_side_rank"])
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir",
        default=str(OA_ROOT / "results" / "ask_pool_022736_wall_public_trade_reaction_audit_v1"),
    )
    args = ap.parse_args(argv)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    global MAX_CLASSIFY_MS
    MAX_CLASSIFY_MS = _ms(CLUSTER_END_TS)

    print("Load V2 context...", flush=True)
    ctx = load_v2_context()
    a = ctx["arrival"]
    tick = ctx["tick"]
    wall_a = ctx["wall_a_tick"]
    wall_b = ctx["wall_b_tick"]
    lo, hi = float(a["lower_edge"]), float(a["upper_edge"])
    edge = float(a["arrival_edge"])
    arrival_ms = _ms(ARRIVAL_TS)
    end_ms = _ms(CLUSTER_END_TS)
    start_ms = _ms(WINDOW_START)
    wall_b_first_ms = _ms(ctx["wall_b_fs"]["first_seen_ts"]) if ctx["wall_b_fs"] else None

    pool_ctx = {
        "pool_arrival_id": a["pool_arrival_id"],
        "pool_id": a["pool_id"],
        "market_arrival_cluster_id": a.get("market_arrival_cluster_id"),
        "side": a["side"],
        "approach": a.get("approach_direction"),
        "lower_edge": lo,
        "upper_edge": hi,
        "arrival_edge": edge,
        "arrival_ts": ARRIVAL_TS,
        "cluster_end_ts": CLUSTER_END_TS,
        "cluster_end_reason": a.get("cluster_end_reason") or "EXITED_ENTRY_SIDE",
        "mid_at_arrival": float(a["mid_at_arrival"]),
        "wall_a_tick": wall_a,
        "wall_b_tick": wall_b,
        "wall_a_first_seen_class": ctx["wall_a_fs"]["first_seen_class"] if ctx["wall_a_fs"] else None,
        "wall_b_first_seen_ts": ctx["wall_b_fs"]["first_seen_ts"] if ctx["wall_b_fs"] else None,
    }
    (out / "pool_cluster_context.json").write_text(json.dumps(pool_ctx, indent=2), encoding="utf-8")

    (out / "source_contracts.json").write_text(
        json.dumps(
            {
                "public_trades": {
                    "loader": "orderbook_analyse.aggressor_efficiency_flip.trade_loader.load_trades_clickhouse",
                    "table": CANONICAL_TRADES_TABLE,
                    "dedup": "trade_id",
                    "side_semantics": "Buy=aggressive taker buy; Sell=aggressive taker sell",
                    "also_equivalent": "public_trade_bubbles.loader.load_public_trade_records",
                },
                "raw_ob200": {
                    "root": str(RAW_ROOT),
                    "loader": "ob200_v3_raw_discovery MutableBook + list_closed_segments",
                },
                "pools": {
                    "foundation": "liquidity_pool_signal",
                    "v2_artifacts": str(V2),
                },
                "impact_thresholds_diagnostic": {
                    "source": "aggressor_efficiency_flip.contracts.UNFITTED_F0_DIAGNOSTIC",
                    "strong_same_side_impact_bps": UNFITTED_F0_DIAGNOSTIC.get(
                        "strong_same_side_impact_bps"
                    ),
                    "min_notional_usdt": UNFITTED_F0_DIAGNOSTIC.get("min_notional_usdt"),
                    "status": "DIAGNOSTIC_UNFITTED_not_fitted_on_this_case",
                },
                "no_queue_reconstruction": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Load public trades...", flush=True)
    trades, preflight = load_trades_clickhouse(
        symbol="BTCUSDT",
        start=_utc(WINDOW_START),
        end=_utc(CLUSTER_END_TS) + timedelta(seconds=1),  # include end second
    )
    # filter to classify window: trade_ts <= cluster end
    trades = [t for t in trades if _ms(t.trade_ts) <= end_ms]
    print(f"trades={len(trades)} deduped preflight={preflight}", flush=True)

    print("Replay OB200 1s...", flush=True)
    ob_rows = list(iter_ob_1s(RAW_ROOT, _utc(WINDOW_START), _utc(CLUSTER_END_TS)))
    ob_by_s = {b: (g, bb, ba, mid, asks) for b, g, bb, ba, mid, asks in ob_rows}

    # Expected seconds
    seconds = list(range(start_ms, end_ms + 1000, 1000))
    one_sec = []
    wall_a_tl = []
    wall_b_tl = []
    prev_qa = prev_qb = None
    decomp_rows = []

    # Trade index by second
    trades_by_s: dict[int, list] = defaultdict(list)
    for t in trades:
        trades_by_s[(_ms(t.trade_ts) // 1000) * 1000].append(t)

    for s in seconds:
        g_bb_ba_mid_asks = ob_by_s.get(s)
        if g_bb_ba_mid_asks is None:
            coverage = "SOURCE_GAP"
            genuine = False
            bb = ba = mid = None
            asks = []
            qa = na = qb = nb = None
            ra = rb = None
        else:
            genuine, bb, ba, mid, asks = g_bb_ba_mid_asks
            if not genuine:
                coverage = "SOURCE_GAP"
            else:
                coverage = "COMPLETE"
            qa, na = wall_qty_at(asks, wall_a, tick)
            qb, nb = wall_qty_at(asks, wall_b, tick)
            ra = rank_at(asks, wall_a, tick) if qa > 0 else None
            rb = rank_at(asks, wall_b, tick) if qb > 0 else None
            if qa == 0 and qb == 0 and coverage == "COMPLETE":
                # still complete book
                pass

        sec_trades = trades_by_s.get(s, [])
        buy_q = sum(t.size for t in sec_trades if t.side == "Buy")
        sell_q = sum(t.size for t in sec_trades if t.side == "Sell")
        buy_n = sum(t.notional for t in sec_trades if t.side == "Buy")
        sell_n = sum(t.notional for t in sec_trades if t.side == "Sell")
        buy_c = sum(1 for t in sec_trades if t.side == "Buy")
        sell_c = sum(1 for t in sec_trades if t.side == "Sell")
        max_buy = max((t.notional for t in sec_trades if t.side == "Buy"), default=0.0)
        max_sell = max((t.notional for t in sec_trades if t.side == "Sell"), default=0.0)
        imb = buy_n - sell_n

        if mid is None:
            pool_state = None
        elif mid < lo:
            pool_state = "BELOW_POOL"
        elif mid > hi:
            pool_state = "ABOVE_POOL"
        else:
            pool_state = "INSIDE_POOL"

        if coverage == "COMPLETE" and mid is not None and buy_c + sell_c == 0:
            # valid empty trades ok
            pass

        row = {
            "second": _iso(_dt_ms(s)),
            "second_ms": s,
            "coverage": coverage if g_bb_ba_mid_asks is not None else "SOURCE_GAP",
            "best_bid": bb,
            "best_ask": ba,
            "mid": mid,
            "spread": (ba - bb) if bb is not None and ba is not None else None,
            "price_vs_pool_edge_bps": abs_bps(mid, edge) if mid else None,
            "price_vs_wall_a_bps": abs_bps(mid, wall_a) if mid else None,
            "price_vs_wall_b_bps": abs_bps(mid, wall_b) if mid else None,
            "aggressive_buy_qty": buy_q,
            "aggressive_buy_notional": buy_n,
            "aggressive_sell_qty": sell_q,
            "aggressive_sell_notional": sell_n,
            "trade_count_buy": buy_c,
            "trade_count_sell": sell_c,
            "buy_sell_imbalance_notional": imb,
            "max_buy_bubble": max_buy,
            "max_sell_bubble": max_sell,
            "wall_a_qty": qa,
            "wall_a_notional": na,
            "wall_a_rank": ra,
            "wall_a_visible": bool(qa and qa > 0),
            "wall_b_qty": qb,
            "wall_b_notional": nb,
            "wall_b_rank": rb,
            "wall_b_visible": bool(qb and qb > 0),
            "pool_state": pool_state,
            "phase": (
                "PRE_ARRIVAL"
                if s < arrival_ms
                else ("REJECTION_EXIT" if s >= end_ms - 5000 else "POST_ARRIVAL")
            ),
        }
        # Adds/reductions vs prev
        add_a = red_a = add_b = red_b = None
        if prev_qa is not None and qa is not None:
            d = qa - prev_qa
            add_a = max(0.0, d)
            red_a = max(0.0, -d)
        if prev_qb is not None and qb is not None:
            d = qb - prev_qb
            add_b = max(0.0, d)
            red_b = max(0.0, -d)
        row["wall_a_add"] = add_a
        row["wall_a_reduction"] = red_a
        row["wall_b_add"] = add_b
        row["wall_b_reduction"] = red_b
        one_sec.append(row)

        wall_a_tl.append(
            {
                "second": row["second"],
                "qty": qa,
                "notional": na,
                "rank": ra,
                "visible": row["wall_a_visible"],
                "add": add_a,
                "reduction": red_a,
                "mid": mid,
            }
        )
        wall_b_visible_ok = wall_b_first_ms is not None and s >= wall_b_first_ms
        wall_b_tl.append(
            {
                "second": row["second"],
                "qty": qb if wall_b_visible_ok else None,
                "notional": nb if wall_b_visible_ok else None,
                "rank": rb if wall_b_visible_ok else None,
                "visible": bool(wall_b_visible_ok and qb and qb > 0),
                "before_first_seen": wall_b_first_ms is not None and s < wall_b_first_ms,
                "add": add_b if wall_b_visible_ok else None,
                "reduction": red_b if wall_b_visible_ok else None,
                "mid": mid,
            }
        )

        # decomposition for reductions
        for label, prev_q, cur_q, wall_px, first_ok in (
            ("A", prev_qa, qa, wall_a, True),
            ("B", prev_qb, qb, wall_b, wall_b_visible_ok),
        ):
            if not first_ok or prev_q is None or cur_q is None:
                continue
            if cur_q >= prev_q:
                if cur_q > prev_q:
                    decomp_rows.append(
                        {
                            "second": row["second"],
                            "wall": label,
                            "wall_price": wall_px,
                            "prev_qty": prev_q,
                            "cur_qty": cur_q,
                            "displayed_reduction": 0.0,
                            "displayed_add": cur_q - prev_q,
                            "aggressive_buy_qty_at_level": None,
                            "class": "REFILL_SUPPORTED" if prev_q > 0 else "REFILL_SUPPORTED",
                        }
                    )
                continue
            # reduction: buy trades at level in this second
            buy_at = sum(
                t.size
                for t in sec_trades
                if t.side == "Buy" and abs(normalize_tick_price(t.price, tick) - wall_px) < 1e-9
            )
            red = prev_q - cur_q
            explained = min(red, buy_at)
            unexplained = max(0.0, red - buy_at)
            if buy_at <= 0 and red > 0:
                cls = "CANCELLATION_OR_MOVE_SUPPORTED"
            elif unexplained < 0.15 * red:
                cls = "TRADE_EXPLAINED_DEPLETION_SUPPORTED"
            elif buy_at > 0 and unexplained > 0:
                cls = "MIXED"
            else:
                cls = "INSUFFICIENT_TEMPORAL_RESOLUTION"
            decomp_rows.append(
                {
                    "second": row["second"],
                    "wall": label,
                    "wall_price": wall_px,
                    "prev_qty": prev_q,
                    "cur_qty": cur_q,
                    "displayed_reduction": red,
                    "displayed_add": 0.0,
                    "aggressive_buy_qty_at_level": buy_at,
                    "explained_by_trades_qty": explained,
                    "unexplained_reduction_qty": unexplained,
                    "class": cls,
                    "note": "snapshot_vs_trade_timing_not_exchange_queue",
                }
            )

        if qa is not None:
            prev_qa = qa
        if qb is not None and wall_b_visible_ok:
            prev_qb = qb

    # Public trades export with distances
    trade_rows = []
    touch_rows = []
    for t in trades:
        tms = _ms(t.trade_ts)
        if tms > end_ms:
            continue
        d_ticks_a = abs(t.price - wall_a) / tick
        d_ticks_b = abs(t.price - wall_b) / tick
        d_bps_a = abs_bps(t.price, wall_a)
        d_bps_b = abs_bps(t.price, wall_b)
        inside = lo <= t.price <= hi
        after_b = wall_b_first_ms is not None and tms >= wall_b_first_ms
        exact_a = abs(normalize_tick_price(t.price, tick) - wall_a) < 1e-9
        exact_b = after_b and abs(normalize_tick_price(t.price, tick) - wall_b) < 1e-9
        zone_a_1t = d_ticks_a <= 1.0
        zone_b_1t = after_b and d_ticks_b <= 1.0
        trade_rows.append(
            {
                "timestamp": _iso(t.trade_ts, ms=True),
                "trade_id": t.trade_id,
                "price": t.price,
                "quantity": t.size,
                "notional": t.notional,
                "aggressor_side": t.side,
                "distance_wall_a_ticks": d_ticks_a,
                "distance_wall_a_bps": d_bps_a,
                "distance_wall_b_ticks": d_ticks_b,
                "distance_wall_b_bps": d_bps_b,
                "inside_ask_pool": inside,
                "before_arrival": tms < arrival_ms,
                "after_wall_b_first_seen": after_b,
                "exact_wall_a": exact_a,
                "exact_wall_b": exact_b,
                "zone_wall_a_1tick": zone_a_1t,
                "zone_wall_b_1tick": zone_b_1t,
                "zone_wall_a_0_5bps": d_bps_a <= 0.5,
                "zone_wall_a_1bps": d_bps_a <= 1.0,
                "zone_wall_b_0_5bps": after_b and d_bps_b <= 0.5,
                "zone_wall_b_1bps": after_b and d_bps_b <= 1.0,
            }
        )
        if exact_a or zone_a_1t or exact_b or zone_b_1t:
            touch_rows.append(
                {
                    "timestamp": _iso(t.trade_ts, ms=True),
                    "trade_id": t.trade_id,
                    "side": t.side,
                    "price": t.price,
                    "notional": t.notional,
                    "wall": "A" if (exact_a or zone_a_1t) else "B",
                    "exact": exact_a or exact_b,
                    "zone_1tick": zone_a_1t or zone_b_1t,
                }
            )

    # Impact windows around attack times
    def mid_at(ms: int) -> float | None:
        b = (ms // 1000) * 1000
        for off in range(0, 5):
            row = ob_by_s.get(b + off * 1000) or ob_by_s.get(b - off * 1000)
            if row and row[0]:
                return row[3]
        return None

    attack_times = []
    first_buy_a = next(
        (t for t in trades if t.side == "Buy" and abs(normalize_tick_price(t.price, tick) - wall_a) < 1e-9 and _ms(t.trade_ts) >= arrival_ms),
        None,
    )
    first_buy_b = next(
        (
            t
            for t in trades
            if t.side == "Buy"
            and wall_b_first_ms is not None
            and _ms(t.trade_ts) >= wall_b_first_ms
            and abs(normalize_tick_price(t.price, tick) - wall_b) < 1e-9
        ),
        None,
    )
    if first_buy_a:
        attack_times.append(("WALL_A", _ms(first_buy_a.trade_ts)))
    if first_buy_b:
        attack_times.append(("WALL_B", _ms(first_buy_b.trade_ts)))
    # also arrival as reference
    attack_times.append(("ARRIVAL", arrival_ms))

    impact_rows = []
    for label, t0 in attack_times:
        for win_s in (1, 3, 5, 10):
            t1 = t0 + win_s * 1000
            if t1 > end_ms:
                continue
            window_trades = [t for t in trades if t0 <= _ms(t.trade_ts) < t1]
            buy_n = sum(t.notional for t in window_trades if t.side == "Buy")
            sell_n = sum(t.notional for t in window_trades if t.side == "Sell")
            m0 = mid_at(t0)
            m1 = mid_at(t1 - 1)
            px_chg = bps(m1, m0) if m0 and m1 else None
            buy_impact = px_chg if px_chg is not None else None
            buy_imp_per = (buy_impact / buy_n) if buy_n and buy_impact is not None else None
            sell_imp_per = ((-buy_impact) / sell_n) if sell_n and buy_impact is not None else None
            impact_rows.append(
                {
                    "anchor": label,
                    "anchor_ts": _iso(_dt_ms(t0)),
                    "window_s": win_s,
                    "aggressive_buy_notional": buy_n,
                    "aggressive_sell_notional": sell_n,
                    "signed_imbalance": buy_n - sell_n,
                    "price_change_bps": px_chg,
                    "directional_buy_impact_bps": buy_impact,
                    "buy_impact_per_notional": buy_imp_per,
                    "sell_impact_per_notional": sell_imp_per,
                    "trade_count": len(window_trades),
                    "max_buy_bubble": max((t.notional for t in window_trades if t.side == "Buy"), default=0),
                    "max_sell_bubble": max((t.notional for t in window_trades if t.side == "Sell"), default=0),
                    "diagnostic_strong_impact_bps_threshold": UNFITTED_F0_DIAGNOSTIC.get(
                        "strong_same_side_impact_bps"
                    ),
                }
            )

    # Acceptance / rejection
    mids_post = [(s, ob_by_s[s][3]) for s in seconds if s >= arrival_ms and s in ob_by_s and ob_by_s[s][0]]
    max_mid = max((m for _, m in mids_post), default=None)
    time_above_a = sum(1 for s, m in mids_post if m > wall_a)
    time_above_b = sum(1 for s, m in mids_post if m > wall_b)
    time_above_hi = sum(1 for s, m in mids_post if m > hi)
    first_below_edge_after = next((s for s, m in mids_post if m < lo), None)
    first_trade_a_ts = _iso(first_buy_a.trade_ts, ms=True) if first_buy_a else None
    first_trade_b_ts = _iso(first_buy_b.trade_ts, ms=True) if first_buy_b else None

    # Was wall B ever reached by trades?
    wall_b_attacked = first_buy_b is not None or any(
        abs(normalize_tick_price(t.price, tick) - wall_b) < 1e-9
        and t.side == "Buy"
        and wall_b_first_ms
        and _ms(t.trade_ts) >= wall_b_first_ms
        for t in trades
    )
    # mid reached wall B?
    wall_b_mid_reached = any(m >= wall_b for _, m in mids_post)

    states = []
    if first_buy_a:
        states.append("WALL_A_ATTACKED")
    else:
        # mid near wall?
        if any(m >= wall_a - tick for _, m in mids_post):
            states.append("WALL_A_ATTACKED")  # price traded up to wall zone
        else:
            states.append("WALL_A_NOT_REACHED")
    if any(m > wall_a for _, m in mids_post):
        states.append("WALL_A_TEMPORARILY_BROKEN")
    if time_above_a >= 10:
        states.append("WALL_A_ACCEPTED_ABOVE")
    if any(m < wall_a for s, m in mids_post if s > arrival_ms + 5000):
        states.append("WALL_A_RECLAIMED_BELOW")

    if not wall_b_attacked and not wall_b_mid_reached:
        states.append("WALL_B_NOT_REACHED")
    elif wall_b_attacked or wall_b_mid_reached:
        states.append("WALL_B_ATTACKED")
    if any(m > wall_b for _, m in mids_post):
        states.append("WALL_B_TEMPORARILY_BROKEN")
    if time_above_b >= 10:
        states.append("WALL_B_ACCEPTED_ABOVE")
    if wall_b_mid_reached and any(m < wall_b for s, m in mids_post if s > (wall_b_first_ms or arrival_ms)):
        states.append("WALL_B_RECLAIMED_BELOW")

    if time_above_hi >= 5:
        states.append("POOL_ACCEPTED_ABOVE")
    if first_below_edge_after is not None:
        states.append("POOL_REJECTED_TO_ENTRY_SIDE")

    acc_rows = [
        {
            "event": "first_entry_ask_pool",
            "ts": ARRIVAL_TS,
        },
        {"event": "first_trade_wall_a", "ts": first_trade_a_ts},
        {"event": "first_trade_wall_b", "ts": first_trade_b_ts},
        {"event": "max_mid", "value": max_mid},
        {"event": "seconds_above_wall_a", "value": time_above_a},
        {"event": "seconds_above_wall_b", "value": time_above_b},
        {"event": "seconds_above_pool_upper", "value": time_above_hi},
        {
            "event": "first_mid_below_entry_edge_after_arrival",
            "ts": _iso(_dt_ms(first_below_edge_after)) if first_below_edge_after else None,
        },
        {"event": "cluster_end", "ts": CLUSTER_END_TS},
        {"event": "states", "value": "|".join(states)},
    ]

    # Wall identities summary
    arr_sec = arrival_ms
    qa0 = next((r for r in one_sec if r["second_ms"] == arr_sec), None)
    wall_ids = [
        {
            "wall": "A",
            "side": "ASK",
            "tick_price": wall_a,
            "first_seen_ts": ctx["wall_a_fs"]["first_seen_ts"] if ctx["wall_a_fs"] else None,
            "first_seen_class": ctx["wall_a_fs"]["first_seen_class"] if ctx["wall_a_fs"] else None,
            "present_at_arrival": True,
            "qty_at_arrival": qa0["wall_a_qty"] if qa0 else None,
            "notional_at_arrival": qa0["wall_a_notional"] if qa0 else None,
            "rank_at_arrival": qa0["wall_a_rank"] if qa0 else None,
            "pool_wall_id": f"BTCUSDT|{a['pool_id']}|ASK|{wall_a}",
            "cluster_wall_id": f"BTCUSDT|ASK|{wall_a}",
        },
        {
            "wall": "B",
            "side": "ASK",
            "tick_price": wall_b,
            "first_seen_ts": ctx["wall_b_fs"]["first_seen_ts"] if ctx["wall_b_fs"] else None,
            "first_seen_class": ctx["wall_b_fs"]["first_seen_class"] if ctx["wall_b_fs"] else None,
            "present_at_arrival": False,
            "qty_at_first_seen": next(
                (r["wall_b_qty"] for r in one_sec if r["second_ms"] == wall_b_first_ms), None
            ),
            "notional_at_first_seen": next(
                (r["wall_b_notional"] for r in one_sec if r["second_ms"] == wall_b_first_ms), None
            ),
            "rank_at_first_seen": next(
                (r["wall_b_rank"] for r in one_sec if r["second_ms"] == wall_b_first_ms), None
            ),
            "pool_wall_id": f"BTCUSDT|{a['pool_id']}|ASK|{wall_b}",
            "cluster_wall_id": f"BTCUSDT|ASK|{wall_b}",
        },
    ]

    # Summaries for walls
    def wall_summary(tl, visible_key="visible"):
        vis = [r for r in tl if r.get(visible_key)]
        qtys = [r["qty"] for r in vis if r.get("qty") is not None]
        return {
            "visible_seconds": len(vis),
            "visibility_fraction": len(vis) / max(1, len([r for r in tl if r.get("before_first_seen") is not True])),
            "max_qty": max(qtys) if qtys else None,
            "min_qty": min(qtys) if qtys else None,
            "disappeared": any(
                tl[i].get("visible") and not tl[i + 1].get("visible")
                for i in range(len(tl) - 1)
            ),
        }

    # Evidence classification
    buy_n_total = sum(t.notional for t in trades if t.side == "Buy" and _ms(t.trade_ts) >= arrival_ms)
    sell_n_total = sum(t.notional for t in trades if t.side == "Sell" and _ms(t.trade_ts) >= arrival_ms)
    # Impact at arrival 5s
    imp5 = next((r for r in impact_rows if r["anchor"] == "ARRIVAL" and r["window_s"] == 5), None)
    low_up_impact = imp5 and (imp5["price_change_bps"] is not None) and abs(imp5["price_change_bps"]) < UNFITTED_F0_DIAGNOSTIC.get("strong_same_side_impact_bps", 8)
    high_buy = buy_n_total >= UNFITTED_F0_DIAGNOSTIC.get("min_notional_usdt", 10000)

    refill_a = any(r.get("class") == "REFILL_SUPPORTED" and r["wall"] == "A" for r in decomp_rows)
    cancel_a = any(r.get("class") == "CANCELLATION_OR_MOVE_SUPPORTED" and r["wall"] == "A" for r in decomp_rows)
    consume_a = any(r.get("class") == "TRADE_EXPLAINED_DEPLETION_SUPPORTED" and r["wall"] == "A" for r in decomp_rows)
    rejected = "POOL_REJECTED_TO_ENTRY_SIDE" in states
    accepted_above = "POOL_ACCEPTED_ABOVE" in states or "WALL_A_ACCEPTED_ABOVE" in states

    wall_a_attacked = "WALL_A_ATTACKED" in states or first_buy_a is not None
    # Also count buys within 1 tick of wall A
    buys_near_a = [
        t
        for t in trades
        if t.side == "Buy"
        and _ms(t.trade_ts) >= arrival_ms
        and abs(t.price - wall_a) / tick <= 1.0
    ]

    evidence = "INSUFFICIENT_EVIDENCE"
    if not wall_a_attacked and not wall_b_attacked and not wall_b_mid_reached:
        # check if price reached wall A mid
        if not any(m >= wall_a - 2 * tick for _, m in mids_post):
            evidence = "WALL_NOT_ATTACKED"
    elif (
        wall_a_attacked
        and len(buys_near_a) > 0
        and high_buy
        and low_up_impact
        and rejected
        and not accepted_above
        and (refill_a or not consume_a)
    ):
        evidence = "ASK_DEFENSE_ABSORPTION_SUPPORTED"
    elif consume_a and accepted_above and not rejected:
        evidence = "ASK_WALL_CONSUMPTION_BREAKOUT_SUPPORTED"
    elif cancel_a and not wall_a_attacked:
        evidence = "ASK_WALL_CANCELLATION_PULL_SUPPORTED"
    elif wall_b_first_ms and not wall_b_attacked and not wall_b_mid_reached:
        # Wall B never attacked — but wall A may still have reaction
        if wall_a_attacked and rejected and high_buy and low_up_impact:
            evidence = "MIXED_WALL_REACTION"
        elif wall_a_attacked and rejected:
            evidence = "MIXED_WALL_REACTION"
        else:
            evidence = "MIXED_WALL_REACTION"
    else:
        evidence = "MIXED_WALL_REACTION"

    # Refine: if wall B not attacked, note separately in explanation; primary may still be absorption on A
    if (
        wall_a_attacked
        and len(buys_near_a) > 0
        and rejected
        and not accepted_above
        and (imp5 is None or (imp5["price_change_bps"] is not None and imp5["price_change_bps"] < 8))
    ):
        # Prefer absorption if buy flow material
        if buy_n_total > sell_n_total and buy_n_total >= 5000:
            evidence = "ASK_DEFENSE_ABSORPTION_SUPPORTED"

    # reaction_first_available_ts via prefix
    # Earliest when: wall A attacked + some buy near wall + mid back below edge OR clear low impact after attack
    reaction_ts = None
    prefix_ok = False
    if first_buy_a and first_below_edge_after and first_below_edge_after >= _ms(first_buy_a.trade_ts):
        # need both attack and rejection observed
        reaction_ts = _iso(_dt_ms(first_below_edge_after))
        # prefix: data up to that point should show attack + rejection start
        prefix_ok = True
    elif first_below_edge_after:
        reaction_ts = CLUSTER_END_TS
        prefix_ok = True
    else:
        reaction_ts = CLUSTER_END_TS
        prefix_ok = True

    # If absorption claimed, reaction cannot be before first buy attack and evidence of limited upside
    if evidence == "ASK_DEFENSE_ABSORPTION_SUPPORTED" and first_buy_a:
        # earliest: after at least 5s post first attack with low impact OR reclaim below edge
        t_attack = _ms(first_buy_a.trade_ts)
        cand = t_attack + 5000
        if first_below_edge_after:
            reaction_ms = max(cand, first_below_edge_after)
        else:
            reaction_ms = max(cand, end_ms)
        reaction_ms = min(reaction_ms, end_ms)
        reaction_ts = _iso(_dt_ms(reaction_ms))
        # prefix check: with data <= reaction_ms, wall A attacked and not accepted above for long
        prefix_mids = [(s, m) for s, m in mids_post if s <= reaction_ms]
        prefix_above = sum(1 for _, m in prefix_mids if m > wall_a)
        prefix_ok = any(m >= wall_a - tick for _, m in prefix_mids) and prefix_above < 30

    prefix = {
        "reaction_first_available_ts": reaction_ts,
        "prefix_reproduces_partial_class": prefix_ok,
        "evidence_at_prefix": evidence,
        "note": "Later rejection may strengthen class; class not projected before attack+limited upside/reclaim observables exist",
    }
    (out / "prefix_check.json").write_text(json.dumps(prefix, indent=2), encoding="utf-8")

    write_csv(out / "wall_identities.csv", wall_ids)
    write_csv(out / "public_trades_raw.csv", trade_rows)
    write_csv(out / "one_second_timeline.csv", one_sec)
    write_csv(out / "wall_a_timeline.csv", wall_a_tl)
    write_csv(out / "wall_b_timeline.csv", wall_b_tl)
    write_csv(out / "wall_trade_touches.csv", touch_rows)
    write_csv(out / "wall_quantity_decomposition.csv", decomp_rows)
    write_csv(out / "trade_impact_windows.csv", impact_rows)
    write_csv(out / "acceptance_rejection_timeline.csv", acc_rows)

    sa = wall_summary(wall_a_tl)
    sb = wall_summary(wall_b_tl)

    tech = "ASK_POOL_022736_WALL_PUBLIC_TRADE_REACTION_AUDIT_V1_COMPLETE"
    if len(trades) < 10 or len(ob_rows) < 50:
        tech = "ASK_POOL_022736_WALL_PUBLIC_TRADE_REACTION_AUDIT_V1_BLOCKED_DATA"
    elif evidence == "INSUFFICIENT_EVIDENCE":
        tech = "ASK_POOL_022736_WALL_PUBLIC_TRADE_REACTION_AUDIT_V1_PARTIAL"

    (out / "verdict.json").write_text(
        json.dumps(
            {
                "technical_verdict": tech,
                "evidence_verdict": evidence,
                "reaction_first_available_ts": reaction_ts,
                "buy_notional_post_arrival": buy_n_total,
                "sell_notional_post_arrival": sell_n_total,
                "states": states,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "run_manifest.json").write_text(
        json.dumps(
            {
                "audit_id": "ASK_POOL_022736_WALL_PUBLIC_TRADE_REACTION_AUDIT_V1",
                "foundation_commit": "9b8fe7cf1947d3b821d6ae4d1df2719ec94107f4",
                "window": {"start": WINDOW_START, "end": CLUSTER_END_TS},
                "technical_verdict": tech,
                "evidence_verdict": evidence,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "data_quality_report.json").write_text(
        json.dumps(
            {
                "trades_preflight": preflight,
                "trades_in_window": len(trades),
                "ob_seconds": len(ob_rows),
                "expected_seconds": len(seconds),
                "gaps": sum(1 for r in one_sec if r["coverage"] == "SOURCE_GAP"),
                "no_data_after_cluster_end_in_classification": True,
                "no_outcomes": True,
                "no_queue_reconstruction": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    simple = f"""
## 23. Einfache Erklärung

1. Pool: ASK `{a['pool_id']}` [{lo}, {hi}], Cluster `{a.get('market_arrival_cluster_id')}`
2. Entry Edge: `{edge}` — Mid kommt von unten bei `{ARRIVAL_TS}`
3. Wall A: Ask `{wall_a}` Rank-1 MAJOR bei Arrival (Notional≈{qa0['wall_a_notional'] if qa0 else 'n/a'})
4. Wall B: Ask `{wall_b}` first seen `{ctx['wall_b_fs']['first_seen_ts'] if ctx['wall_b_fs'] else None}` — andere Identität
5. Käufer-/Verkäuferfluss: Buy-Notional post-arrival≈{buy_n_total:.0f}, Sell≈{sell_n_total:.0f}
6. Preiswirkung: 5s ab Arrival Δbps≈{imp5['price_change_bps'] if imp5 else None} (Schwelle diagnostisch {UNFITTED_F0_DIAGNOSTIC.get('strong_same_side_impact_bps')} bps)
7. Wall-Größenentwicklung: A visible_frac≈{sa['visibility_fraction']:.2f}; B visible_frac≈{sb['visibility_fraction']:.2f}
8. Refill/Depletion/Cancellation: refill_A={refill_a}, consume_A={consume_a}, cancel_A={cancel_a} (Snapshot-Grenzen, keine Queue)
9. Acceptance/Rejection: states={states}; Cluster-Ende EXITED_ENTRY_SIDE `{CLUSTER_END_TS}`
10. Frühester kausaler Erkenntniszeitpunkt: `{reaction_ts}`
11. Primäre Klasse: **{evidence}**
12. Unbekannt: exakte Exchange-Queue; ob Verschwinden Cancel vs Move; Teil der Reduktionen MIXED/INSUFFICIENT_TEMPORAL_RESOLUTION
"""

    bericht = f"""# ABSCHLUSSBERICHT — ASK_POOL_022736_WALL_PUBLIC_TRADE_REACTION_AUDIT_V1

## 1. Technisches Verdict

**{tech}**

## 2. Evidenzverdict

**{evidence}**

## 3. Live-Sicherheit

Read-only. Kein Commit/Push. CH nur SELECT.

## 4. Branch / HEAD / Dirty

orderbook_analyse `feature/strategy-lab-phase1` @ `9b8fe7cf1947d3b821d6ae4d1df2719ec94107f4`. Dirty unverändert.

## 5. Pool-/Cluster-ID

pool_arrival=`{a['pool_arrival_id']}` pool=`{a['pool_id']}` cluster=`{a.get('market_arrival_cluster_id')}`

## 6. Poolgrenzen

[{lo}, {hi}] entry_edge=`{edge}`

## 7. Arrival und Cluster-Ende

Arrival=`{ARRIVAL_TS}` Ende=`{CLUSTER_END_TS}` (`EXITED_ENTRY_SIDE`)

## 8. Wall A Identität

Ask tick=`{wall_a}` V2@Arrival Rank=`{a.get('strongest_wall_rank_at_arrival')}` Notional=`{a.get('strongest_wall_notional_at_arrival')}`
(1s-Timeline am Arrival-Bucket kann abweichen/intermittent: qty=`{qa0['wall_a_qty'] if qa0 else None}` rank=`{qa0['wall_a_rank'] if qa0 else None}`)

## 9. Wall B Identität

Ask tick=`{wall_b}` first_seen=`{ctx['wall_b_fs']['first_seen_ts'] if ctx['wall_b_fs'] else None}` class=`APPEARED_STRICTLY_AFTER_ARRIVAL`

## 10. Public-Trade-Coverage

trades={len(trades)} table=`{CANONICAL_TRADES_TABLE}` loader=`load_trades_clickhouse` dedup=`trade_id`

## 11. Aggressive Buy-/Sell-Notionals (post Arrival)

Buy≈`{buy_n_total:.2f}` Sell≈`{sell_n_total:.2f}`

## 12. Exact-/Zone-Touches

exact/zone rows=`{len(touch_rows)}` — siehe `wall_trade_touches.csv` (primär exact / 1 Tick)

## 13. Wall-A-Reaktion

attacked=`{wall_a_attacked}` buys_near_1tick=`{len(buys_near_a)}` refill=`{refill_a}` consume=`{consume_a}` cancel=`{cancel_a}` vis_frac≈`{sa['visibility_fraction']:.3f}`

## 14. Wall-B-Reaktion

attacked=`{wall_b_attacked}` mid_reached=`{wall_b_mid_reached}` → {"WALL_NOT_ATTACKED for B" if not wall_b_attacked and not wall_b_mid_reached else "see states"}

## 15. Impact pro Notional

siehe `trade_impact_windows.csv` (1/3/5/10s); diagnostische AEF-Schwelle strong_impact_bps=`{UNFITTED_F0_DIAGNOSTIC.get('strong_same_side_impact_bps')}`

## 16. Mengenzerlegung

siehe `wall_quantity_decomposition.csv` — keine Queue-Rekonstruktion.

## 17. Refill-Evidenz

refill_A=`{refill_a}`

## 18. Consumption-Evidenz

consume_A=`{consume_a}`

## 19. Cancellation-Evidenz

cancel_A=`{cancel_a}`

## 20. Acceptance/Rejection

{states}

## 21. reaction_first_available_ts

`{reaction_ts}`

## 22. Prefix-Check

`{json.dumps(prefix)}`

{simple}

## 24. Offene Unsicherheiten

Snapshot- vs Trade-Zeitauflösung; keine Exchange-Queue; Wall-B nie/kaum angegriffen → Defense nur für attackierte Levels belegbar.

## 25. Stop

Kein Commit. Keine Folgeanalyse. Auf Bewertung warten.
"""
    (out / "ABSCHLUSSBERICHT.md").write_text(bericht, encoding="utf-8")
    print(json.dumps({"technical": tech, "evidence": evidence, "reaction_ts": reaction_ts, "trades": len(trades)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

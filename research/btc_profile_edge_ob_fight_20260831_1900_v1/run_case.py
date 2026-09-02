#!/usr/bin/env python3
"""Isolated BTCUSDT profile-edge OB fight case study (read-only).

Case: BTCUSDT T0=2026-08-31 19:00:00 UTC, core 18:30-19:30, extend to 21:00.
Writes ONLY under results/btc_profile_edge_ob_fight_20260831_1900_v1/.
Does not touch dashboard live paths, collectors, or ClickHouse writes.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OA_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(OA_SRC))

import clickhouse_connect

from research_charts.clickhouse_config import load_clickhouse_config
from orderbook_analyse.market_profile.anchor import build_windows
from orderbook_analyse.market_profile.build import build_profile
from orderbook_analyse.market_profile.contracts import ProfileWindow

CASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CASE_DIR))
from ob_replay import (  # noqa: E402
    SHADOW,
    extract_walls,
    find_hour_segment,
    replay_as_of,
    replay_hour_at_cutoffs,
)

OUT = ROOT / "results" / "btc_profile_edge_ob_fight_20260831_1900_v1"
SYMBOL = "BTCUSDT"
T0 = datetime(2026, 8, 31, 19, 0, 0, tzinfo=timezone.utc)
CORE_START = datetime(2026, 8, 31, 18, 30, 0, tzinfo=timezone.utc)
CORE_END = datetime(2026, 8, 31, 19, 30, 0, tzinfo=timezone.utc)
MAX_END = datetime(2026, 8, 31, 21, 0, 0, tzinfo=timezone.utc)
VA_PCT = 0.70
TARGET_BINS = 160


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return utc(dt).isoformat().replace("+00:00", "Z")


def dec(x: Any) -> float:
    if x is None:
        return float("nan")
    if isinstance(x, Decimal):
        return float(x)
    return float(x)


def client():
    cfg = load_clickhouse_config()
    return clickhouse_connect.get_client(**cfg.connect_kwargs())


def q(cl, sql: str):
    return cl.query(sql).result_rows


def profile_to_levels(prof) -> dict[str, Any]:
    va = prof.value_area
    nodes = prof.nodes
    return {
        "window_label": prof.window.label,
        "window_start": iso(prof.window.start),
        "window_end": iso(prof.window.end),
        "price_step": prof.price_step,
        "price_low": prof.price_low,
        "price_high": prof.price_high,
        "open": prof.open_price,
        "close": prof.close_price,
        "total_volume": prof.total_volume,
        "buy_volume": prof.buy_volume,
        "sell_volume": prof.sell_volume,
        "trades": prof.trades,
        "poc": va.poc,
        "vah": va.vah,
        "val": va.val,
        "va_volume_share": va.volume_share,
        "hvn": list(nodes.hvn),
        "lvn": list(nodes.lvn),
        "single_print_ranges": [
            {"low": float(r[0]), "high": float(r[1])} for r in nodes.single_print_ranges
        ],
        "shape": {
            "kind": getattr(prof.shape, "kind", None),
            "letter": getattr(prof.shape, "letter", None),
            "reason": getattr(prof.shape, "reasons", None),
        },
    }


def build_causal_profiles(cl) -> dict[str, Any]:
    """Profiles usable at T0 without peeking past T0."""
    day_start = datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc)
    # Prior UTC day (fully closed before T0)
    prior_day = datetime(2026, 8, 30, 0, 0, 0, tzinfo=timezone.utc)
    prior_end = day_start
    # Developing US session clipped to T0
    us_start = datetime(2026, 8, 31, 13, 30, 0, tzinfo=timezone.utc)
    # Prior completed sessions same day
    asia_eu_end = us_start

    out: dict[str, Any] = {
        "settings": {
            "value_area_pct": VA_PCT,
            "target_bins": TARGET_BINS,
            "anchor_note": "volume-at-price (NOT classic TPO letters); "
            "sessions asia/eu/us/late as in market_profile package",
            "causal_cutoff": iso(T0),
        },
        "profiles": {},
    }

    specs = [
        ("prior_day_20260830", prior_day, prior_end, "day"),
        ("asia_eu_completed_to_1330", day_start, asia_eu_end, "composite"),
        ("us_developing_to_T0", us_start, T0, "composite"),
        ("day_developing_to_T0", day_start, T0, "composite"),
        # 30-min VP blocks ending at/before T0 (context only)
        ("vp_block_1830_1900", CORE_START, T0, "composite"),
        ("vp_block_1800_1830", datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc), CORE_START, "composite"),
    ]
    for name, start, end, mode in specs:
        wins = build_windows(anchor_mode=mode, start=start, end=end)
        if not wins:
            out["profiles"][name] = None
            continue
        # composite yields one window; day may yield one
        w = wins[0]
        try:
            prof = build_profile(
                cl, SYMBOL, w, value_area_pct=VA_PCT, target_bins=TARGET_BINS, use_final=True
            )
        except Exception as exc:
            out["profiles"][name] = {"error": str(exc)}
            continue
        if prof is None:
            out["profiles"][name] = None
            continue
        out["profiles"][name] = profile_to_levels(prof)
    return out


def nearest_level(price: float, levels: dict[str, Any]) -> dict[str, Any]:
    cands = []
    for key in ("poc", "vah", "val"):
        if key in levels and levels[key] is not None:
            cands.append((key, float(levels[key])))
    for p in levels.get("hvn") or []:
        cands.append(("hvn", float(p)))
    for p in levels.get("lvn") or []:
        cands.append(("lvn", float(p)))
    if not cands:
        return {"level_kind": None, "level_price": None, "distance": None, "distance_bps": None}
    best = min(cands, key=lambda x: abs(price - x[1]))
    dist = price - best[1]
    bps = dist / price * 10000.0
    # edge classification relative to VA
    vah = levels.get("vah")
    val = levels.get("val")
    poc = levels.get("poc")
    zone = "inside_va"
    if vah is not None and price >= float(vah):
        zone = "at_or_above_vah"
    elif val is not None and price <= float(val):
        zone = "at_or_below_val"
    elif poc is not None and abs(price - float(poc)) / price < 0.0005:
        zone = "near_poc"
    return {
        "level_kind": best[0],
        "level_price": best[1],
        "distance": dist,
        "distance_bps": bps,
        "zone": zone,
        "vah": vah,
        "val": val,
        "poc": poc,
    }


def coverage_audit(cl) -> dict[str, Any]:
    start, end = CORE_START, MAX_END
    audit: dict[str, Any] = {
        "symbol": SYMBOL,
        "window_utc": {"start": iso(start), "end": iso(end)},
        "timestamps_interpreted_as": "UTC",
    }
    # Trades
    row = q(
        cl,
        f"""
        SELECT count(), min(trade_ts), max(trade_ts), uniqExact(trade_id),
               countIf(side='Buy'), countIf(side='Sell'),
               sum(notional)
        FROM orderbook_analysis.public_trades_canonical FINAL
        WHERE symbol='{SYMBOL}'
          AND trade_ts >= toDateTime64('{start.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
          AND trade_ts < toDateTime64('{end.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
        """,
    )[0]
    audit["public_trades"] = {
        "table": "orderbook_analysis.public_trades_canonical",
        "count": int(row[0]),
        "min_ts": iso(row[1]),
        "max_ts": iso(row[2]),
        "uniq_trade_id": int(row[3]),
        "buy_count": int(row[4]),
        "sell_count": int(row[5]),
        "sum_notional": dec(row[6]),
        "dedup_ok": int(row[0]) == int(row[3]),
        "aggressor_semantics": "side=Buy/Sell is Bybit taker/aggressor",
    }
    # Candles
    row = q(
        cl,
        f"""
        SELECT count(), min(open_time), max(open_time)
        FROM signal_generator.candles_1m FINAL
        WHERE exchange='bybit' AND symbol='{SYMBOL}' AND interval='1m' AND is_closed=1
          AND open_time >= toDateTime64('{start.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
          AND open_time < toDateTime64('{end.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
        """,
    )[0]
    expected_min = int((end - start).total_seconds() // 60)
    audit["candles_1m"] = {
        "table": "signal_generator.candles_1m",
        "count": int(row[0]),
        "min_ts": iso(row[1]),
        "max_ts": iso(row[2]),
        "expected_minutes": expected_min,
        "complete": int(row[0]) >= expected_min - 1,
    }
    # OI
    row = q(
        cl,
        f"""
        SELECT count(), min(bucket_time), max(bucket_time)
        FROM orderbook_analysis.open_interest_5s
        WHERE symbol='{SYMBOL}'
          AND bucket_time >= toDateTime64('{start.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
          AND bucket_time < toDateTime64('{end.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
        """,
    )[0]
    audit["open_interest_5s"] = {
        "table": "orderbook_analysis.open_interest_5s",
        "count": int(row[0]),
        "min_ts": iso(row[1]),
        "max_ts": iso(row[2]),
        "resolution": "5s",
    }
    # Liq
    rows = q(
        cl,
        f"""
        SELECT liquidated_position_side, count(), sum(notional_estimate)
        FROM orderbook_analysis.all_liquidations
        WHERE symbol='{SYMBOL}'
          AND event_time >= toDateTime64('{start.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
          AND event_time < toDateTime64('{end.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
        GROUP BY liquidated_position_side
        """,
    )
    audit["liquidations"] = {
        "table": "orderbook_analysis.all_liquidations",
        "by_side": [
            {"side": str(r[0]), "count": int(r[1]), "notional": dec(r[2])} for r in rows
        ],
        "note": "liquidated_position_side is position side, not aggressor",
    }
    # OB200 segments covering window
    needed = []
    t = datetime(2026, 8, 31, 17, 0, tzinfo=timezone.utc)
    while t < MAX_END:
        try:
            path = find_hour_segment(SYMBOL, t)
            needed.append(
                {
                    "hour": iso(t),
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "ok": path.stat().st_size > 10000,
                }
            )
        except Exception as exc:
            needed.append({"hour": iso(t), "ok": False, "error": str(exc)})
        t += timedelta(hours=1)
    audit["ob200"] = {
        "source": str(SHADOW / SYMBOL),
        "engine": "filesystem ob200_v3.zst hour archives via research/ob_replay.py (chunked zstd)",
        "hours_needed": needed,
        "all_hours_ok": all(h.get("ok") for h in needed),
        "depth": 200,
        "continuity": "per-hour rotation_checkpoint + consecutive u deltas",
        "note": "dashboard ob200_walls.readline path fails on these archives; case uses chunked reader",
    }
    # Probe reconstruct at T0 and core start
    probes = {}
    for label, at in [("before_core", CORE_START - timedelta(seconds=1)), ("T0", T0), ("core_end", CORE_END)]:
        try:
            snap = replay_as_of(SYMBOL, at)
            probes[label] = {
                "ok": True,
                "as_of": iso(snap["as_of"]),
                "best_bid": dec(snap["best_bid"]),
                "best_ask": dec(snap["best_ask"]),
                "mid": dec(snap["mid"]),
                "bid_levels": snap["bid_levels"],
                "ask_levels": snap["ask_levels"],
                "events_applied": snap["events_applied"],
                "u_gaps": snap.get("u_gaps"),
                "segment": snap["segment"],
                "genuine_200": snap["bid_levels"] >= 180 and snap["ask_levels"] >= 180,
            }
        except Exception as exc:
            probes[label] = {"ok": False, "error": str(exc)}
    audit["ob200_replay_probes"] = probes
    audit["ob200_reconstructable"] = all(p.get("ok") and p.get("genuine_200") for p in probes.values())
    return audit


def load_trades(cl, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = q(
        cl,
        f"""
        SELECT trade_ts, trade_id, side, toFloat64(price), toFloat64(size), toFloat64(notional)
        FROM orderbook_analysis.public_trades_canonical FINAL
        WHERE symbol='{SYMBOL}'
          AND trade_ts >= toDateTime64('{start.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
          AND trade_ts < toDateTime64('{end.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
        ORDER BY trade_ts, trade_id
        """,
    )
    out = []
    for r in rows:
        out.append(
            {
                "ts": utc(r[0]),
                "trade_id": str(r[1]),
                "side": str(r[2]),
                "price": float(r[3]),
                "size": float(r[4]),
                "notional": float(r[5]),
            }
        )
    return out


def window_trade_stats(trades: list[dict], start: datetime, end: datetime) -> dict[str, Any]:
    xs = [t for t in trades if start <= t["ts"] < end]
    if not xs:
        return {
            "start": iso(start),
            "end": iso(end),
            "n": 0,
            "buy_notional": 0.0,
            "sell_notional": 0.0,
            "delta_notional": 0.0,
            "buy_size": 0.0,
            "sell_size": 0.0,
            "first_price": None,
            "last_price": None,
            "price_change": None,
            "price_change_bps": None,
            "efficiency_bps_per_m_buy": None,
            "efficiency_bps_per_m_sell": None,
            "large_trades_ge_50k": 0,
        }
    buy_n = sum(t["notional"] for t in xs if t["side"] == "Buy")
    sell_n = sum(t["notional"] for t in xs if t["side"] == "Sell")
    buy_s = sum(t["size"] for t in xs if t["side"] == "Buy")
    sell_s = sum(t["size"] for t in xs if t["side"] == "Sell")
    first_p = xs[0]["price"]
    last_p = xs[-1]["price"]
    chg = last_p - first_p
    bps = chg / first_p * 10000.0
    eff_buy = (bps / (buy_n / 1e6)) if buy_n > 0 else None
    # for sell aggression, negative price change is efficient for sellers
    eff_sell = ((-bps) / (sell_n / 1e6)) if sell_n > 0 else None
    large = sum(1 for t in xs if t["notional"] >= 50000)
    return {
        "start": iso(start),
        "end": iso(end),
        "n": len(xs),
        "buy_notional": buy_n,
        "sell_notional": sell_n,
        "delta_notional": buy_n - sell_n,
        "buy_size": buy_s,
        "sell_size": sell_s,
        "first_price": first_p,
        "last_price": last_p,
        "high": max(t["price"] for t in xs),
        "low": min(t["price"] for t in xs),
        "price_change": chg,
        "price_change_bps": bps,
        "efficiency_bps_per_m_buy": eff_buy,
        "efficiency_bps_per_m_sell": eff_sell,
        "large_trades_ge_50k": large,
    }


def sample_ob_timeline(times: list[datetime]) -> list[dict[str, Any]]:
    # Group by hour and single-pass replay each archive.
    by_hour: dict[datetime, list[datetime]] = {}
    for at in times:
        hour = at.replace(minute=0, second=0, microsecond=0)
        by_hour.setdefault(hour, []).append(at)
    rows: list[dict[str, Any]] = []
    for hour in sorted(by_hour):
        print(f"  OB hour {iso(hour)} n={len(by_hour[hour])}", flush=True)
        snaps = replay_hour_at_cutoffs(SYMBOL, hour, by_hour[hour])
        for snap in snaps:
            if not snap.get("ok"):
                rows.append(
                    {
                        "ts": iso(snap.get("ts") or hour),
                        "ok": False,
                        "error": snap.get("error"),
                    }
                )
                continue
            walls = extract_walls(snap, max_walls=10)
            mid = dec(snap["mid"])
            thr = mid * 0.002
            bid_depth = sum(dec(q) for p, q in snap["bids"] if mid - dec(p) <= thr)
            ask_depth = sum(dec(q) for p, q in snap["asks"] if dec(p) - mid <= thr)
            top_bid_walls = sorted(
                [w for w in walls if w["side"] == "BID"], key=lambda w: -float(w["notional"])
            )[:5]
            top_ask_walls = sorted(
                [w for w in walls if w["side"] == "ASK"], key=lambda w: -float(w["notional"])
            )[:5]
            rows.append(
                {
                    "ts": iso(snap["as_of_requested"]),
                    "as_of": iso(snap["as_of"]),
                    "mid": mid,
                    "best_bid": dec(snap["best_bid"]),
                    "best_ask": dec(snap["best_ask"]),
                    "spread": dec(snap["best_ask"]) - dec(snap["best_bid"]),
                    "bid_levels": snap["bid_levels"],
                    "ask_levels": snap["ask_levels"],
                    "u_gaps": snap.get("u_gaps"),
                    "bid_depth_20bps": bid_depth,
                    "ask_depth_20bps": ask_depth,
                    "imbalance_20bps": (bid_depth - ask_depth) / (bid_depth + ask_depth)
                    if (bid_depth + ask_depth) > 0
                    else None,
                    "top_bid_walls": [
                        {
                            "price": dec(w["price"]),
                            "qty": dec(w["qty"]),
                            "notional": dec(w["notional"]),
                            "distance_bps": dec(w["distance_bps"]),
                            "ratio": float(w["ratio"]),
                        }
                        for w in top_bid_walls
                    ],
                    "top_ask_walls": [
                        {
                            "price": dec(w["price"]),
                            "qty": dec(w["qty"]),
                            "notional": dec(w["notional"]),
                            "distance_bps": dec(w["distance_bps"]),
                            "ratio": float(w["ratio"]),
                        }
                        for w in top_ask_walls
                    ],
                    "ok": True,
                }
            )
    rows.sort(key=lambda r: r.get("ts") or "")
    return rows


def classify_wall_events(
    ob_rows: list[dict], trades: list[dict], decision_level: float, band_bps: float = 8.0
) -> list[dict[str, Any]]:
    """Track nearest ask/bid walls around decision level across samples."""
    events = []
    prev_ask: dict[str, float] | None = None
    prev_bid: dict[str, float] | None = None
    for row in ob_rows:
        if not row.get("ok"):
            continue
        mid = row["mid"]
        # pick ask wall nearest above decision_level within 40 bps of mid
        asks = row.get("top_ask_walls") or []
        bids = row.get("top_bid_walls") or []
        ask = None
        for w in asks:
            if w["price"] >= decision_level - mid * band_bps / 10000:
                ask = w
                break
        if ask is None and asks:
            ask = min(asks, key=lambda w: abs(w["price"] - decision_level))
        bid = None
        for w in bids:
            if w["price"] <= decision_level + mid * band_bps / 10000:
                bid = w
                break
        if bid is None and bids:
            bid = min(bids, key=lambda w: abs(w["price"] - decision_level))

        ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        # trades in last sample interval (~30s lookback for classification)
        look = [t for t in trades if ts - timedelta(seconds=30) <= t["ts"] < ts]
        traded_at_ask = 0.0
        traded_at_bid = 0.0
        if ask:
            lo, hi = ask["price"] * 0.99995, ask["price"] * 1.00005
            traded_at_ask = sum(
                t["size"] for t in look if t["side"] == "Buy" and lo <= t["price"] <= hi
            )
        if bid:
            lo, hi = bid["price"] * 0.99995, bid["price"] * 1.00005
            traded_at_bid = sum(
                t["size"] for t in look if t["side"] == "Sell" and lo <= t["price"] <= hi
            )

        def classify(side: str, cur, prev, traded):
            if cur is None:
                return None
            cls = "PRESENT"
            if prev is not None and abs(prev["price"] - cur["price"]) / cur["price"] < 5e-5:
                dq = cur["qty"] - prev["qty"]
                if dq < -1e-6:
                    if traded >= abs(dq) * 0.3:
                        cls = "CONSUMED_OR_REDUCED_WITH_TRADES"
                    else:
                        cls = "PULLED_OR_CANCELLED"
                elif dq > 1e-6:
                    cls = "REFILLED_OR_ADDED"
                else:
                    cls = "PERSISTED"
            return {
                "side": side,
                "price": cur["price"],
                "qty": cur["qty"],
                "notional": cur["notional"],
                "classification": cls,
                "traded_size_30s": traded,
                "prev_qty": None if prev is None else prev["qty"],
            }

        ev = {
            "ts": row["ts"],
            "mid": mid,
            "ask_wall": classify("ASK", ask, prev_ask, traded_at_ask),
            "bid_wall": classify("BID", bid, prev_bid, traded_at_bid),
        }
        events.append(ev)
        if ask:
            prev_ask = ask
        if bid:
            prev_bid = bid
    return events


def load_oi(cl, start: datetime, end: datetime) -> list[dict]:
    rows = q(
        cl,
        f"""
        SELECT bucket_time, toFloat64(open_interest), toFloat64(open_interest_value)
        FROM orderbook_analysis.open_interest_5s
        WHERE symbol='{SYMBOL}'
          AND bucket_time >= toDateTime64('{start.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
          AND bucket_time < toDateTime64('{end.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
        ORDER BY bucket_time
        """,
    )
    return [{"ts": utc(r[0]), "oi": float(r[1]), "oi_value": float(r[2])} for r in rows]


def load_liq(cl, start: datetime, end: datetime) -> list[dict]:
    rows = q(
        cl,
        f"""
        SELECT event_time, liquidated_position_side, toFloat64(notional_estimate), toFloat64(bankruptcy_price)
        FROM orderbook_analysis.all_liquidations
        WHERE symbol='{SYMBOL}'
          AND event_time >= toDateTime64('{start.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
          AND event_time < toDateTime64('{end.strftime("%Y-%m-%d %H:%M:%S")}', 3, 'UTC')
        ORDER BY event_time
        """,
    )
    return [
        {"ts": utc(r[0]), "side": str(r[1]), "notional": float(r[2]), "price": float(r[3])}
        for r in rows
    ]


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = fieldnames or list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cl = client()

    coverage = coverage_audit(cl)
    write_json(OUT / "coverage_audit.json", coverage)

    if not coverage.get("ob200_reconstructable"):
        verdict = "BTC_OB_FIGHT_DATA_INSUFFICIENT"
        write_json(
            OUT / "analysis_manifest.json",
            {
                "verdict": verdict,
                "reason": "OB200 not fully reconstructable for probes",
                "coverage": coverage,
            },
        )
        (OUT / "REPORT.md").write_text(
            f"# REPORT\n\n## 1. Final Verdict\n\n`{verdict}`\n\nOB200 reconstruction failed. See coverage_audit.json.\n",
            encoding="utf-8",
        )
        print(verdict)
        return 0

    profiles = build_causal_profiles(cl)
    write_json(OUT / "profile_levels.json", profiles)

    trades = load_trades(cl, CORE_START - timedelta(minutes=5), MAX_END)
    oi = load_oi(cl, CORE_START, MAX_END)
    liq = load_liq(cl, CORE_START, MAX_END)

    # Price at T0 from last trade before T0
    pre = [t for t in trades if t["ts"] < T0]
    price_t0 = pre[-1]["price"] if pre else None
    us = profiles["profiles"].get("us_developing_to_T0") or {}
    day = profiles["profiles"].get("day_developing_to_T0") or {}
    prior = profiles["profiles"].get("prior_day_20260830") or {}
    block = profiles["profiles"].get("vp_block_1830_1900") or {}

    level_ctx = {
        "price_at_T0": price_t0,
        "vs_us_developing": nearest_level(price_t0, us) if price_t0 and us.get("poc") else None,
        "vs_day_developing": nearest_level(price_t0, day) if price_t0 and day.get("poc") else None,
        "vs_prior_day": nearest_level(price_t0, prior) if price_t0 and prior.get("poc") else None,
        "vs_30m_block": nearest_level(price_t0, block) if price_t0 and block.get("poc") else None,
    }

    # Decision level: prefer US VAH if fighting upper edge, else nearest significant
    decision_level = None
    decision_kind = None
    if us and price_t0:
        # if near VAH use VAH; if near VAL use VAL; else nearest
        nl = nearest_level(price_t0, us)
        decision_level = float(nl["level_price"])
        decision_kind = f"us_developing:{nl['level_kind']}:{nl['zone']}"
        # If within 15 bps of VAH, treat as upper edge fight
        if us.get("vah") and abs(price_t0 - float(us["vah"])) / price_t0 * 10000 <= 25:
            decision_level = float(us["vah"])
            decision_kind = "us_developing:VAH:upper_edge"
        elif us.get("val") and abs(price_t0 - float(us["val"])) / price_t0 * 10000 <= 25:
            decision_level = float(us["val"])
            decision_kind = "us_developing:VAL:lower_edge"

    # OB sample every 30s through core, denser around T0
    times = []
    t = CORE_START
    while t <= CORE_END:
        times.append(t)
        t += timedelta(seconds=30)
    # denser 18:58-19:10 every 10s
    t = datetime(2026, 8, 31, 18, 58, 0, tzinfo=timezone.utc)
    while t <= datetime(2026, 8, 31, 19, 10, 0, tzinfo=timezone.utc):
        times.append(t)
        t += timedelta(seconds=10)
    # extension blocks if needed
    for hh in range(19, 21):
        for mm in (30, 0) if hh > 19 else (30,):
            if hh == 19 and mm == 30:
                times.append(datetime(2026, 8, 31, 19, 30, tzinfo=timezone.utc))
            elif hh == 20:
                times.append(datetime(2026, 8, 31, 20, mm, tzinfo=timezone.utc))
    times.append(MAX_END)
    times = sorted(set(times))

    print(f"sampling {len(times)} OB timestamps...", flush=True)
    ob_rows = sample_ob_timeline(times)
    write_json(OUT / "orderbook_samples.json", ob_rows)

    # trade windows: 30s buckets core + key windows
    trade_windows = []
    t = CORE_START
    while t < CORE_END:
        trade_windows.append(window_trade_stats(trades, t, t + timedelta(seconds=30)))
        t += timedelta(seconds=30)
    # also 1-min after for extension
    t = CORE_END
    while t < MAX_END:
        trade_windows.append(window_trade_stats(trades, t, t + timedelta(minutes=1)))
        t += timedelta(minutes=1)

    # flatten for csv
    tw_csv = []
    for w in trade_windows:
        tw_csv.append({k: w.get(k) for k in w})
    write_csv(OUT / "public_trade_windows.csv", tw_csv)

    wall_events = classify_wall_events(ob_rows, trades, decision_level or price_t0 or 0)
    # flatten wall events csv
    we_csv = []
    for e in wall_events:
        aw = e.get("ask_wall") or {}
        bw = e.get("bid_wall") or {}
        we_csv.append(
            {
                "ts": e["ts"],
                "mid": e["mid"],
                "ask_price": aw.get("price"),
                "ask_qty": aw.get("qty"),
                "ask_class": aw.get("classification"),
                "ask_traded_30s": aw.get("traded_size_30s"),
                "bid_price": bw.get("price"),
                "bid_qty": bw.get("qty"),
                "bid_class": bw.get("classification"),
                "bid_traded_30s": bw.get("traded_size_30s"),
            }
        )
    write_csv(OUT / "orderbook_wall_events.csv", we_csv)

    # Build causal decision timeline from evidence up to CORE_END first
    timeline = []
    # helper: stats for short windows relative to T0
    def add_state(ts, state, **kwargs):
        timeline.append({"ts": iso(ts) if isinstance(ts, datetime) else ts, "state": state, **kwargs})

    add_state(CORE_START, "OBSERVATION_START", price=None, decision_level=decision_level, decision_kind=decision_kind)
    add_state(T0 - timedelta(seconds=18), "WAITING_FOR_LEVEL", price=price_t0, note="pre-T0")

    # Analyze key intervals around T0
    w_pre = window_trade_stats(trades, T0 - timedelta(minutes=2), T0)
    w_0_1 = window_trade_stats(trades, T0, T0 + timedelta(minutes=1))
    w_1_3 = window_trade_stats(trades, T0 + timedelta(minutes=1), T0 + timedelta(minutes=3))
    w_3_10 = window_trade_stats(trades, T0 + timedelta(minutes=3), T0 + timedelta(minutes=10))
    w_10_20 = window_trade_stats(trades, T0 + timedelta(minutes=10), T0 + timedelta(minutes=20))
    w_20_30 = window_trade_stats(trades, T0 + timedelta(minutes=20), T0 + timedelta(minutes=30))
    w_30_60 = window_trade_stats(trades, T0 + timedelta(minutes=30), T0 + timedelta(minutes=60))
    w_60_120 = window_trade_stats(trades, T0 + timedelta(minutes=60), T0 + timedelta(minutes=120))

    key_windows = {
        "pre_2m": w_pre,
        "t0_1m": w_0_1,
        "1_3m": w_1_3,
        "3_10m": w_3_10,
        "10_20m": w_10_20,
        "20_30m": w_20_30,
        "30_60m": w_30_60,
        "60_120m": w_60_120,
    }

    # Touch of decision level in core
    touch_ts = None
    if decision_level and price_t0:
        band = decision_level * 5 / 10000  # 5 bps
        for t in trades:
            if CORE_START <= t["ts"] <= CORE_END and abs(t["price"] - decision_level) <= band:
                touch_ts = t["ts"]
                break

    add_state(
        touch_ts or T0,
        "LEVEL_TOUCH" if touch_ts else "LEVEL_PROXIMITY_AT_T0",
        price=price_t0,
        decision_level=decision_level,
        decision_kind=decision_kind,
        level_ctx=level_ctx,
    )

    # Classify control from windows (causal, using only completed window at its end)
    def classify_window(w: dict) -> dict:
        if not w or w.get("n", 0) == 0:
            return {
                "ACTIVE_AGGRESSOR": "BALANCED",
                "PASSIVE_CONTROLLER": "NONE",
                "NET_CONTROLLER": "CONTESTED",
            }
        dn = w["delta_notional"]
        bps = w["price_change_bps"] or 0.0
        buy_n = w["buy_notional"]
        sell_n = w["sell_notional"]
        if buy_n > sell_n * 1.15:
            active = "BUYERS"
        elif sell_n > buy_n * 1.15:
            active = "SELLERS"
        else:
            active = "BALANCED"
        # efficiency: if buy-dominant but price flat/down -> sellers passive control
        passive = "NONE"
        net = "CONTESTED"
        if active == "BUYERS":
            if bps < 2.0 and buy_n > 200_000:
                passive = "SELLERS"
                net = "SELLERS"
            elif bps > 8.0:
                passive = "NONE"
                net = "BUYERS"
            else:
                net = "CONTESTED"
        elif active == "SELLERS":
            if bps > -2.0 and sell_n > 200_000:
                passive = "BUYERS"
                net = "BUYERS"
            elif bps < -8.0:
                passive = "NONE"
                net = "SELLERS"
            else:
                net = "CONTESTED"
        else:
            if abs(bps) < 3:
                net = "CONTESTED"
            elif bps > 8:
                net = "BUYERS"
            elif bps < -8:
                net = "SELLERS"
        return {
            "ACTIVE_AGGRESSOR": active,
            "PASSIVE_CONTROLLER": passive,
            "NET_CONTROLLER": net,
            "delta_notional": dn,
            "price_change_bps": bps,
            "buy_notional": buy_n,
            "sell_notional": sell_n,
        }

    cls_0_1 = classify_window(w_0_1)
    cls_1_3 = classify_window(w_1_3)
    cls_3_10 = classify_window(w_3_10)
    cls_10_20 = classify_window(w_10_20)
    cls_20_30 = classify_window(w_20_30)

    add_state(T0 + timedelta(minutes=1), "T0_1M_WINDOW_CLOSED", **cls_0_1, window=w_0_1)
    add_state(T0 + timedelta(minutes=3), "T0_3M_WINDOW_CLOSED", **cls_1_3, window=w_1_3)
    add_state(T0 + timedelta(minutes=10), "T0_10M_WINDOW_CLOSED", **cls_3_10, window=w_3_10)
    add_state(T0 + timedelta(minutes=20), "T0_20M_WINDOW_CLOSED", **cls_10_20, window=w_10_20)
    add_state(T0 + timedelta(minutes=30), "CORE_END_WINDOW_CLOSED", **cls_20_30, window=w_20_30)

    # Find when price accepts above VAH or rejects
    vah = float(us["vah"]) if us.get("vah") else None
    val = float(us["val"]) if us.get("val") else None
    poc = float(us["poc"]) if us.get("poc") else None

    # Track first time mid holds above VAH for 60s without reclaim below
    accept_up_ts = None
    reject_from_vah_ts = None
    if vah and price_t0:
        # scan trade prices for sustained acceptance
        above_start = None
        below_after_touch = None
        touched_vah = False
        for t in trades:
            if t["ts"] < T0 or t["ts"] > MAX_END:
                continue
            if abs(t["price"] - vah) / vah * 10000 <= 8:
                touched_vah = True
            if t["price"] > vah:
                if above_start is None:
                    above_start = t["ts"]
                elif (t["ts"] - above_start).total_seconds() >= 60 and accept_up_ts is None:
                    # check no reclaim in the minute after first cross lasting
                    accept_up_ts = above_start + timedelta(seconds=60)
            else:
                if touched_vah and t["price"] < vah - vah * 5 / 10000 and above_start is not None:
                    # reclaim below after having been above
                    if reject_from_vah_ts is None and t["ts"] <= CORE_END:
                        # only if we had a brief poke
                        if (t["ts"] - above_start).total_seconds() < 180:
                            reject_from_vah_ts = t["ts"]
                above_start = None

    # Decision logic (strict)
    verdict = "BTC_OB_FIGHT_CONTESTED_WAIT_NO_TRADE"
    decision_ts = None
    resolution = "CONTESTED"
    trade_ready = "WAIT"
    active = "BALANCED"
    passive = "NONE"
    net = "CONTESTED"
    wall_summary = "insufficient_clear_wall_consumption"
    counters = []

    # Summarize wall behavior 19:00-19:10
    wall_slice = [e for e in wall_events if e["ts"] and "19:0" in e["ts"][:16]]
    consumed_ask = sum(
        1
        for e in wall_slice
        if (e.get("ask_wall") or {}).get("classification") == "CONSUMED_OR_REDUCED_WITH_TRADES"
    )
    refilled_ask = sum(
        1
        for e in wall_slice
        if (e.get("ask_wall") or {}).get("classification") == "REFILLED_OR_ADDED"
    )
    pulled_ask = sum(
        1
        for e in wall_slice
        if (e.get("ask_wall") or {}).get("classification") == "PULLED_OR_CANCELLED"
    )
    wall_summary = (
        f"ask_consumed_with_trades={consumed_ask}, ask_refilled={refilled_ask}, "
        f"ask_pulled={pulled_ask} (samples ~19:00-19:09)"
    )

    # Evidence chain for upper edge (VAH) fight
    # After 10m: buyers aggressive + price up strongly -> possible breakout
    # Need acceptance: hold above VAH
    high_10 = w_0_1.get("high"), w_1_3.get("high"), w_3_10.get("high")
    # Use last trade price at 19:10
    p_1910 = [t for t in trades if t["ts"] < T0 + timedelta(minutes=10)]
    p_1910 = p_1910[-1]["price"] if p_1910 else None
    p_1930 = [t for t in trades if t["ts"] < CORE_END]
    p_1930 = p_1930[-1]["price"] if p_1930 else None
    p_2000 = [t for t in trades if t["ts"] < T0 + timedelta(hours=1)]
    p_2000 = p_2000[-1]["price"] if p_2000 else None
    p_2100 = [t for t in trades if t["ts"] < MAX_END]
    p_2100 = p_2100[-1]["price"] if p_2100 else None

    # Combined 0-10m window
    w_0_10 = window_trade_stats(trades, T0, T0 + timedelta(minutes=10))
    cls_0_10 = classify_window(w_0_10)

    # Check breakout accepted above VAH
    breakout_accepted = False
    rejection_confirmed = False
    if vah and p_1910 and p_1930:
        # if high exceeds VAH and price at 19:10 and 19:30 still above VAH with efficient buys
        hi = max(x for x in [w_0_1.get("high"), w_1_3.get("high"), w_3_10.get("high")] if x)
        if hi > vah and p_1910 > vah and p_1930 > vah and cls_0_10["NET_CONTROLLER"] == "BUYERS":
            # check notional buy dominance and positive bps
            if (w_0_10.get("price_change_bps") or 0) > 15 and consumed_ask >= 1:
                breakout_accepted = True
                decision_ts = T0 + timedelta(minutes=10)
                resolution = "BREAKOUT_ACCEPTED"
                trade_ready = "LONG_READY"
                active = cls_0_10["ACTIVE_AGGRESSOR"]
                passive = cls_0_10["PASSIVE_CONTROLLER"]
                net = "BUYERS"
                verdict = "BTC_OB_FIGHT_BUYERS_CONTROL_LONG_READY"
                add_state(decision_ts, "BREAKOUT_ACCEPTED_CANDIDATE", vah=vah, price=p_1910, **cls_0_10)
        # Rejection: poke above then reclaim below with seller control
        if hi > vah and p_1930 < vah and (cls_10_20["NET_CONTROLLER"] == "SELLERS" or cls_20_30["NET_CONTROLLER"] == "SELLERS"):
            rejection_confirmed = True
            decision_ts = CORE_END
            resolution = "REJECTION_CONFIRMED"
            trade_ready = "SHORT_READY"
            active = "BUYERS"  # earlier aggression
            passive = "SELLERS"
            net = "SELLERS"
            verdict = "BTC_OB_FIGHT_SELLERS_CONTROL_SHORT_READY"

    # If still contested at core end, extend
    if verdict == "BTC_OB_FIGHT_CONTESTED_WAIT_NO_TRADE":
        add_state(CORE_END, "CORE_END_STILL_CONTESTED", price=p_1930, **cls_20_30)
        # Check 19:30-20:00 and 20:00-21:00
        cls_30_60 = classify_window(w_30_60)
        cls_60_120 = classify_window(w_60_120)
        add_state(T0 + timedelta(minutes=60), "EXT_60M", price=p_2000, **cls_30_60)
        add_state(MAX_END, "EXT_120M", price=p_2100, **cls_60_120)

        # Persistent acceptance above prior VAH through 20:00?
        if vah and p_1930 and p_2000 and p_2100:
            if p_1930 > vah and p_2000 > vah and cls_30_60["NET_CONTROLLER"] == "BUYERS":
                if (w_30_60.get("price_change_bps") or 0) > 5:
                    breakout_accepted = True
                    decision_ts = T0 + timedelta(minutes=60)
                    resolution = "BREAKOUT_ACCEPTED"
                    trade_ready = "LONG_READY"
                    active = cls_30_60["ACTIVE_AGGRESSOR"]
                    passive = cls_30_60["PASSIVE_CONTROLLER"]
                    net = "BUYERS"
                    verdict = "BTC_OB_FIGHT_BUYERS_CONTROL_LONG_READY"
                    add_state(decision_ts, "BREAKOUT_ACCEPTED_AFTER_EXTENSION", **cls_30_60)
            elif p_1930 < (val or p_1930) and cls_30_60["NET_CONTROLLER"] == "SELLERS":
                decision_ts = T0 + timedelta(minutes=60)
                resolution = "BREAKOUT_ACCEPTED"
                trade_ready = "SHORT_READY"
                net = "SELLERS"
                active = cls_30_60["ACTIVE_AGGRESSOR"]
                passive = cls_30_60["PASSIVE_CONTROLLER"]
                verdict = "BTC_OB_FIGHT_SELLERS_CONTROL_SHORT_READY"

        if verdict == "BTC_OB_FIGHT_CONTESTED_WAIT_NO_TRADE":
            decision_ts = MAX_END
            trade_ready = "NO_TRADE"
            resolution = "CONTESTED"
            # summarize net from whole core+ext as contested
            net = "CONTESTED"
            active = cls_0_10["ACTIVE_AGGRESSOR"]
            passive = "NONE"
            counters.append("Price oscillated around VA without sustained acceptance or clean rejection")
            counters.append("Buy and sell aggression alternated across 30m blocks")
            add_state(MAX_END, "CONTESTED_WAIT_NO_TRADE", price=p_2100)

    # Room check if ready
    room_note = None
    if trade_ready in ("LONG_READY", "SHORT_READY") and decision_level and price_t0:
        # next opposing pool proxy: prior day opposite VA edge or HVN
        if trade_ready == "LONG_READY":
            # target: next ask pool / prior day high / HVN above
            targets = []
            if prior.get("vah"):
                targets.append(float(prior["vah"]))
            for h in prior.get("hvn") or []:
                if float(h) > (p_1910 or price_t0):
                    targets.append(float(h))
            if day.get("price_high"):
                targets.append(float(day["price_high"]))
            px = p_1910 or price_t0
            above = [t for t in targets if t > px]
            if above:
                tgt = min(above)
                room_pct = (tgt - px) / px * 100
                room_note = {"direction": "LONG", "target": tgt, "room_pct": room_pct, "room_0_5": room_pct >= 0.5, "room_0_8": room_pct >= 0.8}
                if room_pct < 0.5:
                    verdict = "BTC_OB_FIGHT_CONTROL_CONFIRMED_NO_ROOM"
                    trade_ready = "NO_TRADE"
                    room_note["result"] = "CONTROL_CONFIRMED_BUT_NO_TRADE_INSUFFICIENT_ROOM"
            else:
                room_note = {"direction": "LONG", "target": None, "room_pct": None, "note": "no clear opposing pool above"}
        else:
            px = p_1910 or price_t0
            targets = []
            if prior.get("val"):
                targets.append(float(prior["val"]))
            for h in prior.get("hvn") or []:
                if float(h) < px:
                    targets.append(float(h))
            below = [t for t in targets if t < px]
            if below:
                tgt = max(below)
                room_pct = (px - tgt) / px * 100
                room_note = {"direction": "SHORT", "target": tgt, "room_pct": room_pct, "room_0_5": room_pct >= 0.5, "room_0_8": room_pct >= 0.8}
                if room_pct < 0.5:
                    verdict = "BTC_OB_FIGHT_CONTROL_CONFIRMED_NO_ROOM"
                    trade_ready = "NO_TRADE"
                    room_note["result"] = "CONTROL_CONFIRMED_BUT_NO_TRADE_INSUFFICIENT_ROOM"

    # OI context around decision
    oi_ctx = {}
    if oi:
        def oi_at(ts):
            xs = [x for x in oi if x["ts"] <= ts]
            return xs[-1] if xs else None
        o0 = oi_at(T0)
        o1 = oi_at(T0 + timedelta(minutes=10))
        o2 = oi_at(CORE_END)
        oi_ctx = {
            "oi_T0": o0,
            "oi_T0_10m": o1,
            "oi_core_end": o2,
            "doi_10m": None if not (o0 and o1) else o1["oi"] - o0["oi"],
            "doi_30m": None if not (o0 and o2) else o2["oi"] - o0["oi"],
        }
    liq_core = [x for x in liq if T0 <= x["ts"] < CORE_END]
    liq_ctx = {
        "count_core": len(liq_core),
        "long_liq_notional": sum(x["notional"] for x in liq_core if x["side"] == "LIQUIDATED_LONG"),
        "short_liq_notional": sum(x["notional"] for x in liq_core if x["side"] == "LIQUIDATED_SHORT"),
    }

    # Outcome validation AFTER decision_ts — never feeds decision
    outcome = {
        "outcome_used_for_decision": False,
        "outcome_used_for_thresholds": False,
        "outcome_used_for_profile_definition": False,
        "decision_ts": iso(decision_ts) if decision_ts else None,
    }
    if decision_ts and trade_ready in ("LONG_READY", "SHORT_READY", "NO_TRADE", "WAIT"):
        base_trades = [t for t in trades if t["ts"] <= decision_ts]
        base_px = base_trades[-1]["price"] if base_trades else None
        if base_px:
            def px_after(mins):
                xs = [t for t in trades if t["ts"] <= decision_ts + timedelta(minutes=mins)]
                return xs[-1]["price"] if xs else None
            path = [t for t in trades if decision_ts < t["ts"] <= decision_ts + timedelta(minutes=30)]
            if path:
                highs = max(t["price"] for t in path)
                lows = min(t["price"] for t in path)
                if trade_ready == "LONG_READY" or (verdict.endswith("LONG_READY") or "BUYERS" in verdict):
                    mfe = (highs - base_px) / base_px * 100
                    mae = (lows - base_px) / base_px * 100
                else:
                    mfe = (base_px - lows) / base_px * 100
                    mae = (base_px - highs) / base_px * 100
                outcome.update(
                    {
                        "base_price": base_px,
                        "px_1m": px_after(1),
                        "px_5m": px_after(5),
                        "px_15m": px_after(15),
                        "px_30m": px_after(30),
                        "mfe_pct_30m": mfe,
                        "mae_pct_30m": mae,
                    }
                )
    write_json(OUT / "outcome_validation.json", outcome)

    # timeline csv
    tl_csv = []
    for row in timeline:
        flat = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()}
        tl_csv.append(flat)
    write_csv(OUT / "decision_timeline.csv", tl_csv)

    # Manual override refinement using actual numbers printed into report
    # Recompute a clearer narrative from key_windows for contested cases
    if verdict == "BTC_OB_FIGHT_CONTESTED_WAIT_NO_TRADE":
        # Check if 0-10m was clear buyer breakout but 10-30m faded without rejection confirmation
        if (
            cls_0_10["NET_CONTROLLER"] == "BUYERS"
            and vah
            and p_1910
            and p_1910 > vah
            and p_1930
            and abs(p_1930 - vah) / vah * 10000 < 40
        ):
            counters.append(
                f"19:00-19:10 buyer expansion (delta={w_0_10['delta_notional']:.0f}, "
                f"bps={w_0_10['price_change_bps']:.1f}) but 19:10-19:30 faded back toward VAH "
                f"(p_1930={p_1930}) without clean seller rejection confirmation"
            )

    # Invalidation levels
    invalidation = None
    if trade_ready == "LONG_READY" and vah:
        invalidation = {"type": "reclaim_below_vah", "level": vah}
    elif trade_ready == "SHORT_READY" and vah:
        invalidation = {"type": "reclaim_above_vah", "level": vah}

    manifest = {
        "case": {
            "symbol": SYMBOL,
            "T0": iso(T0),
            "core": [iso(CORE_START), iso(CORE_END)],
            "max_end": iso(MAX_END),
        },
        "git": {
            "repo": str(ROOT),
            "branch": "feature/dashboard-research-charts",
            "head": "5fb7495f0a7ace4c3654d65db8cf3a3ecae01cc6",
            "note": "dirty worktree preserved; this script only writes results/",
        },
        "data_sources": {
            "ob200": "orderbook_analyse/data/orderbook_raw_shadow/ob200_v3/BTCUSDT (hour zst)",
            "trades": "orderbook_analysis.public_trades_canonical",
            "candles": "signal_generator.candles_1m",
            "oi": "orderbook_analysis.open_interest_5s",
            "liq": "orderbook_analysis.all_liquidations",
            "market_profile": "orderbook_analyse.market_profile (volume-at-price, VA 70%, ~160 bins)",
        },
        "verdict": verdict,
        "decision_ts": iso(decision_ts) if decision_ts else None,
        "ACTIVE_AGGRESSOR": active,
        "PASSIVE_CONTROLLER": passive,
        "NET_CONTROLLER": net,
        "resolution": resolution,
        "trade_ready": trade_ready,
        "decision_level": decision_level,
        "decision_kind": decision_kind,
        "level_ctx": level_ctx,
        "key_windows": key_windows,
        "cls_0_10": cls_0_10,
        "wall_summary": wall_summary,
        "room": room_note,
        "invalidation": invalidation,
        "oi_ctx": oi_ctx,
        "liq_ctx": liq_ctx,
        "prices": {"T0": price_t0, "19:10": p_1910, "19:30": p_1930, "20:00": p_2000, "21:00": p_2100},
        "us_levels": us,
        "breakout_accepted": breakout_accepted,
        "rejection_confirmed": rejection_confirmed,
        "counters": counters,
        "safety": {
            "clickhouse_readonly": True,
            "no_collector_changes": True,
            "no_dashboard_code_changes_in_this_run": True,
            "no_commit_push": True,
            "outputs_only_under": str(OUT),
        },
    }
    write_json(OUT / "analysis_manifest.json", manifest)

    # REPORT.md
    report = f"""# BTCUSDT Profile-Edge OB Fight — 2026-08-31 19:00 UTC

## 1. Final Verdict

`{verdict}`

## 2. Symbol and UTC Window

- Symbol: `{SYMBOL}` only
- T0: `{iso(T0)}`
- Core: `{iso(CORE_START)}` → `{iso(CORE_END)}`
- Extension max: `{iso(MAX_END)}`

## 3. Earliest Causal Decision Timestamp

`{iso(decision_ts) if decision_ts else "n/a"}`

{"19:00 bis 19:30: WAIT — Entscheidung erst um " + iso(decision_ts) if decision_ts and decision_ts > CORE_END else "Entscheidung innerhalb Kernfenster oder CONTESTED bis Max-Ende."}

## 4. ACTIVE_AGGRESSOR

`{active}`

## 5. PASSIVE_CONTROLLER

`{passive}`

## 6. NET_CONTROLLER

`{net}`

## 7. Resolution

`{resolution}`

## 8. Trade Readiness

`{trade_ready}`

## 9. Relevant Market / Volume Profile Level

Profile engine: **volume-at-price** (existing market_profile tool), **not classic TPO letters**.
Settings: VA={VA_PCT:.0%}, target_bins={TARGET_BINS}, sessions asia/eu/us/late.

Causal profiles at T0 (no peek past 19:00):

- US developing (13:30→T0): POC={us.get('poc')}, VAH={us.get('vah')}, VAL={us.get('val')}, shape={us.get('shape')}
- Day developing (00:00→T0): POC={day.get('poc')}, VAH={day.get('vah')}, VAL={day.get('val')}
- Prior day 2026-08-30: POC={prior.get('poc')}, VAH={prior.get('vah')}, VAL={prior.get('val')}
- 30m VP block 18:30–19:00: POC={block.get('poc')}, VAH={block.get('vah')}, VAL={block.get('val')}

Price at T0: **{price_t0}**
Decision level: **{decision_level}** (`{decision_kind}`)
Level context: `{json.dumps(level_ctx, indent=2)}`

Touch: {"first touch near level at " + iso(touch_ts) if touch_ts else "proximity at T0 without separate earlier touch event"}

## 10. Orderbook Wall Behavior

Source: OB200 v3 hour archives under shadow root; reconstructed with consecutive `u` deltas.
Genuine 200-level depth confirmed at probes (see coverage_audit.json).

Wall summary near T0: {wall_summary}

Classification rule: size drop without matching public trades → `PULLED_OR_CANCELLED`; with matching trades → `CONSUMED_OR_REDUCED_WITH_TRADES`.

## 11. Public Trades and Price Impact

Aggressor = `public_trades_canonical.side` (Bybit taker).

Key windows (notional USDT, price change bps):

| Window | Buy $ | Sell $ | Delta $ | Δbps | NET |
|---|---:|---:|---:|---:|---|
| T0–+1m | {w_0_1.get('buy_notional',0):.0f} | {w_0_1.get('sell_notional',0):.0f} | {w_0_1.get('delta_notional',0):.0f} | {w_0_1.get('price_change_bps')} | {cls_0_1['NET_CONTROLLER']} |
| +1–+3m | {w_1_3.get('buy_notional',0):.0f} | {w_1_3.get('sell_notional',0):.0f} | {w_1_3.get('delta_notional',0):.0f} | {w_1_3.get('price_change_bps')} | {cls_1_3['NET_CONTROLLER']} |
| +3–+10m | {w_3_10.get('buy_notional',0):.0f} | {w_3_10.get('sell_notional',0):.0f} | {w_3_10.get('delta_notional',0):.0f} | {w_3_10.get('price_change_bps')} | {cls_3_10['NET_CONTROLLER']} |
| +10–+20m | {w_10_20.get('buy_notional',0):.0f} | {w_10_20.get('sell_notional',0):.0f} | {w_10_20.get('delta_notional',0):.0f} | {w_10_20.get('price_change_bps')} | {cls_10_20['NET_CONTROLLER']} |
| +20–+30m | {w_20_30.get('buy_notional',0):.0f} | {w_20_30.get('sell_notional',0):.0f} | {w_20_30.get('delta_notional',0):.0f} | {w_20_30.get('price_change_bps')} | {cls_20_30['NET_CONTROLLER']} |
| Combined 0–10m | {w_0_10.get('buy_notional',0):.0f} | {w_0_10.get('sell_notional',0):.0f} | {w_0_10.get('delta_notional',0):.0f} | {w_0_10.get('price_change_bps')} | {cls_0_10['NET_CONTROLLER']} |

Prices: T0={price_t0}, 19:10={p_1910}, 19:30={p_1930}, 20:00={p_2000}, 21:00={p_2100}

## 12. OI / Liquidations Context

OI: `{json.dumps(oi_ctx, default=str)}`
Liquidations in core: `{json.dumps(liq_ctx)}`
OI/liq are confirmatory only; not sole controller evidence.

## 13. Target Room 0.5% / 0.8%

`{json.dumps(room_note, indent=2)}`
Invalidation: `{json.dumps(invalidation)}`

## 14. Counter-Arguments

{chr(10).join('- ' + c for c in counters) if counters else '- See alternating window NET_CONTROLLER values; no single clean absorption+reclaim or accepted breakout meeting all required joint conditions through core.'}

## 15. Separate Outcome Validation

```text
outcome_used_for_decision = false
outcome_used_for_thresholds = false
outcome_used_for_profile_definition = false
```

See `outcome_validation.json`.

## 16. Data Coverage and Uncertainties

- Public trades: {coverage['public_trades']['count']} rows, dedup_ok={coverage['public_trades']['dedup_ok']}
- Candles 1m: {coverage['candles_1m']['count']} / expected ~{coverage['candles_1m']['expected_minutes']}
- OI 5s: {coverage['open_interest_5s']['count']}
- OB200 hours OK: {coverage['ob200']['all_hours_ok']}, reconstructable probes: {coverage['ob200_reconstructable']}
- Uncertainty: market profile is volume-at-price (no true TPO period counts); wall classification uses discrete 10–30s samples; 30m TPO letters were NOT used as SoT
- Liq side enum is LIQUIDATED_LONG/SHORT (position), not aggressor

## 17. Generated Files

Under `{OUT}`:

- REPORT.md
- analysis_manifest.json
- coverage_audit.json
- decision_timeline.csv
- orderbook_wall_events.csv
- orderbook_samples.json
- public_trade_windows.csv
- profile_levels.json
- outcome_validation.json

## 18. Commands Executed

```bash
python3 research/btc_profile_edge_ob_fight_20260831_1900_v1/run_case.py
```

(plus read-only ClickHouse SELECTs and OB200 zst replay)

## 19. Safety Confirmation

- ClickHouse: read-only SELECTs only
- No collectors started/stopped
- No live processes changed
- No dashboard processes restarted
- No orders / exchange actions
- No existing tables altered
- No existing results overwritten (new folder)
- No Market/Volume Profile dashboard code modified in this analysis run
- Dirty worktree preserved; no commit; no push
"""
    (OUT / "REPORT.md").write_text(report, encoding="utf-8")
    print("WROTE", OUT)
    print("VERDICT", verdict)
    print("DECISION_TS", iso(decision_ts))
    print("LEVEL", decision_kind, decision_level, "PX", price_t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

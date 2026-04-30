#!/usr/bin/env python3
"""
Bybit hedge-ready coin scanner.

Scores USDT perpetuals on fast moves, ATR, cycle frequency,
orderbook depth, tight spread, and simulated 100 USDT slippage.
Falls back to relaxed thresholds when initial filters yield nothing.
"""

import numpy as np
import asyncio
import aiohttp
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

BASE_URL = "https://api.bybit.com"
INTERVAL = "1"
CANDLE_LIMIT = 120
TOP_N = 15
TEST_NOTIONAL_USDT = 100.0
VERBOSE = False

MIN_24H_VOLUME = 5_000_000
MAX_SPREAD_PCT = 0.25
MIN_DEPTH_USD = 70_000
MAX_SLIPPAGE_PCT = 0.35

MIN_MOVE_1M = 0.7
MIN_MOVE_5M = 0.9
VOL_SPIKE_FLOOR = 1.5

FALLBACK_PARAMS = {
    "move_1m_floor": MIN_MOVE_1M * 0.5,
    "move_5m_floor": MIN_MOVE_5M * 0.5,
    "depth_floor": 49_000,
    "vol_spike_floor": 1.0,
    "min_volume": 3_000_000,
    "max_spread_pct": 0.35,
    "max_slippage_pct": 0.50,
}

BEST_COIN_FILE = Path("logs") / "best_coin.json"


def _write_best_coin_atomic(best_coin: dict[str, Any]) -> None:
    try:
        BEST_COIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = BEST_COIN_FILE.with_suffix(".tmp")
        payload = json.dumps(best_coin, ensure_ascii=False, indent=2)
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, BEST_COIN_FILE)
        print(f"🔥 Best coin written to {BEST_COIN_FILE}")
    except Exception as exc:
        print(f"⚠️ Failed to write best coin file: {exc}")



async def fetch_json(session, url):
    async with session.get(url, timeout=10) as r:
        return await r.json()


async def fetch_symbols(session):
    url = f"{BASE_URL}/v5/market/instruments-info?category=linear"
    data = await fetch_json(session, url)
    return [
        s["symbol"]
        for s in data["result"]["list"]
        if s["quoteCoin"] == "USDT" and s["status"] == "Trading"
    ]


async def fetch_ticker(session, symbol):
    url = f"{BASE_URL}/v5/market/tickers?category=linear&symbol={symbol}"
    data = await fetch_json(session, url)
    lst = data["result"]["list"]
    return lst[0] if lst else None


async def fetch_klines(session, symbol):
    url = (
        f"{BASE_URL}/v5/market/kline"
        f"?category=linear&symbol={symbol}"
        f"&interval={INTERVAL}&limit={CANDLE_LIMIT}"
    )
    data = await fetch_json(session, url)
    return data.get("result", {}).get("list")


async def fetch_orderbook(session, symbol):
    url = f"{BASE_URL}/v5/market/orderbook?category=linear&symbol={symbol}&limit=50"
    data = await fetch_json(session, url)
    return data.get("result")


def analyze_orderbook(ob):
    bids = [(float(p), float(q)) for p, q in ob["b"]]
    asks = [(float(p), float(q)) for p, q in ob["a"]]
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2
    spread_pct = (best_ask - best_bid) / (mid or 1) * 100
    bid_depth = sum(p * q for p, q in bids if p >= mid * 0.995)
    ask_depth = sum(p * q for p, q in asks if p <= mid * 1.005)
    depth = bid_depth + ask_depth
    return spread_pct, depth, mid, bids, asks


def compute_slippage(levels, notional, mid):
    remaining = notional
    cost = 0.0
    filled_qty = 0.0
    for price, qty in levels:
        level_value = price * qty
        if level_value <= 0:
            continue
        spend = min(remaining, level_value)
        size = spend / price
        cost += spend
        filled_qty += size
        remaining -= spend
        if remaining <= 1e-9:
            break
    if filled_qty == 0 or remaining > 1e-6:
        return 999.0
    avg_price = cost / filled_qty
    return abs(avg_price - mid) / (mid or 1) * 100


def analyze_klines(klines):
    ordered = sorted(klines, key=lambda k: int(k[0]))
    closes = np.array([float(k[4]) for k in ordered])
    highs = np.array([float(k[2]) for k in ordered])
    lows = np.array([float(k[3]) for k in ordered])
    volumes = np.array([float(k[5]) for k in ordered])
    move_1m = (closes[-1] - closes[-2]) / (closes[-2] or 1) * 100
    move_5m = (closes[-1] - closes[-6]) / (closes[-6] or 1) * 100
    recent = closes[-60:]
    move_60m = (recent.max() - recent.min()) / recent.mean() * 100
    tr = highs - lows
    atr_pct = np.mean(tr / closes) * 100
    base_vol = np.mean(volumes[-21:-1]) if len(volumes) > 21 else np.mean(volumes[:-1])
    vol_spike = volumes[-1] / (base_vol + 1e-9)
    cycle_count = 0
    swing_price = closes[0]
    for price in closes[1:]:
        if price >= swing_price * 1.005 or price <= swing_price * 0.995:
            cycle_count += 1
            swing_price = price
    return move_1m, move_5m, move_60m, atr_pct, vol_spike, cycle_count


def compute_score(metrics, params):
    move_1m_component = max(0.0, abs(metrics["move_1m"]) - params["move_1m_floor"])
    move_5m_component = max(0.0, abs(metrics["move_5m"]) - params["move_5m_floor"])
    vol_component = max(0.0, metrics["vol_spike"] - params["vol_spike_floor"])
    max_slippage = max(metrics["simulated_buy_slippage"], metrics["simulated_sell_slippage"])
    depth_score = min(metrics["depth"] / 1_000_000, 1.5)
    capped_cycle = min(metrics["cycle_count"], 15)
    metrics["cappedCycle"] = capped_cycle
    has_movement = (
        abs(metrics["move_1m"]) >= 0.10
        or abs(metrics["move_5m"]) >= 0.35
        or metrics["move_60m"] >= 1.0
    )
    if not has_movement:
        vol_component = 0.0
    base_score = (
        move_1m_component * 5
        + move_5m_component * 8
        + metrics["move_60m"] * 2
        + metrics["atr_pct"] * 3
        + vol_component * 2
        + capped_cycle * 0.5
        + depth_score
        - metrics["spread_pct"] * 30
        - max_slippage * 50
    )
    if metrics["move_60m"] < 0.5 and metrics["atr_pct"] < 0.05 and metrics["cycle_count"] == 0:
        base_score -= 50
    if abs(metrics["move_1m"]) < 0.05:
        base_score -= 10
    return base_score


async def evaluate_symbol(session, symbol, params):
    ticker = await fetch_ticker(session, symbol)
    if not ticker:
        return None
    volume = float(ticker["turnover24h"])
    min_volume = params.get("min_volume", MIN_24H_VOLUME)
    if volume < min_volume:
        return None
    klines = await fetch_klines(session, symbol)
    if not klines or len(klines) < 60:
        return None
    move_1m, move_5m, move_60m, atr_pct, vol_spike, cycle_count = analyze_klines(klines)
    orderbook = await fetch_orderbook(session, symbol)
    if not orderbook:
        return None
    spread_pct, depth, mid, bids, asks = analyze_orderbook(orderbook)
    max_spread = params.get("max_spread_pct", MAX_SPREAD_PCT)
    if spread_pct > max_spread or depth < params["depth_floor"]:
        return None
    buy_slippage = compute_slippage(asks, TEST_NOTIONAL_USDT, mid)
    sell_slippage = compute_slippage(bids, TEST_NOTIONAL_USDT, mid)
    max_slip = params.get("max_slippage_pct", MAX_SLIPPAGE_PCT)
    if buy_slippage > max_slip or sell_slippage > max_slip:
        return None
    metrics = {
        "symbol": symbol,
        "move_1m": round(move_1m, 2),
        "move_5m": round(move_5m, 2),
        "move_60m": round(move_60m, 2),
        "atr_pct": round(atr_pct, 2),
        "vol_spike": round(vol_spike, 2),
        "cycle_count": cycle_count,
        "spread_pct": round(spread_pct, 4),
        "depth": round(depth, 2),
        "simulated_buy_slippage": round(buy_slippage, 2),
        "simulated_sell_slippage": round(sell_slippage, 2),
    }
    metrics["score"] = round(compute_score(metrics, params), 2)
    if metrics["score"] < 5:
        return None
    return metrics


async def run_scan(params):
    async with aiohttp.ClientSession() as session:
        print("🔍 Lade Bybit Symbole...")
        symbols = await fetch_symbols(session)
        print(f"📊 {len(symbols)} Coins gefunden\n")
        candidates = []
        for i, symbol in enumerate(symbols, 1):
            if i % 25 == 0 or i == len(symbols):
                print(f"[{i}/{len(symbols)}] scanning...")
            try:
                candidate = await evaluate_symbol(session, symbol, params)
                if candidate:
                    if VERBOSE:
                        print(f"✅ {symbol} | score={candidate['score']}")
                    candidates.append(candidate)
            except Exception as exc:
                if VERBOSE:
                    print(f"[{symbol}] Fehler: {exc}")
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:TOP_N]


async def scan():
    base_params = {
        "move_1m_floor": MIN_MOVE_1M,
        "move_5m_floor": MIN_MOVE_5M,
        "depth_floor": MIN_DEPTH_USD,
        "vol_spike_floor": VOL_SPIKE_FLOOR,
        "min_volume": MIN_24H_VOLUME,
        "max_spread_pct": MAX_SPREAD_PCT,
        "max_slippage_pct": MAX_SLIPPAGE_PCT,
    }
    results = await run_scan(base_params)
    if results:
        return results
    print("Fallback mode activated")
    return await run_scan(FALLBACK_PARAMS)


if __name__ == "__main__":
    start = time.time()
    coins = asyncio.run(scan())
    print("\n🔥 Beste hedge-taugliche Coins 🔥\n")
    for coin in coins:
        print(
            f"{coin['symbol']:12} | score:{coin['score']:>6} | "
            f"1m:{coin['move_1m']:>5}% | 5m:{coin['move_5m']:>5}% | 60m:{coin['move_60m']:>5}% | "
            f"ATR:{coin['atr_pct']:>4}% | volSpike:{coin['vol_spike']:>4} | cycle:{coin['cycle_count']:>3} | "
            f"cappedCycle:{coin.get('cappedCycle', 0):>2} | "
            f"spread:{coin['spread_pct']:>5}% | depth:{coin['depth']:>8,.0f} | "
            f"buySlip:{coin['simulated_buy_slippage']:>5}% | sellSlip:{coin['simulated_sell_slippage']:>5}%"
        )
    duration = time.time() - start
    print(f"\n⏱ Laufzeit: {duration:.1f}s")

    if coins:
        best = coins[0]
        best_coin_record = {
            "symbol": best["symbol"],
            "score": best["score"],
            "timestamp": datetime.now(timezone(timedelta(hours=3))).isoformat(),
            "source": "coin_scanner",
            "reason": "highest_score",
            "duration_s": round(duration, 1),
        }
        _write_best_coin_atomic(best_coin_record)
    else:
        if BEST_COIN_FILE.exists():
            print("⚠️ Kein Kandidat gefunden, best_coin.json bleibt unverändert")
        else:
            print("⚠️ Kein Kandidat gefunden und keine best_coin.json vorhanden")

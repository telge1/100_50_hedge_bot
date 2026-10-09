"""Classify 4h pools by role, not only by a late setup label.

This study script does not change the floor-chain files. It reads the same
candles and liquidity pools, then labels each pool that appears from 1 March
2026 onward. EMA200 and EMA59 are context only.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_ROWS = ROOT / "pool_relevance_4h_v1.csv"
OUT_METHOD = ROOT / "pool_relevance_4h_method_v1.json"
LOAD_START = datetime(2025, 12, 11, tzinfo=timezone.utc)
LOAD_END = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
REPORT_FROM = datetime(2026, 3, 1, tzinfo=timezone.utc)
SYMBOLS = ("ADAUSDT", "DOGEUSDT", "XRPUSDT")


def ema(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    k = 2.0 / (period + 1)
    value = sum(closes[:period]) / period
    out[period - 1] = value
    for index, price in enumerate(closes[period:], start=period):
        value = price * k + value * (1.0 - k)
        out[index] = float(value)
    return out


def load_symbol(symbol: str):
    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from pool_pattern.market import ensure_paths

    ensure_paths()
    from dashboard.research_charts.service import _candles_from_packed, lld_config_for_timeframe, load_candles
    from dashboard.research_charts.trp_import import load_trp
    from pool_scan.clock import available_at, bar_close, break_known_at

    packed = load_candles(symbol, "4h", start=int(LOAD_START.timestamp()), end=int(LOAD_END.timestamp()), limit=50000)
    if not packed.get("strict_complete_buckets"):
        raise SystemExit(f"candles not strict for {symbol}")
    candles = _candles_from_packed(packed)
    closes = [float(candle.close) for candle in candles]
    ema59 = ema(closes, 59)
    ema200 = ema(closes, 200)
    bars = []
    for index, candle in enumerate(candles):
        bars.append({
            "close_time": bar_close(candle.timestamp, "4h"),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "ema59": ema59[index],
            "ema200": ema200[index],
        })
    trp = load_trp()
    config = lld_config_for_timeframe(trp["LiquidityLocationConfig"].defaults(), "4h")
    result = trp["run_liquidity_location"](candles, config)
    pools = []
    for pool in list(getattr(result, "pools_all", None) or result.pools):
        known = available_at(pool)
        if known is None or pool.side not in ("lower", "upper"):
            continue
        pools.append({
            "pool_id": pool.pool_id,
            "side": pool.side,
            "bottom": float(pool.bottom_price),
            "top": float(pool.top_price),
            "available_at": known,
            "break_known_at": break_known_at(pool),
        })
    return bars, pools


def bars_after(bars: list[dict], known: datetime) -> list[dict]:
    return [bar for bar in bars if bar["close_time"] >= known]


def outer_taken(pool: dict, bar: dict) -> bool:
    if pool["side"] == "lower":
        return bar["low"] < pool["bottom"]
    return bar["high"] > pool["top"]


def next_beyond(pools: list[dict], current: dict) -> dict | None:
    if current["side"] == "lower":
        choices = [
            pool for pool in pools
            if pool["side"] == "lower"
            and pool["pool_id"] != current["pool_id"]
            and pool["available_at"] <= current["available_at"]
            and pool["top"] < current["bottom"]
            and (pool["break_known_at"] is None or pool["break_known_at"] > current["available_at"])
        ]
        if not choices:
            return None
        return max(choices, key=lambda pool: (pool["top"], pool["available_at"]))
    choices = [
        pool for pool in pools
        if pool["side"] == "upper"
        and pool["pool_id"] != current["pool_id"]
        and pool["available_at"] <= current["available_at"]
        and pool["bottom"] > current["top"]
        and (pool["break_known_at"] is None or pool["break_known_at"] > current["available_at"])
    ]
    if not choices:
        return None
    return min(choices, key=lambda pool: (pool["bottom"], pool["available_at"]))


def gap_pct(current: dict, nxt: dict | None) -> float | None:
    if nxt is None:
        return None
    if current["side"] == "lower":
        if nxt["top"] <= 0:
            return None
        return (current["bottom"] - nxt["top"]) / nxt["top"] * 100.0
    if current["top"] <= 0:
        return None
    return (nxt["bottom"] - current["top"]) / current["top"] * 100.0


def life(pool: dict, bars: list[dict]) -> dict:
    seen = bars_after(bars, pool["available_at"])
    held_until = seen[-1]["close_time"] if seen else pool["available_at"]
    breakout = None
    for bar in seen:
        if breakout is None and bar["close_time"] > pool["available_at"]:
            if pool["side"] == "lower" and bar["close"] > pool["top"]:
                breakout = bar
            elif pool["side"] == "upper" and bar["close"] < pool["bottom"]:
                breakout = bar
        if outer_taken(pool, bar):
            held_until = bar["close_time"]
            if breakout is not None and breakout["close_time"] >= bar["close_time"]:
                breakout = None
            break
    hold_days = (held_until - pool["available_at"]).total_seconds() / 86400.0
    birth = next((bar for bar in seen if bar["close_time"] == pool["available_at"]), seen[0] if seen else None)
    return {"hold_days": hold_days, "held_until": held_until, "breakout": breakout, "birth": birth}


def is_stair(pool: dict, pools: list[dict]) -> bool:
    previous = [
        other for other in pools
        if other["side"] == pool["side"]
        and other["pool_id"] != pool["pool_id"]
        and other["available_at"] < pool["available_at"]
        and (other["break_known_at"] is None or other["break_known_at"] > pool["available_at"])
    ]
    if pool["side"] == "lower":
        return any(other["top"] < pool["top"] and other["bottom"] <= pool["bottom"] for other in previous)
    return any(other["bottom"] > pool["bottom"] and other["top"] >= pool["top"] for other in previous)


def classify(pool: dict, pools: list[dict], bars: list[dict]) -> dict:
    story = life(pool, bars)
    nxt = next_beyond(pools, pool)
    gap = gap_pct(pool, nxt)
    birth = story["birth"]
    breakout = story["breakout"]
    hold_days = story["hold_days"]
    stair = is_stair(pool, pools)
    roles = []
    if hold_days < 2:
        roles.append("bridge")
    elif hold_days >= 10:
        roles.append("anchor")
    if stair and pool["side"] == "lower":
        roles.append("support_stair")
    if stair and pool["side"] == "upper":
        roles.append("resistance_stair")
    if gap is not None and 3.0 <= gap <= 15.0:
        roles.append("tp_context")
    if breakout is not None and hold_days >= 1:
        ema200 = breakout["ema200"]
        if pool["side"] == "lower" and ema200 is not None and breakout["close"] >= ema200:
            roles.append("long_context")
        if pool["side"] == "upper" and ema200 is not None and breakout["close"] <= ema200:
            roles.append("short_context")
    if not roles:
        roles.append("watch")
    primary = roles[0]
    for preferred in ("long_context", "short_context", "anchor", "support_stair", "resistance_stair", "tp_context", "bridge", "watch"):
        if preferred in roles:
            primary = preferred
            break
    score = min(hold_days, 20) * 2
    if gap is not None and 4 <= gap <= 8:
        score += 25
    elif gap is not None and gap >= 3:
        score += 12
    if "anchor" in roles:
        score += 15
    if "support_stair" in roles or "resistance_stair" in roles:
        score += 10
    if "long_context" in roles or "short_context" in roles:
        score += 20
    if "bridge" in roles:
        score -= 25
    score = max(0, min(100, round(score)))
    return {
        "symbol": "",
        "timeframe": "4h",
        "side": pool["side"],
        "pool_id": pool["pool_id"],
        "available_at": pool["available_at"].isoformat(),
        "bottom_price": pool["bottom"],
        "top_price": pool["top"],
        "held_until": story["held_until"].isoformat(),
        "hold_days": round(hold_days, 2),
        "role": primary,
        "roles": "|".join(roles),
        "relevance_score": score,
        "next_pool_id": "" if nxt is None else nxt["pool_id"],
        "next_bottom_price": "" if nxt is None else nxt["bottom"],
        "next_top_price": "" if nxt is None else nxt["top"],
        "gap_pct": "" if gap is None else round(gap, 4),
        "ema59_at_birth": "" if birth is None or birth["ema59"] is None else round(birth["ema59"], 6),
        "ema200_at_birth": "" if birth is None or birth["ema200"] is None else round(birth["ema200"], 6),
        "context_time": "" if breakout is None else breakout["close_time"].isoformat(),
        "context_close": "" if breakout is None else breakout["close"],
        "ema59_at_context": "" if breakout is None or breakout["ema59"] is None else round(breakout["ema59"], 6),
        "ema200_at_context": "" if breakout is None or breakout["ema200"] is None else round(breakout["ema200"], 6),
    }


def main() -> None:
    for path in (OUT_ROWS, OUT_METHOD):
        if path.exists():
            raise SystemExit(f"refuse overwrite {path}")
    rows = []
    for symbol in SYMBOLS:
        bars, pools = load_symbol(symbol)
        for pool in pools:
            if pool["available_at"] < REPORT_FROM:
                continue
            row = classify(pool, pools, bars)
            row["symbol"] = symbol
            rows.append(row)
        print(symbol, "pools", sum(1 for row in rows if row["symbol"] == symbol), flush=True)
    rows.sort(key=lambda row: (row["symbol"], -row["relevance_score"], row["available_at"]))
    with OUT_ROWS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["role"]] = counts.get(row["role"], 0) + 1
    OUT_METHOD.write_text(json.dumps({
        "script": Path(__file__).name,
        "timeframe": "4h",
        "report_from": REPORT_FROM.isoformat(),
        "symbols": list(SYMBOLS),
        "roles": {
            "bridge": "outer edge taken in under two days",
            "anchor": "pool holds at least ten days",
            "support_stair": "lower pool steps above an older still-active lower pool",
            "resistance_stair": "upper pool steps below an older still-active upper pool",
            "tp_context": "next pool beyond the outer edge is 3 to 15 percent away",
            "long_context": "lower pool, first later close above its top, close at or above EMA200, bottom not taken first",
            "short_context": "upper pool, first later close below its bottom, close at or below EMA200, top not taken first",
        },
        "ema": "EMA59 and EMA200 on 4h closes, context only",
        "counts": counts,
    }, indent=2) + "\n")
    print("rows", len(rows), counts)


if __name__ == "__main__":
    main()

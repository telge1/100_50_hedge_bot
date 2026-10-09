"""Mark 4h pools that stay the last extreme on their side.

A lower pool qualifies when, for at least 20 days after it becomes available:
- no 4h low trades under its bottom edge
- no later pool sits entirely under that edge
- the close moves closer to EMA200
A later close above the 20-day high is recorded as the upward breakout.

The upper side is the mirror. This is a study marker, not a trading rule.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_ROWS = ROOT / "terminal_pool_candidates_v1.csv"
OUT_METHOD = ROOT / "terminal_pool_method_v1.json"

LOAD_START = datetime(2025, 12, 11, tzinfo=timezone.utc)
LOAD_END = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
HOLD_DAYS = 20
EMA_NEAR_PCT = 3.0
EXAMPLE_ID = "lld:DOGEUSDT:4h:lower:1784808000"


def load_symbol(symbol: str):
    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from pool_pattern.market import ensure_paths

    ensure_paths()
    from dashboard.research_charts.service import _candles_from_packed, lld_config_for_timeframe, load_candles
    from dashboard.research_charts.trp_import import load_trp
    from indicators.ema import ema
    from pool_scan.clock import available_at, bar_close, break_known_at

    packed = load_candles(symbol, "4h", start=int(LOAD_START.timestamp()), end=int(LOAD_END.timestamp()), limit=50000)
    if not packed.get("strict_complete_buckets"):
        raise SystemExit(f"candles not strict for {symbol}")
    candles = _candles_from_packed(packed)
    closes = [float(candle.close) for candle in candles]
    ema200 = ema(closes, 200)
    bars = []
    for index, candle in enumerate(candles):
        if ema200[index] is None:
            continue
        bars.append({
            "close_time": bar_close(candle.timestamp, "4h"),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "ema200": float(ema200[index]),
        })
    trp = load_trp()
    config = lld_config_for_timeframe(trp["LiquidityLocationConfig"].defaults(), "4h")
    result = trp["run_liquidity_location"](candles, config)
    pools = []
    for pool in list(getattr(result, "pools_all", None) or result.pools):
        known = available_at(pool)
        if known is None:
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


def beyond_outer(pool: dict, other: dict) -> bool:
    if pool["side"] == "lower":
        return other["side"] == "lower" and other["top"] < pool["bottom"]
    return other["side"] == "upper" and other["bottom"] > pool["top"]


def edge_broken(pool: dict, bar: dict) -> bool:
    if pool["side"] == "lower":
        return bar["low"] < pool["bottom"]
    return bar["high"] > pool["top"]


def gap_pct(bar: dict) -> float:
    return abs(bar["close"] - bar["ema200"]) / bar["ema200"] * 100.0


def scan(symbol: str, bars: list[dict], pools: list[dict]) -> list[dict]:
    rows = []
    for pool in pools:
        if pool["side"] not in ("lower", "upper"):
            continue
        born = pool["available_at"]
        future = [bar for bar in bars if bar["close_time"] > born]
        if not future:
            continue
        hold_end = born + timedelta(days=HOLD_DAYS)
        hold = [bar for bar in future if bar["close_time"] <= hold_end]
        if len(hold) < HOLD_DAYS * 4:
            continue
        if any(edge_broken(pool, bar) for bar in hold):
            continue
        deeper = [
            other for other in pools
            if other["pool_id"] != pool["pool_id"]
            and born < other["available_at"] <= hold_end
            and beyond_outer(pool, other)
        ]
        if deeper:
            continue
        start_gap = gap_pct(hold[0])
        gaps = [gap_pct(bar) for bar in hold]
        min_gap = min(gaps)
        min_gap_time = hold[gaps.index(min_gap)]["close_time"]
        if not (min_gap < start_gap and min_gap <= EMA_NEAR_PCT):
            continue
        if pool["side"] == "lower":
            range_high = max(bar["high"] for bar in hold)
            range_low = min(bar["low"] for bar in hold)
            breakout = next((bar for bar in future if bar["close_time"] > hold_end and bar["close"] > range_high), None)
            direction = "up"
        else:
            range_high = max(bar["high"] for bar in hold)
            range_low = min(bar["low"] for bar in hold)
            breakout = next((bar for bar in future if bar["close_time"] > hold_end and bar["close"] < range_low), None)
            direction = "down"
        quiet_edge = next((bar for bar in future if edge_broken(pool, bar)), None)
        quiet_pool = next((
            other for other in sorted(pools, key=lambda item: item["available_at"])
            if other["available_at"] > born and beyond_outer(pool, other)
        ), None)
        rows.append({
            "symbol": symbol,
            "timeframe": "4h",
            "pool_id": pool["pool_id"],
            "side": pool["side"],
            "available_at": born.isoformat(),
            "bottom_price": pool["bottom"],
            "top_price": pool["top"],
            "hold_days": HOLD_DAYS,
            "hold_range_pct": (range_high / range_low - 1.0) * 100.0,
            "ema_gap_start_pct": start_gap,
            "ema_gap_min_pct": min_gap,
            "ema_gap_min_time": min_gap_time.isoformat(),
            "breakout_time": "" if breakout is None else breakout["close_time"].isoformat(),
            "breakout_close": "" if breakout is None else breakout["close"],
            "breakout_direction": "" if breakout is None else direction,
            "days_until_outer_edge_break": "" if quiet_edge is None else round((quiet_edge["close_time"] - born).total_seconds() / 86400.0, 3),
            "days_until_pool_beyond_edge": "" if quiet_pool is None else round((quiet_pool["available_at"] - born).total_seconds() / 86400.0, 3),
            "example_match": str(pool["pool_id"] == EXAMPLE_ID).lower(),
            "note": (
                "Last extreme for 20 days: edge holds, no later pool sits entirely beyond the outer edge, "
                "and the close moves to within 3 percent of EMA200. Breakout is the later close through the 20-day range."
            ),
        })
    return rows


def main() -> None:
    for path in (OUT_ROWS, OUT_METHOD):
        if path.exists():
            raise SystemExit(f"refuse overwrite {path}")
    rows = []
    for symbol in ("ADAUSDT", "DOGEUSDT"):
        bars, pools = load_symbol(symbol)
        found = scan(symbol, bars, pools)
        print(symbol, "pools", len(pools), "marked", len(found), "with breakout", sum(1 for row in found if row["breakout_time"]), flush=True)
        rows.extend(found)
    rows.sort(key=lambda row: (row["symbol"], row["available_at"], row["pool_id"]))
    with OUT_ROWS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    OUT_METHOD.write_text(json.dumps({
        "script": Path(__file__).name,
        "symbols": ["ADAUSDT", "DOGEUSDT"],
        "timeframe": "4h",
        "window": {"start": LOAD_START.isoformat(), "end": LOAD_END.isoformat()},
        "hold_days": HOLD_DAYS,
        "ema_near_pct": EMA_NEAR_PCT,
        "beyond_outer": {
            "lower": "a later lower pool has top_price below this bottom_price",
            "upper": "a later upper pool has bottom_price above this top_price",
        },
        "example_pool": EXAMPLE_ID,
        "example_found": any(row["pool_id"] == EXAMPLE_ID for row in rows),
        "counts": {
            "marked": len(rows),
            "with_breakout": sum(1 for row in rows if row["breakout_time"]),
        },
    }, indent=2) + "\n")
    print("marked", len(rows), "example", any(row["pool_id"] == EXAMPLE_ID for row in rows))


if __name__ == "__main__":
    main()

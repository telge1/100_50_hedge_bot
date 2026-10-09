"""Find 4h trades on the first candle under a pool that has already started.

The pool is known at its confirmation close. The trade candle is the first
later 4h candle whose close is beyond the near edge. On the chart that candle
is labeled by its open time. A wick through the pool stays valid when that
close is back beyond the near edge and under, or above, EMA9 and EMA20.

Short: close under the upper-pool bottom, close under EMA9, EMA20 and EMA200,
EMA9 under EMA20. EMA9 or EMA20 lies inside the pool on the confirmation bar.
Stop is the top plus 0.2 percent.
Long is the mirror, stop at the bottom minus 0.2 percent.

The first beyond-edge close has to appear within two bars. One row per symbol,
direction and trade open. Short keeps the highest top, long the lowest bottom.

This file does not rewrite the relevance or floor-chain outputs.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_ROWS = ROOT / "ema_pool_setups_4h_v1.csv"
OUT_METHOD = ROOT / "ema_pool_setups_4h_method_v1.json"
LOAD_START = datetime(2025, 12, 11, tzinfo=timezone.utc)
LOAD_END = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
REPORT_FROM = datetime(2026, 3, 1, tzinfo=timezone.utc)
SYMBOLS = ("ADAUSDT", "DOGEUSDT", "XRPUSDT")
STOP_PAD = 0.002
SIGNAL_WINDOW = 2


def load_symbol(symbol: str):
    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from pool_pattern.market import ensure_paths

    ensure_paths()
    from dashboard.research_charts.service import _candles_from_packed, lld_config_for_timeframe, load_candles
    from dashboard.research_charts.trp_import import load_trp
    from pool_scan.clock import available_at, bar_close
    from pool_state_maschine.features import ema

    packed = load_candles(symbol, "4h", start=int(LOAD_START.timestamp()), end=int(LOAD_END.timestamp()), limit=50000)
    if not packed.get("strict_complete_buckets"):
        raise SystemExit(f"candles not strict for {symbol}")
    candles = _candles_from_packed(packed)
    closes = [float(candle.close) for candle in candles]
    ema9 = ema(closes, 9)
    ema20 = ema(closes, 20)
    ema200 = ema(closes, 200)
    closes_at = [bar_close(candle.timestamp, "4h") for candle in candles]
    step = closes_at[1] - closes_at[0]
    bars = []
    for index, candle in enumerate(candles):
        close_time = closes_at[index]
        bars.append({
            "open_time": close_time - step,
            "close_time": close_time,
            "close": float(candle.close),
            "high": float(candle.high),
            "low": float(candle.low),
            "ema9": ema9[index],
            "ema20": ema20[index],
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
        })
    return bars, pools


def band_name(pool: dict, bar: dict) -> str:
    holds9 = bar["ema9"] is not None and pool["bottom"] <= bar["ema9"] <= pool["top"]
    holds20 = bar["ema20"] is not None and pool["bottom"] <= bar["ema20"] <= pool["top"]
    if holds9 and holds20:
        return "ema9+ema20"
    if holds20:
        return "ema20"
    if holds9:
        return "ema9"
    return ""


def first_signal(pool: dict, bars: list[dict]) -> dict | None:
    """First candle after the pool is known that closes beyond the near edge."""
    later = [bar for bar in bars if bar["close_time"] > pool["available_at"]][:SIGNAL_WINDOW]
    if pool["side"] == "upper":
        return next((bar for bar in later if bar["close"] < pool["bottom"]), None)
    return next((bar for bar in later if bar["close"] > pool["top"]), None)


def qualifies(pool: dict, birth: dict | None, signal: dict | None) -> bool:
    if birth is None or signal is None:
        return False
    if signal["ema9"] is None or signal["ema20"] is None or signal["ema200"] is None:
        return False
    if not band_name(pool, birth):
        return False
    if pool["side"] == "upper":
        return (
            signal["close"] < signal["ema9"]
            and signal["close"] < signal["ema20"]
            and signal["close"] < signal["ema200"]
            and signal["ema9"] < signal["ema20"]
        )
    return (
        signal["close"] > signal["ema9"]
        and signal["close"] > signal["ema20"]
        and signal["close"] > signal["ema200"]
        and signal["ema9"] > signal["ema20"]
    )


def over_pool(pool: dict, bar: dict) -> str:
    if pool["side"] == "upper":
        if bar["high"] > pool["top"]:
            return "through_top"
        if bar["high"] > pool["bottom"]:
            return "into_pool"
        return "under"
    if bar["low"] < pool["bottom"]:
        return "through_bottom"
    if bar["low"] < pool["top"]:
        return "into_pool"
    return "above"


def stop_price(pool: dict) -> float:
    if pool["side"] == "upper":
        return pool["top"] * (1.0 + STOP_PAD)
    return pool["bottom"] * (1.0 - STOP_PAD)


def pct(price: float, reference: float | None) -> float | None:
    if reference is None or reference == 0:
        return None
    return (price - reference) / reference * 100.0


def row_for(symbol: str, pool: dict, birth: dict, signal: dict) -> dict:
    width = (pool["top"] - pool["bottom"]) / pool["bottom"] * 100.0 if pool["bottom"] else None
    return {
        "symbol": symbol,
        "timeframe": "4h",
        "direction": "short" if pool["side"] == "upper" else "long",
        "pool_id": pool["pool_id"],
        "available_at": pool["available_at"].isoformat(),
        "signal_open": signal["open_time"].isoformat(),
        "signal_close": signal["close_time"].isoformat(),
        "bottom_price": pool["bottom"],
        "top_price": pool["top"],
        "stop_price": round(stop_price(pool), 8),
        "band": band_name(pool, birth),
        "wick": over_pool(pool, signal),
        "close": signal["close"],
        "high": signal["high"],
        "low": signal["low"],
        "ema9": round(signal["ema9"], 8),
        "ema20": round(signal["ema20"], 8),
        "ema200": round(signal["ema200"], 8),
        "close_vs_ema9_pct": round(pct(signal["close"], signal["ema9"]), 4),
        "close_vs_ema20_pct": round(pct(signal["close"], signal["ema20"]), 4),
        "close_vs_ema200_pct": round(pct(signal["close"], signal["ema200"]), 4),
        "pool_width_pct": "" if width is None else round(width, 4),
    }


def choose_pair(group: list[tuple[dict, dict]], side: str) -> tuple[dict, dict]:
    if side == "upper":
        return max(group, key=lambda item: (item[0]["top"], item[0]["available_at"].isoformat()))
    return min(group, key=lambda item: (item[0]["bottom"], item[0]["available_at"].isoformat()))


def main() -> None:
    for path in (OUT_ROWS, OUT_METHOD):
        if path.exists():
            raise SystemExit(f"refuse overwrite {path}")
    rows = []
    for symbol in SYMBOLS:
        bars, pools = load_symbol(symbol)
        by_close = {bar["close_time"]: bar for bar in bars}
        grouped: dict[tuple[str, datetime], list[tuple[dict, dict]]] = {}
        for pool in pools:
            if pool["available_at"] < REPORT_FROM:
                continue
            birth = by_close.get(pool["available_at"])
            signal = first_signal(pool, bars)
            if not qualifies(pool, birth, signal):
                continue
            grouped.setdefault((pool["side"], signal["open_time"]), []).append((pool, birth))
        for (side, moment), group in grouped.items():
            pool, birth = choose_pair(group, side)
            signal = next(bar for bar in bars if bar["open_time"] == moment)
            rows.append(row_for(symbol, pool, birth, signal))
        print(symbol, "setups", sum(1 for row in rows if row["symbol"] == symbol), flush=True)
    rows.sort(key=lambda row: (row["symbol"], row["direction"], row["signal_open"]))
    with OUT_ROWS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    counts: dict[str, int] = {}
    for row in rows:
        key = f"{row['symbol']}:{row['direction']}"
        counts[key] = counts.get(key, 0) + 1
    OUT_METHOD.write_text(json.dumps({
        "script": Path(__file__).name,
        "timeframe": "4h",
        "report_from": REPORT_FROM.isoformat(),
        "symbols": list(SYMBOLS),
        "ema": "EMA9, EMA20 and EMA200 on 4h closes, SMA-seeded",
        "short": "first close under the upper-pool bottom within two bars after the pool is known, close under EMA9 EMA20 and EMA200, EMA9 under EMA20, EMA9 or EMA20 inside the pool on the confirmation bar, a wick through the pool stays valid",
        "long": "first close above the lower-pool top within two bars after the pool is known, close above EMA9 EMA20 and EMA200, EMA9 above EMA20, EMA9 or EMA20 inside the pool on the confirmation bar, a wick through the pool stays valid",
        "chart_time": "signal_open is the 4h candle open shown on the chart",
        "stop": "short stop is pool top times 1.002, long stop is pool bottom times 0.998",
        "same_bar": "short keeps the highest qualifying top, long keeps the lowest qualifying bottom",
        "counts": counts,
        "rows": len(rows),
    }, indent=2) + "\n")
    print("rows", len(rows), counts)


if __name__ == "__main__":
    main()

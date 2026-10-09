"""Track the active floor pool and the next pool beyond its outer edge.

A lower pool stays the observation pool while price holds above its bottom.
A close above the range high of that watch is a long context.
If the bottom is taken, the next pool entirely below that bottom becomes the
new observation pool. A gap of at least 5 percent keeps the nearer shelf;
the pool beyond that gap is the next one. An overlap is not a new floor.

The upper side is the mirror. This is a study state, not an entry rule.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_ROWS = ROOT / "pool_floor_chain_v1.csv"
OUT_METHOD = ROOT / "pool_floor_chain_method_v1.json"
LOAD_START = datetime(2025, 12, 11, tzinfo=timezone.utc)
LOAD_END = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
HOLD_DAYS = 20
MIN_GAP_PCT = 6.0


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
    bars = [
        {
            "close_time": bar_close(candle.timestamp, "4h"),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
        }
        for candle in candles
    ]
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


def active_at(pool: dict, close_time: datetime) -> bool:
    if pool["available_at"] > close_time:
        return False
    broken = pool["break_known_at"]
    return broken is None or broken > close_time


def nearest_floor(pools: list[dict], side: str, close_time: datetime, close: float) -> dict | None:
    if side == "lower":
        choices = [
            pool for pool in pools
            if pool["side"] == "lower" and active_at(pool, close_time) and pool["bottom"] < close
        ]
        if not choices:
            return None
        return max(choices, key=lambda pool: (pool["bottom"], pool["available_at"], pool["pool_id"]))
    choices = [
        pool for pool in pools
        if pool["side"] == "upper" and active_at(pool, close_time) and pool["top"] > close
    ]
    if not choices:
        return None
    return min(choices, key=lambda pool: (pool["top"], pool["available_at"], pool["pool_id"]))


def next_shelf(pools: list[dict], current: dict, close_time: datetime) -> dict | None:
    """Next floor if this outer edge is taken: the nearest edge still beyond it."""
    if current["side"] == "lower":
        choices = [
            pool for pool in pools
            if pool["side"] == "lower"
            and pool["pool_id"] != current["pool_id"]
            and active_at(pool, close_time)
            and pool["bottom"] < current["bottom"]
        ]
        if not choices:
            return None
        return max(choices, key=lambda pool: (pool["bottom"], pool["available_at"], pool["pool_id"]))
    choices = [
        pool for pool in pools
        if pool["side"] == "upper"
        and pool["pool_id"] != current["pool_id"]
        and active_at(pool, close_time)
        and pool["top"] > current["top"]
    ]
    if not choices:
        return None
    return min(choices, key=lambda pool: (pool["top"], pool["available_at"], pool["pool_id"]))


def next_beyond(pools: list[dict], current: dict, close_time: datetime) -> dict | None:
    if current["side"] == "lower":
        choices = [
            pool for pool in pools
            if pool["side"] == "lower"
            and pool["pool_id"] != current["pool_id"]
            and active_at(pool, close_time)
            and pool["top"] < current["bottom"]
        ]
        if not choices:
            return None
        return max(choices, key=lambda pool: (pool["top"], pool["available_at"], pool["pool_id"]))
    choices = [
        pool for pool in pools
        if pool["side"] == "upper"
        and pool["pool_id"] != current["pool_id"]
        and active_at(pool, close_time)
        and pool["bottom"] > current["top"]
    ]
    if not choices:
        return None
    return min(choices, key=lambda pool: (pool["bottom"], pool["available_at"], pool["pool_id"]))


def gap_pct(current: dict, nxt: dict | None) -> str:
    if nxt is None:
        return ""
    if current["side"] == "lower":
        return (current["bottom"] - nxt["top"]) / nxt["top"] * 100.0
    return (nxt["bottom"] - current["top"]) / current["top"] * 100.0


def edge_taken(current: dict, bar: dict) -> bool:
    if current["side"] == "lower":
        return bar["low"] < current["bottom"]
    return bar["high"] > current["top"]


def extreme_pool(pools: list[dict], side: str, close_time: datetime) -> dict | None:
    """Lowest floor that is fully beyond the previous one. An overlap stays the older pool."""
    alive = [pool for pool in pools if pool["side"] == side and active_at(pool, close_time)]
    if not alive:
        return None
    ordered = sorted(alive, key=lambda pool: (pool["available_at"], pool["pool_id"]))
    deep = ordered[0]
    if side == "lower":
        for pool in ordered[1:]:
            if pool["top"] < deep["bottom"]:
                deep = pool
        return deep
    for pool in ordered[1:]:
        if pool["bottom"] > deep["top"]:
            deep = pool
    return deep


def observation_at(pools: list[dict], side: str, close_time: datetime) -> tuple[dict | None, dict | None]:
    """Shelf in front of a real gap, otherwise the extreme itself.

    A later pool that only overlaps the outer edge does not become the floor.
    A large gap keeps the nearer shelf as the observation and the pool beyond
    the gap as the next one.
    """
    alive = [pool for pool in pools if pool["side"] == side and active_at(pool, close_time)]
    deep = extreme_pool(pools, side, close_time)
    if deep is None:
        return None, None
    if side == "lower":
        shelves = [
            pool for pool in alive
            if pool["pool_id"] != deep["pool_id"]
            and pool["bottom"] > deep["top"]
            and (pool["bottom"] - deep["top"]) / deep["top"] * 100.0 >= MIN_GAP_PCT
        ]
        if not shelves:
            return deep, next_beyond(pools, deep, close_time)
        shelf = min(shelves, key=lambda pool: (pool["bottom"], pool["available_at"], pool["pool_id"]))
        return shelf, deep
    shelves = [
        pool for pool in alive
        if pool["pool_id"] != deep["pool_id"]
        and pool["top"] < deep["bottom"]
        and (deep["bottom"] - pool["top"]) / pool["top"] * 100.0 >= MIN_GAP_PCT
    ]
    if not shelves:
        return deep, next_beyond(pools, deep, close_time)
    shelf = max(shelves, key=lambda pool: (pool["top"], pool["pool_id"]))
    return shelf, deep


def track(symbol: str, bars: list[dict], pools: list[dict]) -> list[dict]:
    """One observation pool, plus the next pool behind a gap.

    The observation stays until its outer edge is taken. A large gap does not
    drop it. After the edge is taken, the deeper pool is the new observation.
    """
    rows = []
    for side in ("lower", "upper"):
        spell = None
        for bar in bars:
            current, nxt = observation_at(pools, side, bar["close_time"])
            if spell is not None and edge_taken(spell["pool"], bar):
                rows.append(finish(symbol, spell, bar["close_time"], "edge_taken"))
                spell = None
            elif spell is not None and active_at(spell["pool"], bar["close_time"]):
                # A pool above this one by a real gap can become the shelf.
                # A smaller later pool, an overlap, or a wick into the box cannot.
                shelf_takes_over = False
                if current is not None and current["pool_id"] != spell["pool"]["pool_id"]:
                    if side == "lower" and current["bottom"] > spell["pool"]["top"]:
                        gap = (current["bottom"] - spell["pool"]["top"]) / spell["pool"]["top"] * 100.0
                        shelf_takes_over = gap >= MIN_GAP_PCT
                    elif side == "upper" and current["top"] < spell["pool"]["bottom"]:
                        gap = (spell["pool"]["bottom"] - current["top"]) / current["top"] * 100.0
                        shelf_takes_over = gap >= MIN_GAP_PCT
                if shelf_takes_over:
                    rows.append(finish(symbol, spell, bar["close_time"], "replaced"))
                    spell = None
                else:
                    current = spell["pool"]
                    nxt = next_beyond(pools, current, bar["close_time"])
            elif spell is not None:
                rows.append(finish(symbol, spell, bar["close_time"], "replaced"))
                spell = None
            taken = current is not None and edge_taken(current, bar)
            if current is None or taken:
                continue
            if spell is None:
                spell = start_spell(current, bar)
                spell["next"] = nxt
                continue
            held_days = (bar["close_time"] - spell["start"]).total_seconds() / 86400.0
            if (
                spell["breakout_time"] is None
                and held_days >= HOLD_DAYS
                and (
                    (side == "lower" and bar["close"] > spell["range_high"])
                    or (side == "upper" and bar["close"] < spell["range_low"])
                )
            ):
                spell["breakout_time"] = bar["close_time"]
                spell["breakout_close"] = bar["close"]
            spell["range_high"] = max(spell["range_high"], bar["high"])
            spell["range_low"] = min(spell["range_low"], bar["low"])
            spell["end"] = bar["close_time"]
            spell["bars"] += 1
            spell["next"] = nxt
        if spell is not None:
            rows.append(finish(symbol, spell, bars[-1]["close_time"], "still_watching"))
    return rows


def start_spell(current: dict, bar: dict) -> dict:
    return {
        "pool": current,
        "pool_id": current["pool_id"],
        "start": bar["close_time"],
        "end": bar["close_time"],
        "range_high": bar["high"],
        "range_low": bar["low"],
        "bars": 1,
        "breakout_time": None,
        "breakout_close": "",
        "next": None,
    }


def status_for(reason: str, nxt: dict | None) -> str:
    if reason == "breakout":
        return "long_setup" if nxt is None or True else "long_setup"
    if reason == "edge_taken":
        return "deeper_pool_active" if nxt is not None else "edge_taken_no_deeper_pool"
    if reason == "still_watching":
        return "watching"
    return "replaced_by_nearer_pool"


def finish(symbol: str, spell: dict, end_time: datetime, reason: str) -> dict:
    current = spell["pool"]
    # The next pool is the one known at the end of the watch.
    return {
        "symbol": symbol,
        "timeframe": "4h",
        "side": current["side"],
        "pool_id": current["pool_id"],
        "bottom_price": current["bottom"],
        "top_price": current["top"],
        "available_at": current["available_at"].isoformat(),
        "watch_start": spell["start"].isoformat(),
        "watch_end": end_time.isoformat(),
        "bars": spell["bars"],
        "range_high": spell["range_high"],
        "range_low": spell["range_low"],
        "end_reason": reason,
        "breakout_time": "" if spell["breakout_time"] is None else spell["breakout_time"].isoformat(),
        "breakout_close": spell["breakout_close"],
        "_next_lookup_time": end_time,
        "_pool": current,
        "_next": spell.get("next"),
    }


def attach_next(rows: list[dict], pools: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        current = row.pop("_pool")
        row.pop("_next_lookup_time")
        nxt = row.pop("_next")
        side = current["side"]
        reason = row["end_reason"]
        broke_out = bool(row["breakout_time"])
        if reason == "edge_taken" and nxt is not None:
            status = "deeper_pool_active"
        elif reason == "edge_taken":
            status = "edge_taken_no_deeper_pool"
        elif broke_out and side == "lower":
            status = "long_setup"
        elif broke_out and side == "upper":
            status = "short_setup"
        elif reason == "still_watching":
            status = "watching"
        else:
            status = "replaced_by_nearer_pool"
        row["next_pool_id"] = "" if nxt is None else nxt["pool_id"]
        row["next_bottom_price"] = "" if nxt is None else nxt["bottom"]
        row["next_top_price"] = "" if nxt is None else nxt["top"]
        row["gap_pct"] = "" if nxt is None else round(gap_pct(current, nxt), 6)
        row["status"] = status
        row["note"] = (
            "The observation stays until its outer edge is taken. A smaller later pool and a wick into the box do not replace it. "
            "A large gap stays as the next pool and is ignored until that edge is taken. "
            "long_setup is a close through the watch range after 20 days."
        )
        out.append(row)
        del side
    return out


def main() -> None:
    for path in (OUT_ROWS, OUT_METHOD):
        if path.exists():
            raise SystemExit(f"refuse overwrite {path}")
    rows = []
    for symbol in ("ADAUSDT", "DOGEUSDT"):
        bars, pools = load_symbol(symbol)
        tracked = track(symbol, bars, pools)
        rows.extend(attach_next(tracked, pools))
        print(symbol, "spells", len(tracked), flush=True)
    rows.sort(key=lambda row: (row["symbol"], row["side"], row["watch_start"], row["pool_id"]))
    with OUT_ROWS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    OUT_METHOD.write_text(json.dumps({
        "script": Path(__file__).name,
        "symbols": ["ADAUSDT", "DOGEUSDT"],
        "timeframe": "4h",
        "window": {"start": LOAD_START.isoformat(), "end": LOAD_END.isoformat()},
        "hold_days_before_breakout": HOLD_DAYS,
        "min_gap_pct": MIN_GAP_PCT,
        "gap": "lower: (bottom - next top) / next top; upper: (next bottom - top) / top",
        "status": {
            "watching": "outer edge not taken and no 20-day range break yet",
            "long_setup": "lower floor, close above the watch range after 20 days, edge not taken by the data end",
            "short_setup": "upper floor, close below the watch range after 20 days, edge not taken by the data end",
            "deeper_pool_active": "outer edge taken, the next pool beyond that edge becomes the floor",
            "edge_taken_no_deeper_pool": "outer edge taken and no pool sits entirely beyond it",
        },
        "counts": {"spells": len(rows), **counts},
    }, indent=2) + "\n")
    print("spells", len(rows), counts)


if __name__ == "__main__":
    main()

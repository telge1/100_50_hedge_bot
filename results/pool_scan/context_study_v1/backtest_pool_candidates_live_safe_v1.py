"""Live-safe backtest for pool-touch candidates.

Principles:
- use only pools visible at the entry time
- no future-born pool information for target selection
- fixed 1% stop
- evaluate TP1 and TP2 scenarios separately

This is a retrospective test, but it avoids the obvious lookahead from the
earlier exploratory shortlist.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "mtf_context_8pct_cases.jsonl"
OUT_ROWS = ROOT / "pool_live_safe_backtest_rows_v1.csv"
OUT_SUMMARY = ROOT / "pool_live_safe_backtest_summary_v1.csv"
OUT_METHOD = ROOT / "pool_live_safe_backtest_method_v1.json"

LOAD_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
LOAD_END = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
STEP_SECONDS = {"15m": 900, "30m": 1800, "4h": 14400}
STOP_PCT = 1.0


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def load_series() -> dict[tuple[str, str], list[dict]]:
    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    from pool_pattern.market import ensure_paths

    ensure_paths()
    from dashboard.research_charts.service import _candles_from_packed, load_candles

    start = int(LOAD_START.timestamp())
    end = int(LOAD_END.timestamp())
    series: dict[tuple[str, str], list[dict]] = {}
    for symbol in ("ADAUSDT", "DOGEUSDT"):
        for timeframe in ("15m", "30m", "4h"):
            packed = load_candles(symbol, timeframe, start=start, end=end, limit=50000)
            if not packed.get("strict_complete_buckets"):
                raise SystemExit(f"candles are not strict for {symbol} {timeframe}")
            candles = _candles_from_packed(packed)
            rows = []
            previous = None
            for candle in candles:
                close_at = int(candle.timestamp.timestamp()) + STEP_SECONDS[timeframe]
                if previous is not None and close_at - previous != STEP_SECONDS[timeframe]:
                    raise SystemExit(f"gap detected for {symbol} {timeframe} at {close_at}")
                previous = close_at
                rows.append(
                    {
                        "close_time": close_at,
                        "open_time": int(candle.timestamp.timestamp()),
                        "open": float(candle.open),
                        "high": float(candle.high),
                        "low": float(candle.low),
                        "close": float(candle.close),
                    }
                )
            series[(symbol, timeframe)] = rows
    return series


def pick_visible_targets(
    pools: list[dict],
    direction: str,
    pool_bottom: float,
    pool_top: float,
) -> list[dict]:
    if direction == "short":
        # Targets below the touched pool, sorted from nearest to farther.
        hits = [row for row in pools if float(row["top_price"]) < pool_bottom]
        hits.sort(key=lambda row: (float(row["top_price"]), float(row["bottom_price"]), row["pool_id"]), reverse=True)
        return hits
    hits = [row for row in pools if float(row["bottom_price"]) > pool_top]
    hits.sort(key=lambda row: (float(row["bottom_price"]), float(row["top_price"]), row["pool_id"]))
    return hits


def target_level(row: dict, direction: str) -> float:
    return float(row["top_price"]) if direction == "short" else float(row["bottom_price"])


def simulate(
    future: list[dict],
    direction: str,
    entry_price: float,
    stop_pct: float,
    target_price: float,
) -> tuple[str, str, bool, bool]:
    """Return outcome, hit_time, stop_hit, target_hit.

    Conservative same-bar rule: if stop and target both happen in the same bar,
    count it as stop.
    """
    if direction == "long":
        stop_price = entry_price * (1.0 - stop_pct / 100.0)
        for bar in future:
            stop_hit = float(bar["low"]) <= stop_price
            target_hit = float(bar["high"]) >= target_price
            if stop_hit and target_hit:
                return "stop_same_bar", datetime.fromtimestamp(bar["close_time"], tz=timezone.utc).isoformat(), True, True
            if stop_hit:
                return "stop", datetime.fromtimestamp(bar["close_time"], tz=timezone.utc).isoformat(), True, False
            if target_hit:
                return "target", datetime.fromtimestamp(bar["close_time"], tz=timezone.utc).isoformat(), False, True
        return "timeout", "", False, False

    stop_price = entry_price * (1.0 + stop_pct / 100.0)
    for bar in future:
        stop_hit = float(bar["high"]) >= stop_price
        target_hit = float(bar["low"]) <= target_price
        if stop_hit and target_hit:
            return "stop_same_bar", datetime.fromtimestamp(bar["close_time"], tz=timezone.utc).isoformat(), True, True
        if stop_hit:
            return "stop", datetime.fromtimestamp(bar["close_time"], tz=timezone.utc).isoformat(), True, False
        if target_hit:
            return "target", datetime.fromtimestamp(bar["close_time"], tz=timezone.utc).isoformat(), False, True
    return "timeout", "", False, False


def main() -> None:
    for path in (OUT_ROWS, OUT_SUMMARY, OUT_METHOD):
        if path.exists():
            raise SystemExit(f"refuse overwrite {path}")

    cases = [json.loads(line) for line in CASES.open()]
    if len(cases) != 126:
        raise SystemExit(f"expected 126 cases, got {len(cases)}")
    series = load_series()

    rows_out = []
    summary = defaultdict(float)

    for rec in cases:
        symbol = rec["symbol"]
        case_end = int(parse(rec["analysis_as_of_last"]).timestamp())
        pools_by_tf = defaultdict(list)
        for item in rec["new_pools"]:
            pools_by_tf[item["timeframe"]].append(item)

        for contact in rec["contacts"]:
            timeframe = contact["timeframe"]
            side = contact["side"]
            direction = "short" if side == "upper" else "long"
            touch_time = parse(contact["candle_close_time"])
            touch_ts = int(touch_time.timestamp())
            candles = series[(symbol, timeframe)]
            close_index = {row["close_time"]: idx for idx, row in enumerate(candles)}
            if touch_ts not in close_index:
                raise SystemExit(f"missing touch candle {rec['case_id']} {contact['pool_id']} {contact['candle_close_time']}")
            entry_index = close_index[touch_ts]
            entry_price = candles[entry_index]["close"]
            future = [row for row in candles[entry_index + 1 :] if row["close_time"] <= case_end]
            if not future:
                continue

            visible_pools = [p for p in pools_by_tf[timeframe] if parse(p["available_at"]) <= touch_time]
            tp1_candidates = pick_visible_targets(
                visible_pools,
                direction,
                float(contact["bottom_price"]),
                float(contact["top_price"]),
            )
            tp1 = tp1_candidates[0] if len(tp1_candidates) >= 1 else None
            tp2 = tp1_candidates[1] if len(tp1_candidates) >= 2 else None

            for scenario, tp in (("tp1", tp1), ("tp2", tp2)):
                if tp is None:
                    summary[(timeframe, direction, scenario, "no_target")] += 1
                    continue
                target_price = target_level(tp, direction)
                outcome, hit_time, stop_hit, target_hit = simulate(
                    future,
                    direction,
                    entry_price,
                    STOP_PCT,
                    target_price,
                )
                rows_out.append(
                    {
                        "case_id": rec["case_id"],
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "retrospective_direction": rec["retrospective_direction"],
                        "pool_id": contact["pool_id"],
                        "side": side,
                        "hypothetical_direction": direction,
                        "scenario": scenario,
                        "entry_time": contact["candle_close_time"],
                        "entry_price": entry_price,
                        "pool_bottom_price": contact["bottom_price"],
                        "pool_top_price": contact["top_price"],
                        "target_pool_id": tp["pool_id"],
                        "target_pool_level": target_price,
                        "target_pool_side": tp["side"],
                        "target_visible_at_entry": tp["available_at"],
                        "exit_time": hit_time,
                        "outcome": outcome,
                        "stop_price": entry_price * (1.0 - STOP_PCT / 100.0) if direction == "long" else entry_price * (1.0 + STOP_PCT / 100.0),
                        "target_hit": str(target_hit).lower(),
                        "stop_hit": str(stop_hit).lower(),
                    }
                )

                summary[(timeframe, direction, scenario, "n")] += 1
                summary[(timeframe, direction, scenario, "target")] += int(outcome == "target")
                summary[(timeframe, direction, scenario, "stop")] += int(outcome.startswith("stop"))
                summary[(timeframe, direction, scenario, "timeout")] += int(outcome == "timeout")
                summary[(timeframe, direction, scenario, "avg_target_level")] += target_price
                summary[(timeframe, direction, scenario, "avg_entry_price")] += entry_price

    if not rows_out:
        raise SystemExit("no rows produced")

    with OUT_ROWS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows_out[0]))
        writer.writeheader()
        writer.writerows(rows_out)

    summary_rows = []
    for timeframe in ("4h", "30m", "15m"):
        for direction in ("long", "short"):
            for scenario in ("tp1", "tp2"):
                n = int(summary[(timeframe, direction, scenario, "n")])
                if not n:
                    continue
                summary_rows.append(
                    {
                        "timeframe": timeframe,
                        "direction": direction,
                        "scenario": scenario,
                        "candidates": n,
                        "targets_hit": int(summary[(timeframe, direction, scenario, "target")]),
                        "stops_hit": int(summary[(timeframe, direction, scenario, "stop")]),
                        "timeouts": int(summary[(timeframe, direction, scenario, "timeout")]),
                        "target_hit_rate": summary[(timeframe, direction, scenario, "target")] / n,
                        "stop_rate": summary[(timeframe, direction, scenario, "stop")] / n,
                        "timeout_rate": summary[(timeframe, direction, scenario, "timeout")] / n,
                        "avg_target_level_pct": summary[(timeframe, direction, scenario, "avg_target_level")] / n,
                        "avg_entry_price_pct": summary[(timeframe, direction, scenario, "avg_entry_price")] / n,
                    }
                )

    with OUT_SUMMARY.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)

    OUT_METHOD.write_text(
        json.dumps(
            {
                "script": Path(__file__).name,
                "reads": [CASES.name, "live-visible pools from new_pools"],
                "stop_pct": STOP_PCT,
                "target_definition": (
                    "TP1/TP2 are chosen only from pools whose available_at is <= the entry time. "
                    "Targets are the nearest and second-nearest visible pools in profit direction."
                ),
                "same_bar_rule": "If stop and target happen on the same bar, the bar is counted as stop.",
                "counts": {
                    "rows": len(rows_out),
                    "summary_rows": len(summary_rows),
                },
            },
            indent=2,
        )
        + "\n"
    )
    print("rows", len(rows_out), "summary_rows", len(summary_rows))


if __name__ == "__main__":
    main()

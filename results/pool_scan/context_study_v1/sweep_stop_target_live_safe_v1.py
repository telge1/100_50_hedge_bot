"""Sweep stop size against minimum target distance on the live-safe pool test.

15m and 30m only. Targets are chosen from pools already visible at the entry
close. A same-bar stop and target is counted as a stop.
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
OUT_ROWS = ROOT / "pool_stop_target_sweep_v1.csv"
OUT_METHOD = ROOT / "pool_stop_target_sweep_method_v1.json"

LOAD_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
LOAD_END = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
STEP_SECONDS = {"15m": 900, "30m": 1800}
STOPS = (0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0, 1.25, 1.5)
THRESHOLDS = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0)


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
        for timeframe, step in STEP_SECONDS.items():
            packed = load_candles(symbol, timeframe, start=start, end=end, limit=50000)
            if not packed.get("strict_complete_buckets"):
                raise SystemExit(f"candles not strict {symbol} {timeframe}")
            candles = _candles_from_packed(packed)
            rows = []
            previous = None
            for candle in candles:
                close_at = int(candle.timestamp.timestamp()) + step
                if previous is not None and close_at - previous != step:
                    raise SystemExit(f"gap {symbol} {timeframe} {close_at}")
                previous = close_at
                rows.append({
                    "close_time": close_at,
                    "high": float(candle.high),
                    "low": float(candle.low),
                    "close": float(candle.close),
                })
            series[(symbol, timeframe)] = rows
            print(symbol, timeframe, len(rows), flush=True)
    return series


def visible_targets(pools: list[dict], direction: str, bottom: float, top: float) -> list[dict]:
    if direction == "short":
        hits = [row for row in pools if float(row["top_price"]) < bottom]
        hits.sort(key=lambda row: (float(row["top_price"]), float(row["bottom_price"]), row["pool_id"]), reverse=True)
        return hits
    hits = [row for row in pools if float(row["bottom_price"]) > top]
    hits.sort(key=lambda row: (float(row["bottom_price"]), float(row["top_price"]), row["pool_id"]))
    return hits


def path_to_target(future: list[dict], direction: str, entry: float, target: float) -> tuple[float, float | None]:
    """Return max adverse percent before the target bar, and adverse percent on the target bar.

    The second value is None when the target is never reached. Adverse on the
    target bar is kept separate so a same-bar stop still counts as a stop.
    """
    max_before = 0.0
    for bar in future:
        if direction == "long":
            adverse = (entry - bar["low"]) / entry * 100.0
            reached = bar["high"] >= target
        else:
            adverse = (bar["high"] - entry) / entry * 100.0
            reached = bar["low"] <= target
        if reached:
            return max_before, adverse
        if adverse > max_before:
            max_before = adverse
    return max_before, None


def main() -> None:
    for path in (OUT_ROWS, OUT_METHOD):
        if path.exists():
            raise SystemExit(f"refuse overwrite {path}")
    series = load_series()
    indexes = {
        key: {row["close_time"]: index for index, row in enumerate(rows)}
        for key, rows in series.items()
    }
    cases = [json.loads(line) for line in CASES.open()]
    prepared = []
    for rec in cases:
        pools_by_tf: dict[str, list[dict]] = defaultdict(list)
        for item in rec["new_pools"]:
            if item["timeframe"] in STEP_SECONDS:
                pools_by_tf[item["timeframe"]].append(item)
        case_end = int(parse(rec["analysis_as_of_last"]).timestamp())
        for contact in rec["contacts"]:
            timeframe = contact["timeframe"]
            if timeframe not in STEP_SECONDS:
                continue
            direction = "short" if contact["side"] == "upper" else "long"
            touch = parse(contact["candle_close_time"])
            candles = series[(rec["symbol"], timeframe)]
            index = indexes[(rec["symbol"], timeframe)].get(int(touch.timestamp()))
            if index is None:
                raise SystemExit(f"missing candle {rec['case_id']} {contact['pool_id']}")
            entry = candles[index]["close"]
            future = [row for row in candles[index + 1:] if row["close_time"] <= case_end]
            if not future:
                continue
            visible = [
                row for row in pools_by_tf[timeframe]
                if parse(row["available_at"]) <= touch
            ]
            targets = visible_targets(
                visible,
                direction,
                float(contact["bottom_price"]),
                float(contact["top_price"]),
            )
            for scenario, target_row in (("tp1", targets[0] if targets else None), ("tp2", targets[1] if len(targets) > 1 else None)):
                if target_row is None:
                    continue
                level = float(target_row["top_price"] if direction == "short" else target_row["bottom_price"])
                if direction == "short":
                    target_pct = (entry / level - 1.0) * 100.0
                else:
                    target_pct = (level / entry - 1.0) * 100.0
                if target_pct <= 0:
                    continue
                before, on_bar = path_to_target(future, direction, entry, level)
                prepared.append({
                    "timeframe": timeframe,
                    "scenario": scenario,
                    "target_pct": target_pct,
                    "adverse_before": before,
                    "adverse_on_target_bar": on_bar,
                })
    print("prepared", len(prepared), flush=True)

    rows = []
    for stop in STOPS:
        for threshold in THRESHOLDS:
            for timeframe in ("15m", "30m", "both"):
                for scenario in ("tp1", "tp2"):
                    subset = [
                        item for item in prepared
                        if item["scenario"] == scenario
                        and item["target_pct"] >= threshold
                        and (timeframe == "both" or item["timeframe"] == timeframe)
                    ]
                    hits = stops_hit = timeouts = 0
                    profit = 0.0
                    for item in subset:
                        on_bar = item["adverse_on_target_bar"]
                        if on_bar is None:
                            if item["adverse_before"] >= stop:
                                stops_hit += 1
                            else:
                                timeouts += 1
                            continue
                        if item["adverse_before"] >= stop or on_bar >= stop:
                            stops_hit += 1
                        else:
                            hits += 1
                            profit += item["target_pct"]
                    loss = stops_hit * stop
                    trades = len(subset)
                    rows.append({
                        "timeframe": timeframe,
                        "scenario": scenario,
                        "stop_pct": stop,
                        "min_target_pct": threshold,
                        "trades": trades,
                        "target_hits": hits,
                        "stops": stops_hit,
                        "timeouts": timeouts,
                        "profit_pct": round(profit, 6),
                        "loss_pct": round(loss, 6),
                        "net_pct": round(profit - loss, 6),
                        "avg_profit_on_hit_pct": round(profit / hits, 6) if hits else 0.0,
                        "avg_loss_on_stop_pct": stop if stops_hit else 0.0,
                    })

    with OUT_ROWS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    positive = [row for row in rows if row["timeframe"] == "both" and row["net_pct"] > 0 and row["trades"] >= 50]
    positive.sort(key=lambda row: row["net_pct"], reverse=True)
    OUT_METHOD.write_text(json.dumps({
        "script": Path(__file__).name,
        "window": {"start": LOAD_START.isoformat(), "end": LOAD_END.isoformat()},
        "symbols": ["ADAUSDT", "DOGEUSDT"],
        "timeframes": ["15m", "30m"],
        "stops": list(STOPS),
        "min_target_pct": list(THRESHOLDS),
        "same_bar": "stop and target on the same bar counts as stop",
        "targets": "only pools with available_at <= entry close",
        "profit": "target distance in percent when the target is reached before a stop",
        "loss": "fixed stop percent when the stop is reached first or on the same bar",
        "timeout": "no target and no stop inside the case window; contributes 0",
        "prepared_paths": len(prepared),
        "rows": len(rows),
        "positive_both_min_50_trades": len(positive),
    }, indent=2) + "\n")
    print("rows", len(rows), "positive both n>=50", len(positive))
    for row in positive[:20]:
        print(
            row["scenario"], "stop", row["stop_pct"], "target>=", row["min_target_pct"],
            "n", row["trades"], "net", row["net_pct"],
            "profit", row["profit_pct"], "loss", row["loss_pct"],
        )


if __name__ == "__main__":
    main()

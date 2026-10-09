"""Study-local trade candidate analysis for pool touches.

This script turns the stored 126-case study into a candidate list and measures
descriptive forward-path metrics:

- hypothetical direction from the touched pool side
- MFE and MAE before MFE
- TP1 / TP2 hit if the next or second-next pool in profit direction is reached

It is exploratory only. It does not define a live trading rule.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "mtf_context_8pct_cases.jsonl"
ASSIGN = ROOT / "pool_age_distance_8pct_assignments.csv"
OUT_ROWS = ROOT / "pool_trade_candidates_v1.csv"
OUT_SUMMARY = ROOT / "pool_trade_candidates_summary_v1.csv"
OUT_METHOD = ROOT / "pool_trade_candidates_method_v1.json"

LOAD_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
LOAD_END = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
STEP_SECONDS = {"15m": 900, "30m": 1800, "4h": 14400}


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


def target_candidates(
    rows: list[dict],
    direction: str,
    pool_bottom: float,
    pool_top: float,
) -> list[dict]:
    if direction == "short":
        hits = [row for row in rows if float(row["top_price"]) < pool_bottom]
        hits.sort(key=lambda row: (float(row["top_price"]), float(row["bottom_price"]), row["pool_id"]), reverse=True)
        return hits
    hits = [row for row in rows if float(row["bottom_price"]) > pool_top]
    hits.sort(key=lambda row: (float(row["bottom_price"]), float(row["top_price"]), row["pool_id"]))
    return hits


def hit_target(bar: dict, row: dict, direction: str) -> bool:
    if direction == "short":
        return float(bar["low"]) <= float(row["top_price"])
    return float(bar["high"]) >= float(row["bottom_price"])


def main() -> None:
    for path in (OUT_ROWS, OUT_SUMMARY, OUT_METHOD):
        if path.exists():
            raise SystemExit(f"refuse overwrite {path}")

    cases = [json.loads(line) for line in CASES.open()]
    if len(cases) != 126:
        raise SystemExit(f"expected 126 cases, got {len(cases)}")
    assign_rows = list(csv.DictReader(ASSIGN.open()))
    by_case_tf: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in assign_rows:
        by_case_tf[(row["case_id"], row["timeframe"])].append(row)

    born_by_case_tf: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for rec in cases:
        for item in rec["new_pools"]:
            born_by_case_tf[(rec["symbol"], item["timeframe"])].append(item)

    series = load_series()

    # Use the stored contacts as candidate entries.
    candidate_rows = []
    summary = Counter()
    for rec in cases:
        case_id = rec["case_id"]
        symbol = rec["symbol"]
        case_end = int(parse(rec["analysis_as_of_last"]).timestamp())
        for contact in rec["contacts"]:
            timeframe = contact["timeframe"]
            pool_id = contact["pool_id"]
            side = contact["side"]
            direction = "short" if side == "upper" else "long"
            touch_time = int(parse(contact["candle_close_time"]).timestamp())
            key = (symbol, timeframe)
            candles = series[key]
            close_index = {row["close_time"]: idx for idx, row in enumerate(candles)}
            if touch_time not in close_index:
                raise SystemExit(f"missing touch candle {case_id} {pool_id} {contact['candle_close_time']}")
            entry_index = close_index[touch_time]
            entry_price = candles[entry_index]["close"]
            future = [row for row in candles[entry_index + 1 :] if row["close_time"] <= case_end]
            if not future:
                continue

            if direction == "long":
                mfe_price = max(row["high"] for row in future)
                mfe_pct = (mfe_price / entry_price - 1.0) * 100.0
                mfe_index = next(i for i, row in enumerate(future) if row["high"] == mfe_price)
                adverse_before_mfe = min(row["low"] for row in future[: mfe_index + 1])
                mae_before_mfe_pct = (entry_price - adverse_before_mfe) / entry_price * 100.0
                absolute_mae_pct = (entry_price - min(row["low"] for row in future)) / entry_price * 100.0
            else:
                mfe_price = min(row["low"] for row in future)
                mfe_pct = (entry_price / mfe_price - 1.0) * 100.0
                mfe_index = next(i for i, row in enumerate(future) if row["low"] == mfe_price)
                adverse_before_mfe = max(row["high"] for row in future[: mfe_index + 1])
                mae_before_mfe_pct = (adverse_before_mfe - entry_price) / entry_price * 100.0
                absolute_mae_pct = (max(row["high"] for row in future) - entry_price) / entry_price * 100.0

            targets = target_candidates(
                born_by_case_tf[key],
                direction,
                float(contact["bottom_price"]),
                float(contact["top_price"]),
            )
            tp1 = targets[0] if len(targets) >= 1 else None
            tp2 = targets[1] if len(targets) >= 2 else None
            tp1_hit = ""
            tp2_hit = ""
            tp1_hit_time = ""
            tp2_hit_time = ""
            for row in future:
                if tp1 is not None and not tp1_hit and hit_target(row, tp1, direction):
                    tp1_hit = "true"
                    tp1_hit_time = datetime.fromtimestamp(row["close_time"], tz=timezone.utc).isoformat()
                if tp2 is not None and not tp2_hit and hit_target(row, tp2, direction):
                    tp2_hit = "true"
                    tp2_hit_time = datetime.fromtimestamp(row["close_time"], tz=timezone.utc).isoformat()
                if tp1_hit and (tp2 is None or tp2_hit):
                    break

            candidate_rows.append(
                {
                    "case_id": case_id,
                    "symbol": symbol,
                    "retrospective_direction": rec["retrospective_direction"],
                    "timeframe": timeframe,
                    "pool_id": pool_id,
                    "side": side,
                    "hypothetical_direction": direction,
                    "entry_time": contact["candle_close_time"],
                    "entry_price": entry_price,
                    "touch_time": contact["first_as_of"],
                    "pool_available_at": contact["available_at"],
                    "bottom_price": contact["bottom_price"],
                    "top_price": contact["top_price"],
                    "future_bars": len(future),
                    "mfe_pct": mfe_pct,
                    "mae_before_mfe_pct": mae_before_mfe_pct,
                    "absolute_mae_pct": absolute_mae_pct,
                    "tp1_pool_id": "" if tp1 is None else tp1["pool_id"],
                    "tp1_side": "" if tp1 is None else tp1["side"],
                    "tp1_bottom_price": "" if tp1 is None else tp1["bottom_price"],
                    "tp1_top_price": "" if tp1 is None else tp1["top_price"],
                    "tp1_hit": tp1_hit,
                    "tp1_hit_time": tp1_hit_time,
                    "tp2_pool_id": "" if tp2 is None else tp2["pool_id"],
                    "tp2_side": "" if tp2 is None else tp2["side"],
                    "tp2_bottom_price": "" if tp2 is None else tp2["bottom_price"],
                    "tp2_top_price": "" if tp2 is None else tp2["top_price"],
                    "tp2_hit": tp2_hit,
                    "tp2_hit_time": tp2_hit_time,
                    "note": (
                        "Entry is the touch-bar close. MFE/MAE are measured on subsequent candles only. "
                        "TP1/TP2 are the next and second-next active pools in profit direction."
                    ),
                }
            )

            summary[(symbol, timeframe, direction, "candidates")] += 1
            summary[(symbol, timeframe, direction, "tp1_hit")] += int(tp1_hit == "true")
            summary[(symbol, timeframe, direction, "tp2_hit")] += int(tp2_hit == "true")
            summary[(symbol, timeframe, direction, "mfe_sum")] += mfe_pct
            summary[(symbol, timeframe, direction, "mae_sum")] += mae_before_mfe_pct
            summary[(symbol, timeframe, direction, "absolute_mae_sum")] += absolute_mae_pct

    with OUT_ROWS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(candidate_rows[0]))
        writer.writeheader()
        writer.writerows(candidate_rows)

    summary_rows = []
    for symbol in ("ADAUSDT", "DOGEUSDT"):
        for timeframe in ("4h", "30m", "15m"):
            for direction in ("long", "short"):
                n = summary[(symbol, timeframe, direction, "candidates")]
                if n == 0:
                    continue
                summary_rows.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "direction": direction,
                        "candidates": n,
                        "tp1_hit": summary[(symbol, timeframe, direction, "tp1_hit")],
                        "tp2_hit": summary[(symbol, timeframe, direction, "tp2_hit")],
                        "tp1_hit_rate": summary[(symbol, timeframe, direction, "tp1_hit")] / n,
                        "tp2_hit_rate": summary[(symbol, timeframe, direction, "tp2_hit")] / n,
                        "avg_mfe_pct": summary[(symbol, timeframe, direction, "mfe_sum")] / n,
                        "avg_mae_before_mfe_pct": summary[(symbol, timeframe, direction, "mae_sum")] / n,
                        "avg_absolute_mae_pct": summary[(symbol, timeframe, direction, "absolute_mae_sum")] / n,
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
                "reads": [CASES.name, ASSIGN.name, "closed candles from load_candles"],
                "window": {
                    "start": LOAD_START.isoformat(),
                    "end": LOAD_END.isoformat(),
                },
                "candidate_definition": (
                    "Every stored touch becomes a hypothetical entry at the touch-bar close. "
                    "Upper side maps to short, lower side maps to long. "
                    "TP1 and TP2 are the next two pools in profit direction within the same case/timeframe."
                ),
                "metrics": {
                    "mfe_pct": "best forward excursion after entry, in percent",
                    "mae_before_mfe_pct": "worst adverse excursion before the MFE bar is first reached, in percent",
                    "absolute_mae_pct": "worst adverse excursion over the full forward path, in percent",
                },
                "not_included": "EMA, OB+delta, live execution, stop rules, and any trade-rule claim.",
                "counts": {
                    "candidates": len(candidate_rows),
                    "summary_rows": len(summary_rows),
                },
            },
            indent=2,
        )
        + "\n"
    )
    print("candidates", len(candidate_rows), "summary_rows", len(summary_rows))


if __name__ == "__main__":
    main()

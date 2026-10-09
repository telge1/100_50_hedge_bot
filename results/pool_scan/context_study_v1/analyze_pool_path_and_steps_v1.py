"""Close path after a stored touch, and same-side near-edge steps.

Reads the stored 126-case file and closed candles. Does not run the scanner
and does not overwrite earlier observation files.

A close beyond the outer edge is recorded as a close. It is not labeled a
breakout or a failed break. A later close back through the near edge is only
a measurement against that edge.
"""

from __future__ import annotations

import csv
import json
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "mtf_context_8pct_cases.jsonl"
PATH_OUT = ROOT / "pool_observation_touch_close_path_v1.csv"
PATH_SUMMARY_OUT = ROOT / "pool_observation_touch_close_path_summary_v1.csv"
STEP_OUT = ROOT / "pool_observation_same_side_step_runs_v1.csv"
STEP_SUMMARY_OUT = ROOT / "pool_observation_same_side_step_summary_v1.csv"
METHOD_OUT = ROOT / "pool_observation_path_steps_method_v1.json"

STEP_SECONDS = {"15m": 900, "30m": 1800, "4h": 14400}
LOAD_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
LOAD_END = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def close_place(side: str, price: float, bottom: float, top: float) -> str:
    if side == "upper":
        if price > top:
            return "beyond_outer"
        if price < bottom:
            return "beyond_near"
        return "inside_box"
    if side == "lower":
        if price < bottom:
            return "beyond_outer"
        if price > top:
            return "beyond_near"
        return "inside_box"
    raise ValueError(side)


def steps_away(side: str, previous_near: float, near: float) -> bool:
    if side == "upper":
        return near > previous_near
    if side == "lower":
        return near < previous_near
    raise ValueError(side)


def near_edge(side: str, bottom: float, top: float) -> float:
    return bottom if side == "upper" else top


def load_series() -> dict[tuple[str, str], list[tuple[int, float]]]:
    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from pool_pattern.market import ensure_paths

    ensure_paths()
    from dashboard.research_charts.service import _candles_from_packed, load_candles

    start = int(LOAD_START.timestamp())
    end = int(LOAD_END.timestamp())
    series: dict[tuple[str, str], list[tuple[int, float]]] = {}
    for symbol in ("ADAUSDT", "DOGEUSDT"):
        for timeframe, step in STEP_SECONDS.items():
            packed = load_candles(symbol, timeframe, start=start, end=end, limit=50000)
            if not packed.get("strict_complete_buckets"):
                raise SystemExit(f"candles not strict for {symbol} {timeframe}")
            candles = _candles_from_packed(packed)
            rows = []
            previous = None
            for candle in candles:
                close_at = int(candle.timestamp.timestamp()) + step
                if previous is not None and close_at - previous != step:
                    raise SystemExit(f"candle gap {symbol} {timeframe} at {close_at}")
                previous = close_at
                rows.append((close_at, float(candle.close)))
            if len(rows) < 100:
                raise SystemExit(f"too few candles {symbol} {timeframe} {len(rows)}")
            series[(symbol, timeframe)] = rows
            print(symbol, timeframe, "closes", len(rows), flush=True)
    return series


def main() -> None:
    outputs = (PATH_OUT, PATH_SUMMARY_OUT, STEP_OUT, STEP_SUMMARY_OUT, METHOD_OUT)
    for path in outputs:
        if path.exists():
            raise SystemExit(f"refuse overwrite {path}")
    cases = [json.loads(line) for line in CASES.open()]
    if len(cases) != 126:
        raise SystemExit(f"cases {len(cases)}")
    series = load_series()

    path_rows = []
    summary = Counter()
    for rec in cases:
        case_end = int(parse(rec["analysis_as_of_last"]).timestamp())
        for item in rec["contacts"]:
            timeframe = item["timeframe"]
            step = STEP_SECONDS[timeframe]
            closes = series[(rec["symbol"], timeframe)]
            times = [row[0] for row in closes]
            touch_at = int(parse(item["candle_close_time"]).timestamp())
            bottom = float(item["bottom_price"])
            top = float(item["top_price"])
            side = item["side"]
            start_index = bisect_right(times, touch_at)
            later = []
            cursor = start_index
            expected = touch_at + step
            gap = False
            while cursor < len(closes) and closes[cursor][0] <= case_end:
                close_at, price = closes[cursor]
                if close_at != expected:
                    gap = True
                    break
                later.append((close_at, price))
                expected += step
                cursor += 1
            if gap:
                raise SystemExit(
                    f"missing close after touch {rec['case_id']} {item['pool_id']} {touch_at}"
                )
            first_place = ""
            first_time = ""
            beyond_time = ""
            bars_until_beyond = ""
            back_time = ""
            if later:
                first_place = close_place(side, later[0][1], bottom, top)
                first_time = datetime.fromtimestamp(later[0][0], tz=timezone.utc).isoformat()
                for offset, (close_at, price) in enumerate(later, start=1):
                    if close_place(side, price, bottom, top) == "beyond_outer":
                        beyond_time = datetime.fromtimestamp(close_at, tz=timezone.utc).isoformat()
                        bars_until_beyond = offset
                        break
                if beyond_time:
                    beyond_epoch = int(parse(beyond_time).timestamp())
                    for close_at, price in later:
                        if close_at <= beyond_epoch:
                            continue
                        if close_place(side, price, bottom, top) == "beyond_near":
                            back_time = datetime.fromtimestamp(close_at, tz=timezone.utc).isoformat()
                            break
            break_after = ""
            break_bar_place = ""
            for row in rec["breaks_during"]:
                if row["timeframe"] != timeframe or row["pool_id"] != item["pool_id"]:
                    continue
                known = int(parse(row["break_known_at"]).timestamp())
                if known < touch_at:
                    continue
                if break_after == "" or known < int(parse(break_after).timestamp()):
                    break_after = row["break_known_at"]
                    index = bisect_right(times, known) - 1
                    if index >= 0 and times[index] == known:
                        break_bar_place = close_place(side, closes[index][1], bottom, top)
                    else:
                        raise SystemExit(f"break candle missing {rec['case_id']} {row['pool_id']} {known}")
            key = (rec["symbol"], timeframe, side)
            summary[(key, "touches")] += 1
            summary[(key, "with_later_close")] += int(bool(later))
            if first_place:
                summary[(key, f"first_{first_place}")] += 1
            summary[(key, "later_close_beyond_outer")] += int(bool(beyond_time))
            summary[(key, "back_through_near_after_outside_close")] += int(bool(back_time))
            summary[(key, "stored_break_after_touch")] += int(bool(break_after))
            summary[(key, "stored_break_bar_close_beyond_outer")] += int(break_bar_place == "beyond_outer")
            summary[(key, "stored_break_bar_close_not_beyond_outer")] += int(
                bool(break_after) and break_bar_place != "beyond_outer"
            )
            path_rows.append({
                "case_id": rec["case_id"],
                "symbol": rec["symbol"],
                "retrospective_direction": rec["retrospective_direction"],
                "timeframe": timeframe,
                "pool_id": item["pool_id"],
                "side": side,
                "touch_time": item["candle_close_time"],
                "bottom_price": item["bottom_price"],
                "top_price": item["top_price"],
                "later_closes_in_case": len(later),
                "first_later_close_time": first_time,
                "first_later_close_place": first_place,
                "first_close_beyond_outer_time": beyond_time,
                "bars_until_close_beyond_outer": bars_until_beyond,
                "later_close_back_through_near_edge_time": back_time,
                "stored_break_after_touch": break_after,
                "stored_break_bar_close_place": break_bar_place,
                "note": (
                    "Places use the same-timeframe close after the touch bar. "
                    "beyond_outer is a close past the outer edge, not a breakout or failed-break label. "
                    "back_through_near_edge is a later close past the facing edge after that outside close. "
                    "The return edge is not chosen."
                ),
            })

    step_rows = []
    births_by: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for rec in cases:
        for item in rec["new_pools"]:
            births_by[(rec["case_id"], item["timeframe"], item["side"])].append(item)
    for key, group in births_by.items():
        group.sort(key=lambda item: (parse(item["available_at"]), item["pool_id"]))
        case_id, timeframe, side = key
        rec = next(row for row in cases if row["case_id"] == case_id)
        run_start = 0
        for index, item in enumerate(group):
            continues = False
            if index + 1 < len(group):
                continues = steps_away(
                    side,
                    near_edge(side, float(item["bottom_price"]), float(item["top_price"])),
                    near_edge(side, float(group[index + 1]["bottom_price"]), float(group[index + 1]["top_price"])),
                )
            if continues:
                continue
            run = group[run_start:index + 1]
            first = run[0]
            last = run[-1]
            step_rows.append({
                "case_id": case_id,
                "symbol": rec["symbol"],
                "retrospective_direction": rec["retrospective_direction"],
                "timeframe": timeframe,
                "side": side,
                "run_pools": len(run),
                "first_available_at": first["available_at"],
                "last_available_at": last["available_at"],
                "first_pool_id": first["pool_id"],
                "last_pool_id": last["pool_id"],
                "first_near_edge": near_edge(side, float(first["bottom_price"]), float(first["top_price"])),
                "last_near_edge": near_edge(side, float(last["bottom_price"]), float(last["top_price"])),
                "note": (
                    "Same-side births only. A run continues while the next near edge steps away: "
                    "upper bottom_price rises, lower top_price falls. Opposite-side births are ignored."
                ),
            })
            run_start = index + 1

    def write(path: Path, rows: list[dict]) -> None:
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    summary_rows = []
    for symbol in ("ADAUSDT", "DOGEUSDT"):
        for timeframe in ("4h", "30m", "15m"):
            for side in ("upper", "lower"):
                key = (symbol, timeframe, side)
                touches = summary[(key, "touches")]
                summary_rows.append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "side": side,
                    "touches": touches,
                    "with_later_close": summary[(key, "with_later_close")],
                    "first_beyond_outer": summary[(key, "first_beyond_outer")],
                    "first_inside_box": summary[(key, "first_inside_box")],
                    "first_beyond_near": summary[(key, "first_beyond_near")],
                    "any_later_close_beyond_outer": summary[(key, "later_close_beyond_outer")],
                    "back_through_near_after_outside_close": summary[(key, "back_through_near_after_outside_close")],
                    "stored_break_after_touch": summary[(key, "stored_break_after_touch")],
                    "stored_break_bar_close_beyond_outer": summary[(key, "stored_break_bar_close_beyond_outer")],
                    "stored_break_bar_close_not_beyond_outer": summary[(key, "stored_break_bar_close_not_beyond_outer")],
                })
    step_summary = []
    counted = Counter()
    for row in step_rows:
        key = (row["symbol"], row["timeframe"], row["side"])
        counted[(key, "runs")] += 1
        counted[(key, "pools")] += int(row["run_pools"])
        length = int(row["run_pools"])
        counted[(key, "len1")] += int(length == 1)
        counted[(key, "len2")] += int(length == 2)
        counted[(key, "len_at_least_3")] += int(length >= 3)
    for symbol in ("ADAUSDT", "DOGEUSDT"):
        for timeframe in ("4h", "30m", "15m"):
            for side in ("upper", "lower"):
                key = (symbol, timeframe, side)
                step_summary.append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "side": side,
                    "runs": counted[(key, "runs")],
                    "pools_in_runs": counted[(key, "pools")],
                    "run_length_1": counted[(key, "len1")],
                    "run_length_2": counted[(key, "len2")],
                    "run_length_at_least_3": counted[(key, "len_at_least_3")],
                })

    write(PATH_OUT, path_rows)
    write(PATH_SUMMARY_OUT, summary_rows)
    write(STEP_OUT, step_rows)
    write(STEP_SUMMARY_OUT, step_summary)
    METHOD_OUT.write_text(json.dumps({
        "script": Path(__file__).name,
        "reads": [CASES.name, "closed candles ADAUSDT and DOGEUSDT 15m 30m 4h"],
        "candle_window": {
            "start": LOAD_START.isoformat(),
            "end": LOAD_END.isoformat(),
            "strict_complete_buckets": True,
        },
        "close_place": {
            "upper_beyond_outer": "close > top_price",
            "upper_beyond_near": "close < bottom_price",
            "lower_beyond_outer": "close < bottom_price",
            "lower_beyond_near": "close > top_price",
            "inside_box": "close between bottom_price and top_price inclusive",
        },
        "not_a_label": (
            "A close beyond the outer edge is not classified as breakout or failed break. "
            "The near edge is only the measurement edge for a later close. No return edge is chosen."
        ),
        "same_side_step": (
            "Runs use new pools of one side inside one case and timeframe. "
            "Upper continues when the next bottom_price is higher. "
            "Lower continues when the next top_price is lower."
        ),
        "counts": {
            "touch_rows": len(path_rows),
            "same_side_runs": len(step_rows),
        },
    }, indent=2) + "\n")
    print("touch paths", len(path_rows), "same-side runs", len(step_rows))
    for row in summary_rows:
        print(
            row["symbol"], row["timeframe"], row["side"],
            "touch", row["touches"],
            "first", row["first_beyond_outer"], row["first_inside_box"], row["first_beyond_near"],
            "beyond", row["any_later_close_beyond_outer"],
            "back", row["back_through_near_after_outside_close"],
            "break", row["stored_break_after_touch"],
            "break_close_out", row["stored_break_bar_close_beyond_outer"],
            "break_close_in", row["stored_break_bar_close_not_beyond_outer"],
        )


if __name__ == "__main__":
    main()

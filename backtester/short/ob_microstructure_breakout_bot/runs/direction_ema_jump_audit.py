#!/usr/bin/env python3
"""Read-only historical audit: touch direction A (current EMA59) vs B (prev EMA59).

Does NOT change production logic. Writes a report under
``ob_microstructure_breakout_bot/runs/``.

Usage (from repo root)::

    PYTHONPATH=.:/path/to/signal_generator/.../src \\
      python -m ob_microstructure_breakout_bot.runs.direction_ema_jump_audit \\
      --symbol DOGEUSDT --from 2026-09-16T00:00:00 --to 2026-09-20T00:00:00
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_EXTRA = [
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src",
    "/home/telgenbuescher/projects/orderbook_analyse/src",
    str(_REPO),
]
for p in reversed(_EXTRA):
    if p not in sys.path:
        sys.path.insert(0, p)

from ob_microstructure_breakout_bot.backtest.scan_touches import detect_ema59_touches
from ob_microstructure_breakout_bot.data.bars import load_5m_bars
from ob_microstructure_breakout_bot.data.ema_candles import _ema_series
from ob_microstructure_breakout_bot.models import TouchDirection


def _parse_utc(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _dir_vs_ema(
    *,
    prev_close: float,
    ema59_ref: float,
    high: float,
    low: float,
    close: float,
) -> str | None:
    from_above = prev_close >= ema59_ref and low <= ema59_ref
    from_below = prev_close <= ema59_ref and high >= ema59_ref
    if from_above and not from_below:
        return TouchDirection.FROM_ABOVE.value
    if from_below and not from_above:
        return TouchDirection.FROM_BELOW.value
    if from_above and from_below:
        return (
            TouchDirection.FROM_ABOVE.value
            if close < ema59_ref
            else TouchDirection.FROM_BELOW.value
        )
    return None


@dataclass
class DiffRow:
    ts: str
    prod_dir: str
    alt_dir: str
    prev_close: float
    cur_ema59: float
    prev_ema59: float
    ema_abs_move: float
    ema_rel_move_bps: float
    rule_path_impact: str


def _rule_path_impact(prod: str, alt: str) -> str:
    """Production: FROM_ABOVE → exit/hold branch first; FROM_BELOW → breakout path."""
    if prod == alt:
        return "none"
    if prod == TouchDirection.FROM_ABOVE.value and alt == TouchDirection.FROM_BELOW.value:
        return "prod_exit_path_vs_alt_breakout_path"
    if prod == TouchDirection.FROM_BELOW.value and alt == TouchDirection.FROM_ABOVE.value:
        return "prod_breakout_path_vs_alt_exit_path"
    return f"prod_{prod}_vs_alt_{alt}"


def analyze(symbol: str, start: datetime, end: datetime) -> dict:
    warmup = start - timedelta(minutes=5 * 220)
    try:
        bars = load_5m_bars(symbol, warmup, end)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "NOT_RUN_NO_HISTORICAL_DATA",
            "reason": f"load_5m_bars failed: {type(exc).__name__}: {exc}",
            "symbol": symbol,
            "from": start.isoformat(),
            "to": end.isoformat(),
        }

    if len(bars) < 60:
        return {
            "status": "NOT_RUN_NO_HISTORICAL_DATA",
            "reason": f"Not enough 5m bars ({len(bars)})",
            "symbol": symbol,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "bar_count": len(bars),
        }

    closes = [b.close for b in bars]
    e59 = _ema_series(closes, 59)
    touches = detect_ema59_touches(bars)
    # Restrict to scan window (same as scanner)
    in_window = [t for t in touches if start <= t.bar_ts < end]

    diffs: list[DiffRow] = []
    same = 0
    a_above_b_below = 0
    a_below_b_above = 0
    abs_moves: list[float] = []
    rule_relevant = 0

    by_ts = {b.ts: i for i, b in enumerate(bars)}
    for touch in in_window:
        i = by_ts[touch.bar_ts]
        if i <= 0 or e59[i] is None or e59[i - 1] is None:
            continue
        prev_close = bars[i - 1].close
        cur_ema = float(e59[i])
        prev_ema = float(e59[i - 1])
        bar = bars[i]

        prod = _dir_vs_ema(
            prev_close=prev_close,
            ema59_ref=cur_ema,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )
        alt = _dir_vs_ema(
            prev_close=prev_close,
            ema59_ref=prev_ema,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )
        # Production detect_ema59_touches always emits a direction when touch exists.
        if prod is None:
            prod = touch.direction.value
        if alt is None:
            # Alternate reference may not even count as a touch against prev EMA.
            alt = "none"

        abs_move = abs(cur_ema - prev_ema)
        rel_bps = (abs_move / prev_close * 10000.0) if prev_close else 0.0
        abs_moves.append(abs_move)

        if prod == alt:
            same += 1
            continue

        if (
            prod == TouchDirection.FROM_ABOVE.value
            and alt == TouchDirection.FROM_BELOW.value
        ):
            a_above_b_below += 1
        elif (
            prod == TouchDirection.FROM_BELOW.value
            and alt == TouchDirection.FROM_ABOVE.value
        ):
            a_below_b_above += 1

        impact = _rule_path_impact(prod, alt)
        if impact != "none" and alt != "none":
            rule_relevant += 1

        diffs.append(
            DiffRow(
                ts=touch.bar_ts.isoformat(),
                prod_dir=prod,
                alt_dir=alt,
                prev_close=prev_close,
                cur_ema59=cur_ema,
                prev_ema59=prev_ema,
                ema_abs_move=abs_move,
                ema_rel_move_bps=rel_bps,
                rule_path_impact=impact,
            )
        )

    n = len(in_window)
    diff_n = len(diffs)
    report = {
        "status": "OK",
        "symbol": symbol,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "bar_count": len(bars),
        "bars_in_warmup_to_end": len(bars),
        "touch_count": n,
        "same_direction": same,
        "different_direction": diff_n,
        "difference_rate_pct": (100.0 * diff_n / n) if n else 0.0,
        "prod_above_alt_below": a_above_b_below,
        "prod_below_alt_above": a_below_b_above,
        "ema_abs_move_max": max(abs_moves) if abs_moves else None,
        "ema_abs_move_median": statistics.median(abs_moves) if abs_moves else None,
        "rule_engine_relevant_diffs": rule_relevant,
        "note": (
            "Production direction uses finished-touch-bar EMA59. "
            "Alternate uses previous-bar EMA59. "
            "Rule impact: FROM_ABOVE prefers exit/hold path; FROM_BELOW prefers breakout path."
        ),
        "sample_diffs": [asdict(d) for d in diffs[:25]],
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument("--from", dest="scan_from", default="2026-09-16T00:00:00")
    parser.add_argument("--to", dest="scan_to", default="2026-09-20T00:00:00")
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parent),
        help="Non-production report directory (default: this runs/ folder)",
    )
    args = parser.parse_args(argv)

    start = _parse_utc(args.scan_from)
    end = _parse_utc(args.scan_to)
    report = analyze(args.symbol, start, end)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"direction_ema_jump_audit_{args.symbol}_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    # Human-readable summary beside JSON
    md_path = out_dir / f"direction_ema_jump_audit_{args.symbol}_{stamp}.md"
    if report.get("status") != "OK":
        md = (
            f"# Direction EMA jump audit\n\n"
            f"**Status:** `{report.get('status')}`\n\n"
            f"{report.get('reason', '')}\n"
        )
    else:
        md = f"""# Direction EMA jump audit

- **Symbol:** {report['symbol']}
- **Zeitraum:** {report['from']} → {report['to']}
- **Anzahl 5m-Bars:** {report['bar_count']}
- **Anzahl EMA59-Touches:** {report['touch_count']}
- **Gleiche Richtung:** {report['same_direction']}
- **Unterschiedliche Richtungen:** {report['different_direction']}
- **Unterschiedsquote:** {report['difference_rate_pct']:.2f}%
- **prod above / alt below:** {report['prod_above_alt_below']}
- **prod below / alt above:** {report['prod_below_alt_above']}
- **Max |ΔEMA59|:** {report['ema_abs_move_max']}
- **Median |ΔEMA59|:** {report['ema_abs_move_median']}
- **Rule-Engine-relevante Unterschiede:** {report['rule_engine_relevant_diffs']}

{report['note']}
"""
    md_path.write_text(md, encoding="utf-8")
    print(json.dumps({"report_json": str(out_path), "report_md": str(md_path), **{
        k: report[k]
        for k in (
            "status",
            "symbol",
            "touch_count",
            "different_direction",
            "difference_rate_pct",
            "rule_engine_relevant_diffs",
            "reason",
        )
        if k in report
    }}, indent=2))
    return 0 if report.get("status") in {"OK", "NOT_RUN_NO_HISTORICAL_DATA"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

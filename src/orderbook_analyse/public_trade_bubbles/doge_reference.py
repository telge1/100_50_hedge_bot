"""DOGEUSDT reference-window bubble analysis (research-only, read-only CH)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.public_trade_bubbles.aggregate import (
    aggregate_bubbles,
    filter_display_mode,
    tick_size_for_symbol,
)
from orderbook_analyse.public_trade_bubbles.loader import coverage_summary, load_public_trade_records

WINDOWS = {
    "A_rejection": {
        "label": "EMA200/Ask-Pool-Rejection",
        "start": "2026-08-28T06:25:00Z",
        "end": "2026-08-28T07:10:00Z",
        "focus_times": [
            "2026-08-28T06:35:00Z",
            "2026-08-28T06:50:00Z",
            "2026-08-28T06:55:00Z",
        ],
    },
    "B_terminal_long": {
        "label": "Terminal Long",
        "start": "2026-08-28T09:45:00Z",
        "end": "2026-08-28T10:40:00Z",
        "focus_times": [
            "2026-08-28T10:00:00Z",
            "2026-08-28T10:20:00Z",
            "2026-08-28T10:31:00Z",
        ],
    },
}


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _summarize_at(bubbles, as_of: datetime) -> dict[str, Any]:
    # only known_at <= as_of
    vis = [b for b in bubbles if b.known_at <= as_of]
    buy = sum(b.buy_notional for b in vis)
    sell = sum(b.sell_notional for b in vis)
    large = [b for b in vis if b.size_class in ("LARGE", "EXTREME")]
    return {
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "n_bubbles": len(vis),
        "n_large_extreme": len(large),
        "buy_notional": buy,
        "sell_notional": sell,
        "delta": buy - sell,
        "forming": sum(1 for b in vis if b.forming),
        "top_bubbles": [
            {
                "bubble_id": b.bubble_id,
                "time": b.bucket_start.isoformat().replace("+00:00", "Z"),
                "price": b.price,
                "delta": b.delta_notional,
                "total": b.total_notional,
                "side": b.dominant_side,
                "size_class": b.size_class,
                "forming": b.forming,
            }
            for b in sorted(large or vis, key=lambda x: -x.total_notional)[:8]
        ],
    }


def run_doge_reference(out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    symbol = "DOGEUSDT"
    report: dict[str, Any] = {
        "symbol": symbol,
        "table": "orderbook_analysis.public_trades_canonical",
        "windows": {},
        "pool_context": "NOT_LINKED_IN_THIS_PASS — descriptive trade metrics only; pool/EMA200 join deferred",
        "verdict_hint": None,
    }
    all_rows: list[dict[str, Any]] = []

    for wid, spec in WINDOWS.items():
        start = _parse(spec["start"])
        end = _parse(spec["end"])
        # warm-up lookback for size class
        warm_start = start.replace(minute=max(0, start.minute - 15)) if False else start
        from datetime import timedelta

        warm_start = start - timedelta(minutes=20)
        cov = coverage_summary(symbol=symbol, start=warm_start, end=end)
        trades = load_public_trade_records(symbol=symbol, start=warm_start, end=end)
        tick = tick_size_for_symbol(symbol)
        win_info: dict[str, Any] = {
            "label": spec["label"],
            "coverage": cov,
            "n_trades_loaded": len(trades),
            "tick_size": tick,
            "time_bucket_s": 1,
            "price_ticks_per_bucket": 5,
            "focus": {},
        }
        # Full-window bubbles at end (for CSV dump of closed only)
        closed_end = aggregate_bubbles(
            trades,
            symbol=symbol,
            as_of=end,
            include_forming=False,
            require_received=False,
        )
        for b in closed_end:
            row = b.to_dict()
            row["window_id"] = wid
            all_rows.append(row)

        for focus in spec["focus_times"]:
            as_of = _parse(focus)
            bubbles = aggregate_bubbles(
                trades,
                symbol=symbol,
                as_of=as_of,
                include_forming=True,
                require_received=False,
            )
            # causal: no trades after as_of already enforced
            large = filter_display_mode(bubbles, "large_medium")
            win_info["focus"][focus] = {
                **_summarize_at(bubbles, as_of),
                "n_large_medium": len(large),
            }
        report["windows"][wid] = win_info

    # write CSV
    if all_rows:
        keys = list(all_rows[0].keys())
        with (out_dir / "bubbles_closed.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(all_rows)

    # narrative notes from focus points
    a = report["windows"]["A_rejection"]["focus"]
    b = report["windows"]["B_terminal_long"]["focus"]
    report["analysis_notes"] = {
        "A_06:35": a.get("2026-08-28T06:35:00Z"),
        "A_06:50": a.get("2026-08-28T06:50:00Z"),
        "A_06:55": a.get("2026-08-28T06:55:00Z"),
        "B_10:00": b.get("2026-08-28T10:00:00Z"),
        "B_10:20": b.get("2026-08-28T10:20:00Z"),
        "B_10:31": b.get("2026-08-28T10:31:00Z"),
        "interpretation_A": (
            "At 06:35 compare buy vs sell notional into upper prices (WATCH). "
            "At 06:55 compare delta flip / sell dominance for REJECTION timing. "
            "Pool availability not joined in this pass."
        ),
        "interpretation_B": (
            "During sweep expect elevated sell notional; absorption if sell pressure "
            "without proportional downside continuation; reclaim ~10:31 check buy delta."
        ),
    }
    (out_dir / "analysis.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out_dir / "report.md").write_text(
        "\n".join(
            [
                "# Public Trade Bubbles — DOGEUSDT 2026-08-28 UTC",
                "",
                f"- Trades source: `{cov.get('table') if False else 'orderbook_analysis.public_trades_canonical'}`",
                f"- Window A trades: {report['windows']['A_rejection']['coverage']['count']}",
                f"- Window B trades: {report['windows']['B_terminal_long']['coverage']['count']}",
                "",
                "## Focus snapshots (causal)",
                "",
                "### A Rejection",
                f"- 06:35: {json.dumps(a.get('2026-08-28T06:35:00Z'), default=str)}",
                f"- 06:50: {json.dumps(a.get('2026-08-28T06:50:00Z'), default=str)}",
                f"- 06:55: {json.dumps(a.get('2026-08-28T06:55:00Z'), default=str)}",
                "",
                "### B Terminal Long",
                f"- 10:00: {json.dumps(b.get('2026-08-28T10:00:00Z'), default=str)}",
                f"- 10:20: {json.dumps(b.get('2026-08-28T10:20:00Z'), default=str)}",
                f"- 10:31: {json.dumps(b.get('2026-08-28T10:31:00Z'), default=str)}",
                "",
                "## Limits",
                "",
                "- Pool/EMA200 join not applied in this pass (descriptive trade layer only).",
                "- Chart API wiring lives in dashboard; activation needs process reload (not done).",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return report


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[3]
    out = root / "results" / "public_trade_bubbles_doge_20260828_v1"
    print(json.dumps(run_doge_reference(out), indent=2, default=str)[:2000])
    print("→", out)

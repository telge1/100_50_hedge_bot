#!/usr/bin/env python3
"""Isolated AAVE TEM structure-break telemetry case (research-only).

Trade: AAVEUSDT|two_early_medium|continuous|0006
Entry: 2026-01-13 22:10 UTC @ 178.5 long

No bot/runtime/order/freeze/exit side effects.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.multicoin_blocker_price_staging import FULL_HISTORY_CANDLE_LIMIT
from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv
from research.regime_scanner.tem_structure_break.monitor import (
    SIGNAL_VERSION,
    candles_to_frame,
    find_bar_by_timestamp,
    run_in_trade_monitor,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    ROOT
    / "research/backtests/results/tem_continuous_27_blocker_root_cause_20260722"
)
DEFAULT_OUT = (
    ROOT
    / "research/backtests/results/tem_structure_break_aave_0006_20260723"
)

TRADE_ID = "AAVEUSDT|two_early_medium|continuous|0006"
COIN = "AAVEUSDT"
ENTRY_TS = "2026-01-13T22:10:00+00:00"
ENTRY_PRICE = 178.5
SIDE = "long"


def log(msg: str) -> None:
    print(msg, flush=True)


def load_cycle_rows() -> list[dict[str, Any]]:
    path = SOURCE / "blocker_cycle_timelines.csv"
    return [
        r
        for r in csv.DictReader(path.open(encoding="utf-8"))
        if r.get("trade_id") == TRADE_ID or r.get("coin") == COIN
    ]


def load_blocker_row() -> dict[str, Any]:
    path = SOURCE / "tem_end_blockers_27.csv"
    for r in csv.DictReader(path.open(encoding="utf-8")):
        if r.get("trade_id") == TRADE_ID:
            return dict(r)
    raise FileNotFoundError(TRADE_ID)


def bar_to_ts(frame, bar: int) -> str:
    if bar is None or bar < 0 or bar >= len(frame):
        return ""
    return str(frame.iloc[int(bar)]["timestamp"])


def join_cycle_timeline(frame, cycles: list[dict[str, Any]], rt) -> list[dict[str, Any]]:
    rows = []
    for c in sorted(cycles, key=lambda x: int(float(x.get("cycle_index") or 0))):
        bar = int(float(c.get("first_leg_fill_bar") or c.get("start_bar") or -1))
        rows.append(
            {
                "kind": "cycle_first_leg",
                "cycle_index": c.get("cycle_index"),
                "purpose": c.get("first_leg_purpose"),
                "bar": bar,
                "timestamp": bar_to_ts(frame, bar),
                "long_qty": c.get("long_qty"),
                "short_qty": c.get("short_qty"),
                "cycle_open_mtm": c.get("cycle_open_mtm"),
                "scanner_state_at_or_before": _state_at(rt, bar),
            }
        )
    for ev in rt.events:
        rows.append(
            {
                "kind": "scanner_event",
                "event": ev.get("event"),
                "state": ev.get("state"),
                "bar": ev.get("bar"),
                "timestamp": ev.get("timestamp"),
                "signal_available_ts": ev.get("signal_available_ts"),
                "level": ev.get("level"),
                "break_kind": ev.get("kind"),
                "timeframe": ev.get("timeframe"),
                "reasons": ev.get("reasons"),
            }
        )
    rows.sort(key=lambda r: (str(r.get("timestamp") or ""), str(r.get("kind"))))
    return rows


def _state_at(rt, bar: int) -> str:
    last = ""
    for snap in rt.timeline:
        if int(snap["bar"]) <= int(bar):
            last = snap["state"]
        else:
            break
    if not last and rt.decision:
        return {
            "ALLOW": "ENTRY_ALLOWED",
            "WEAK_ALLOW": "ENTRY_WEAK_ALLOW",
            "BLOCK": "ENTRY_BLOCKED",
        }.get(rt.decision.decision, "")
    return last


def lead_time_hours(signal_ts: str | None, ref_ts: str | None) -> float | None:
    if not signal_ts or not ref_ts:
        return None
    a = datetime.fromisoformat(str(signal_ts).replace("Z", "+00:00"))
    b = datetime.fromisoformat(str(ref_ts).replace("Z", "+00:00"))
    return (b - a).total_seconds() / 3600.0


def main() -> None:
    parser = argparse.ArgumentParser(description="AAVE TEM structure-break telemetry case")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument(
        "--max-bars-after-entry",
        type=int,
        default=None,
        help="Optional cap for faster smoke (absolute bars after entry)",
    )
    args = parser.parse_args()
    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    # Preserve ROOT_CAUSE.md; refresh regenerable artifacts only.
    for name in (
        "summary.json",
        "REPORT.md",
        "entry_snapshot.csv",
        "frozen_levels.csv",
        "scanner_events.csv",
        "in_trade_timeline_sampled.csv",
        "joined_cycle_scanner_timeline.csv",
    ):
        p = out / name
        if p.exists():
            p.unlink()

    blocker = load_blocker_row()
    cycles = load_cycle_rows()
    log(f"Loading candles {COIN} limit={args.candle_limit}")
    candles = normalize_candles(COIN, load_candles_for_symbol(COIN, limit=args.candle_limit))
    frame = candles_to_frame(candles)
    entry_bar = find_bar_by_timestamp(frame, ENTRY_TS)
    artifact_start = int(float(blocker.get("start_bar") or entry_bar))
    if entry_bar != artifact_start:
        log(f"NOTE: timestamp bar={entry_bar} vs artifact start_bar={artifact_start}; using timestamp bar")

    end_bar = None
    if args.max_bars_after_entry is not None:
        end_bar = min(len(frame) - 1, entry_bar + int(args.max_bars_after_entry))

    log(f"RUN monitor entry_bar={entry_bar} ts={frame.iloc[entry_bar]['timestamp']} end_bar={end_bar}")
    rt = run_in_trade_monitor(
        frame_5m=frame,
        entry_bar=entry_bar,
        entry_price=ENTRY_PRICE,
        side=SIDE,
        end_bar=end_bar,
    )

    # Cycle reference timestamps
    cycle_by = {int(float(c["cycle_index"])): c for c in cycles}
    c4 = cycle_by.get(4)
    c5 = cycle_by.get(5)
    c4_ts = bar_to_ts(frame, int(float(c4["first_leg_fill_bar"]))) if c4 else None
    c5_ts = bar_to_ts(frame, int(float(c5["first_leg_fill_bar"]))) if c5 else None

    # Last BREAK_PENDING before invalidation (decisive episode)
    decisive_pending_ts = None
    decisive_pending_kind = None
    decisive_cycle_id = None
    for ev in rt.events:
        if ev.get("event") == "BREAK_PENDING_4H":
            decisive_pending_ts = ev.get("signal_available_ts") or ev.get("timestamp")
            decisive_pending_kind = ev.get("kind")
            decisive_cycle_id = ev.get("break_cycle_id")
        if ev.get("event") == "LONG_THESIS_INVALIDATED":
            break

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "signal_version": SIGNAL_VERSION,
        "trade_id": TRADE_ID,
        "coin": COIN,
        "entry_timestamp": ENTRY_TS,
        "entry_price": ENTRY_PRICE,
        "side": SIDE,
        "entry_bar": entry_bar,
        "artifact_start_bar": artifact_start,
        "entry_decision": None if rt.decision is None else asdict(rt.decision),
        "frozen_levels": None if rt.frozen is None else asdict(rt.frozen),
        "final_state": rt.state.value,
        "warning_ts": rt.warning_ts,
        "warning_kind": rt.warning_kind,
        "first_5m_frozen_break_ts": rt.first_5m_frozen_break_ts,
        "first_1h_break_ts": rt.first_1h_break_ts,
        "first_4h_break_ts": rt.first_4h_break_ts,
        "break_pending_ts": rt.break_pending_ts,
        "decisive_break_pending_ts": decisive_pending_ts,
        "decisive_break_kind": decisive_pending_kind,
        "decisive_break_cycle_id": decisive_cycle_id,
        "break_confirmed_ts": rt.break_confirmed_ts,
        "reclaim_ts": rt.reclaim_ts,
        "last_reclaim_level": rt.last_reclaim_level,
        "invalidated_ts": rt.invalidated_ts,
        "broken_level": rt.broken_level,
        "break_kind": rt.break_kind,
        "break_timeframe": rt.break_timeframe,
        "break_cycle_id": rt.break_cycle_id,
        "ever_broken": rt.ever_broken,
        "cycle4_long_add_ts": c4_ts,
        "cycle5_long_add_ts": c5_ts,
        "lead_hours_warning_before_c4": lead_time_hours(rt.warning_ts, c4_ts),
        "lead_hours_warning_before_c5": lead_time_hours(rt.warning_ts, c5_ts),
        "lead_hours_4h_break_before_c4": lead_time_hours(rt.first_4h_break_ts, c4_ts),
        "lead_hours_4h_break_before_c5": lead_time_hours(rt.first_4h_break_ts, c5_ts),
        "lead_hours_decisive_break_before_c4": lead_time_hours(decisive_pending_ts, c4_ts),
        "lead_hours_decisive_break_before_c5": lead_time_hours(decisive_pending_ts, c5_ts),
        "lead_hours_invalidation_before_c4": lead_time_hours(rt.invalidated_ts, c4_ts),
        "lead_hours_invalidation_before_c5": lead_time_hours(rt.invalidated_ts, c5_ts),
        "n_timeline_rows": len(rt.timeline),
        "n_events": len(rt.events),
        "telemetry_only": True,
        "no_bot_integration": True,
    }

    joined = join_cycle_timeline(frame, cycles, rt)
    write_csv(out / "entry_snapshot.csv", [summary["entry_decision"] or {}])
    write_csv(out / "frozen_levels.csv", [summary["frozen_levels"] or {}])
    write_csv(out / "scanner_events.csv", rt.events)
    # downsample timeline: event/cycle bars + state transitions + ~hourly samples
    keep_bars = {int(e["bar"]) for e in rt.events if e.get("bar") is not None}
    for c in cycles:
        keep_bars.add(int(float(c.get("first_leg_fill_bar") or -1)))
    timeline_out = []
    prev_state = None
    for i, snap in enumerate(rt.timeline):
        state = snap["state"]
        keep = (
            int(snap["bar"]) in keep_bars
            or i % 12 == 0
            or state != prev_state
        )
        if keep:
            timeline_out.append(snap)
        prev_state = state
    write_csv(out / "in_trade_timeline_sampled.csv", timeline_out)
    write_csv(out / "joined_cycle_scanner_timeline.csv", joined)
    atomic_write_json(out / "summary.json", summary)

    report = f"""# TEM Structure Break — AAVEUSDT continuous|0006

Generated: `{summary['generated_at']}`
Signal version: `{SIGNAL_VERSION}`
Telemetry only. No bot / order / freeze / exit.

See also: `ROOT_CAUSE.md` (why v1 missed the Jan 19–20 break).

## Entry

- Trade: `{TRADE_ID}`
- Entry: `{ENTRY_TS}` @ `{ENTRY_PRICE}` long
- Decision: **{summary['entry_decision'] and summary['entry_decision']['decision']}**
- Reasons: `{summary['entry_decision'] and summary['entry_decision']['reasons']}`
- G1: `{summary['entry_decision'] and summary['entry_decision']['g1_long']}`
- 5m major / EMA / m30 / h4: `{summary['entry_decision'] and (summary['entry_decision']['major_5m'], summary['entry_decision']['ema_regime'], summary['entry_decision']['m30_major'], summary['entry_decision']['h4_major'])}`

## Frozen invalidation levels

```json
{json.dumps(summary['frozen_levels'], indent=2)}
```

## Structure-break telemetry (multi-episode)

| Milestone | Timestamp |
|--|--|
| First warning | `{rt.warning_ts}` ({rt.warning_kind}) |
| First 4h break pending | `{rt.first_4h_break_ts}` |
| Last reclaim | `{rt.reclaim_ts}` (level `{rt.last_reclaim_level}`) |
| Decisive break pending | `{decisive_pending_ts}` (cycle `{decisive_cycle_id}`, `{decisive_pending_kind}`) |
| 1h PL break | `{rt.first_1h_break_ts}` |
| Thesis invalidated | `{rt.invalidated_ts}` |
| Final state | `{rt.state.value}` |
| Break cycles | `{rt.break_cycle_id}` |

## vs TEM cycles

Positive lead = scanner signal **before** the cycle LONG_ADD.

| Cycle | LONG_ADD ts | Lead warning (h) | Lead first-4h (h) | Lead decisive-4h (h) | Lead invalidation (h) |
|--|--|--|--|--|--|
| 4 | `{c4_ts}` | `{summary['lead_hours_warning_before_c4']}` | `{summary['lead_hours_4h_break_before_c4']}` | `{summary['lead_hours_decisive_break_before_c4']}` | `{summary['lead_hours_invalidation_before_c4']}` |
| 5 | `{c5_ts}` | `{summary['lead_hours_warning_before_c5']}` | `{summary['lead_hours_4h_break_before_c5']}` | `{summary['lead_hours_decisive_break_before_c5']}` | `{summary['lead_hours_invalidation_before_c5']}` |

## Notes

- v2: reclaim → `STRUCTURE_AT_RISK`; new episodes via rebreak of last reclaim level and frozen entry 1h/4h floors.
- Live protected-low / BOS edges alone are insufficient after HTF major flip (PL becomes NaN).
- No strategy mutation.
"""
    (out / "REPORT.md").write_text(report, encoding="utf-8")
    log(json.dumps({k: summary[k] for k in summary if k not in {"entry_decision", "frozen_levels"}}, indent=2, default=str))
    log(f"Wrote {out}")


if __name__ == "__main__":
    main()

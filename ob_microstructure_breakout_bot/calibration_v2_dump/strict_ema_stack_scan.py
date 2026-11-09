"""V2 test scan: keep EMA59 touches only when EMA stack is already aligned.

This is intentionally isolated from the baseline calibration flow.
Longs require EMA9 > EMA20 > EMA59, shorts require the mirrored stack.
EMA200 is ignored for the decision gate in this V2 test.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
_EXTRA = [
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src",
    "/home/telgenbuescher/projects/orderbook_analyse/src",
]
for _p in reversed(_EXTRA):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ob_microstructure_breakout_bot.backtest.scan_touches import scan_ema59_touches
from ob_microstructure_breakout_bot.calibration_v2.ema_stack_filters import ema_stack_ok
from ob_microstructure_breakout_bot.models import TouchDirection
from ob_microstructure_breakout_bot.thresholds import load_thresholds


@dataclass(frozen=True)
class StackFilteredTouch:
    bar_ts: datetime
    decision_ts: datetime
    direction: str
    state: str
    tier: str | None
    ema_stack_ok: bool
    reason: str
    ema: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar_ts": self.bar_ts.isoformat(),
            "decision_ts": self.decision_ts.isoformat(),
            "direction": self.direction,
            "state": self.state,
            "tier": self.tier,
            "ema_stack_ok": self.ema_stack_ok,
            "reason": self.reason,
            "ema": self.ema,
        }


def _parse_utc(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ema_dict(ema) -> dict[str, Any] | None:
    if ema is None:
        return None
    return {
        "ema9": ema.ema9,
        "ema20": ema.ema20,
        "ema59": ema.ema59,
        "ema200": ema.ema200,
        "price": ema.price,
        "bullish_stack": ema.bullish_stack,
    }


def run_strict_stack_scan(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    client: Any | None = None,
    fetch_ob: bool = True,
) -> tuple[list[StackFilteredTouch], dict[str, Any]]:
    thresholds = load_thresholds(symbol)
    scanned = scan_ema59_touches(
        symbol,
        start,
        end,
        thresholds,
        client=client,
        fetch_ob=fetch_ob,
    )

    candidates: list[StackFilteredTouch] = []
    breakouts: list[StackFilteredTouch] = []
    for item in scanned:
        touch = item.touch
        stack_ok = ema_stack_ok(touch.ema, touch.direction)
        # Candidate signal = EMA stack ok. Keep the final breakout state for review.
        if not stack_ok:
            continue
        evt = StackFilteredTouch(
            bar_ts=touch.bar_ts,
            decision_ts=item.decision_ts,
            direction=touch.direction.value,
            state=item.result.state.value,
            tier=item.result.tier.value if item.result.tier else None,
            ema_stack_ok=True,
            reason="strict EMA stack gate",
            ema=_ema_dict(touch.ema),
        )
        candidates.append(evt)
        if item.result.state.value == "breakout_confirmed":
            breakouts.append(evt)

    payload = {
        "symbol": symbol.upper().replace("/", ""),
        "scan_from": start.isoformat(),
        "scan_to": end.isoformat(),
        "n_scanned": len(scanned),
        "n_candidates": len(candidates),
        "n_breakouts": len(breakouts),
        "n_long_candidates": sum(1 for e in candidates if e.direction == "from_below"),
        "n_short_candidates": sum(1 for e in candidates if e.direction == "from_above"),
        "n_long_breakouts": sum(1 for e in breakouts if e.direction == "from_below"),
        "n_short_breakouts": sum(1 for e in breakouts if e.direction == "from_above"),
        "ema200_ignored": True,
        # Backward-compatible alias: keep the candidate list in "events".
        "events": [e.to_dict() for e in candidates],
        "candidate_events": [e.to_dict() for e in candidates],
        "breakout_events": [e.to_dict() for e in breakouts],
    }
    return candidates, payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V2 strict EMA-stack scan")
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument("--scan-from", required=True)
    parser.add_argument("--scan-to", required=True)
    parser.add_argument("--no-ob", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json-stdout", action="store_true")
    args = parser.parse_args(argv)

    start = _parse_utc(args.scan_from)
    end = _parse_utc(args.scan_to)
    symbol = args.symbol.upper().replace("/", "")
    _, payload = run_strict_stack_scan(
        symbol,
        start,
        end,
        fetch_ob=not args.no_ob,
    )

    out = args.out
    if out is None:
        out_dir = Path(__file__).resolve().parent / "events"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{symbol}_strict_ema_stack_scan.json"
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if args.json_stdout:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"V2 strict EMA-stack {symbol}: scanned={payload['n_scanned']} "
            f"candidates={payload['n_candidates']} breakouts={payload['n_breakouts']}"
        )
        for ev in payload["candidate_events"]:
            print(
                f"  {ev['bar_ts']}  {ev['direction']}  "
                f"{ev['state']}  tier={ev['tier']}  {ev['ema']['ema9']:.6g}/"
                f"{ev['ema']['ema20']:.6g}/{ev['ema']['ema59']:.6g}"
            )
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


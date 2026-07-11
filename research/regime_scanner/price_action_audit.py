"""Research harness: walk SetupActivation → PriceActionConfirmation events.

No TP / momentum / entry. Outputs JSON + CSV event rows.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .point_audit import json_safe
from .price_action import (
    PriceActionConfig,
    confirmed_pivot_to_swing,
    default_price_action_config,
    evaluate_price_action_confirmation,
    filter_swings_as_of,
    initialize_price_action_state,
    sort_swings,
    update_price_action_state,
)
from .swings import ConfirmedPivot, find_confirmed_pivots


def swings_from_candles(
    candles: pd.DataFrame,
    *,
    config: PriceActionConfig | None = None,
) -> list[dict[str, Any]]:
    cfg = config or default_price_action_config()
    pivots = find_confirmed_pivots(
        candles,
        pivot_left=cfg.pivot_left,
        pivot_right=cfg.pivot_right,
    )
    return [
        confirmed_pivot_to_swing(p, source_timeframe=cfg.source_timeframe)
        for p in pivots
    ]


def walk_price_action(
    *,
    setup_activation: dict[str, Any],
    candles: pd.DataFrame,
    config: PriceActionConfig | None = None,
    swings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Causal walk: for each closed candle, feed newly confirmed swings."""
    cfg = config or default_price_action_config()
    frame = candles.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values("timestamp").reset_index(drop=True)

    all_swings = swings if swings is not None else swings_from_candles(frame, config=cfg)
    all_swings = sort_swings(all_swings)

    activation_ts = setup_activation.get("setup_activation_timestamp")
    initial = filter_swings_as_of(all_swings, activation_ts) if activation_ts else []
    state = initialize_price_action_state(
        setup_activation,
        cfg,
        confirmed_swings_as_of_setup=initial,
    )

    # Start after activation candle if timestamps align with frame.
    start_idx = 0
    if activation_ts is not None:
        act = pd.Timestamp(activation_ts)
        if act.tzinfo is None:
            act = act.tz_localize("UTC")
        matches = frame.index[frame["timestamp"] > act]
        start_idx = int(matches[0]) if len(matches) else len(frame)

    emitted_rows: list[dict[str, Any]] = []
    for ev in state.get("event_log") or []:
        emitted_rows.append(_event_row(ev, setup_activation))

    rows = emitted_rows

    for i in range(start_idx, len(frame)):
        candle = frame.iloc[i].to_dict()
        ts = candle["timestamp"]
        usable = filter_swings_as_of(all_swings, ts)
        processed = {
            tuple(k) for k in (state.get("processed_swing_keys") or [])
        }
        newly = [s for s in usable if (s["confirmation_index"], s["pivot_index"], s["side"]) not in {
            (int(k[0]), int(k[1]), str(k[2])) for k in processed
        }]
        # Prefer key via swing fields
        newly = []
        seen = {tuple(k) for k in (state.get("processed_swing_keys") or [])}
        for s in usable:
            key = (int(s["confirmation_index"]), int(s["pivot_index"]), str(s["side"]))
            if key not in seen:
                newly.append(s)

        before = len(state.get("event_log") or [])
        state = update_price_action_state(state, candle, newly)
        for ev in (state.get("event_log") or [])[before:]:
            rows.append(_event_row(ev, setup_activation))

        if state["state"] in {"price_action_confirmed", "invalidated", "expired"}:
            break

    confirmation = evaluate_price_action_confirmation(state)
    return {
        "setup_activation": setup_activation,
        "final_state": {
            k: v
            for k, v in state.items()
            if k not in {"known_swings", "event_log", "processed_swing_keys"}
        },
        "confirmation": confirmation,
        "events": rows,
        "event_log": state.get("event_log") or [],
    }


def _event_row(event: dict[str, Any], setup: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": event.get("event"),
        "timestamp": event.get("timestamp"),
        "state": event.get("state"),
        "setup_side": setup.get("setup_side"),
        "setup_type": setup.get("setup_type"),
        "pattern_type": event.get("pattern_type"),
        "reason": event.get("reason"),
        "confirmation_level": event.get("confirmation_level"),
        "level": event.get("level"),
        "extreme": event.get("extreme"),
    }


def write_price_action_audit(
    payload: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": out / "price_action_audit.json",
        "csv": out / "price_action_events.csv",
    }
    paths["json"].write_text(
        json.dumps(json_safe(payload), indent=2, allow_nan=False),
        encoding="utf-8",
    )
    events = payload.get("events") or []
    pd.DataFrame(events).to_csv(paths["csv"], index=False)
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Price Action audit harness (research-only, no entry/TP).",
    )
    parser.add_argument(
        "--setup-json",
        required=True,
        help="Path to SetupActivation JSON object",
    )
    parser.add_argument(
        "--candles-csv",
        required=True,
        help="CSV with timestamp,open,high,low,close[,volume]",
    )
    parser.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_price_action_audit",
    )
    parser.add_argument("--max-setup-age-candles", type=int, default=96)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    setup = json.loads(Path(args.setup_json).read_text(encoding="utf-8"))
    candles = pd.read_csv(args.candles_csv)
    cfg = PriceActionConfig(max_setup_age_candles=args.max_setup_age_candles)
    payload = walk_price_action(
        setup_activation=setup,
        candles=candles,
        config=cfg,
    )
    paths = write_price_action_audit(payload, args.output_dir)
    conf = payload.get("confirmation")
    print(
        f"PA audit: state={payload['final_state'].get('state')} "
        f"events={len(payload.get('events') or [])} "
        f"confirmed={conf is not None}"
    )
    for path in paths.values():
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

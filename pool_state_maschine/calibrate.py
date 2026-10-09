"""Replay one frozen profile on a closed calibration window.

Candles and pools end at the window. Nothing after that date is loaded.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pool_state_maschine.coin_profile import CoinProfile
from pool_state_maschine.features import observations
from pool_state_maschine.market_data import load_market
from pool_state_maschine.state_machine import PoolStateMachine

CALIBRATE_FROM = datetime(2026, 3, 17, tzinfo=timezone.utc)
CALIBRATE_TO = datetime(2026, 5, 17, 20, tzinfo=timezone.utc)
WARMUP = timedelta(days=45)
SYMBOLS = ("DOGEUSDT", "ADAUSDT", "XRPUSDT")
CONFIG_DIR = Path(__file__).resolve().parent / "configs"


def frozen_profile(symbol: str) -> CoinProfile:
    return CoinProfile(symbol=symbol)


def play(symbol: str) -> list:
    profile = frozen_profile(symbol)
    bars, pools = load_market(symbol, CALIBRATE_FROM - WARMUP, CALIBRATE_TO)
    machine = PoolStateMachine(symbol)
    states = machine.replay(observations(bars), pools, profile)
    return [state for state in states if state.as_of is not None and CALIBRATE_FROM <= state.as_of <= CALIBRATE_TO]


def ranges(states: list) -> list[tuple]:
    if not states:
        return []
    out = []
    start = states[0]
    previous = states[0]
    for state in states[1:]:
        if state.phase != previous.phase:
            out.append((start, previous.as_of))
            start = state
        previous = state
    out.append((start, previous.as_of))
    return out


def save_config(symbol: str) -> Path:
    CONFIG_DIR.mkdir(exist_ok=True)
    profile = frozen_profile(symbol)
    payload = {
        "symbol": symbol,
        "calibrate_from": CALIBRATE_FROM.isoformat(),
        "calibrate_to": CALIBRATE_TO.isoformat(),
        "fitted": False,
        "thresholds": profile.as_dict(),
    }
    path = CONFIG_DIR / f"{symbol}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    for symbol in SYMBOLS:
        save_config(symbol)
        print(symbol)
        for start, end in ranges(play(symbol)):
            print(f"  {start.phase.value:12} {start.as_of} -> {end}")


if __name__ == "__main__":
    main()

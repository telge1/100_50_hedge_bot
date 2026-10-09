"""Bootstrap scan. Loads context first, then hands the phase to the state machine.

One coin, one lookback, one profile, one resulting phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pool_state_maschine.coin_profile import CoinProfile
from pool_state_maschine.state_machine import MarketState, PoolStateMachine


@dataclass(frozen=True)
class ScanRequest:
    symbol: str
    as_of: datetime
    lookback_days: int


@dataclass(frozen=True)
class ScanResult:
    request: ScanRequest
    profile: CoinProfile
    state: MarketState


def request_for(symbol: str, *, lookback_days: int = 180, as_of: datetime | None = None) -> ScanRequest:
    moment = as_of or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return ScanRequest(symbol=symbol.upper(), as_of=moment, lookback_days=lookback_days)


def window_start(request: ScanRequest) -> datetime:
    return request.as_of - timedelta(days=request.lookback_days)


def _played(request: ScanRequest, profile: CoinProfile | None = None) -> tuple[CoinProfile, list[MarketState]]:
    from pool_state_maschine.features import observations
    from pool_state_maschine.market_data import load_market

    used = profile or CoinProfile(symbol=request.symbol, lookback_days=request.lookback_days)
    bars, pools = load_market(request.symbol, window_start(request) - timedelta(days=45), request.as_of)
    machine = PoolStateMachine(request.symbol)
    played = machine.replay(observations(bars), pools, used)
    start = window_start(request)
    visible = [state for state in played if state.as_of is not None and start <= state.as_of <= request.as_of]
    return used, visible


def bootstrap(request: ScanRequest, profile: CoinProfile | None = None) -> ScanResult:
    """Read the 4h context and name the phase at the last closed bar."""
    used, states = _played(request, profile)
    state = states[-1] if states else PoolStateMachine(request.symbol).state
    return ScanResult(request=request, profile=used, state=state)


def walk(request: ScanRequest, profile: CoinProfile | None = None) -> list[MarketState]:
    """Phase at every closed 4h bar inside the requested window."""
    _, states = _played(request, profile)
    return states


def _phase_ranges(states: list[MarketState]) -> list[tuple[MarketState, datetime]]:
    if not states:
        return []
    ranges: list[tuple[MarketState, datetime]] = []
    start = states[0]
    previous = states[0]
    for state in states[1:]:
        if state.phase != previous.phase:
            ranges.append((start, previous.as_of or start.as_of or request_as_fallback(previous)))
            start = state
        previous = state
    ranges.append((start, previous.as_of or start.as_of))
    return ranges


def request_as_fallback(state: MarketState) -> datetime:
    if state.as_of is None:
        return datetime.now(timezone.utc)
    return state.as_of


def main() -> None:
    request = request_for("DOGEUSDT", lookback_days=180)
    states = walk(request)
    current = states[-1] if states else bootstrap(request).state
    print(
        f"{current.symbol} phase={current.phase.value} as_of={current.as_of.isoformat() if current.as_of else '-'} reason={current.reason}"
    )
    print(f"bars={len(states)}")
    for start, end in _phase_ranges(states):
        print(f"{start.phase.value:12} {start.as_of.isoformat()} -> {end.isoformat()}  {start.reason}")


if __name__ == "__main__":
    main()

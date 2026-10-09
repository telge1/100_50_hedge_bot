"""Walk-forward check. Three months set the phase, the next month is scored.

Thresholds stay frozen. The exam month is never used to change them.
Candles stop at the exam end, so a later month cannot move a stamp.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pool_state_maschine.coin_profile import CoinProfile
from pool_state_maschine.features import Observation, observations
from pool_state_maschine.market_data import load_market
from pool_state_maschine.state_machine import MarketPhase, MarketState, PoolStateMachine

SYMBOLS = ("DOGEUSDT", "ADAUSDT", "XRPUSDT")
DATA_FROM = datetime(2025, 12, 11, tzinfo=timezone.utc)
FIRST_EXAM = datetime(2026, 3, 11, tzinfo=timezone.utc)
WARMUP = timedelta(days=45)
SHORT_BARS = 2
POOL_REASONS = {
    MarketPhase.STAIR_DOWN: frozenset({"lower_stairs_being_collected"}),
    MarketPhase.UPPER_BUILD: frozenset({"band_cleared_from_below", "price_working_through_upper_pools"}),
}


@dataclass(frozen=True)
class Round:
    train_from: datetime
    train_to: datetime
    exam_from: datetime
    exam_to: datetime


@dataclass(frozen=True)
class Stamp:
    symbol: str
    as_of: datetime
    phase: str
    reason: str
    bars: int
    close: float
    flags: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.flags


def add_months(moment: datetime, months: int) -> datetime:
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = min(moment.day, monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def rounds(last_bar: datetime) -> list[Round]:
    out: list[Round] = []
    exam_from = FIRST_EXAM
    while exam_from < last_bar:
        exam_to = min(add_months(exam_from, 1), last_bar)
        out.append(
            Round(
                train_from=add_months(exam_from, -3),
                train_to=exam_from,
                exam_from=exam_from,
                exam_to=exam_to,
            )
        )
        exam_from = add_months(exam_from, 1)
    return out


def judge_segment(
    symbol: str,
    segment: list[MarketState],
    by_time: dict[datetime, Observation],
    last_as_of: datetime,
) -> Stamp:
    start = segment[0]
    obs = by_time[start.as_of]
    censored = segment[-1].as_of == last_as_of
    flags: list[str] = []
    if not censored and len(segment) <= SHORT_BARS:
        flags.append("zu_kurz")
    if not censored and start.phase is MarketPhase.STAIR_DOWN:
        low = min(by_time[state.as_of].low for state in segment)
        if obs.low <= low:
            flags.append("am_tief")
    if not censored and start.phase is MarketPhase.UPPER_BUILD:
        high = max(by_time[state.as_of].high for state in segment)
        if obs.high >= high:
            flags.append("am_hoch")
    expected = POOL_REASONS.get(start.phase)
    if expected is not None and start.reason not in expected:
        flags.append("nicht_am_pool")
    return Stamp(symbol, start.as_of, start.phase.value, start.reason, len(segment), obs.price, tuple(flags))


def score_exam(
    symbol: str,
    states: list[MarketState],
    samples: list[Observation],
    exam_from: datetime,
    exam_to: datetime,
) -> list[Stamp]:
    by_time = {sample.as_of: sample for sample in samples}
    last_as_of = states[-1].as_of if states else exam_to
    stamps: list[Stamp] = []
    index = 0
    while index < len(states):
        phase = states[index].phase
        end = index
        while end + 1 < len(states) and states[end + 1].phase is phase:
            end += 1
        segment = states[index : end + 1]
        start = segment[0].as_of
        if start is not None and exam_from <= start < exam_to and start in by_time:
            stamps.append(judge_segment(symbol, segment, by_time, last_as_of))
        index = end + 1
    return stamps


def play_round(symbol: str, window: Round) -> list[Stamp]:
    profile = CoinProfile(symbol=symbol)
    load_from = max(DATA_FROM, window.train_from - WARMUP)
    bars, pools = load_market(symbol, load_from, window.exam_to)
    samples = observations(bars)
    states = PoolStateMachine(symbol).replay(samples, pools, profile)
    return score_exam(symbol, states, samples, window.exam_from, window.exam_to)


def main() -> None:
    bars, _ = load_market(SYMBOLS[0], DATA_FROM, datetime(2026, 9, 28, tzinfo=timezone.utc))
    last_bar = bars[-1].ts + timedelta(hours=4)
    print(f"daten {DATA_FROM.date()} -> {last_bar.isoformat()}  schwellen bleiben eingefroren")
    suspicious: list[tuple[Round, Stamp]] = []
    for window in rounds(last_bar):
        print(
            f"\nrunde train {window.train_from.date()} -> {window.train_to.date()}  "
            f"pruefung {window.exam_from.date()} -> {window.exam_to.date()}"
        )
        for symbol in SYMBOLS:
            stamps = play_round(symbol, window)
            bad = [stamp for stamp in stamps if not stamp.ok]
            print(f"  {symbol:10} wechsel={len(stamps):3}  ok={len(stamps) - len(bad):3}  auffaellig={len(bad):3}")
            suspicious.extend((window, stamp) for stamp in bad)
    print("\nauffaellig")
    if not suspicious:
        print("  keine")
        return
    for window, stamp in suspicious:
        mark = ",".join(stamp.flags)
        print(
            f"  {stamp.symbol:10} {window.exam_from.date()}  {stamp.as_of.isoformat()}  "
            f"{stamp.phase:12} {mark:16} close={stamp.close:.5f} kerzen={stamp.bars}"
        )


if __name__ == "__main__":
    main()

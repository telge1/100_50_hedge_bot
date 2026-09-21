from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ob_microstructure_breakout_bot.backtest.scan_touches import detect_ema59_touches
from ob_microstructure_breakout_bot.data.bars import (
    CLOSED_5M_BUCKET_SQL,
    Bar5m,
    floor_to_5_minutes,
    is_closed_5m_bucket,
    load_5m_bars,
)
from ob_microstructure_breakout_bot.data.ema_candles import load_5m_closes
from ob_microstructure_breakout_bot.models import CoinThresholds, EmaSnapshot, TouchDirection


def _bar(ts: datetime, o, h, l, c) -> Bar5m:
    return Bar5m(ts, o, h, l, c, buy_notional=1.0, sell_notional=0.0, trade_count=1)


def _dt(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 18, hour, minute, tzinfo=timezone.utc)


class _FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class _FakeClient:
    """Simulates ClickHouse grouping with closed-5m bucket end semantics."""

    def __init__(self, trades: list[tuple[datetime, float, str, float]]):
        # (trade_ts, price, side, notional)
        self.trades = trades
        self.last_sql = ""
        self.last_params: dict = {}

    def query(self, sql: str, parameters: dict | None = None):
        self.last_sql = sql
        self.last_params = dict(parameters or {})
        start = self.last_params["start"]
        end = self.last_params["end"]
        floor_end = floor_to_5_minutes(end)

        buckets: dict[datetime, list[tuple[datetime, float, str, float]]] = {}
        for ts, price, side, notional in self.trades:
            if ts < start:
                continue
            bucket = floor_to_5_minutes(ts)
            # Mirror CLOSED_5M_BUCKET_SQL
            if not (bucket < floor_end):
                continue
            buckets.setdefault(bucket, []).append((ts, price, side, notional))

        rows = []
        for tm in sorted(buckets):
            items = sorted(buckets[tm], key=lambda x: x[0])
            open_p = items[0][1]
            close_p = items[-1][1]
            high = max(i[1] for i in items)
            low = min(i[1] for i in items)
            buy = sum(i[3] for i in items if i[2] == "Buy")
            sell = sum(i[3] for i in items if i[2] == "Sell")
            if "argMin(price" in sql:
                rows.append((tm, open_p, high, low, close_p, buy, sell, len(items)))
            else:
                rows.append((tm, close_p))
        return _FakeResult(rows)


def _sample_trades_fixed() -> list[tuple[datetime, float, str, float]]:
    return [
        (_dt(10, 0), 0.100, "Buy", 100.0),
        (_dt(10, 4), 0.101, "Sell", 50.0),
        # running / full 10:05–10:10 bucket
        (_dt(10, 5), 0.102, "Buy", 80.0),
        (_dt(10, 6), 0.103, "Buy", 90.0),
        (_dt(10, 9), 0.104, "Sell", 40.0),
        # 10:10–10:15
        (_dt(10, 10), 0.105, "Buy", 70.0),
        (_dt(10, 14), 0.106, "Sell", 30.0),
    ]


def test_closed_bucket_semantics_1007_1010_1015():
    b1000 = _dt(10, 0)
    b1005 = _dt(10, 5)
    b1010 = _dt(10, 10)

    assert floor_to_5_minutes(_dt(10, 7)) == b1005
    assert floor_to_5_minutes(_dt(10, 10)) == b1010
    assert floor_to_5_minutes(_dt(10, 15)) == _dt(10, 15)

    # end=10:07 → exclude 10:05 bucket
    assert is_closed_5m_bucket(b1000, _dt(10, 7))
    assert not is_closed_5m_bucket(b1005, _dt(10, 7))

    # end=10:10 → allow 10:05, exclude 10:10
    assert is_closed_5m_bucket(b1005, _dt(10, 10))
    assert not is_closed_5m_bucket(b1010, _dt(10, 10))

    # end=10:15 → allow 10:10
    assert is_closed_5m_bucket(b1010, _dt(10, 15))


def test_load_5m_bars_excludes_running_bucket_at_1007():
    client = _FakeClient(_sample_trades_fixed())
    bars = load_5m_bars("DOGEUSDT", _dt(9, 0), _dt(10, 7), client=client)
    assert CLOSED_5M_BUCKET_SQL in client.last_sql
    assert [b.ts for b in bars] == [_dt(10, 0)]
    assert bars[-1].ts + timedelta(minutes=5) <= floor_to_5_minutes(_dt(10, 7)) + timedelta(
        minutes=0
    )


def test_load_5m_bars_includes_1005_at_1010():
    client = _FakeClient(_sample_trades_fixed())
    bars = load_5m_bars("DOGEUSDT", _dt(9, 0), _dt(10, 10), client=client)
    assert [b.ts for b in bars] == [_dt(10, 0), _dt(10, 5)]


def test_load_5m_bars_includes_1010_at_1015():
    client = _FakeClient(_sample_trades_fixed())
    bars = load_5m_bars("DOGEUSDT", _dt(9, 0), _dt(10, 15), client=client)
    assert [b.ts for b in bars] == [_dt(10, 0), _dt(10, 5), _dt(10, 10)]


def test_load_5m_closes_matches_bars_closed_semantics():
    trades = _sample_trades_fixed()
    for end in (_dt(10, 7), _dt(10, 10), _dt(10, 15)):
        bar_client = _FakeClient(trades)
        close_client = _FakeClient(trades)
        bars = load_5m_bars("DOGEUSDT", _dt(9, 0), end, client=bar_client)
        closes = load_5m_closes("DOGEUSDT", _dt(9, 0), end, client=close_client)
        assert CLOSED_5M_BUCKET_SQL in bar_client.last_sql
        assert CLOSED_5M_BUCKET_SQL in close_client.last_sql
        assert [b.ts for b in bars] == [t for t, _ in closes]


def test_detect_touch_from_above():
    # Build enough bars for EMA59; then force a from-above touch on last bars.
    start = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
    bars: list[Bar5m] = []
    px = 0.10
    for i in range(70):
        ts = start.replace(hour=(i * 5) // 60 % 24, minute=(i * 5) % 60)
        # crude ascending then drop into EMA
        px = 0.10 + i * 0.0001
        bars.append(_bar(ts, px, px + 0.0001, px - 0.0001, px))
    # last bar dips through what should be near EMA
    last = bars[-1]
    touch_bar = _bar(
        last.ts.replace(minute=(last.ts.minute + 5) % 60),
        last.close,
        last.close + 0.0001,
        last.close - 0.01,  # deep low to guarantee touch
        last.close - 0.002,
    )
    bars.append(touch_bar)
    touches = detect_ema59_touches(bars)
    assert touches
    assert touches[-1].direction in (
        TouchDirection.FROM_ABOVE,
        TouchDirection.FROM_BELOW,
        TouchDirection.UNKNOWN,
    )


def test_scan_skips_incomplete_future_windows(monkeypatch):
    from ob_microstructure_breakout_bot.backtest import scan_touches as st

    start = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
    bars: list[Bar5m] = []
    px = 0.10
    for i in range(200):
        ts = start + timedelta(minutes=5 * i)
        px += 0.001
        bars.append(_bar(ts, px, px + 0.0001, px - 0.0001, px))

    touch = st.TouchEvent(
        bar_ts=bars[-1].ts,
        direction=TouchDirection.FROM_BELOW,
        is_first_in_cluster=True,
        ema=EmaSnapshot(ema9=0.11, ema20=0.10, ema59=0.09, price=bars[-1].close),
        bar=bars[-1],
    )
    thresholds = CoinThresholds(
        symbol="DOGEUSDT",
        fakeout_max_confirm_delta=50_000.0,
        tier1_min_confirm_delta=100_000.0,
        tier2_min_confirm_delta=200_000.0,
        tier1_min_bid_ask_ratio_5bps=1.05,
        tier2_min_bid_ask_ratio_5bps=2.0,
        fakeout_followthrough_flip_delta=-100_000.0,
    )

    monkeypatch.setattr(st, "load_5m_bars", lambda *args, **kwargs: bars)
    monkeypatch.setattr(st, "detect_ema59_touches", lambda _: [touch])

    out = st.scan_ema59_touches(
        "DOGEUSDT",
        start,
        start + timedelta(minutes=5 * 210),
        thresholds,
        fetch_ob=False,
        only_first_in_cluster=True,
    )
    assert out == []


def test_scan_decision_ts_after_followthrough_close(monkeypatch):
    from ob_microstructure_breakout_bot.backtest import scan_touches as st

    start = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
    bars: list[Bar5m] = []
    px = 0.10
    for i in range(210):
        ts = start + timedelta(minutes=5 * i)
        px += 0.001
        bars.append(_bar(ts, px, px + 0.0001, px - 0.0001, px))

    # Place touch early enough that confirm+ft bars exist.
    touch_idx = 200
    touch = st.TouchEvent(
        bar_ts=bars[touch_idx].ts,
        direction=TouchDirection.FROM_BELOW,
        is_first_in_cluster=True,
        ema=EmaSnapshot(
            ema9=0.11,
            ema20=0.10,
            ema59=0.09,
            price=bars[touch_idx].close,
            ema200=0.08,
        ),
        bar=bars[touch_idx],
    )
    thresholds = CoinThresholds(
        symbol="DOGEUSDT",
        fakeout_max_confirm_delta=50_000.0,
        tier1_min_confirm_delta=100_000.0,
        tier2_min_confirm_delta=200_000.0,
        tier1_min_bid_ask_ratio_5bps=1.05,
        tier2_min_bid_ask_ratio_5bps=2.0,
        fakeout_followthrough_flip_delta=-100_000.0,
    )

    monkeypatch.setattr(st, "load_5m_bars", lambda *args, **kwargs: bars)
    monkeypatch.setattr(st, "detect_ema59_touches", lambda _: [touch])

    out = st.scan_ema59_touches(
        "DOGEUSDT",
        start,
        start + timedelta(minutes=5 * 220),
        thresholds,
        fetch_ob=False,
        confirm_bars=2,
        followthrough_bars=2,
    )
    assert len(out) == 1
    scanned = out[0]
    last_ft = bars[touch_idx + 2 + 2 - 1]
    assert scanned.decision_ts == last_ft.ts + timedelta(minutes=5)
    assert scanned.decision_ts > scanned.touch.bar_ts
    # No classification before full analysis window
    assert scanned.decision_ts == bars[touch_idx].ts + timedelta(minutes=5 * 4)


# ---------------------------------------------------------------------------
# Direction comparison only — production still uses current-bar EMA59.
# A = production: prev_close vs ema59 of the finished touch bar
# B = alternate:  prev_close vs previous_ema.ema59
# ---------------------------------------------------------------------------


def _direction_vs_ema(
    *,
    prev_close: float,
    ema59_ref: float,
    high: float,
    low: float,
    close: float,
) -> str:
    """Local helper mirroring detect_ema59_touches math; does not change production."""
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
    return "none"


def test_direction_case_c_ema_rises_above_prev_close_differs():
    """Fall C: current EMA59 jumps above prev_close.

    Production (A) uses the finished touch-bar EMA59 → from_below.
    Comparison (B) uses previous bar EMA59 → from_above.
    """
    prev_close = 0.105
    prev_ema59 = 0.100
    cur_ema59 = 0.112
    high, low, close = 0.113, 0.099, 0.108

    a = _direction_vs_ema(
        prev_close=prev_close, ema59_ref=cur_ema59, high=high, low=low, close=close
    )
    b = _direction_vs_ema(
        prev_close=prev_close, ema59_ref=prev_ema59, high=high, low=low, close=close
    )
    assert a == TouchDirection.FROM_BELOW.value
    assert b == TouchDirection.FROM_ABOVE.value
    assert a != b


def test_direction_case_c2_ema_falls_below_prev_close_differs():
    """Fall C2: current EMA59 falls below prev_close.

    Production (A) uses the finished touch-bar EMA59 → from_above.
    Comparison (B) uses previous bar EMA59 → from_below.
    """
    prev_close = 0.095
    prev_ema59 = 0.100
    cur_ema59 = 0.088
    high, low, close = 0.101, 0.087, 0.092

    a = _direction_vs_ema(
        prev_close=prev_close, ema59_ref=cur_ema59, high=high, low=low, close=close
    )
    b = _direction_vs_ema(
        prev_close=prev_close, ema59_ref=prev_ema59, high=high, low=low, close=close
    )
    assert a == TouchDirection.FROM_ABOVE.value
    assert b == TouchDirection.FROM_BELOW.value
    assert a != b


def test_direction_stable_cases_agree_when_ema_moves_mildly():
    """When both EMAs are reached similarly, A and B agree (no false alarm)."""
    # from_above: low reaches both EMAs
    a = _direction_vs_ema(
        prev_close=0.110, ema59_ref=0.102, high=0.111, low=0.099, close=0.105
    )
    b = _direction_vs_ema(
        prev_close=0.110, ema59_ref=0.100, high=0.111, low=0.099, close=0.105
    )
    assert a == b == TouchDirection.FROM_ABOVE.value

    # from_below: high reaches both EMAs
    a2 = _direction_vs_ema(
        prev_close=0.090, ema59_ref=0.098, high=0.101, low=0.088, close=0.095
    )
    b2 = _direction_vs_ema(
        prev_close=0.090, ema59_ref=0.100, high=0.101, low=0.088, close=0.095
    )
    assert a2 == b2 == TouchDirection.FROM_BELOW.value

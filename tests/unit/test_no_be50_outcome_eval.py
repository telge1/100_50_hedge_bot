"""Unit tests for NO_BE50 exit evaluation (active strategy)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from signal_generator.pipeline.outcome_eval import (
    RESULT_LOSS,
    RESULT_OPEN,
    RESULT_WIN,
    evaluate_signal_be50,
    evaluate_signal_no_be50,
    outcome_needs_reevaluation_no_be50,
    summarize_trade_views,
)
from signal_generator.pipeline.versions import (
    STRATEGY_VERSION,
    STRATEGY_VERSION_BE50_FROZEN,
    STRATEGY_VERSION_NO_BE50,
    uses_be50_exit,
)
from signal_generator.strategy.wave_fade.parameters import INTRABAR_POLICY, SOURCE_COMMIT


def _bar(ts, o, h, l, c):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def _sig(*, side, tf, entry, entry_time, strategy_version=STRATEGY_VERSION_NO_BE50, sid="22222222-2222-2222-2222-222222222222"):
    import json
    import pandas as pd

    return {
        "signal_id": sid,
        "direction": side,
        "timeframe": tf,
        "signal_price": entry,
        "strategy_version": strategy_version,
        "candle_close_time": entry_time - timedelta(minutes=1),
        "metadata": json.dumps(
            {
                "entry_price": entry,
                "entry_time": entry_time.isoformat().replace("+00:00", "Z"),
                "entry_valid": True,
            }
        ),
    }


def _df(bars):
    import pandas as pd

    return pd.DataFrame(bars)


def test_versions_distinct_and_frozen_preserved():
    assert STRATEGY_VERSION_BE50_FROZEN == f"wave_fade_frozen_{SOURCE_COMMIT[:7]}"
    assert STRATEGY_VERSION_NO_BE50 == "wave_fade_no_be50_v1"
    assert STRATEGY_VERSION == STRATEGY_VERSION_NO_BE50
    assert STRATEGY_VERSION != STRATEGY_VERSION_BE50_FROZEN
    assert uses_be50_exit(STRATEGY_VERSION_BE50_FROZEN) is True
    assert uses_be50_exit(STRATEGY_VERSION_NO_BE50) is False
    assert INTRABAR_POLICY == "SL_FIRST"


def test_no_be50_long_tp_win():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.2, 99.9, 100.1),
        _bar(et + timedelta(minutes=1), 100.1, 101.5, 100.0, 101.2),
    ]
    v = evaluate_signal_no_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _df(bars), as_of=et + timedelta(minutes=2))
    assert v.result == RESULT_WIN
    assert v.display_result == "WIN"
    assert v.be50_activated is False
    assert v.pnl_pct == pytest.approx(1.0)


def test_no_be50_long_sl_loss():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(et, 100, 100.1, 98.5, 99.0)]
    v = evaluate_signal_no_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _df(bars), as_of=et + timedelta(minutes=1))
    assert v.result == RESULT_LOSS
    assert v.display_result == "LOSS"
    assert v.pnl_pct == pytest.approx(-1.0)


def test_no_be50_short_tp_win():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(et, 100, 100.1, 98.5, 99.0)]
    v = evaluate_signal_no_be50(_sig(side="SHORT", tf="15m", entry=100.0, entry_time=et), _df(bars), as_of=et + timedelta(minutes=1))
    assert v.result == RESULT_WIN


def test_no_be50_short_sl_loss():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(et, 100, 101.5, 99.9, 101.0)]
    v = evaluate_signal_no_be50(_sig(side="SHORT", tf="15m", entry=100.0, entry_time=et), _df(bars), as_of=et + timedelta(minutes=1))
    assert v.result == RESULT_LOSS


def test_no_be50_50pct_touch_does_nothing():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    # be_trig for LONG 15m at 100.5 — touch it without TP/SL
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 100.6, 100.0, 100.2),
    ]
    v = evaluate_signal_no_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _df(bars), as_of=et + timedelta(minutes=2))
    assert v.result == RESULT_OPEN
    assert v.be50_activated is False


def test_no_be50_return_to_entry_does_not_exit():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),  # 50% zone
        _bar(et + timedelta(minutes=1), 100.55, 100.4, 100.0, 100.05),  # back to entry
    ]
    v = evaluate_signal_no_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _df(bars), as_of=et + timedelta(minutes=2))
    assert v.result == RESULT_OPEN


def test_no_be50_later_tp_after_return_win():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 100.4, 100.0, 100.05),
        _bar(et + timedelta(minutes=2), 100.0, 101.5, 99.9, 101.2),
    ]
    v = evaluate_signal_no_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _df(bars), as_of=et + timedelta(minutes=3))
    assert v.result == RESULT_WIN
    assert v.pnl_pct == pytest.approx(1.0)


def test_no_be50_later_sl_after_return_loss():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 100.4, 100.0, 100.05),
        _bar(et + timedelta(minutes=2), 100.0, 100.1, 98.5, 99.0),
    ]
    v = evaluate_signal_no_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _df(bars), as_of=et + timedelta(minutes=3))
    assert v.result == RESULT_LOSS
    assert v.pnl_pct == pytest.approx(-1.0)


def test_no_be50_same_candle_sl_first():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(et, 100, 101.5, 98.5, 100.0)]
    v = evaluate_signal_no_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _df(bars), as_of=et + timedelta(minutes=1))
    assert v.result == RESULT_LOSS


def test_old_be50_still_exits_be_on_same_path():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 100.6, 99.95, 100.0),
    ]
    sig = _sig(
        side="LONG",
        tf="15m",
        entry=100.0,
        entry_time=et,
        strategy_version=STRATEGY_VERSION_BE50_FROZEN,
    )
    be = evaluate_signal_be50(sig, _df(bars), as_of=et + timedelta(minutes=2))
    assert be.result == "BE"
    assert be.pnl_pct == pytest.approx(0.0)
    nb = evaluate_signal_no_be50(sig, _df(bars), as_of=et + timedelta(minutes=2))
    assert nb.result == RESULT_OPEN


def test_summary_win_rate_excludes_open():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    win = evaluate_signal_no_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et, sid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        _df([_bar(et, 100, 101.5, 100.0, 101.0)]),
        as_of=et + timedelta(minutes=1),
    )
    loss = evaluate_signal_no_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et, sid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        _df([_bar(et, 100, 100.1, 98.5, 99.0)]),
        as_of=et + timedelta(minutes=1),
    )
    open_v = evaluate_signal_no_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et, sid="cccccccc-cccc-cccc-cccc-cccccccccccc"),
        _df([_bar(et, 100, 100.2, 99.9, 100.1)]),
        as_of=et + timedelta(minutes=1),
    )
    s = summarize_trade_views([win, loss, open_v])
    assert s["signals"] == 3
    assert s["wins"] == 1
    assert s["losses"] == 1
    assert s["open"] == 1
    assert s["win_rate_pct"] == pytest.approx(50.0)
    assert s["gross_profit_pct"] == pytest.approx(1.0)
    assert s["gross_loss_pct"] == pytest.approx(-1.0)
    assert s["total_pnl_pct"] == pytest.approx(0.0)
    assert outcome_needs_reevaluation_no_be50(win) is False
    assert outcome_needs_reevaluation_no_be50(open_v) is True

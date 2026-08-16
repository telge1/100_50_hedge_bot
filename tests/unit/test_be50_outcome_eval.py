"""Unit tests for Frozen BE50 trade outcome evaluator + No-BE50 counterfactual."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from signal_generator.pipeline.outcome_eval import (
    RESULT_BE,
    RESULT_LOSS,
    RESULT_OPEN,
    RESULT_WIN,
    display_result_for,
    evaluate_signal_be50,
    map_be50_reason_to_result,
    outcome_needs_reevaluation,
    simulate_no_be50_counterfactual,
    trade_outcome_from_metadata,
)
from signal_generator.strategy.wave_fade.be50 import simulate_be50_trade, trade_levels
from signal_generator.strategy.wave_fade.parameters import FEE_PCT, INTRABAR_POLICY


def _bar(ts: datetime, o: float, h: float, l: float, c: float) -> dict:
    return {
        "timestamp": ts,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": 1.0,
    }


def _c1m(bars: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(bars)


def _sig(*, side: str, tf: str, entry: float, entry_time: datetime, sid: str = "11111111-1111-1111-1111-111111111111") -> dict:
    import json

    return {
        "signal_id": sid,
        "direction": side,
        "timeframe": tf,
        "signal_price": entry,
        "candle_close_time": entry_time - timedelta(minutes=1),
        "strategy_version": "test",
        "metadata": json.dumps(
            {
                "entry_price": entry,
                "entry_time": entry_time.isoformat().replace("+00:00", "Z"),
                "entry_valid": True,
            }
        ),
    }


def test_sl_first_policy_constant():
    assert INTRABAR_POLICY == "SL_FIRST"


def test_map_reasons():
    assert map_be50_reason_to_result("TP") == RESULT_WIN
    assert map_be50_reason_to_result("SL") == RESULT_LOSS
    assert map_be50_reason_to_result("BE") == RESULT_BE
    assert map_be50_reason_to_result("TIMEOUT") == RESULT_OPEN
    assert map_be50_reason_to_result("DATA_MISSING") == RESULT_OPEN


def test_display_result_mapping():
    assert display_result_for(RESULT_WIN, None) == "WIN"
    assert display_result_for(RESULT_LOSS, None) == "LOSS"
    assert display_result_for(RESULT_OPEN, None) == "OPEN"
    assert display_result_for(RESULT_BE, RESULT_WIN) == "BE / WIN"
    assert display_result_for(RESULT_BE, RESULT_LOSS) == "BE / LOSS"
    assert display_result_for(RESULT_BE, RESULT_OPEN) == "BE / OPEN"
    assert display_result_for(RESULT_BE, None) == "BE / OPEN"


def test_long_direct_tp_win():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    # 15m: TP +1%, SL -1%; entry 100 → tp 101, sl 99, be_trig 100.5
    bars = [
        _bar(et, 100, 100.2, 99.9, 100.1),
        _bar(et + timedelta(minutes=1), 100.1, 101.5, 100.0, 101.2),
    ]
    view = evaluate_signal_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _c1m(bars), as_of=et + timedelta(minutes=2))
    assert view.result == RESULT_WIN
    assert view.display_result == "WIN"
    assert view.exit_reason == "TP"
    assert view.pnl_pct == pytest.approx(1.0)
    assert view.pnl_basis == "gross"
    assert view.exit_price == pytest.approx(101.0)
    assert view.duration_seconds == 60
    assert view.counterfactual_no_be_result is None


def test_long_direct_sl_loss():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.1, 98.5, 99.0),
    ]
    view = evaluate_signal_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _c1m(bars), as_of=et + timedelta(minutes=1))
    assert view.result == RESULT_LOSS
    assert view.display_result == "LOSS"
    assert view.exit_reason == "SL"
    assert view.pnl_pct == pytest.approx(-1.0)
    assert view.be50_activated is False
    assert view.counterfactual_no_be_result is None


def test_long_be50_then_entry_be():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    # Arm BE at 100.5, then return to entry — CF still open (no TP/SL yet)
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),  # arm
        _bar(et + timedelta(minutes=1), 100.55, 100.6, 99.95, 100.0),  # BE
    ]
    view = evaluate_signal_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _c1m(bars), as_of=et + timedelta(minutes=2))
    assert view.result == RESULT_BE
    assert view.be50_activated is True
    assert view.pnl_pct == pytest.approx(0.0)
    assert view.exit_price == pytest.approx(100.0)
    assert view.counterfactual_no_be_result == RESULT_OPEN
    assert view.display_result == "BE / OPEN"


def test_long_be50_then_tp_win():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 101.2, 100.2, 101.0),
    ]
    view = evaluate_signal_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _c1m(bars), as_of=et + timedelta(minutes=2))
    assert view.result == RESULT_WIN
    assert view.display_result == "WIN"
    assert view.be50_activated is True
    assert view.pnl_pct == pytest.approx(1.0)
    assert view.counterfactual_no_be_result is None


def test_short_direct_tp_win():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.1, 98.5, 99.0),
    ]
    view = evaluate_signal_be50(_sig(side="SHORT", tf="15m", entry=100.0, entry_time=et), _c1m(bars), as_of=et + timedelta(minutes=1))
    assert view.result == RESULT_WIN
    assert view.display_result == "WIN"
    assert view.pnl_pct == pytest.approx(1.0)


def test_short_direct_sl_loss():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 101.5, 99.9, 101.0),
    ]
    view = evaluate_signal_be50(_sig(side="SHORT", tf="15m", entry=100.0, entry_time=et), _c1m(bars), as_of=et + timedelta(minutes=1))
    assert view.result == RESULT_LOSS
    assert view.display_result == "LOSS"
    assert view.pnl_pct == pytest.approx(-1.0)


def test_short_be50_then_entry_be():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    # be_trig = 99.5 for SHORT 15m
    bars = [
        _bar(et, 100, 99.9, 99.4, 99.45),
        _bar(et + timedelta(minutes=1), 99.45, 100.05, 99.4, 100.0),
    ]
    view = evaluate_signal_be50(_sig(side="SHORT", tf="15m", entry=100.0, entry_time=et), _c1m(bars), as_of=et + timedelta(minutes=2))
    assert view.result == RESULT_BE
    assert view.be50_activated is True
    assert view.pnl_pct == pytest.approx(0.0)
    assert view.display_result == "BE / OPEN"
    assert view.counterfactual_no_be_result == RESULT_OPEN


def test_short_be50_then_tp_win():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 99.9, 99.4, 99.45),
        _bar(et + timedelta(minutes=1), 99.45, 99.5, 98.8, 99.0),
    ]
    view = evaluate_signal_be50(_sig(side="SHORT", tf="15m", entry=100.0, entry_time=et), _c1m(bars), as_of=et + timedelta(minutes=2))
    assert view.result == RESULT_WIN
    assert view.be50_activated is True


def test_same_candle_tp_and_sl_long_sl_first():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(et, 100, 101.5, 98.5, 100.0)]
    view = evaluate_signal_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _c1m(bars), as_of=et + timedelta(minutes=1))
    assert view.result == RESULT_LOSS
    assert "AMBIGUOUS" in view.ambiguity_flag or view.exit_reason == "SL"


def test_open_when_no_exit_yet():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.2, 99.9, 100.1),
        _bar(et + timedelta(minutes=1), 100.1, 100.3, 100.0, 100.2),
    ]
    view = evaluate_signal_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), _c1m(bars), as_of=et + timedelta(minutes=2))
    assert view.result == RESULT_OPEN
    assert view.display_result == "OPEN"
    assert view.pnl_pct is None
    assert view.duration_seconds == 60  # last bar open - entry


def test_parity_with_simulate_be50_trade():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 100.6, 99.95, 100.0),
    ]
    df = _c1m(bars)
    tr = pd.Series(
        {
            "entry_time": et,
            "entry_price": 100.0,
            "side": "LONG",
            "highest_tf_reached": "15m",
            "exit_time": et,
        }
    )
    levels = trade_levels(tr)
    sim = simulate_be50_trade(tr, df, levels)
    view = evaluate_signal_be50(_sig(side="LONG", tf="15m", entry=100.0, entry_time=et), df, as_of=et + timedelta(minutes=2))
    assert map_be50_reason_to_result(sim["be50_reason"]) == view.result
    assert sim["be50_reason"] == view.exit_reason
    assert float(sim["be50_exit_price"]) == pytest.approx(view.exit_price)
    assert bool(sim["be50_triggered"]) == view.be50_activated
    # gross preferred; net = gross - FEE
    assert view.pnl_pct == pytest.approx(sim["be50_gross_pct"])
    assert sim["be50_net_pct"] == pytest.approx(sim["be50_gross_pct"] - FEE_PCT)


def test_only_closed_1m_used_as_of_truncation():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    # Future bar would hit TP but as_of excludes it
    bars = [
        _bar(et, 100, 100.2, 99.9, 100.1),
        _bar(et + timedelta(minutes=1), 100.1, 101.5, 100.0, 101.2),
    ]
    view = evaluate_signal_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et),
        _c1m(bars),
        as_of=et + timedelta(minutes=1),  # only first bar closed (open=et)
    )
    assert view.result == RESULT_OPEN


# --- Counterfactual classification ---


def test_long_be_later_tp_be_win():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),  # arm BE50
        _bar(et + timedelta(minutes=1), 100.55, 100.6, 99.95, 100.0),  # frozen BE at entry
        _bar(et + timedelta(minutes=2), 100.0, 101.5, 99.9, 101.2),  # no-BE would hit TP
    ]
    view = evaluate_signal_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et),
        _c1m(bars),
        as_of=et + timedelta(minutes=3),
    )
    assert view.result == RESULT_BE
    assert view.pnl_pct == pytest.approx(0.0)
    assert view.counterfactual_no_be_result == RESULT_WIN
    assert view.counterfactual_no_be_pnl_pct == pytest.approx(1.0)
    assert view.counterfactual_no_be_exit_reason == "TP"
    assert view.display_result == "BE / WIN"


def test_long_be_later_sl_be_loss():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 100.6, 99.95, 100.0),  # frozen BE
        _bar(et + timedelta(minutes=2), 100.0, 100.2, 98.5, 99.0),  # original SL
    ]
    view = evaluate_signal_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et),
        _c1m(bars),
        as_of=et + timedelta(minutes=3),
    )
    assert view.result == RESULT_BE
    assert view.pnl_pct == pytest.approx(0.0)
    assert view.counterfactual_no_be_result == RESULT_LOSS
    assert view.counterfactual_no_be_pnl_pct == pytest.approx(-1.0)
    assert view.counterfactual_no_be_exit_reason == "SL"
    assert view.display_result == "BE / LOSS"


def test_short_be_later_tp_be_win():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 99.9, 99.4, 99.45),  # arm
        _bar(et + timedelta(minutes=1), 99.45, 100.05, 99.4, 100.0),  # frozen BE
        _bar(et + timedelta(minutes=2), 100.0, 100.1, 98.5, 99.0),  # no-BE TP
    ]
    view = evaluate_signal_be50(
        _sig(side="SHORT", tf="15m", entry=100.0, entry_time=et),
        _c1m(bars),
        as_of=et + timedelta(minutes=3),
    )
    assert view.result == RESULT_BE
    assert view.pnl_pct == pytest.approx(0.0)
    assert view.counterfactual_no_be_result == RESULT_WIN
    assert view.display_result == "BE / WIN"


def test_short_be_later_sl_be_loss():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 99.9, 99.4, 99.45),
        _bar(et + timedelta(minutes=1), 99.45, 100.05, 99.4, 100.0),  # frozen BE
        _bar(et + timedelta(minutes=2), 100.0, 101.5, 99.9, 101.0),  # original SL
    ]
    view = evaluate_signal_be50(
        _sig(side="SHORT", tf="15m", entry=100.0, entry_time=et),
        _c1m(bars),
        as_of=et + timedelta(minutes=3),
    )
    assert view.result == RESULT_BE
    assert view.pnl_pct == pytest.approx(0.0)
    assert view.counterfactual_no_be_result == RESULT_LOSS
    assert view.display_result == "BE / LOSS"


def test_no_be_uses_original_sl_not_entry():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    # After arming, price returns to entry (would BE under BE50) then continues —
    # No-BE must NOT exit at entry; only at original SL 99.
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 100.4, 100.0, 100.05),  # touch entry
        _bar(et + timedelta(minutes=2), 100.05, 100.2, 99.5, 99.6),  # above SL
        _bar(et + timedelta(minutes=3), 99.6, 99.7, 98.8, 99.0),  # hit SL 99
    ]
    tr = pd.Series(
        {
            "entry_time": et,
            "entry_price": 100.0,
            "side": "LONG",
            "highest_tf_reached": "15m",
            "exit_time": et,
        }
    )
    levels = trade_levels(tr)
    assert levels["sl"] == pytest.approx(99.0)
    cf = simulate_no_be50_counterfactual(
        side="LONG",
        entry_price=100.0,
        entry_time=et,
        levels=levels,
        df=_c1m(bars),
        hold_min=240,
    )
    assert cf["result"] == RESULT_LOSS
    assert cf["exit_price"] == pytest.approx(99.0)
    assert cf["exit_reason"] == "SL"
    # Must not have exited at bar1 entry touch
    assert cf["duration_seconds"] == 180


def test_same_candle_tp_sl_counterfactual_sl_first():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    # Frozen: arm then BE on bar1 path; CF on later same-candle TP+SL uses SL_FIRST
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 100.4, 99.95, 100.0),  # BE
        _bar(et + timedelta(minutes=2), 100.0, 101.5, 98.5, 100.0),  # both
    ]
    view = evaluate_signal_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et),
        _c1m(bars),
        as_of=et + timedelta(minutes=3),
    )
    assert view.result == RESULT_BE
    assert view.counterfactual_no_be_result == RESULT_LOSS
    assert view.display_result == "BE / LOSS"


def test_be_open_later_updates_to_be_win():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars_open = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 100.6, 99.95, 100.0),
    ]
    v1 = evaluate_signal_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et),
        _c1m(bars_open),
        as_of=et + timedelta(minutes=2),
    )
    assert v1.display_result == "BE / OPEN"
    assert outcome_needs_reevaluation(v1) is True

    bars_win = bars_open + [
        _bar(et + timedelta(minutes=2), 100.0, 101.5, 99.9, 101.2),
    ]
    v2 = evaluate_signal_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et),
        _c1m(bars_win),
        as_of=et + timedelta(minutes=3),
    )
    assert v2.result == RESULT_BE
    assert v2.display_result == "BE / WIN"
    assert outcome_needs_reevaluation(v2) is False


def test_be_open_later_updates_to_be_loss():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars_open = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 100.6, 99.95, 100.0),
    ]
    bars_loss = bars_open + [
        _bar(et + timedelta(minutes=2), 100.0, 100.1, 98.5, 99.0),
    ]
    v2 = evaluate_signal_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et),
        _c1m(bars_loss),
        as_of=et + timedelta(minutes=3),
    )
    assert v2.display_result == "BE / LOSS"
    assert outcome_needs_reevaluation(v2) is False


def test_as_of_truncation_applies_to_counterfactual():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 100.6, 99.95, 100.0),
        _bar(et + timedelta(minutes=2), 100.0, 101.5, 99.9, 101.2),  # future CF TP
    ]
    view = evaluate_signal_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et),
        _c1m(bars),
        as_of=et + timedelta(minutes=2),  # exclude last bar
    )
    assert view.result == RESULT_BE
    assert view.display_result == "BE / OPEN"


def test_api_metadata_roundtrip_display_result():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [
        _bar(et, 100, 100.6, 100.1, 100.55),
        _bar(et + timedelta(minutes=1), 100.55, 100.6, 99.95, 100.0),
        _bar(et + timedelta(minutes=2), 100.0, 101.5, 99.9, 101.2),
    ]
    view = evaluate_signal_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et),
        _c1m(bars),
        as_of=et + timedelta(minutes=3),
    )
    api = view.as_api()
    assert api["display_result"] == "BE / WIN"
    assert api["frozen_result"] == RESULT_BE
    assert api["pnl_pct"] == pytest.approx(0.0)
    assert api["counterfactual_no_be_pnl_pct"] == pytest.approx(1.0)
    restored = trade_outcome_from_metadata(api, signal_id=view.signal_id)
    assert restored is not None
    assert restored.display_result == "BE / WIN"
    assert restored.counterfactual_no_be_result == RESULT_WIN


def test_win_loss_immutable_for_reeval():
    et = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(et, 100, 101.5, 100.0, 101.2)]
    win = evaluate_signal_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et),
        _c1m(bars),
        as_of=et + timedelta(minutes=1),
    )
    assert win.result == RESULT_WIN
    assert outcome_needs_reevaluation(win) is False
    loss_bars = [_bar(et, 100, 100.1, 98.5, 99.0)]
    loss = evaluate_signal_be50(
        _sig(side="LONG", tf="15m", entry=100.0, entry_time=et),
        _c1m(loss_bars),
        as_of=et + timedelta(minutes=1),
    )
    assert loss.result == RESULT_LOSS
    assert outcome_needs_reevaluation(loss) is False

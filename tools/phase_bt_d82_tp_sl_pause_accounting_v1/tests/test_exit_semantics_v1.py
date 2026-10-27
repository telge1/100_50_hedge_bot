import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exit_semantics_audit_v1 import intrabar_exit_decision


def test_short_sl_tp_direction_and_ambiguity() -> None:
    reason, price, amb = intrabar_exit_decision(
        ohlc={"h": 1.1, "l": 0.9},
        sl_price=1.05,
        tp_price=0.95,
        policy="no_trade_ambiguous",
    )
    assert reason == "AMBIGUOUS_INTRABAR"
    assert price is None
    assert amb is True

    reason2, price2, amb2 = intrabar_exit_decision(
        ohlc={"h": 1.06, "l": 0.99},
        sl_price=1.05,
        tp_price=0.95,
        policy="no_trade_ambiguous",
    )
    assert reason2 == "SL"
    assert price2 == 1.05
    assert amb2 is False

    reason3, price3, _ = intrabar_exit_decision(
        ohlc={"h": 1.0, "l": 0.94},
        sl_price=1.05,
        tp_price=0.95,
        policy="no_trade_ambiguous",
    )
    assert reason3 == "TP"
    assert price3 == 0.95

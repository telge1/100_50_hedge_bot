import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "phase_bt_d82_tp_sl_pause_accounting_v1"))
from exit_semantics_audit_v1 import intrabar_exit_decision


def test_no_trade_ambiguous_both_hit() -> None:
    r, p, a = intrabar_exit_decision(
        ohlc={"h": 1.0, "l": 0.5},
        sl_price=0.9,
        tp_price=0.6,
        policy="no_trade_ambiguous",
    )
    assert r == "AMBIGUOUS_INTRABAR"
    assert p is None
    assert a is True

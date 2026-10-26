from contracts_v1 import RegimeContextV1
from regime_classifier_v1 import RegimeClassifierV1


def test_regime_uses_only_provided_close_context():
    clf = RegimeClassifierV1()
    early = clf.regime_at_close(
        RegimeContextV1(
            decision_close_utc="2026-09-09T16:30:00Z",
            ohlc_to_close={"o": 0.2, "h": 0.2, "l": 0.19, "c": 0.195},
        )
    )
    late = clf.regime_at_close(
        RegimeContextV1(
            decision_close_utc="2026-09-09T21:25:00Z",
            ohlc_to_close={"o": 0.1, "h": 0.1, "l": 0.09, "c": 0.095},
        )
    )
    assert early.regime == late.regime == "UNCERTAIN"
    assert early.evidence[0] == late.evidence[0]

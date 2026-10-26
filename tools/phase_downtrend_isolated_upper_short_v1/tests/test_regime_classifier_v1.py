from regime_classifier_v1 import RegimeClassifierV1
from contracts_v1 import RegimeContextV1


def test_default_regime_uncertain():
    r = RegimeClassifierV1().regime_at_close(
        RegimeContextV1(decision_close_utc="2026-09-09T16:30:00Z", ohlc_to_close={"o": 1, "h": 1, "l": 1, "c": 1})
    )
    assert r.regime == "UNCERTAIN"
    assert r.blocks_new_short_intent is True


def test_missing_close_fails_closed():
    r = RegimeClassifierV1().regime_at_close(RegimeContextV1(decision_close_utc="", ohlc_to_close=None))
    assert r.regime == "UNCERTAIN"
    assert "MISSING_DECISION_CLOSE_UTC" in r.evidence


def test_missing_ohlc_fails_closed():
    r = RegimeClassifierV1().regime_at_close(RegimeContextV1(decision_close_utc="2026-09-09T16:30:00Z"))
    assert r.regime == "UNCERTAIN"
    assert "MISSING_OHLC_AT_CLOSE" in r.evidence

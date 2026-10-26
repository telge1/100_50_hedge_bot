from isolated_upper_eligibility_v1 import NOT_YET_VALIDATED, evaluate_isolated_upper_eligibility


def test_eligibility_defaults_false():
    snap = {"upper_bands": [], "lower_bands": [], "pipeline_line_id": "p", "config_hash": "h"}
    r = evaluate_isolated_upper_eligibility(
        decision_close_utc="2026-09-09T16:30:00Z",
        baseline_signal=None,
        snapshot=snap,
    )
    assert r.eligible is False
    assert r.reason == NOT_YET_VALIDATED

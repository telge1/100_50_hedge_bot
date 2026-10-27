from window_selection_v1 import PILOT_WINDOWS, DEVELOPMENT_EVIDENCE


def test_pilot_windows_deterministic() -> None:
    assert len(PILOT_WINDOWS) >= 5
    ids = [w["window_id"] for w in PILOT_WINDOWS]
    assert len(ids) == len(set(ids))


def test_dev_windows_flagged() -> None:
    dev = [w for w in PILOT_WINDOWS if w["evidence_class"] in DEVELOPMENT_EVIDENCE]
    assert len(dev) == 2

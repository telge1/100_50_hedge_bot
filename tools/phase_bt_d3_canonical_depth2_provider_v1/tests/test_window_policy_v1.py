from __future__ import annotations

import pytest

from window_policy_v1 import assert_on_5m_decision_close, causal_window_bounds, parity_fixture_bounds


def test_parity_fixture_sep25():
    assert parity_fixture_bounds("2026-09-25T11:00:00Z") == (
        "2026-09-23T11:00:00Z",
        "2026-09-25T11:05:00Z",
    )


def test_causal_window_48h():
    vf, vt = causal_window_bounds("2026-09-25T11:00:00Z")
    assert vf == "2026-09-23T11:05:00Z"
    assert vt == "2026-09-25T11:05:00Z"


def test_rejects_off_grid():
    with pytest.raises(ValueError):
        assert_on_5m_decision_close("2026-09-25T11:01:00Z")

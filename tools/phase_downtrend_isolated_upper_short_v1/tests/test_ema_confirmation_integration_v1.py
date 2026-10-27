"""BT-D66 EMA confirmation integration tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from contracts_v1 import PureEmaConfirmationV1, RegimeContextV1  # noqa: E402
from density_bridge_v1 import FixtureDensitySessionV1  # noqa: E402
from entry_gate_v1 import evaluate_entry_gate  # noqa: E402
from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
from pure_ema_confirmation_v1 import PureEmaConfirmationGateV1  # noqa: E402
from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402

FIXTURE = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1/fixtures/synthetic_integration_v1.json"


class _StubEmaProvider:
    def __init__(self, decisions: dict[str, str]) -> None:
        self._decisions = decisions

    def confirm_at_close(self, ts: str) -> dict:
        d = self._decisions.get(ts, "UNCERTAIN")
        return {
            "decision_close_utc": ts,
            "ema_ready": True,
            "ema_structure_decision": d,
            "decision_reason_code": f"STUB_{d}",
            "chart_review_focus": "stub",
            "ema_path": "trp_research_charts",
            "bt_d65_rules_module": "test",
        }


def _stub_gate(decisions: dict[str, str]) -> PureEmaConfirmationGateV1:
    return PureEmaConfirmationGateV1(provider=_StubEmaProvider(decisions))


def _fake_sig():
    return SimpleNamespace(
        pool_lower=0.0995,
        pool_upper=0.1,
        pool_key="0.0995|0.1",
        origin_ids="a|b",
        entry_level=0.0995,
    )


def _isolated_enabled():
    return {
        "isolated_strategy_v1": {
            "pure_ema_confirmation": {"enabled": True},
            "isolated_upper_eligibility": {"research_pass_on_baseline_signal": True},
        }
    }


def test_no_pool_candidate_no_authorization():
    pause = ShortPauseControllerV1()
    pause.set_ready_for_research_only()
    decision = evaluate_entry_gate(
        context=RegimeContextV1(
            decision_close_utc="2026-01-01T12:05:00Z",
            ohlc_to_close={"o": 0.1, "h": 0.1, "l": 0.09, "c": 0.095},
        ),
        pause=pause,
        baseline_signal=None,
        snapshot={"upper_bands": [], "lower_bands": []},
        ema_gate=_stub_gate({"2026-01-01T12:05:00Z": "SHORT_OK"}),
        isolated_strategy_config=_isolated_enabled(),
    )
    assert decision.allow_new_short_intent is False
    assert "BASELINE_NO_SIGNAL" in decision.reasons


def test_pool_plus_ema_short_ok_authorized():
    pause = ShortPauseControllerV1()
    pause.set_ready_for_research_only()
    decision = evaluate_entry_gate(
        context=RegimeContextV1(
            decision_close_utc="2026-01-01T12:05:00Z",
            ohlc_to_close={"o": 0.1, "h": 0.1, "l": 0.09, "c": 0.095},
        ),
        pause=pause,
        baseline_signal=_fake_sig(),
        snapshot={"upper_bands": [], "lower_bands": [], "pipeline_line_id": "t"},
        ema_gate=_stub_gate({"2026-01-01T12:05:00Z": "SHORT_OK"}),
        isolated_strategy_config=_isolated_enabled(),
    )
    assert decision.allow_new_short_intent is True


def test_pool_plus_ema_short_no_or_uncertain_blocked():
    pause = ShortPauseControllerV1()
    pause.set_ready_for_research_only()
    for ema_dec in ("SHORT_NO", "UNCERTAIN"):
        decision = evaluate_entry_gate(
            context=RegimeContextV1(
                decision_close_utc="2026-01-01T12:05:00Z",
                ohlc_to_close={"o": 0.1, "h": 0.1, "l": 0.09, "c": 0.095},
            ),
            pause=pause,
            baseline_signal=_fake_sig(),
            snapshot={"upper_bands": [], "lower_bands": []},
            ema_gate=_stub_gate({"2026-01-01T12:05:00Z": ema_dec}),
            isolated_strategy_config=_isolated_enabled(),
        )
        assert decision.allow_new_short_intent is False
        assert any(r.startswith(f"EMA_{ema_dec}") for r in decision.reasons)


def test_missing_ema_provider_blocks():
    pause = ShortPauseControllerV1()
    pause.set_ready_for_research_only()
    decision = evaluate_entry_gate(
        context=RegimeContextV1(decision_close_utc="2026-01-01T12:05:00Z", ohlc_to_close={"o": 1, "h": 1, "l": 1, "c": 1}),
        pause=pause,
        baseline_signal=_fake_sig(),
        snapshot={},
        ema_gate=PureEmaConfirmationGateV1(provider=None),
        isolated_strategy_config=_isolated_enabled(),
    )
    assert decision.allow_new_short_intent is False


def test_pause_overrides_ema_short_ok():
    pause = ShortPauseControllerV1()
    decision = evaluate_entry_gate(
        context=RegimeContextV1(decision_close_utc="2026-01-01T12:05:00Z", ohlc_to_close={"o": 1, "h": 1, "l": 1, "c": 1}),
        pause=pause,
        baseline_signal=_fake_sig(),
        snapshot={},
        ema_gate=_stub_gate({"2026-01-01T12:05:00Z": "SHORT_OK"}),
        isolated_strategy_config=_isolated_enabled(),
    )
    assert decision.allow_new_short_intent is False
    assert any(r.startswith("PAUSE_") for r in decision.reasons)


def _enrich(close: str, raw: dict) -> dict:
    return {
        "decision_close_utc": close,
        "as_of_utc": close,
        "upper_bands": raw["upper_bands"],
        "lower_bands": raw["lower_bands"],
        "pipeline_line_id": "fixture",
        "config_hash": "fixture",
    }


def test_pending_canceled_on_ema_loss():
    fix = json.loads(FIXTURE.read_text())
    bars = ["2026-01-01T12:05:00Z", "2026-01-01T12:10:00Z"]
    snaps = {k: _enrich(k, fix["snapshots"][k]) for k in bars}
    from config_loader_v1 import load_validated_baseline_config

    cfg = load_validated_baseline_config()
    sig = _fake_sig()
    gate = _stub_gate(
        {
            "2026-01-01T12:05:00Z": "SHORT_OK",
            "2026-01-01T12:10:00Z": "SHORT_NO",
        }
    )
    pause = ShortPauseControllerV1()
    pause.set_ready_for_research_only()
    isolated = _isolated_enabled()["isolated_strategy_v1"]

    with (
        patch("isolated_chronological_runner_v1.detect_short_signal", side_effect=[sig, sig]),
        patch("isolated_chronological_runner_v1._try_fill", return_value=False),
    ):
        out = run_isolated_chronological_replay(
            config=cfg,
            isolated_bundle=isolated,
            prices=fix["prices"],
            bars=bars,
            density_session=FixtureDensitySessionV1(snaps),
            pause=pause,
            ema_gate=gate,
        )
    assert out["pending_orders_created"] >= 1
    assert out["pending_cancels_ema"] >= 1
    assert any(e.get("cancel_reason") == "EMA_PERMISSION_LOST" for e in out["pending_cancel_audit"])


@pytest.mark.skipif(not (ROOT / "results/phase_bt_d60_ema_history_v1/ema_history_manifest.json").is_file(), reason="no d60")
def test_reference_timestamps_match_bt_d65():
    manifest = json.loads(
        (ROOT / "results/phase_bt_d60_ema_history_v1/ema_history_manifest.json").read_text()
    )
    if manifest.get("verdict") != "EMA_HISTORY_VALIDATED":
        pytest.skip("d60 not validated")
    d60_pkg = ROOT / "tools/phase_bt_d60_ema_history_validation_v1"
    sys.path.insert(0, str(d60_pkg))
    from datetime import timedelta

    from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch
    from validation_config_v1 import REFERENCE_WARMUP_HOURS

    from pure_ema_series_provider_v1 import PureEmaSeriesProviderV1

    probes = [
        ("2026-09-09T20:55:00Z", "SHORT_OK"),
        ("2026-09-11T12:00:00Z", "UNCERTAIN"),
        ("2026-08-05T03:30:00Z", "SHORT_OK"),
        ("2026-08-05T09:30:00Z", "SHORT_NO"),
        ("2026-08-05T21:30:00Z", "UNCERTAIN"),
        ("2026-08-05T23:00:00Z", "UNCERTAIN"),
        ("2026-08-06T00:00:00Z", "SHORT_OK"),
        ("2026-08-12T08:30:00Z", "SHORT_OK"),
    ]
    lo = _parse_utc("2026-08-01T00:00:00Z") - timedelta(hours=REFERENCE_WARMUP_HOURS + 24)
    hi = _parse_utc("2026-09-30T00:00:00Z")
    keys, prices, _ = load_5m_ohlc_ch(_iso(lo), _iso(hi))
    provider = PureEmaSeriesProviderV1(bar_keys=keys, prices=prices)
    gate = PureEmaConfirmationGateV1(provider=provider)
    for ts, expected in probes:
        assert gate.at_close(ts).decision == expected

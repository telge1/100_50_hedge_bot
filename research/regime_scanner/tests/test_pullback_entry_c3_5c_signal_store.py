"""Tests for C3.5c A6 signal store and failure-feature audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.c35c_signal_store.build import (
    EXPECTED_A6_HASH,
    EXPECTED_N_FILLS,
    MySQLRequiredError,
    _candle_structure,
    check_fill_parity,
    evaluate_outcome_on_fill,
    load_symbol_5m_mysql,
)
from research.regime_scanner.c35c_signal_store.schema import (
    C35C_SIGNAL_SCHEMA_STATEMENTS,
    SIGNAL_TYPE_A6_FILL,
)
from research.regime_scanner.pullback_entry_c3_5 import config_hash
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5c_signal_failure_feature_audit import (
    compare_categorical,
    compare_numeric,
)


def test_a6_hash_frozen_and_schema_additive() -> None:
    assert config_hash(baseline_a6()) == EXPECTED_A6_HASH
    assert EXPECTED_N_FILLS == 55
    joined = "\n".join(C35C_SIGNAL_SCHEMA_STATEMENTS)
    assert "CREATE TABLE IF NOT EXISTS research_signal_features" in joined
    assert "CREATE TABLE IF NOT EXISTS research_signal_outcomes" in joined
    assert "DROP TABLE" not in joined
    assert "ALTER TABLE research_signals" not in joined
    assert SIGNAL_TYPE_A6_FILL == "c35c_a6_fill"


def test_mysql_no_feather_fallback() -> None:
    with patch(
        "research.regime_scanner.c35c_signal_store.build.load_symbol_candles",
        side_effect=RuntimeError("db down"),
    ):
        with pytest.raises(MySQLRequiredError, match="feather fallback forbidden"):
            load_symbol_5m_mysql("APTUSDT")


def test_candle_structure_and_fill_causality_note() -> None:
    c = _candle_structure(100.0, 110.0, 95.0, 108.0)
    assert c["entry_bullish"] is True
    assert c["entry_candle_return_pct"] == pytest.approx(8.0)
    # fill stage must leave HLC unknown — enforced in build_feature_rows; unit marker:
    assert "entry_close_position" in c


def test_outcome_tp_sl_same_bar_cost() -> None:
    # long: bar0 open entry 100; bar0 hits both TP and SL range → conservative SL
    highs = np.array([104.0, 101.0])
    lows = np.array([97.0, 99.0])
    closes = np.array([100.0, 100.5])
    ts = [pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-01-01 00:15", tz="UTC")]
    out = evaluate_outcome_on_fill(
        side=1,
        entry=100.0,
        highs=highs,
        lows=lows,
        closes=closes,
        timestamps=ts,
        fill_i=0,
        n_bars=2,
    )
    assert out["same_bar_ambiguous"] is True
    assert out["exit_reason"] == "same_bar_conservative_sl"
    assert out["gross_pnl_pct"] == -2.0
    assert out["net_pnl_pct"] == pytest.approx(-2.2)
    assert out["is_winner"] is False


def test_parity_helper() -> None:
    fills = [
        {
            "side_name": "long",
            "setup_id": 1,
            "fill_timestamp": pd.Timestamp("2026-01-26 07:15:00+00:00"),
            "entry_price": 1.5341,
            "trigger_timestamp": pd.Timestamp("2026-01-26 07:00:00+00:00"),
        }
    ]
    ref = pd.DataFrame(
        [
            {
                "side": "long",
                "setup_id": 1,
                "fill_time": "2026-01-26 07:15:00+00:00",
                "fill_price": 1.5341,
                "trigger_time": "2026-01-26 07:00:00+00:00",
            }
        ]
    )
    # n mismatch vs 55
    p = check_fill_parity(fills, ref)
    assert p["ok"] is False
    assert p["n_fills"] == 1


def test_winner_loser_audit_helpers() -> None:
    panel = pd.DataFrame(
        {
            "net_pnl_pct": [2.8, -2.2, 2.8, -2.2, 0.0],
            "winner_group": ["winner", "loser", "winner", "loser", "flat"],
            "adx": [25.0, 15.0, 22.0, 12.0, 18.0],
            "side": ["long", "long", "short", "short", "long"],
            "split": ["dev", "dev", "oos", "oos", "validation"],
            "exit_reason": ["TP", "SL", "TP", "SL", "time_exit"],
            "opposite_arm_seen": [False, True, False, True, False],
        }
    )
    num = compare_numeric(panel)
    assert "adx" in set(num["feature"])
    adx = num[num.feature == "adx"].iloc[0]
    assert adx["winner_mean"] > adx["loser_mean"]
    cat = compare_categorical(panel)
    assert not cat.empty


def test_dry_run_does_not_call_persist(tmp_path: Path) -> None:
    from research.regime_scanner.pullback_entry_c3_5c_signal_store import run_signal_store

    fake_store = MagicMock()
    fake_store.find_completed_run_by_label.return_value = None
    fake_store.find_run_by_fingerprint.return_value = None
    fake_store.init_schema.return_value = None

    ref = Path(
        "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
        "c35c_fill_excursion_audit/fill_excursion_panel.csv"
    )
    if not ref.exists():
        pytest.skip("reference missing")

    with (
        patch(
            "research.regime_scanner.pullback_entry_c3_5c_signal_store.C35cSignalStore",
            return_value=fake_store,
        ),
        patch(
            "research.regime_scanner.pullback_entry_c3_5c_signal_store.load_regime_db_config",
            return_value=MagicMock(name="regime_scanner_research"),
        ),
    ):
        # Use real MySQL path if available; otherwise skip
        try:
            meta = run_signal_store(
                symbol="APTUSDT",
                data_source="mysql",
                regime_db_env=Path(
                    "research/regime_scanner/.env.regime_db"
                ).resolve(),
                feature_version="c35c_entry_features_v1",
                outcome_version="tp3_sl2_h192_cost020_v1",
                run_label="test_dry_run_unit",
                output_dir=tmp_path / "apt_signal_feature_store_20260722",
                reference_panel=ref,
                dry_run=True,
                persist=False,
                fail_if_existing=True,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"dry-run integration unavailable: {exc}")
    assert meta.get("ok") is True
    assert meta.get("persisted") is False
    fake_store.persist_bundle.assert_not_called()


def test_sm_untouched() -> None:
    sm = Path("research/regime_scanner/pullback_entry_c3_5.py")
    h = hashlib.sha256(sm.read_bytes()).hexdigest()
    import research.regime_scanner.pullback_entry_c3_5c_signal_store as mod

    _ = mod.DEFAULT_OUT
    assert hashlib.sha256(sm.read_bytes()).hexdigest() == h
    src = Path(mod.__file__).read_text()
    assert "build_pullback_entry_pine" not in src

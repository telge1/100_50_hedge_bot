"""Synthetic unit tests for multicoin reference enrichment (no ClickHouse / no enrich run)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment import (
    constants as C,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.analysis_hypotheses import (
    assign_quartile,
    global_quartile_edges,
    leave_one_coin_out_logistic,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.causality import (
    completed_bars,
    mirror_for_direction,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.checkpoint_io import (
    hash_mismatch,
    should_skip_complete,
    write_enrichment_checkpoint,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.cli import (
    build_parser,
    main,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.ema_structure import (
    compute_ema_structure_features,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.enrich_candidate import (
    enrich_candidate_row,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.flow_features import (
    compute_flow_features,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.hashes import (
    all_hashes,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.lld_features import (
    compute_lld_features,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.orderbook_features import (
    compute_orderbook_features,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.parity import (
    ReferenceParityError,
    assert_parity_or_raise,
    check_reference_parity,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.price_atr import (
    compute_price_atr_features,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.reference_filter import (
    filter_reference_trades,
    is_excluded_symbol,
    is_reference_trade,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.runner import (
    enrich_symbol_from_frames,
    run_analyze,
    run_dry_run,
)


UTC = timezone.utc


def _bars(n: int, start: datetime, *, close0: float = 100.0, drift: float = 0.1, minutes: int = 5) -> pd.DataFrame:
    rows = []
    c = close0
    for i in range(n):
        o = c
        h = o + abs(drift) + 0.05
        l = o - abs(drift) - 0.05
        c = o + drift
        rows.append(
            {
                "open_time": start + timedelta(minutes=minutes * i),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1.0,
            }
        )
    return pd.DataFrame(rows)


def test_01_no_data_after_decision_at():
    start = datetime(2026, 7, 24, tzinfo=UTC)
    df = _bars(100, start)
    dec = start + timedelta(minutes=5 * 50)
    closed = completed_bars(df, dec, tf_minutes=5)
    assert closed["open_time"].max() + pd.Timedelta(minutes=5) <= pd.Timestamp(dec)
    assert (pd.to_datetime(closed["open_time"], utc=True) + pd.Timedelta(minutes=5) <= pd.Timestamp(dec)).all()


def test_02_ema_only_completed_bars():
    start = datetime(2026, 7, 24, tzinfo=UTC)
    df = _bars(80, start, drift=0.05)
    dec = start + timedelta(minutes=5 * 70)
    # inject future bar that would change EMA if included
    future = df.copy()
    future.loc[len(future)] = {
        "open_time": dec,
        "open": 999,
        "high": 1000,
        "low": 998,
        "close": 999,
        "volume": 1,
    }
    feats = compute_ema_structure_features(future, dec, "LONG")
    feats2 = compute_ema_structure_features(df, dec, "LONG")
    assert feats["ema9"].value == feats2["ema9"].value


def test_03_atr_calculation():
    start = datetime(2026, 7, 24, tzinfo=UTC)
    df = _bars(40, start, drift=0.2)
    dec = start + timedelta(minutes=5 * 30)
    feats = compute_price_atr_features(df, dec)
    assert feats["atr14_abs"].coverage_status == "OK"
    assert feats["atr14_abs"].value is not None and feats["atr14_abs"].value > 0
    assert feats["atr14_pct"].value is not None


def test_04_long_short_mirroring_symmetry():
    assert mirror_for_direction(0.5, "LONG") == 0.5
    assert mirror_for_direction(0.5, "SHORT") == -0.5
    assert mirror_for_direction(-0.2, "SHORT") == 0.2


def test_05_tp_sl_atr_ratios():
    start = datetime(2026, 7, 24, tzinfo=UTC)
    df = _bars(40, start, drift=0.2)
    dec = start + timedelta(minutes=5 * 30)
    feats = compute_price_atr_features(df, dec)
    assert feats["tp_pct"].value == 0.75
    assert feats["sl_pct"].value == 0.50
    if feats["atr14_pct"].value:
        assert abs(feats["tp_atr_ratio"].value - (0.75 / feats["atr14_pct"].value)) < 1e-9
        assert abs(feats["sl_atr_ratio"].value - (0.50 / feats["atr14_pct"].value)) < 1e-9


def test_06_ema_slope_atr():
    start = datetime(2026, 7, 24, tzinfo=UTC)
    df = _bars(80, start, drift=0.1)
    dec = start + timedelta(minutes=5 * 70)
    feats = compute_ema_structure_features(df, dec, "LONG")
    assert feats["ema59_slope_atr"].coverage_status == "OK"
    assert feats["ema9_slope_atr"].value is not None


def test_07_ob_directional_mirror():
    dec = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    rows = []
    for i in range(10):
        rows.append(
            {
                "bucket_start": dec - timedelta(seconds=10 - i),
                "last_source_ts": dec - timedelta(seconds=10 - i),
                "is_valid": 1,
                "imbalance_l10": 0.1,
                "imbalance_l50": 0.4,
                "spread_bps": 2.0,
            }
        )
    ob = pd.DataFrame(rows)
    long_f = compute_orderbook_features(ob, dec, "LONG")
    short_f = compute_orderbook_features(ob, dec, "SHORT")
    assert long_f["ob_imbalance_directional"].value == pytest.approx(0.4)
    assert short_f["ob_imbalance_directional"].value == pytest.approx(-0.4)


def test_08_flow_directional_mirror():
    dec = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    rows = []
    for i in range(5):
        rows.append(
            {
                "minute": dec - timedelta(minutes=5 - i),
                "buy_notional": 100.0,
                "sell_notional": 40.0,
                "trade_count": 10,
            }
        )
    tr = pd.DataFrame(rows)
    long_f = compute_flow_features(tr, dec, "LONG")
    short_f = compute_flow_features(tr, dec, "SHORT")
    assert long_f["directional_flow_5m"].value > 0
    assert short_f["directional_flow_5m"].value == pytest.approx(-long_f["directional_flow_5m"].value)


def test_09_missing_is_null_never_zero():
    dec = datetime(2026, 8, 1, tzinfo=UTC)
    feats = compute_orderbook_features(None, dec, "LONG")
    assert feats["ob_imbalance_l50_last"].value is None
    assert feats["ob_imbalance_l50_last"].value != 0
    assert feats["ob_imbalance_l50_last"].coverage_status in ("MISSING", "NOT_AVAILABLE", "INSUFFICIENT", "STALE")


def test_10_stale_ob_not_used():
    dec = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    stale_ts = dec - timedelta(seconds=C.OB_STALE_SECONDS + 10)
    ob = pd.DataFrame(
        [
            {
                "bucket_start": stale_ts,
                "last_source_ts": stale_ts,
                "is_valid": 1,
                "imbalance_l10": 0.9,
                "imbalance_l50": 0.9,
                "spread_bps": 1.0,
            }
        ]
    )
    feats = compute_orderbook_features(ob, dec, "LONG")
    assert feats["ob_imbalance_l50_last"].value is None
    assert feats["ob_imbalance_l50_last"].coverage_status == "STALE"


def test_11_lld_causality_unproven():
    dec = datetime(2026, 8, 1, tzinfo=UTC)
    feats = compute_lld_features(dec)
    assert all(f.value is None for f in feats.values())
    assert all(f.coverage_status == "CAUSALITY_UNPROVEN" for f in feats.values())
    assert all(f.causal is False for f in feats.values())


def test_12_labels_not_in_feature_functions():
    start = datetime(2026, 7, 24, tzinfo=UTC)
    df = _bars(80, start)
    dec = start + timedelta(minutes=5 * 70)
    # Feature functions do not accept label kwargs
    feats = compute_price_atr_features(df, dec)
    assert "net_pnl_usdt" not in feats
    assert "exit_reason" not in feats


def test_13_xrp_included_after_parity_confirmed():
    assert not is_excluded_symbol("XRPUSDT")
    trades = [
        {
            "symbol": "XRPUSDT",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "group": "CORE_RESEARCH_SUPPORTIVE",
            "strategy_key": "M0_TP075_SL050_H8",
            "candidate_id": "x",
        }
    ]
    assert len(filter_reference_trades(trades)) == 1


def test_14_reference_strategy_filter():
    ok = {
        "timeframe": "5m",
        "mode_id": "M0_STRICT_SYNC",
        "group": "CORE_RESEARCH_SUPPORTIVE",
        "strategy_key": "M0_TP075_SL050_H8",
    }
    bad = {**ok, "strategy_key": "M0_TP060_SL050_H6"}
    assert is_reference_trade(ok)
    assert not is_reference_trade(bad)


def test_15_one_trade_per_candidate_id():
    cands = [
        {
            "candidate_id": "a",
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
            "decision_at": "2026-08-01T00:00:00+00:00",
            "direction": "LONG",
        }
    ]
    trades = [
        {
            "candidate_id": "a",
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "group": "CORE_RESEARCH_SUPPORTIVE",
            "strategy_key": "M0_TP075_SL050_H8",
            "decision_at": "2026-08-01T00:00:00+00:00",
            "entry_at": "2026-08-01T00:00:00+00:00",
            "exit_reason": "TP_EXIT",
            "exit_at": "2026-08-01T01:00:00+00:00",
            "duration_minutes": 60,
            "gross_return_pct": 0.75,
            "net_return_pct": 0.6,
            "net_pnl_usdt": 6.0,
        },
        {
            "candidate_id": "a",
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "group": "CORE_RESEARCH_SUPPORTIVE",
            "strategy_key": "M0_TP075_SL050_H8",
            "decision_at": "2026-08-01T00:00:00+00:00",
            "entry_at": "2026-08-01T00:00:00+00:00",
            "exit_reason": "TP_EXIT",
            "exit_at": "2026-08-01T01:00:00+00:00",
            "duration_minutes": 60,
            "gross_return_pct": 0.75,
            "net_return_pct": 0.6,
            "net_pnl_usdt": 6.0,
        },
    ]
    summary = check_reference_parity(checkpoint_candidates=cands, checkpoint_trades=trades, symbol="BTCUSDT")
    assert summary["n_duplicate_candidate_ids"] >= 1
    assert summary["parity_pass"] is False


def test_16_entry_rule_ge_decision_at():
    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.reference_filter import (
        entry_rule_ok,
    )

    assert entry_rule_ok("2026-08-01T12:00:00+00:00", "2026-08-01T12:00:00+00:00")
    assert entry_rule_ok("2026-08-01T12:00:00+00:00", "2026-08-01T12:01:00+00:00")
    assert not entry_rule_ok("2026-08-01T12:00:00+00:00", "2026-08-01T11:59:00+00:00")


def test_17_label_parity():
    start = datetime(2026, 7, 24, tzinfo=UTC)
    candles = _bars(80, start)
    dec = start + timedelta(minutes=5 * 70)
    cand = {
        "candidate_id": "edc:1",
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "mode_id": "M0_STRICT_SYNC",
        "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        "decision_at": dec.isoformat(),
        "direction": "LONG",
        "entry_at": dec.isoformat(),
        "trade_flow_verdict": "CONFIRMING",
        "orderbook_verdict": "NEUTRAL",
        "liquidity_location_verdict": "NEUTRAL",
        "volatility_verdict": "NEUTRAL",
        "fake_impulse_verdict": "NEUTRAL",
        "production_gate_verdict": "ALLOW",
        "coverage_segment": "FULL_MULTISOURCE",
    }
    trade = {
        "candidate_id": "edc:1",
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "mode_id": "M0_STRICT_SYNC",
        "group": "CORE_RESEARCH_SUPPORTIVE",
        "strategy_key": "M0_TP075_SL050_H8",
        "decision_at": dec.isoformat(),
        "entry_at": dec.isoformat(),
        "direction": "LONG",
        "exit_reason": "TP_EXIT",
        "exit_at": (dec + timedelta(hours=1)).isoformat(),
        "duration_minutes": 60,
        "gross_return_pct": 0.75,
        "net_return_pct": 0.6,
        "net_pnl_usdt": 6.0,
    }
    row = enrich_candidate_row(cand, trade, candles_5m=candles, trades=None, ob_1s=None)
    assert row["label__net_pnl_usdt"] == 6.0
    assert row["label__exit_reason"] == "TP_EXIT"
    summary = check_reference_parity(
        checkpoint_candidates=[cand],
        checkpoint_trades=[trade],
        enriched_rows=[row],
        symbol="BTCUSDT",
    )
    assert summary["all_labels_unchanged"]
    assert summary["parity_pass"]


def test_18_parity_error_hard_abort():
    summary = {"parity_pass": False, "message": C.STATUS_FAILED_PARITY, "status": C.STATUS_FAILED_PARITY}
    with pytest.raises(ReferenceParityError):
        assert_parity_or_raise(summary)


def test_19_quartiles_without_outcome_optimization():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
    edges = global_quartile_edges(s)
    # Edges depend only on feature series
    assert assign_quartile(1.0, edges) == "Q1"
    assert assign_quartile(8.0, edges) == "Q4"
    # Shuffling outcomes would not change edges — edges ignore y
    edges2 = global_quartile_edges(s)
    assert edges == edges2


def test_20_train_fold_imputation_no_leakage():
    rng = np.random.default_rng(0)
    n = 40
    df = pd.DataFrame(
        {
            "symbol": ["A"] * 20 + ["B"] * 20,
            "label__net_pnl_usdt": rng.normal(size=n),
            "feature__atr14_pct": list(rng.normal(size=18)) + [np.nan, np.nan] + list(rng.normal(size=20)),
            "feature__tp_atr_ratio": rng.normal(size=n),
            "feature__ema59_slope_atr": rng.normal(size=n),
            "feature__ema9_20_distance_atr": rng.normal(size=n),
            "feature__ob_imbalance_directional": rng.normal(size=n),
            "feature__directional_flow_5m": rng.normal(size=n),
        }
    )
    # Should run without using test fold for imputation (sklearn Pipeline fits on train only)
    rows = leave_one_coin_out_logistic(
        df,
        [
            "feature__atr14_pct",
            "feature__tp_atr_ratio",
            "feature__ema59_slope_atr",
            "feature__ema9_20_distance_atr",
            "feature__ob_imbalance_directional",
            "feature__directional_flow_5m",
        ],
    )
    assert isinstance(rows, list)
    assert len(rows) == 2


def test_21_dry_run_opens_no_db(tmp_path: Path):
    input_dir = tmp_path / "in"
    (input_dir / "checkpoints").mkdir(parents=True)
    out = tmp_path / "out"
    with mock.patch(
        "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.market_loaders.open_clickhouse_client"
    ) as m_client:
        result = run_dry_run(
            {
                "input_dir": str(input_dir),
                "output_dir": str(out),
                "start": C.DEFAULT_START,
                "end": C.DEFAULT_END,
                "limit_symbols": None,
                "symbols": None,
            }
        )
        m_client.assert_not_called()
    assert result["clickhouse_queries"] is False
    assert (out / "feature_specification.json").exists()


def test_22_analyze_opens_no_db(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    # incomplete artifacts → INCOMPLETE_ENRICHMENT, still no DB
    pd.DataFrame(
        [
            {
                "symbol": "BTCUSDT",
                "feature__direction": "LONG",
                "feature__decision_at": "2026-08-01T00:00:00+00:00",
                "feature__atr14_pct": 0.2,
                "feature__tp_atr_ratio": 3.0,
                "feature__sl_atr_ratio": 2.0,
                "feature__ema59_slope_atr": 0.1,
                "feature__ema9_20_distance_atr": 0.05,
                "feature__ob_imbalance_directional": 0.1,
                "feature__directional_flow_5m": 1.0,
                "feature__existing_orderbook_verdict": "NEUTRAL",
                "feature__existing_trade_flow_verdict": "CONFIRMING",
                "label__net_pnl_usdt": 1.0,
                "label__tp_exit": 1,
                "label__sl_exit": 0,
                "label__time_exit": 0,
            }
        ]
    ).to_csv(out / "enriched_candidates.csv", index=False)
    with mock.patch(
        "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.market_loaders.open_clickhouse_client"
    ) as m_client:
        result = run_analyze({"output_dir": str(out), "input_dir": str(out)})
        m_client.assert_not_called()
    assert result["clickhouse_queries"] is False
    assert result["verdict"] == C.STATUS_V2_FAILED


def test_23_atomic_checkpoints(tmp_path: Path):
    ck = tmp_path / "checkpoints"
    ck.mkdir()
    path = write_enrichment_checkpoint(
        ck,
        symbol="BTCUSDT",
        status="COMPLETE",
        candidate_ids=["a"],
        feature_rows=[{"x": 1}],
        coverage_summary={},
        parity_summary={"parity_pass": True},
    )
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["status"] == "COMPLETE"
    assert "feature_definition_hash" in data


def test_24_hash_mismatch_prevents_resume():
    hashes = all_hashes()
    rec = {**hashes, "status": "COMPLETE", "feature_definition_hash": "deadbeef"}
    assert hash_mismatch(rec) is True
    assert should_skip_complete(rec) is False
    good = {**hashes, "status": "COMPLETE"}
    # schema_version in all_hashes is string
    assert should_skip_complete(good) is True


def test_25_long_short_symmetry_enrich():
    start = datetime(2026, 7, 24, tzinfo=UTC)
    candles = _bars(80, start, drift=0.05)
    dec = start + timedelta(minutes=5 * 70)
    base_cand = {
        "candidate_id": "edc:L",
        "symbol": "ETHUSDT",
        "timeframe": "5m",
        "mode_id": "M0_STRICT_SYNC",
        "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        "decision_at": dec.isoformat(),
        "entry_at": dec.isoformat(),
        "trade_flow_verdict": "NEUTRAL",
        "orderbook_verdict": "NEUTRAL",
        "liquidity_location_verdict": "NEUTRAL",
        "volatility_verdict": "NEUTRAL",
        "fake_impulse_verdict": "NEUTRAL",
        "production_gate_verdict": "INCONCLUSIVE_DATA",
        "coverage_segment": "FULL_MULTISOURCE",
    }
    trade_base = {
        "symbol": "ETHUSDT",
        "timeframe": "5m",
        "mode_id": "M0_STRICT_SYNC",
        "group": "CORE_RESEARCH_SUPPORTIVE",
        "strategy_key": "M0_TP075_SL050_H8",
        "decision_at": dec.isoformat(),
        "entry_at": dec.isoformat(),
        "exit_reason": "TIME_EXIT",
        "exit_at": (dec + timedelta(hours=8)).isoformat(),
        "duration_minutes": 480,
        "gross_return_pct": 0.0,
        "net_return_pct": -0.15,
        "net_pnl_usdt": -1.5,
    }
    ob = pd.DataFrame(
        [
            {
                "bucket_start": dec - timedelta(seconds=i),
                "last_source_ts": dec - timedelta(seconds=i),
                "is_valid": 1,
                "imbalance_l10": 0.2,
                "imbalance_l50": 0.3,
                "spread_bps": 1.5,
            }
            for i in range(10, 0, -1)
        ]
    )
    long_row = enrich_candidate_row(
        {**base_cand, "direction": "LONG", "candidate_id": "edc:L"},
        {**trade_base, "direction": "LONG", "candidate_id": "edc:L"},
        candles_5m=candles,
        ob_1s=ob,
    )
    short_row = enrich_candidate_row(
        {**base_cand, "direction": "SHORT", "candidate_id": "edc:S"},
        {**trade_base, "direction": "SHORT", "candidate_id": "edc:S"},
        candles_5m=candles,
        ob_1s=ob,
    )
    assert long_row["feature__ob_imbalance_directional"] == pytest.approx(
        -short_row["feature__ob_imbalance_directional"]
    )
    assert long_row["feature__atr14_pct"] == short_row["feature__atr14_pct"]


def test_cli_help():
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["--help"])
    assert e.value.code == 0


def test_cli_dry_run(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "in"
    (input_dir / "checkpoints").mkdir(parents=True)
    out = tmp_path / "out"
    # Avoid writing into real repo: monkeypatch resolve via absolute paths
    rc = main(
        [
            "--dry-run",
            "--input-dir",
            str(input_dir),
            "--output-dir",
            str(out),
        ]
    )
    assert rc == 0


def test_enrich_symbol_parity_frames():
    start = datetime(2026, 7, 24, tzinfo=UTC)
    candles = _bars(80, start)
    dec = start + timedelta(minutes=5 * 70)
    cand = {
        "candidate_id": "edc:1",
        "symbol": "SOLUSDT",
        "timeframe": "5m",
        "mode_id": "M0_STRICT_SYNC",
        "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        "decision_at": dec.isoformat(),
        "direction": "LONG",
        "entry_at": dec.isoformat(),
        "trade_flow_verdict": "CONFIRMING",
        "orderbook_verdict": "NEUTRAL",
        "liquidity_location_verdict": "NEUTRAL",
        "volatility_verdict": "NEUTRAL",
        "fake_impulse_verdict": "NEUTRAL",
        "production_gate_verdict": "ALLOW",
        "coverage_segment": "FULL_MULTISOURCE",
    }
    trade = {
        "candidate_id": "edc:1",
        "symbol": "SOLUSDT",
        "timeframe": "5m",
        "mode_id": "M0_STRICT_SYNC",
        "group": "CORE_RESEARCH_SUPPORTIVE",
        "strategy_key": "M0_TP075_SL050_H8",
        "decision_at": dec.isoformat(),
        "entry_at": dec.isoformat(),
        "direction": "LONG",
        "exit_reason": "SL_EXIT",
        "exit_at": (dec + timedelta(hours=2)).isoformat(),
        "duration_minutes": 120,
        "gross_return_pct": -0.5,
        "net_return_pct": -0.65,
        "net_pnl_usdt": -6.5,
    }
    ck = {"status": "COMPLETE", "candidates": [cand], "trades": [trade]}
    result = enrich_symbol_from_frames(
        symbol="SOLUSDT",
        checkpoint=ck,
        candles_5m=candles,
        trades=None,
        ob_1s=None,
    )
    assert result["status"] == "COMPLETE"
    assert result["parity"]["parity_pass"]


# --- Regression: enrich execution path (mocked ClickHouse) ---


def _ref_checkpoint(symbol: str, *, direction: str = "BULLISH", cid: str = "edc:1", dec: datetime | None = None) -> dict:
    dec = dec or datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    cand = {
        "candidate_id": cid,
        "symbol": symbol,
        "timeframe": "5m",
        "mode_id": "M0_STRICT_SYNC",
        "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        "decision_at": dec.isoformat(),
        "direction": direction,
        "entry_at": dec.isoformat(),
        "trade_flow_verdict": "CONFIRMING",
        "orderbook_verdict": "NEUTRAL",
        "liquidity_location_verdict": "NEUTRAL",
        "volatility_verdict": "NEUTRAL",
        "fake_impulse_verdict": "NEUTRAL",
        "production_gate_verdict": "ALLOW",
        "coverage_segment": "FULL_MULTISOURCE",
    }
    trade = {
        "candidate_id": cid,
        "symbol": symbol,
        "timeframe": "5m",
        "mode_id": "M0_STRICT_SYNC",
        "group": "CORE_RESEARCH_SUPPORTIVE",
        "strategy_key": "M0_TP075_SL050_H8",
        "decision_at": dec.isoformat(),
        "entry_at": dec.isoformat(),
        "entry_price": 100.0,
        "direction": direction,
        "exit_reason": "TP_EXIT",
        "exit_at": (dec + timedelta(hours=1)).isoformat(),
        "exit_price": 100.75,
        "duration_minutes": 60,
        "gross_return_pct": 0.75,
        "gross_pnl_usdt": 7.5,
        "net_return_pct": 0.6,
        "net_pnl_usdt": 6.0,
        "costs_usdt": 1.5,
        "notional_usdt": 1000.0,
    }
    return {"status": "COMPLETE", "candidates": [cand], "trades": [trade], "symbol": symbol}


def test_26_bullish_bearish_direction_accepted():
    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.causality import (
        direction_sign,
        normalize_direction,
    )

    assert normalize_direction("BULLISH") == "LONG"
    assert normalize_direction("BEARISH") == "SHORT"
    assert direction_sign("BULLISH") == 1
    assert direction_sign("BEARISH") == -1


def test_27_enrich_calls_real_runner_not_code_ready(tmp_path: Path):
    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment import (
        cli as cli_mod,
    )
    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment import (
        runner as runner_mod,
    )

    input_dir = tmp_path / "in"
    (input_dir / "checkpoints").mkdir(parents=True)
    out = tmp_path / "out"
    start = datetime(2026, 7, 24, tzinfo=UTC)
    candles = _bars(80, start)
    dec = start + timedelta(minutes=5 * 70)
    ck = _ref_checkpoint("BTCUSDT", direction="BULLISH", dec=dec)
    (input_dir / "checkpoints" / "BTCUSDT.json").write_text(json.dumps(ck), encoding="utf-8")

    market = {
        "candles_1m": candles,
        "candles_5m": candles,
        "trades": None,
        "ob_1s": None,
    }

    with mock.patch.object(cli_mod, "run_enrich", wraps=runner_mod.run_enrich) as wrapped:
        with mock.patch(
            "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.market_loaders.open_clickhouse_client",
            return_value=object(),
        ):
            with mock.patch(
                "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.market_loaders.load_enrichment_market_data",
                return_value=market,
            ):
                rc = main(
                    [
                        "--enrich",
                        "--input-dir",
                        str(input_dir),
                        "--output-dir",
                        str(out),
                    ]
                )
        assert wrapped.called
    assert rc == 0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["verdict"] == C.STATUS_COMPLETE
    assert summary["verdict"] != C.CODE_STATUS
    assert summary["enriched_rows"] == 1
    assert (out / "enriched_candidates.csv").exists()
    assert (out / "run_manifest.json").exists()
    assert (out / "checkpoints" / "BTCUSDT.json").exists()


def test_28_empty_frozen_reference_hard_fail(tmp_path: Path):
    input_dir = tmp_path / "in"
    (input_dir / "checkpoints").mkdir(parents=True)
    (input_dir / "checkpoints" / "BTCUSDT.json").write_text(
        json.dumps({"status": "COMPLETE", "candidates": [], "trades": []}),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    with mock.patch(
        "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.market_loaders.open_clickhouse_client"
    ) as m_client:
        rc = main(["--enrich", "--input-dir", str(input_dir), "--output-dir", str(out)])
        m_client.assert_not_called()
    assert rc != 0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["verdict"] == C.STATUS_EMPTY_REFERENCE


def test_29_parity_fails_before_db(tmp_path: Path):
    input_dir = tmp_path / "in"
    (input_dir / "checkpoints").mkdir(parents=True)
    out = tmp_path / "out"
    dec = datetime(2026, 8, 1, tzinfo=UTC)
    ck = _ref_checkpoint("BTCUSDT", dec=dec)
    ck["trades"][0]["entry_at"] = (dec - timedelta(minutes=1)).isoformat()
    (input_dir / "checkpoints" / "BTCUSDT.json").write_text(json.dumps(ck), encoding="utf-8")
    with mock.patch(
        "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.market_loaders.open_clickhouse_client"
    ) as m_client:
        rc = main(["--enrich", "--input-dir", str(input_dir), "--output-dir", str(out)])
        m_client.assert_not_called()
    assert rc != 0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["verdict"] == C.STATUS_FAILED_PARITY
    assert not (out / "enriched_candidates.csv").exists()


def test_30_exception_not_swallowed_as_code_ready(tmp_path: Path):
    input_dir = tmp_path / "in"
    (input_dir / "checkpoints").mkdir(parents=True)
    out = tmp_path / "out"
    start = datetime(2026, 7, 24, tzinfo=UTC)
    dec = start + timedelta(minutes=5 * 70)
    ck = _ref_checkpoint("BTCUSDT", direction="BULLISH", dec=dec)
    (input_dir / "checkpoints" / "BTCUSDT.json").write_text(json.dumps(ck), encoding="utf-8")

    with mock.patch(
        "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.market_loaders.open_clickhouse_client",
        return_value=object(),
    ):
        with mock.patch(
            "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.market_loaders.load_enrichment_market_data",
            side_effect=RuntimeError("boom_db"),
        ):
            rc = main(["--enrich", "--input-dir", str(input_dir), "--output-dir", str(out)])
    assert rc != 0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["verdict"] == C.STATUS_ENRICHMENT_FAILED
    assert summary["verdict"] != C.CODE_STATUS


def test_31_analyze_accepts_complete_enrichment(tmp_path: Path):
    out = tmp_path / "out"
    (out / "checkpoints").mkdir(parents=True)
    row = {
        "symbol": "BTCUSDT",
        "candidate_id": "edc:1",
        "feature__direction": "BULLISH",
        "feature__decision_at": "2026-08-01T00:00:00+00:00",
        "feature__atr14_pct": 0.2,
        "feature__tp_atr_ratio": 3.0,
        "feature__sl_atr_ratio": 2.0,
        "feature__ema59_slope_atr": 0.1,
        "feature__ema9_20_distance_atr": 0.05,
        "feature__ob_imbalance_directional": 0.1,
        "feature__directional_flow_5m": 1.0,
        "feature__existing_orderbook_verdict": "NEUTRAL",
        "feature__existing_trade_flow_verdict": "CONFIRMING",
        "label__net_pnl_usdt": 1.0,
        "label__tp_exit": 1,
        "label__sl_exit": 0,
        "label__time_exit": 0,
    }
    pd.DataFrame([row]).to_csv(out / "enriched_candidates.csv", index=False)
    hashes = all_hashes()
    write_enrichment_checkpoint(
        out / "checkpoints",
        symbol="BTCUSDT",
        status="COMPLETE",
        candidate_ids=["edc:1"],
        feature_rows=[row],
        coverage_summary={},
        parity_summary={"parity_pass": True},
    )
    (out / "run_manifest.json").write_text(
        json.dumps({"verdict": C.STATUS_COMPLETE, "hashes": hashes, "n_rows": 1}),
        encoding="utf-8",
    )
    (out / "summary.json").write_text(
        json.dumps({"verdict": C.STATUS_COMPLETE, "n_rows": 1}),
        encoding="utf-8",
    )
    result = run_analyze({"output_dir": str(out), "input_dir": str(tmp_path / "missing_input")})
    assert result["verdict"] in (C.STATUS_V2_COMPLETE, C.STATUS_V2_PARTIAL)
    assert (out / "hypothesis_h1_orderbook.csv").exists()
    assert (out / "analysis_report.md").exists()
    assert (out / "analysis_summary.json").exists()


def test_32_defaults_point_at_v2_shared_engine():
    assert "multicoin_30d_frozen_validation_v2_shared_engine" in C.DEFAULT_INPUT_DIR
    assert "multicoin_reference_enrichment_v2_shared_engine" in C.DEFAULT_OUTPUT_DIR
    assert C.ENTRY_RULE == "SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR"


def test_33_lookahead_completed_bars_only():
    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.causality import (
        completed_bars,
    )

    start = datetime(2026, 7, 24, tzinfo=UTC)
    df = _bars(10, start)
    # decision at close of bar index 2 → open_time start+10m, close start+15m
    dec = start + timedelta(minutes=15)
    done = completed_bars(df, dec, tf_minutes=5)
    assert len(done) == 3
    assert pd.Timestamp(done.iloc[-1]["open_time"]) == pd.Timestamp(start + timedelta(minutes=10))


def test_34_enrich_path_not_code_ready_and_writes_rows(tmp_path: Path):
    """Regression: --enrich must call real enrich path and never return CODE_READY."""
    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment import (
        runner as runner_mod,
    )

    input_dir = tmp_path / "in"
    (input_dir / "checkpoints").mkdir(parents=True)
    out = tmp_path / "out"
    start = datetime(2026, 7, 24, tzinfo=UTC)
    dec = start + timedelta(minutes=5 * 70)
    ck = _ref_checkpoint("BTCUSDT", direction="BULLISH", dec=dec)
    (input_dir / "checkpoints" / "BTCUSDT.json").write_text(json.dumps(ck), encoding="utf-8")
    c5 = _bars(90, start)
    market = {
        "candles_1m": _bars(450, start, minutes=1),
        "candles_5m": c5,
        "trades": pd.DataFrame(),
        "ob_1s": pd.DataFrame(),
        "oi_1m": pd.DataFrame(),
        "liq": pd.DataFrame(),
    }
    with mock.patch(
        "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.market_loaders.open_clickhouse_client",
        return_value=object(),
    ):
        with mock.patch(
            "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.market_loaders.load_enrichment_market_data",
            return_value=market,
        ):
            rc = main(["--enrich", "--input-dir", str(input_dir), "--output-dir", str(out)])
    assert rc == 0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["verdict"] == C.STATUS_COMPLETE
    assert summary["verdict"] != C.CODE_STATUS
    assert (out / "enriched_candidates.csv").exists()
    assert (out / "enriched_trades.csv").exists()
    assert summary["enriched_rows"] == 1
    ar = run_analyze({"output_dir": str(out), "input_dir": str(input_dir)})
    assert ar["verdict"] in (C.STATUS_V2_COMPLETE, C.STATUS_V2_PARTIAL)
    assert ar["enriched_rows"] == 1


def test_35_idempotent_enrich_resume(tmp_path: Path):
    input_dir = tmp_path / "in"
    (input_dir / "checkpoints").mkdir(parents=True)
    out = tmp_path / "out"
    start = datetime(2026, 7, 24, tzinfo=UTC)
    dec = start + timedelta(minutes=5 * 70)
    ck = _ref_checkpoint("ETHUSDT", direction="BEARISH", dec=dec)
    (input_dir / "checkpoints" / "ETHUSDT.json").write_text(json.dumps(ck), encoding="utf-8")
    c5 = _bars(90, start)
    calls = {"n": 0}

    def _market(*_a, **_k):
        calls["n"] += 1
        return {
            "candles_1m": _bars(450, start, minutes=1),
            "candles_5m": c5,
            "trades": pd.DataFrame(),
            "ob_1s": pd.DataFrame(),
            "oi_1m": pd.DataFrame(),
            "liq": pd.DataFrame(),
        }

    with mock.patch(
        "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.market_loaders.open_clickhouse_client",
        return_value=object(),
    ):
        with mock.patch(
            "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.market_loaders.load_enrichment_market_data",
            side_effect=_market,
        ):
            assert main(["--enrich", "--input-dir", str(input_dir), "--output-dir", str(out)]) == 0
            assert main(["--enrich", "--input-dir", str(input_dir), "--output-dir", str(out)]) == 0
    assert calls["n"] == 1
    df = pd.read_csv(out / "enriched_candidates.csv")
    assert len(df) == 1
    assert df["candidate_id"].nunique() == 1

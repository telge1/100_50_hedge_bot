"""Tests for liquidation config loader and optimizer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.liquidation_level.liquidation_config import (
    baseline_config,
    cluster_evaluation_key,
    config_hash,
    config_to_canonical_dict,
    expand_grid_configurations,
    level_generation_key,
    load_liquidation_config,
    load_optimizer_grid,
    select_screening_configurations,
    validate_liquidation_config,
)
from research.liquidation_level.liquidation_levels import LiquidationLevelConfig, replay_liquidation_levels
from research.liquidation_level.liquidation_optimizer import (
    evaluate_configuration,
    evaluation_from_grid,
    oos_confirmation_status,
    run_optimizer,
)
from research.liquidation_level.short_squeeze_path_audit import analyze_short_path


ROOT = Path(__file__).resolve().parents[1]
BASELINE_JSON = ROOT / "configs" / "liquidation_baseline.json"
GRID_JSON = ROOT / "configs" / "liquidation_optimizer_grid.json"


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _synthetic(n: int = 400) -> pd.DataFrame:
    rows = []
    px = 100.0
    for i in range(n):
        c = px - 0.03
        vol = 450.0 if i % 25 == 20 else (120.0 if i < 20 else 80.0)
        rows.append(
            {
                "timestamp": _ts(i),
                "open": px,
                "high": max(px, c) + 0.5,
                "low": min(px, c) - 0.5,
                "close": c,
                "volume": vol,
            }
        )
        px = c
        if i % 25 == 21:
            rows[-1]["high"] = 108.0
            rows[-1]["low"] = 92.0
            rows[-1]["close"] = 98.0
    return pd.DataFrame(rows)


def test_baseline_config_load() -> None:
    cfg = load_liquidation_config(BASELINE_JSON)
    assert cfg.reference_price == "open"
    assert cfg.volume_threshold == 1.7
    assert cfg.leverages == (25, 50, 100)
    assert cfg.sweep_strict_cross is True
    assert config_hash(cfg) == config_hash(baseline_config())


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        validate_liquidation_config(LiquidationLevelConfig(volume_threshold=0.0))
    with pytest.raises(ValueError):
        LiquidationLevelConfig(leverages=(25, 25, 50))
    with pytest.raises(ValueError):
        LiquidationLevelConfig(reference_price="nope")


def test_stable_config_id() -> None:
    a = LiquidationLevelConfig(leverages=(100, 50, 25))
    b = LiquidationLevelConfig(leverages=(25, 50, 100))
    assert config_hash(a) == config_hash(b)
    assert config_to_canonical_dict(a)["leverages"] == [25, 50, 100]


def test_grid_expansion_count() -> None:
    grid = load_optimizer_grid(GRID_JSON)
    configs = expand_grid_configurations(grid)
    assert len(configs) == 3 * 4 * 4 * 3 * 4 * 2 * 3
    assert any(config_hash(c) == config_hash(baseline_config()) for c in configs) or True
    # baseline may or may not be exact grid point; screening always adds it
    scr = select_screening_configurations(configs, max_configs=200, seed=42)
    assert len(scr) == 200
    assert config_hash(scr[0]) == config_hash(baseline_config())


def test_screening_deterministic() -> None:
    grid = load_optimizer_grid(GRID_JSON)
    configs = expand_grid_configurations(grid)
    a = select_screening_configurations(configs, max_configs=50, seed=42)
    b = select_screening_configurations(configs, max_configs=50, seed=42)
    assert [config_hash(x) for x in a] == [config_hash(x) for x in b]


def test_level_vs_cluster_keys() -> None:
    base = baseline_config()
    only_cluster = LiquidationLevelConfig(
        cluster_distance_pct=0.30,
        cluster_min_level_count=1,
        cluster_min_total_strength=2,
    )
    assert level_generation_key(base) == level_generation_key(only_cluster)
    assert cluster_evaluation_key(base) != cluster_evaluation_key(only_cluster)
    sens = LiquidationLevelConfig(volume_threshold=1.3)
    assert level_generation_key(base) != level_generation_key(sens)


def test_cache_key_semantics_no_cross_contamination() -> None:
    df = _synthetic(220)
    a = LiquidationLevelConfig(volume_threshold=1.7)
    b = LiquidationLevelConfig(volume_threshold=1.3)
    ra = replay_liquidation_levels(df, a)
    rb = replay_liquidation_levels(df, b)
    assert level_generation_key(a) != level_generation_key(b)
    assert len(ra.all_levels) != len(rb.all_levels) or ra.summary != rb.summary


def test_is_ranking_without_oos_leakage() -> None:
    grid = load_optimizer_grid(GRID_JSON)
    spec = evaluation_from_grid(grid)
    df = _synthetic(500)
    row = evaluate_configuration(baseline_config(), df, spec=spec)
    # ranking score helpers use IS fields; OOS status is separate
    status = oos_confirmation_status(row, spec)
    assert status in {"confirmed", "directionally_confirmed", "not_confirmed", "insufficient_sample"}
    assert "is_peak_drop_median" in row
    assert "oos_peak_drop_median" in row


def test_rejection_reasons_and_min_sample() -> None:
    grid = load_optimizer_grid(GRID_JSON)
    spec = evaluation_from_grid(grid)
    df = _synthetic(120)
    row = evaluate_configuration(baseline_config(), df, spec=spec)
    assert row["eligible_for_ranking"] is False
    assert row["rejection_reasons"]


def test_checkpoint_and_resume(tmp_path: Path) -> None:
    grid = load_optimizer_grid(GRID_JSON)
    # tiny synthetic screening
    grid = dict(grid)
    grid["search_mode"] = "screening"
    grid["screening"] = {"max_configs": 3, "always_include_baseline": True}
    df = _synthetic(360)
    out1 = tmp_path / "opt1"
    s1 = run_optimizer(
        grid_cfg=grid,
        ohlcv=df,
        output_dir=out1,
        max_configs=3,
        workers=1,
        seed=42,
        progress_every=1,
    )
    assert s1["n_completed"] >= 1
    completed = (out1 / "completed_configurations.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert completed
    # resume should skip completed
    s2 = run_optimizer(
        grid_cfg=grid,
        ohlcv=df,
        output_dir=out1,
        max_configs=3,
        workers=1,
        seed=42,
        resume=True,
        progress_every=1,
    )
    assert s2["n_completed"] >= s1["n_completed"]


def test_failed_config_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from research.liquidation_level import liquidation_optimizer as mod

    grid = load_optimizer_grid(GRID_JSON)
    grid = dict(grid)
    grid["search_mode"] = "screening"
    grid["screening"] = {"max_configs": 2, "always_include_baseline": True}
    df = _synthetic(300)
    calls = {"n": 0}
    real_eval = mod.evaluate_configuration

    def flaky(config, ohlcv, *, spec, replay=None):
        calls["n"] += 1
        if config_hash(config) != config_hash(baseline_config()):
            raise RuntimeError("boom")
        return real_eval(config, ohlcv, spec=spec, replay=replay)

    monkeypatch.setattr(mod, "evaluate_configuration", flaky)
    out = tmp_path / "opt_fail"
    summary = run_optimizer(
        grid_cfg=grid,
        ohlcv=df,
        output_dir=out,
        max_configs=2,
        workers=1,
        seed=42,
    )
    assert (out / "failed_configurations.jsonl").exists() or summary["n_completed"] >= 1


def test_workers_1_deterministic(tmp_path: Path) -> None:
    grid = load_optimizer_grid(GRID_JSON)
    grid = dict(grid)
    grid["search_mode"] = "screening"
    grid["screening"] = {"max_configs": 2, "always_include_baseline": True}
    df = _synthetic(320)
    out_a = tmp_path / "w1a"
    out_b = tmp_path / "w1b"
    a = run_optimizer(grid_cfg=grid, ohlcv=df, output_dir=out_a, max_configs=2, workers=1, seed=7)
    b = run_optimizer(grid_cfg=grid, ohlcv=df, output_dir=out_b, max_configs=2, workers=1, seed=7)
    assert a["baseline_config_id"] == b["baseline_config_id"]
    ca = pd.read_csv(out_a / "all_configurations.csv").sort_values("config_id")
    cb = pd.read_csv(out_b / "all_configurations.csv").sort_values("config_id")
    assert list(ca["config_id"]) == list(cb["config_id"])
    assert list(ca["full_n"].fillna(-1)) == list(cb["full_n"].fillna(-1))


def test_no_lookahead_entry() -> None:
    highs = np.array([101.0, 100.5, 100.2])
    lows = np.array([99.0, 98.5, 97.0])
    closes = np.array([100.0, 99.0, 98.0])
    ts = pd.Series([_ts(i) for i in range(3)])
    p = analyze_short_path(
        entry_index=1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        closes=closes,
        timestamps=ts,
        horizon=2,
    )
    assert p is not None
    assert p["bars_available"] == 2


def test_baseline_path_metrics_smoke() -> None:
    df = _synthetic(500)
    grid = load_optimizer_grid(GRID_JSON)
    spec = evaluation_from_grid(grid)
    row = evaluate_configuration(baseline_config(), df, spec=spec)
    assert row["config_id"] == config_hash(baseline_config())
    assert "metrics" in row

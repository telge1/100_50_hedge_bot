"""Unit tests for multi-coin price-staging grid runner helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from research.backtests.multicoin_price_staging_grid import (
    assert_output_dir_safe,
    atomic_write_json,
    completed_key,
    empty_checkpoint,
    extract_undercoverage_cases,
    load_checkpoint,
    log,
    parse_sizes,
    run_grid,
)
from research.backtests.second_leg_price_staging import (
    GRID_PROFILE_SPECS,
    list_grid_profile_names,
    parse_profile_selection,
    resolve_grid_profile,
    resolve_profile,
    validate_config,
)


def test_grid_profile_definitions_load() -> None:
    names = list_grid_profile_names()
    assert "legacy" in names
    assert "two_early_small" in names
    assert "three_conservative" in names
    assert "four_frontloaded" in names
    assert len(names) == len(GRID_PROFILE_SPECS)
    for name in names:
        cfg = resolve_grid_profile(name)
        assert validate_config(cfg) == []
        if name == "legacy":
            assert cfg.enabled is False
        else:
            assert cfg.enabled is True
            assert cfg.stage_count == len(cfg.price_distribution.fractions)


def test_profiles_all() -> None:
    cfgs = parse_profile_selection("all")
    assert cfgs[0].profile_name == "legacy"
    assert len(cfgs) == len(GRID_PROFILE_SPECS)


def test_profiles_subset_selection() -> None:
    cfgs = parse_profile_selection("legacy,two_early_small,three_conservative")
    assert [c.profile_name for c in cfgs] == [
        "legacy",
        "two_early_small",
        "three_conservative",
    ]


def test_invalid_profile_rejected() -> None:
    with pytest.raises(ValueError, match="unknown"):
        resolve_grid_profile("not_a_real_profile")


def test_invalid_fractions_rejected() -> None:
    from dataclasses import replace

    from research.backtests.second_leg_price_staging import PriceDistribution

    cfg = resolve_grid_profile("two_equal")
    bad = replace(
        cfg,
        price_distribution=PriceDistribution(mode="custom_fractions", fractions=(0.9, 0.5)),
    )
    errs = validate_config(bad)
    assert errs


def test_lab_linear4_still_resolves() -> None:
    cfg = resolve_profile("linear4")
    assert cfg.profile_name == "linear4"
    assert cfg.enabled is True


def test_atomic_checkpoint_write(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    payload = empty_checkpoint(profiles=["legacy"], coins=["APTUSDT"])
    atomic_write_json(path, payload)
    loaded = load_checkpoint(path)
    assert loaded is not None
    assert loaded["profiles"] == ["legacy"]
    assert not path.with_name("checkpoint.json.tmp").exists()


def test_logging_flush(capsys: pytest.CaptureFixture[str]) -> None:
    log("hello-grid")
    captured = capsys.readouterr()
    assert "hello-grid" in captured.out


def test_parse_sizes() -> None:
    assert parse_sizes("1000:500") == (1000.0, 500.0)
    with pytest.raises(ValueError):
        parse_sizes("1000")


def test_undercoverage_classification() -> None:
    result = MagicMock()
    result.fills_log = []
    result.intent_log = []
    row = {
        "undercoverage": 1,
        "status": "open",
        "fallback_single_stage": 0,
        "staging_activated": 1,
    }
    with patch(
        "research.backtests.multicoin_price_staging_grid.build_pnl_coverage_audit",
        return_value=[
            {
                "cycle_index": 4,
                "status": "undercovered",
                "loss_pnl": -10.0,
                "cover_pnl": 4.0,
                "missing_pnl": 6.0,
                "qty_shortfall": 1.5,
                "loss_purpose": "CYCLE_4_LONG_ADD",
                "cover_purpose": "CYCLE_4_SHORT_REDUCE",
            }
        ],
    ):
        cases = extract_undercoverage_cases(
            coin="APTUSDT",
            profile="two_early_small",
            trade_number=3,
            result=result,
            row=row,
        )
    assert len(cases) == 1
    assert cases[0]["required_coverage"] == 10.0
    assert cases[0]["realized_coverage"] == 4.0
    assert cases[0]["rest_coverage"] == 6.0
    assert cases[0]["coverage_gate_state"] == "undercovered"


def test_checkpoint_resume_skips_and_dedupes(tmp_path: Path) -> None:
    """Synthetic resume: completed key is skipped; no duplicate rows."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    # Minimal blocker csv
    (baseline / "blocker_trades.csv").write_text(
        "coin,trade_number,start_index,mtm_pnl,status\n"
        "APTUSDT,3,570,-1,open\n"
        "BTCUSDT,1,100,-1,open\n",
        encoding="utf-8",
    )

    profiles = ["legacy", "two_early_small"]
    keys = [completed_key("APTUSDT", p) for p in profiles]
    atomic_write_json(
        output_dir / "checkpoint.json",
        {
            "version": 1,
            "profiles": profiles,
            "coins": ["APTUSDT", "BTCUSDT"],
            "completed_coins": ["APTUSDT"],
            "completed_keys": keys,
            "updated_at": "2026-07-21T00:00:00+00:00",
        },
    )
    # partial already has APT rows
    (output_dir / "partial_per_coin_per_profile.csv").write_text(
        "coin,profile,trade_flat,final_mtm,undercoverage,invalid_partial,"
        "planned_stages,filled_stages,staging_activated,fallback_single_stage,"
        "distinct_triggers,status,over_close,duplicate_stage\n"
        "APTUSDT,legacy,0,-1,0,0,0,0,0,0,0,open,0,0\n"
        "APTUSDT,two_early_small,1,1,0,0,2,2,1,0,2,closed,0,0\n",
        encoding="utf-8",
    )
    (output_dir / "undercoverage_cases.csv").write_text("", encoding="utf-8")
    atomic_write_json(output_dir / "apt_parity.json", {"ok": True, "checks": {}})

    fake_row = {
        "coin": "BTCUSDT",
        "trade_number": 1,
        "start_index": 100,
        "profile": "legacy",
        "trade_flat": 0,
        "final_mtm": -2.0,
        "undercoverage": 0,
        "invalid_partial": 0,
        "planned_stages": 0,
        "filled_stages": 0,
        "staging_activated": 0,
        "fallback_single_stage": 0,
        "distinct_triggers": 0,
        "status": "open",
        "worst_mtm": -2.0,
        "gross_exposure": 0.0,
        "net_exposure": 0.0,
        "duration_candles": 10,
        "exit_before_first_stage": None,
        "exit_after_first_stage": None,
        "strongest_exit_drop": None,
        "bounce_reaches_exit": 0,
        "improvement_usdt": 0.0,
        "classification": "legacy_control",
        "closed_positive": 0,
    }

    call_log: list[str] = []

    def fake_run_isolated(**kwargs):
        call_log.append(f"{kwargs['coin']}:{kwargs['staging_config'].profile_name}")
        result = MagicMock()
        result.fills_log = []
        result.intent_log = []
        result.order_log = []
        result.error = None
        result.exit_reason = "open"
        result.realized_pnl = 0.0
        result.candles_processed = 10
        result.cycles_seen = 1
        result.final_strategy_state_excerpt = {}
        return result

    def fake_analyze(**kwargs):
        row = dict(fake_row)
        row["coin"] = kwargs["coin"]
        row["profile"] = kwargs["profile"]
        row["trade_number"] = kwargs["trade_number"]
        return row

    with (
        patch(
            "research.backtests.multicoin_price_staging_grid.load_candles_for_symbol",
            return_value=[MagicMock()] * 200,
        ),
        patch(
            "research.backtests.multicoin_price_staging_grid.normalize_candles",
            side_effect=lambda _sym, candles: candles,
        ),
        patch(
            "research.backtests.multicoin_price_staging_grid.run_isolated_blocker",
            side_effect=fake_run_isolated,
        ),
        patch(
            "research.backtests.multicoin_price_staging_grid.analyze_blocker_run",
            side_effect=fake_analyze,
        ),
        patch(
            "research.backtests.multicoin_price_staging_grid.detect_stage_safety",
            return_value={"duplicate_stage": 0, "over_close": 0},
        ),
    ):
        payload = run_grid(
            baseline_dir=baseline,
            profiles_spec="legacy,two_early_small",
            output_dir=output_dir,
            candle_limit=200,
            resume=True,
            skip_apt_gate=True,
        )

    assert payload.get("aborted") is False
    # APT profiles must not be re-run
    assert not any(c.startswith("APTUSDT:") for c in call_log)
    assert any(c.startswith("BTCUSDT:") for c in call_log)
    rows = [
        line
        for line in (output_dir / "per_coin_per_profile.csv").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("coin,")
    ]
    apt_legacy = [r for r in rows if r.startswith("APTUSDT,legacy")]
    assert len(apt_legacy) == 1
    ck = json.loads((output_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert ck.get("finished") is True
    assert completed_key("APTUSDT", "legacy") in ck["completed_keys"]
    assert completed_key("BTCUSDT", "two_early_small") in ck["completed_keys"]


def test_assert_output_dir_safe_resume_allows_nonempty(tmp_path: Path) -> None:
    d = tmp_path / "out"
    d.mkdir()
    (d / "x.txt").write_text("a", encoding="utf-8")
    assert_output_dir_safe(d, resume=True)
    with pytest.raises(FileExistsError):
        assert_output_dir_safe(d, resume=False)

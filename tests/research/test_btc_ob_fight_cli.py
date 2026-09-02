"""Tests for BTC OB Fight CLI argument parsing and run allocation."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.btc_ob_fight.cli import EXIT_CLI, EXIT_DATA, build_parser, parse_timestamp, validate_args
from research.btc_ob_fight.config import allocate_run_dir
from research.btc_ob_fight.facts import json_safe


def test_parse_timestamp_z():
    dt = parse_timestamp("2026-08-31T19:00:00Z")
    assert dt == datetime(2026, 8, 31, 19, 0, 0, tzinfo=timezone.utc)


def test_parse_timestamp_offset_normalizes_to_utc():
    dt = parse_timestamp("2026-08-31T21:00:00+02:00")
    assert dt == datetime(2026, 8, 31, 19, 0, 0, tzinfo=timezone.utc)


def test_reject_timestamp_without_zone():
    with pytest.raises(ValueError, match="timezone"):
        parse_timestamp("2026-08-31T19:00:00")


def test_invalid_timestamp():
    with pytest.raises(ValueError):
        parse_timestamp("not-a-ts")


def test_btc_only_gate(tmp_path: Path):
    args = build_parser().parse_args(
        ["--timestamp", "2026-08-31T19:00:00Z", "--symbol", "ETHUSDT", "--ob-root", str(tmp_path)]
    )
    with pytest.raises(ValueError, match="BTCUSDT"):
        validate_args(args)


def test_before_after_validation(tmp_path: Path):
    args = build_parser().parse_args(
        ["--timestamp", "2026-08-31T19:00:00Z", "--before-minutes", "0", "--ob-root", str(tmp_path)]
    )
    with pytest.raises(ValueError, match="positive"):
        validate_args(args)


def test_no_overwrite_run_dirs(tmp_path: Path):
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    d1 = allocate_run_dir(tmp_path, anchor)
    d2 = allocate_run_dir(tmp_path, anchor)
    assert d1.name == "run_001"
    assert d2.name == "run_002"


def test_json_without_nan_infinity():
    payload = json_safe({"x": float("nan"), "y": float("inf"), "z": 1.0})
    assert payload["x"] is None
    assert payload["y"] is None
    text = json.dumps(payload)
    assert "NaN" not in text
    assert "Infinity" not in text


def test_trade_verdict_not_evaluated_in_summary_schema():
    from research.btc_ob_fight.reporting import build_summary_payload
    from research.btc_ob_fight.config import RunConfig

    cfg = RunConfig(
        symbol="BTCUSDT",
        anchor=datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc),
        before_minutes=30,
        after_minutes=30,
        ob_root=Path("/tmp"),
        out_root=Path("/tmp"),
    )
    s = build_summary_payload(
        cfg,
        profile_facts={},
        level_events=[],
        trade_facts={},
        wall_facts=[],
        oi_liq_facts={},
        factual_reasons=[],
        data_quality="PASS",
    )
    assert s["trade_verdict_evaluated"] is False
    assert s["rules_frozen"] is False
    assert s["causality"]["outcome_used_for_decision"] is False

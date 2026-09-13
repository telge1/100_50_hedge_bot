"""Focused tests for Phase-2 frozen baseline evaluation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.liquidity_destination_bias.baselines.contract import (
    EXPECTED_DATASET_FINGERPRINT,
    MAJORITY_TIE_BREAK,
    PRIMARY_CLASSES,
    RESEARCH_BANNER,
    SPLIT_BOUNDS,
)
from research.liquidity_destination_bias.baselines.data import (
    EpisodeRow,
    dataset_fingerprint,
    verify_and_load_dataset,
)
from research.liquidity_destination_bias.baselines.metrics import (
    classification_metrics,
    wilson_interval,
)
from research.liquidity_destination_bias.baselines.rules import (
    hash_control,
    inverse_nearest,
    majority_label,
    nearest_target,
)
from research.liquidity_destination_bias.baselines.splits import (
    assert_no_split_leak,
    assign_split,
    split_episodes,
)


UTC = timezone.utc


def ep(
    *,
    eid: str,
    symbol: str,
    t0: str,
    up: float,
    lo: float,
    outcome: str,
) -> EpisodeRow:
    ts = datetime.fromisoformat(t0.replace("Z", "+00:00"))
    return EpisodeRow(eid, symbol, ts, up, lo, outcome, {})


def test_split_boundaries():
    assert assign_split(datetime(2026, 8, 25, tzinfo=UTC)) == "TRAIN"
    assert assign_split(datetime(2026, 8, 28, 23, 59, tzinfo=UTC)) == "TRAIN"
    assert assign_split(datetime(2026, 8, 29, tzinfo=UTC)) == "VALIDATION"
    assert assign_split(datetime(2026, 8, 29, 23, 59, tzinfo=UTC)) == "VALIDATION"
    assert assign_split(datetime(2026, 8, 30, tzinfo=UTC)) == "TEST"
    assert assign_split(datetime(2026, 8, 31, 23, tzinfo=UTC)) == "TEST"
    assert SPLIT_BOUNDS["TRAIN"][1] == datetime(2026, 8, 29, tzinfo=UTC)


def test_no_episode_leak_between_splits():
    rows = [
        ep(eid="a", symbol="BTCUSDT", t0="2026-08-25T01:00:00Z", up=1, lo=2, outcome="UPPER_FIRST"),
        ep(eid="b", symbol="BTCUSDT", t0="2026-08-29T01:00:00Z", up=1, lo=2, outcome="LOWER_FIRST"),
        ep(eid="c", symbol="BTCUSDT", t0="2026-08-30T01:00:00Z", up=1, lo=2, outcome="NEITHER_WITHIN_HORIZON"),
    ]
    splits = split_episodes(rows)
    assert_no_split_leak(splits)
    assert {r.episode_id for r in splits["TRAIN"]} == {"a"}
    assert {r.episode_id for r in splits["VALIDATION"]} == {"b"}
    assert {r.episode_id for r in splits["TEST"]} == {"c"}


def test_majority_uses_only_provided_train_rows():
    train = [
        ep(eid="1", symbol="BTCUSDT", t0="2026-08-25T01:00:00Z", up=1, lo=2, outcome="LOWER_FIRST"),
        ep(eid="2", symbol="BTCUSDT", t0="2026-08-25T02:00:00Z", up=1, lo=2, outcome="LOWER_FIRST"),
        ep(eid="3", symbol="BTCUSDT", t0="2026-08-25T03:00:00Z", up=1, lo=2, outcome="UPPER_FIRST"),
    ]
    assert majority_label(train) == "LOWER_FIRST"
    # Tie prefers NEITHER, then UPPER, then LOWER.
    tied = [
        ep(eid="1", symbol="BTCUSDT", t0="2026-08-25T01:00:00Z", up=1, lo=2, outcome="UPPER_FIRST"),
        ep(eid="2", symbol="BTCUSDT", t0="2026-08-25T02:00:00Z", up=1, lo=2, outcome="LOWER_FIRST"),
        ep(eid="3", symbol="BTCUSDT", t0="2026-08-25T03:00:00Z", up=1, lo=2, outcome="NEITHER_WITHIN_HORIZON"),
    ]
    assert majority_label(tied) == "NEITHER_WITHIN_HORIZON"
    assert MAJORITY_TIE_BREAK[0] == "NEITHER_WITHIN_HORIZON"


def test_symbol_specific_majority_ignores_other_symbol():
    train = [
        ep(eid="1", symbol="BTCUSDT", t0="2026-08-25T01:00:00Z", up=1, lo=2, outcome="UPPER_FIRST"),
        ep(eid="2", symbol="BTCUSDT", t0="2026-08-25T02:00:00Z", up=1, lo=2, outcome="UPPER_FIRST"),
        ep(eid="3", symbol="DOGEUSDT", t0="2026-08-25T01:00:00Z", up=1, lo=2, outcome="LOWER_FIRST"),
        ep(eid="4", symbol="DOGEUSDT", t0="2026-08-25T02:00:00Z", up=1, lo=2, outcome="LOWER_FIRST"),
        ep(eid="5", symbol="DOGEUSDT", t0="2026-08-25T03:00:00Z", up=1, lo=2, outcome="LOWER_FIRST"),
    ]
    assert majority_label([r for r in train if r.symbol == "BTCUSDT"]) == "UPPER_FIRST"
    assert majority_label([r for r in train if r.symbol == "DOGEUSDT"]) == "LOWER_FIRST"


def test_nearest_and_tie_and_inverse():
    upper = ep(eid="u", symbol="BTCUSDT", t0="2026-08-25T01:00:00Z", up=5, lo=10, outcome="UPPER_FIRST")
    lower = ep(eid="l", symbol="BTCUSDT", t0="2026-08-25T01:00:00Z", up=10, lo=5, outcome="LOWER_FIRST")
    tie = ep(eid="t", symbol="BTCUSDT", t0="2026-08-25T01:00:00Z", up=7, lo=7, outcome="NEITHER_WITHIN_HORIZON")
    assert nearest_target(upper) == "UPPER_FIRST"
    assert nearest_target(lower) == "LOWER_FIRST"
    assert nearest_target(tie) == "NEITHER_WITHIN_HORIZON"
    assert inverse_nearest(upper) == "LOWER_FIRST"
    assert inverse_nearest(lower) == "UPPER_FIRST"
    assert inverse_nearest(tie) == "NEITHER_WITHIN_HORIZON"


def test_hash_control_deterministic():
    row = ep(eid="abc", symbol="BTCUSDT", t0="2026-08-25T01:00:00Z", up=1, lo=2, outcome="UPPER_FIRST")
    a = hash_control(row, dataset_fingerprint="fp")
    b = hash_control(row, dataset_fingerprint="fp")
    assert a == b
    assert a in PRIMARY_CLASSES
    other = hash_control(row, dataset_fingerprint="other")
    # may or may not differ; still in class set
    assert other in PRIMARY_CLASSES


def test_metrics_zero_division_no_nan():
    metrics = classification_metrics(
        ["UPPER_FIRST", "UPPER_FIRST"],
        ["LOWER_FIRST", "LOWER_FIRST"],
        PRIMARY_CLASSES,
    )
    assert metrics["macro_precision"] >= 0.0
    assert "missing_predicted_class:UPPER_FIRST" in metrics["warnings"]
    assert "missing_predicted_class:NEITHER_WITHIN_HORIZON" in metrics["warnings"]
    lo, hi = wilson_interval(1, 2)
    assert 0.0 <= lo <= hi <= 1.0


def test_confusion_matrix_complete():
    metrics = classification_metrics(
        ["UPPER_FIRST", "LOWER_FIRST", "NEITHER_WITHIN_HORIZON"],
        ["UPPER_FIRST", "UPPER_FIRST", "NEITHER_WITHIN_HORIZON"],
        PRIMARY_CLASSES,
    )
    cm = metrics["confusion_matrix"]
    assert set(cm) == set(PRIMARY_CLASSES)
    assert all(set(cm[a]) == set(PRIMARY_CLASSES) for a in cm)


def test_dataset_hash_and_schema_gate():
    root = Path("results/liquidity_destination_bias_phase_1d_full_bounded_history")
    episodes, meta = verify_and_load_dataset(root, require_frozen_manifest=True)
    assert len(episodes) == 1719
    assert meta["dataset_fingerprint_sha256"] == EXPECTED_DATASET_FINGERPRINT
    assert meta["database_connection"] is False


def test_unknown_outcome_fail_closed(tmp_path: Path):
    from research.liquidity_destination_bias.baselines.data import load_episodes_csv

    src = Path(
        "results/liquidity_destination_bias_phase_1d_full_bounded_history/BTCUSDT/episodes.csv"
    )
    text = src.read_text(encoding="utf-8").splitlines()
    bad = text[1].replace("UPPER_FIRST", "WEIRD_OUTCOME", 1).replace(
        "LOWER_FIRST", "WEIRD_OUTCOME", 1
    )
    path = tmp_path / "episodes.csv"
    path.write_text("\n".join([text[0], bad]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown outcome"):
        load_episodes_csv(path)


def test_hash_mismatch_fail_closed(tmp_path: Path):
    src_btc = Path(
        "results/liquidity_destination_bias_phase_1d_full_bounded_history/BTCUSDT/episodes.csv"
    )
    src_doge = Path(
        "results/liquidity_destination_bias_phase_1d_full_bounded_history/DOGEUSDT/episodes.csv"
    )
    (tmp_path / "BTCUSDT").mkdir()
    (tmp_path / "DOGEUSDT").mkdir()
    (tmp_path / "BTCUSDT" / "episodes.csv").write_text(
        src_btc.read_text(encoding="utf-8").replace("UPPER_FIRST", "LOWER_FIRST", 1),
        encoding="utf-8",
    )
    (tmp_path / "DOGEUSDT" / "episodes.csv").write_bytes(src_doge.read_bytes())
    with pytest.raises(ValueError, match="hash mismatch|fingerprint"):
        verify_and_load_dataset(tmp_path, require_frozen_manifest=False)


def test_neither_primary_and_directional_handling():
    rows = [
        ep(eid="1", symbol="BTCUSDT", t0="2026-08-30T01:00:00Z", up=1, lo=2, outcome="UPPER_FIRST"),
        ep(eid="2", symbol="BTCUSDT", t0="2026-08-30T02:00:00Z", up=2, lo=1, outcome="LOWER_FIRST"),
        ep(eid="3", symbol="BTCUSDT", t0="2026-08-30T03:00:00Z", up=1, lo=1, outcome="NEITHER_WITHIN_HORIZON"),
    ]
    from research.liquidity_destination_bias.baselines.splits import (
        directional_rows,
        primary_rows,
    )

    assert len(primary_rows(rows)) == 3
    assert len(directional_rows(rows)) == 2


def test_no_future_fields_in_rules():
    # Rules only use distances / train majority / episode id hash.
    row = ep(eid="x", symbol="BTCUSDT", t0="2026-08-25T01:00:00Z", up=3, lo=9, outcome="LOWER_FIRST")
    assert nearest_target(row) == "UPPER_FIRST"


def test_cli_banner_and_reproducible_fingerprint():
    from research.liquidity_destination_bias.baselines import cli as baselines_cli

    assert RESEARCH_BANNER in baselines_cli.parser().description
    btc = Path(
        "results/liquidity_destination_bias_phase_1d_full_bounded_history/BTCUSDT/episodes.csv"
    ).read_bytes()
    doge = Path(
        "results/liquidity_destination_bias_phase_1d_full_bounded_history/DOGEUSDT/episodes.csv"
    ).read_bytes()
    assert dataset_fingerprint(btc, doge) == EXPECTED_DATASET_FINGERPRINT
    assert (
        hashlib.sha256(
            Path(
                "results/liquidity_destination_bias_phase_2_baselines/evaluation_contract_frozen_v1.yaml"
            ).read_bytes()
        ).hexdigest()
        == json.loads(
            Path(
                "results/liquidity_destination_bias_phase_2_baselines/dataset_freeze_manifest.json"
            ).read_text()
        )["evaluation_contract_sha256"]
    )

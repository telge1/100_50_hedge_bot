"""Fast offline tests for OI/Liquidation/Impact/L2 F2 event-chain discovery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.oi_liq_impact_l2.event_chain import (
    DEFAULT_HORIZON_MINUTES,
    STAGE_COMPRESSION_ONLY,
    STAGE_FLUSH_ONLY,
    STAGE_L2_RECOVERY_ONLY,
    STAGE_PRICE_RECLAIM,
    TERMINATION_COMPLETE,
    TERMINATION_IMPACT_DATA_ABORT,
    TERMINATION_L2_DATA_ABORT,
    TERMINATION_TECHNICAL_GAP_ABORT,
    TERMINATION_TIMEOUT,
    TERMINATION_WINDOW_END,
    EventChainError,
    build_episode,
    load_f1_artifacts,
    run_event_chain_discovery,
)


def _write_f1_bundle(
    tmp_path: Path,
    *,
    minute_features: pd.DataFrame,
    flush_candidates: pd.DataFrame,
    labels: pd.DataFrame | None = None,
) -> Path:
    input_dir = tmp_path / "f1"
    input_dir.mkdir(parents=True, exist_ok=True)
    minute_features.to_csv(input_dir / "minute_features.csv", index=False)
    flush_candidates.to_csv(input_dir / "flush_candidates.csv", index=False)
    manifest = {
        "format_version": "oi_liq_impact_l2_discovery/v2",
        "counts": {
            "minute_feature_rows": len(minute_features),
            "candidate_rows": len(flush_candidates),
            "label_rows": len(labels) if labels is not None else 0,
        },
        "symbols": ["TESTUSDT"],
    }
    (input_dir / "discovery_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )
    if labels is not None:
        labels.to_csv(input_dir / "labels_sidecar.csv", index=False)
    return input_dir


def _base_minutes(count: int = 8) -> pd.DatetimeIndex:
    return pd.date_range("2026-08-20T12:33:00Z", periods=count, freq="1min", tz="UTC")


def _row(
    minute: str,
    direction: str,
    *,
    close: float,
    previous_close: float | None = None,
    flush: bool = False,
    compression: bool = False,
    recovery: bool = False,
    trades_present: bool = True,
    technical_gap: bool = False,
    ob_genuine_seconds: int = 60,
    depth_change: float | None = None,
    imbalance_change: float | None = None,
    net_add_change: float | None = None,
    aggressive_notional: float = 100.0,
    previous_impact: float | None = 0.2,
    impact: float | None = 0.1,
) -> dict[str, object]:
    return {
        "symbol": "TESTUSDT",
        "minute": minute,
        "decision_at": pd.Timestamp(minute) + pd.Timedelta(minutes=1),
        "direction": direction,
        "technical_gap": technical_gap,
        "quality_reason": "",
        "candle_present": True,
        "trades_present": trades_present,
        "oi_present": True,
        "oi_state_valid": True,
        "orderbook_present": True,
        "ob_seconds": 60,
        "ob_invalid_seconds": 0,
        "ob_carried_forward_seconds": 0,
        "ob_genuine_seconds": ob_genuine_seconds,
        "ob_carried_forward_rate": 0.0,
        "ob_genuine_rate": 1.0,
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "previous_close": previous_close,
        "close_vs_previous_close_pct": None,
        "price_displacement_pct": -0.01 if direction == "LONG" else 0.01,
        "directional_adverse_displacement_pct": 0.01,
        "oi_last": 1000.0,
        "oi_delta_abs_1m": -10.0,
        "oi_delta_pct_1m": -0.01,
        "aggressive_notional": aggressive_notional,
        "opposite_notional": 50.0,
        "trade_count": 10,
        "liquidation_count": 1,
        "liquidation_notional": 20.0,
        "liquidation_to_aggressive_notional": 0.2,
        "impact_per_aggressive_notional": impact,
        "previous_impact_per_aggressive_notional": previous_impact,
        "aggressive_notional_change": 0.0,
        "impact_compression_observed": compression,
        "genuine_spread_bps_mean": 1.0,
        "genuine_imbalance_l50_mean": 0.1,
        "directional_imbalance": 0.1 if direction == "LONG" else -0.1,
        "genuine_support_depth_l50_mean": 100.0,
        "genuine_opposing_depth_l50_mean": 90.0,
        "directional_depth_change": depth_change,
        "directional_imbalance_change": imbalance_change,
        "directional_ofi": 1.0,
        "directional_net_add": 5.0,
        "directional_net_add_change": net_add_change,
        "l2_recovery_observed": recovery,
        "directional_flush_observed": flush,
        "stage_reached": "DIRECTIONAL_FLUSH_OBSERVED" if flush else "NONE",
    }


def _long_index(minute_index: int) -> int:
    return minute_index * 2


def _synthetic_chain_bundle() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    minutes = [_base_minutes()[i].isoformat().replace("+00:00", "Z") for i in range(8)]
    rows: list[dict[str, object]] = []
    for minute in minutes:
        for direction in ("LONG", "SHORT"):
            rows.append(
                _row(
                    minute,
                    direction,
                    close=100.0 if direction == "LONG" else 101.0,
                    previous_close=101.0 if direction == "LONG" else 100.0,
                )
            )
    rows[_long_index(0)]["directional_flush_observed"] = True
    rows[_long_index(0)]["stage_reached"] = "DIRECTIONAL_FLUSH_OBSERVED"
    rows[_long_index(0)]["previous_close"] = 101.0
    rows[_long_index(2)]["impact_compression_observed"] = True
    rows[_long_index(4)]["l2_recovery_observed"] = True
    rows[_long_index(4)]["directional_depth_change"] = 1.0
    rows[_long_index(6)]["close"] = 101.5
    rows[_long_index(6)]["high"] = 102.0
    rows[_long_index(6)]["low"] = 100.5

    minute_features = pd.DataFrame(rows)
    for row in minute_features.to_dict("records"):
        row["decision_at"] = (
            pd.Timestamp(str(row["minute"])) + pd.Timedelta(minutes=1)
        ).isoformat().replace("+00:00", "Z")
    minute_features = pd.DataFrame(minute_features.to_dict("records"))

    flush_candidates = pd.DataFrame(
        [
            {
                "candidate_id": "oildisc:testlong000000000001",
                "symbol": "TESTUSDT",
                "minute": minutes[0],
                "decision_at": (
                    pd.Timestamp(minutes[0]) + pd.Timedelta(minutes=1)
                ).isoformat().replace("+00:00", "Z"),
                "direction": "LONG",
                "stage_reached": "DIRECTIONAL_FLUSH_OBSERVED",
                "quality_reason": "",
                "price_displacement_pct": -0.01,
                "oi_delta_pct_1m": -0.01,
                "liquidation_count": 1,
                "liquidation_notional": 20.0,
                "aggressive_notional": 100.0,
                "impact_per_aggressive_notional": 0.2,
                "previous_impact_per_aggressive_notional": 0.3,
                "impact_compression_observed": False,
                "l2_recovery_observed": False,
            }
        ]
    )
    labels = pd.DataFrame(
        [
            {
                "candidate_id": "oildisc:testlong000000000001",
                "symbol": "TESTUSDT",
                "direction": "LONG",
                "decision_at": flush_candidates.iloc[0]["decision_at"],
                "entry_at": flush_candidates.iloc[0]["decision_at"],
                "entry_price": 100.0,
                "label_horizon_minutes": 60,
                "mfe_pct": 0.01,
                "mae_pct": -0.01,
                "forward_return_pct": 0.005,
                "label_status": "COMPLETE",
            }
        ]
    )
    return minute_features, flush_candidates, labels


def _lookup(minute_features: pd.DataFrame) -> dict[tuple[str, str, str], pd.Series]:
    return {
        (str(row["symbol"]), str(row["minute"]), str(row["direction"])): row
        for _, row in minute_features.iterrows()
    }


def test_flush_minute_does_not_confirm_later_stages() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    lookup = _lookup(minute_features)
    candidate = flush_candidates.iloc[0].to_dict()
    episode, timeline = build_episode(candidate, lookup, horizon_minutes=6)
    assert timeline[0]["relative_minute"] == 1
    assert episode["compression_minute"] == minute_features.iloc[_long_index(2)]["minute"]
    assert episode["recovery_minute"] == minute_features.iloc[_long_index(4)]["minute"]
    assert all(row["relative_minute"] >= 1 for row in timeline)


def test_compression_must_follow_flush() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    lookup = _lookup(minute_features)
    episode, _ = build_episode(flush_candidates.iloc[0].to_dict(), lookup, horizon_minutes=6)
    assert episode["minutes_flush_to_compression"] == 2


def test_recovery_must_follow_compression() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    lookup = _lookup(minute_features)
    episode, _ = build_episode(flush_candidates.iloc[0].to_dict(), lookup, horizon_minutes=6)
    assert episode["minutes_compression_to_recovery"] == 2


def test_reclaim_must_follow_recovery() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    lookup = _lookup(minute_features)
    episode, timeline = build_episode(flush_candidates.iloc[0].to_dict(), lookup, horizon_minutes=6)
    assert episode["stage_reached"] == STAGE_PRICE_RECLAIM
    assert episode["minutes_recovery_to_reclaim"] == 2
    reclaim_rows = [row for row in timeline if row["transition_reason"] == "PRICE_RECLAIM_CONFIRMED"]
    assert len(reclaim_rows) == 1
    assert reclaim_rows[0]["relative_minute"] > 4


def test_long_reclaim_uses_close_against_pre_flush_level() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    lookup = _lookup(minute_features)
    episode, _ = build_episode(flush_candidates.iloc[0].to_dict(), lookup, horizon_minutes=6)
    assert episode["pre_flush_level"] == 101.0
    assert episode["reclaim_close"] == 101.5


def test_short_reclaim_uses_close_against_pre_flush_level() -> None:
    minutes = [_base_minutes()[i].isoformat().replace("+00:00", "Z") for i in range(6)]
    rows = []
    for idx, minute in enumerate(minutes):
        rows.append(
            _row(
                minute,
                "SHORT",
                close=100.0 if idx < 5 else 99.0,
                previous_close=100.5,
                flush=idx == 0,
                compression=idx == 2,
                recovery=idx == 4,
                depth_change=1.0 if idx == 4 else None,
            )
        )
    minute_features = pd.DataFrame(rows)
    candidate = {
        "candidate_id": "oildisc:testshort00000000001",
        "symbol": "TESTUSDT",
        "minute": minutes[0],
        "decision_at": (pd.Timestamp(minutes[0]) + pd.Timedelta(minutes=1))
        .isoformat()
        .replace("+00:00", "Z"),
        "direction": "SHORT",
    }
    episode, _ = build_episode(candidate, _lookup(minute_features), horizon_minutes=5)
    assert episode["stage_reached"] == STAGE_PRICE_RECLAIM
    assert episode["reclaim_close"] == 99.0


def test_high_low_alone_do_not_confirm_reclaim() -> None:
    minutes = [_base_minutes()[i].isoformat().replace("+00:00", "Z") for i in range(5)]
    rows = []
    for idx, minute in enumerate(minutes):
        row = _row(
            minute,
            "LONG",
            close=100.0,
            previous_close=101.0,
            flush=idx == 0,
            compression=idx == 1,
            recovery=idx == 2,
            depth_change=1.0 if idx == 2 else None,
        )
        if idx == 3:
            row["close"] = 100.5
            row["high"] = 102.0
        rows.append(row)
    minute_features = pd.DataFrame(rows)
    candidate = {
        "candidate_id": "oildisc:testhighlow0000000001",
        "symbol": "TESTUSDT",
        "minute": minutes[0],
        "decision_at": (pd.Timestamp(minutes[0]) + pd.Timedelta(minutes=1))
        .isoformat()
        .replace("+00:00", "Z"),
        "direction": "LONG",
    }
    episode, _ = build_episode(candidate, _lookup(minute_features), horizon_minutes=4)
    assert episode["stage_reached"] == STAGE_L2_RECOVERY_ONLY


def test_timeout_after_exact_horizon() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    minute_features = minute_features.copy()
    minute_features["impact_compression_observed"] = False
    minute_features["l2_recovery_observed"] = False
    episode, timeline = build_episode(
        flush_candidates.iloc[0].to_dict(), _lookup(minute_features), horizon_minutes=3
    )
    assert episode["stage_reached"] == STAGE_FLUSH_ONLY
    assert episode["termination_reason"] == TERMINATION_TIMEOUT
    assert len(timeline) == 3


def test_technical_gap_aborts_episode() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    minute_features = minute_features.copy()
    minute_features.loc[_long_index(1), "technical_gap"] = True
    episode, timeline = build_episode(
        flush_candidates.iloc[0].to_dict(), _lookup(minute_features), horizon_minutes=6
    )
    assert episode["termination_reason"] == TERMINATION_TECHNICAL_GAP_ABORT
    assert timeline[0]["transition_reason"] == TERMINATION_TECHNICAL_GAP_ABORT


def test_missing_trade_data_aborts_compression_search() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    minute_features = minute_features.copy()
    minute_features.loc[_long_index(1), "trades_present"] = False
    episode, timeline = build_episode(
        flush_candidates.iloc[0].to_dict(), _lookup(minute_features), horizon_minutes=6
    )
    assert episode["termination_reason"] == TERMINATION_IMPACT_DATA_ABORT
    assert timeline[0]["transition_reason"] == TERMINATION_IMPACT_DATA_ABORT


def test_cf_only_minute_aborts_recovery_search() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    minute_features = minute_features.copy()
    minute_features.loc[_long_index(3), "ob_genuine_seconds"] = 0
    episode, timeline = build_episode(
        flush_candidates.iloc[0].to_dict(), _lookup(minute_features), horizon_minutes=6
    )
    assert episode["termination_reason"] == TERMINATION_L2_DATA_ABORT
    assert timeline[2]["transition_reason"] == TERMINATION_L2_DATA_ABORT


def test_window_end_when_minute_missing() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    minute_features = minute_features.iloc[:2]
    episode, timeline = build_episode(
        flush_candidates.iloc[0].to_dict(), _lookup(minute_features), horizon_minutes=6
    )
    assert episode["termination_reason"] == TERMINATION_WINDOW_END


def test_overlapping_episodes_remain_separate(tmp_path: Path) -> None:
    minute_features, flush_candidates, labels = _synthetic_chain_bundle()
    minute_features = minute_features.copy()
    overlap_minute = minute_features.iloc[_long_index(2)]["minute"]
    minute_features.loc[_long_index(2), "directional_flush_observed"] = True
    minute_features.loc[_long_index(2), "stage_reached"] = "DIRECTIONAL_FLUSH_OBSERVED"
    second = flush_candidates.iloc[0].to_dict()
    second["candidate_id"] = "oildisc:testlong000000000002"
    second["minute"] = overlap_minute
    second["decision_at"] = (
        pd.Timestamp(str(second["minute"])) + pd.Timedelta(minutes=1)
    ).isoformat().replace("+00:00", "Z")
    flush_candidates = pd.concat(
        [flush_candidates, pd.DataFrame([second])], ignore_index=True
    )
    labels = pd.concat(
        [
            labels,
            pd.DataFrame(
                [{**labels.iloc[0].to_dict(), "candidate_id": second["candidate_id"]}]
            ),
        ],
        ignore_index=True,
    )
    input_dir = _write_f1_bundle(
        tmp_path,
        minute_features=minute_features,
        flush_candidates=flush_candidates,
        labels=labels,
    )
    result = run_event_chain_discovery(
        input_dir=input_dir,
        output_dir=tmp_path / "f2",
        horizon_minutes=6,
    )
    episodes = pd.read_csv(tmp_path / "f2" / "event_episodes.csv")
    assert result.episode_count == 2
    assert episodes["candidate_id"].nunique() == 2
    assert episodes["overlap_with_other_episodes"].any()


def test_future_labels_do_not_change_transitions(tmp_path: Path) -> None:
    minute_features, flush_candidates, labels = _synthetic_chain_bundle()
    labels.loc[0, "forward_return_pct"] = 999.0
    labels.loc[0, "mfe_pct"] = 999.0
    input_dir = _write_f1_bundle(
        tmp_path,
        minute_features=minute_features,
        flush_candidates=flush_candidates,
        labels=labels,
    )
    first = run_event_chain_discovery(
        input_dir=input_dir,
        output_dir=tmp_path / "f2a",
        horizon_minutes=6,
    )
    labels.loc[0, "forward_return_pct"] = -999.0
    _write_f1_bundle(
        tmp_path / "labels_only",
        minute_features=minute_features,
        flush_candidates=flush_candidates,
        labels=labels,
    )
    second = run_event_chain_discovery(
        input_dir=input_dir,
        output_dir=tmp_path / "f2b",
        horizon_minutes=6,
    )
    episodes_a = (tmp_path / "f2a" / "event_episodes.csv").read_bytes()
    episodes_b = (tmp_path / "f2b" / "event_episodes.csv").read_bytes()
    assert first.summary == second.summary
    assert episodes_a == episodes_b


def test_deterministic_outputs(tmp_path: Path) -> None:
    minute_features, flush_candidates, labels = _synthetic_chain_bundle()
    input_dir = _write_f1_bundle(
        tmp_path,
        minute_features=minute_features,
        flush_candidates=flush_candidates,
        labels=labels,
    )
    snapshots = []
    for name in ("out1", "out2"):
        run_event_chain_discovery(
            input_dir=input_dir,
            output_dir=tmp_path / name,
            horizon_minutes=6,
        )
        snapshots.append(
            {
                path.name: path.read_bytes()
                for path in sorted((tmp_path / name).iterdir())
            }
        )
    assert snapshots[0] == snapshots[1]


def test_input_hash_mismatch_is_detected(tmp_path: Path) -> None:
    minute_features, flush_candidates, labels = _synthetic_chain_bundle()
    input_dir = _write_f1_bundle(
        tmp_path,
        minute_features=minute_features,
        flush_candidates=flush_candidates,
        labels=labels,
    )
    loaded = load_f1_artifacts(input_dir)
    assert loaded.input_hashes["minute_features.csv"] == hashlib.sha256(
        (input_dir / "minute_features.csv").read_bytes()
    ).hexdigest()
    manifest = json.loads((input_dir / "discovery_manifest.json").read_text())
    manifest["counts"]["candidate_rows"] = 999
    (input_dir / "discovery_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    with pytest.raises(EventChainError):
        load_f1_artifacts(input_dir)


def test_missing_trades_present_blocks_instead_of_inventing_semantics(
    tmp_path: Path,
) -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    minute_features = minute_features.drop(columns=["trades_present"])
    input_dir = _write_f1_bundle(
        tmp_path,
        minute_features=minute_features,
        flush_candidates=flush_candidates,
    )
    with pytest.raises(EventChainError, match="cannot distinguish missing public trades"):
        run_event_chain_discovery(
            input_dir=input_dir,
            output_dir=tmp_path / "f2",
            horizon_minutes=6,
            include_outcomes=False,
        )


def test_compression_only_stage_when_no_recovery() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    minute_features["l2_recovery_observed"] = False
    episode, _ = build_episode(
        flush_candidates.iloc[0].to_dict(), _lookup(minute_features), horizon_minutes=4
    )
    assert episode["stage_reached"] == STAGE_COMPRESSION_ONLY
    assert episode["termination_reason"] == TERMINATION_TIMEOUT


def test_net_add_recovery_branch_recorded() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    minute_features = minute_features.copy()
    minute_features.loc[_long_index(4), "directional_depth_change"] = pd.NA
    minute_features.loc[_long_index(4), "directional_imbalance_change"] = pd.NA
    minute_features.loc[_long_index(4), "directional_net_add_change"] = 2.0
    episode, _ = build_episode(
        flush_candidates.iloc[0].to_dict(), _lookup(minute_features), horizon_minutes=6
    )
    assert episode["recovery_confirmed_by"] == "NET_ADD"


def test_complete_chain_termination_reason() -> None:
    minute_features, flush_candidates, _ = _synthetic_chain_bundle()
    episode, _ = build_episode(
        flush_candidates.iloc[0].to_dict(), _lookup(minute_features), horizon_minutes=6
    )
    assert episode["termination_reason"] == TERMINATION_COMPLETE

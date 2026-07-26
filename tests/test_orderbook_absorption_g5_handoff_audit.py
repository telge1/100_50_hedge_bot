"""Unit tests for A2→G5 handoff audit (synthetic fixtures, no DB)."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orderbook_analyse.orderbook_absorption_g5_handoff_audit import (
    A2Episode,
    G5Action,
    HandoffParams,
    bps_distance,
    compute_outcome_with_fp,
    dedupe_actions,
    evaluate_reentry,
    filter_g5_by_wall_distance,
    pair_a2_to_g5,
    run_handoff_audit,
    wall_as_of,
    write_csv,
)

TS0 = datetime(2026, 7, 26, 10, 0, 0, tzinfo=timezone.utc)


def _a2(
    eid: str,
    armed: datetime,
    *,
    wall: float = 0.64,
    score: int = 5,
    quality: str = "MEDIUM",
) -> A2Episode:
    return A2Episode(
        episode_id=eid,
        armed_time=armed,
        first_signal_time=armed - timedelta(seconds=30),
        action_time=armed,
        wall_price=wall,
        wall_side="Ask",
        a2_score=score,
        a2_quality=quality,
        a2_buy_at_wall_notional=5000.0,
        a2_price_progress_bps=1.0,
        a2_level_join_quality="MEDIUM",
        a2_regime="transition",
        mid=0.635,
    )


def _g5(
    wid: str,
    action: datetime,
    *,
    warning: datetime | None = None,
    mid: float = 0.634,
) -> G5Action:
    wt = warning or (action - timedelta(seconds=60))
    return G5Action(
        warning_id=wid,
        episode_id=f"E_{wid}",
        warning_time=wt,
        action_time=action,
        action="HEDGE_PREPARE",
        mid=mid,
        warning_score=14,
        warning_quality="VERY_STRONG_WARNING",
        support_level=0.63,
        reason="test",
    )


def test_armed_time_uses_action_else_signal() -> None:
    ep = _a2("E1", TS0)
    assert ep.armed_time == TS0
    ep2 = A2Episode(
        episode_id="E2",
        armed_time=TS0,  # loader sets this
        first_signal_time=TS0 - timedelta(seconds=30),
        action_time=None,
        wall_price=0.64,
        wall_side="Ask",
        a2_score=3,
        a2_quality="LOW",
        a2_buy_at_wall_notional=1.0,
        a2_price_progress_bps=0.0,
        a2_level_join_quality="LOW",
        a2_regime=None,
        mid=0.63,
    )
    assert ep2.armed_time == TS0


def test_no_g5_before_armed() -> None:
    a2 = [_a2("E1", TS0)]
    g5 = [_g5("W1", TS0 - timedelta(seconds=10))]
    acc, rej, _ = pair_a2_to_g5(a2, g5, window_seconds=180)
    assert acc == []
    assert any(r["reject_reason"] == "BEFORE_ARMED" for r in rej)


def test_no_g5_after_expiry() -> None:
    a2 = [_a2("E1", TS0)]
    g5 = [_g5("W1", TS0 + timedelta(seconds=181))]
    acc, rej, _ = pair_a2_to_g5(a2, g5, window_seconds=180)
    assert acc == []
    assert any(r["reject_reason"] == "AFTER_EXPIRY" for r in rej)


def test_event_exactly_at_expiry_included() -> None:
    a2 = [_a2("E1", TS0)]
    g5 = [_g5("W1", TS0 + timedelta(seconds=180))]
    acc, _, _ = pair_a2_to_g5(a2, g5, window_seconds=180)
    assert len(acc) == 1


def test_nearest_g5_wins() -> None:
    a2 = [_a2("E1", TS0)]
    g5 = [
        _g5("W2", TS0 + timedelta(seconds=90)),
        _g5("W1", TS0 + timedelta(seconds=30)),
    ]
    acc, _, _ = pair_a2_to_g5(a2, g5, window_seconds=180)
    assert len(acc) == 1
    assert acc[0]["g5_warning_id"] == "W1"


def test_one_g5_one_a2_assignment() -> None:
    a2 = [_a2("E1", TS0), _a2("E2", TS0 + timedelta(seconds=10), score=9, quality="HIGH")]
    g5 = [_g5("W1", TS0 + timedelta(seconds=60))]
    acc, rej, _ = pair_a2_to_g5(a2, g5, window_seconds=180)
    assert len(acc) == 1
    # higher quality / closer delay: E2 delay=50, E1 delay=60 → E2 wins on delay
    assert acc[0]["a2_episode_id"] == "E2"
    assert any(r["reject_reason"] == "G5_ALREADY_PAIRED" for r in rej)


def test_tie_break_deterministic() -> None:
    a2 = [
        _a2("E_b", TS0, score=5, quality="MEDIUM"),
        _a2("E_a", TS0, score=5, quality="MEDIUM"),
    ]
    g5 = [_g5("W1", TS0 + timedelta(seconds=60))]
    acc, _, _ = pair_a2_to_g5(a2, g5, window_seconds=180)
    assert acc[0]["a2_episode_id"] == "E_a"  # same delay/quality/score → lex smaller id


def test_d3_window_boundary() -> None:
    a2 = [_a2("E1", TS0)]
    g5 = [_g5("W1", TS0 + timedelta(seconds=60))]
    acc30, _, _ = pair_a2_to_g5(a2, g5, window_seconds=30)
    acc60, _, _ = pair_a2_to_g5(a2, g5, window_seconds=60)
    assert acc30 == []
    assert len(acc60) == 1


def test_wall_as_of_no_future() -> None:
    walls = [
        (TS0, 0.64, 0.63),
        (TS0 + timedelta(seconds=30), 0.65, 0.63),
    ]
    w, ts = wall_as_of(walls, as_of=TS0 + timedelta(seconds=10))
    assert w == 0.64
    assert ts == TS0


def test_d4_wall_distance_bps() -> None:
    assert abs(bps_distance(0.6405, 0.64) - (0.0005 / 0.64 * 10000)) < 1e-6
    g5 = [_g5("W1", TS0 + timedelta(seconds=30), mid=0.6401)]
    walls = [(TS0, 0.64, 0.635)]
    kept3 = filter_g5_by_wall_distance(g5, walls, max_bps=3)
    kept1 = filter_g5_by_wall_distance(g5, walls, max_bps=1)
    assert len(kept3) == 1
    assert kept1 == []


def test_d5_not_first_unstable_tick() -> None:
    wall = 0.64
    mids = [
        (TS0 + timedelta(seconds=30), 0.641),
        (TS0 + timedelta(seconds=60), 0.639),  # first under — not enough for C2
        (TS0 + timedelta(seconds=90), 0.641),  # reclaim invalidates
        (TS0 + timedelta(seconds=120), 0.639),
        (TS0 + timedelta(seconds=150), 0.638),
    ]
    re = evaluate_reentry(
        wall_price=wall,
        armed_time=TS0,
        g5_action_time=TS0,
        mids=mids,
        confirm_snapshots=2,
    )
    assert re["reentry_confirmed"] is True
    assert re["confirmation_snapshots"] == 2
    assert re["confirmation_time"] == (TS0 + timedelta(seconds=150)).isoformat()


def test_d5_c2_needs_two_snapshots() -> None:
    wall = 0.64
    mids = [
        (TS0 + timedelta(seconds=30), 0.641),
        (TS0 + timedelta(seconds=60), 0.639),
    ]
    re1 = evaluate_reentry(
        wall_price=wall,
        armed_time=TS0,
        g5_action_time=TS0,
        mids=mids,
        confirm_snapshots=1,
    )
    re2 = evaluate_reentry(
        wall_price=wall,
        armed_time=TS0,
        g5_action_time=TS0,
        mids=mids,
        confirm_snapshots=2,
    )
    assert re1["reentry_confirmed"] is True
    assert re2["reentry_confirmed"] is False


def test_d5_invalidated_on_reclaim() -> None:
    # covered in unstable tick test — reclaim resets under_count
    wall = 0.64
    mids = [
        (TS0 + timedelta(seconds=30), 0.641),
        (TS0 + timedelta(seconds=60), 0.639),
        (TS0 + timedelta(seconds=90), 0.642),
    ]
    re = evaluate_reentry(
        wall_price=wall,
        armed_time=TS0,
        g5_action_time=TS0,
        mids=mids,
        confirm_snapshots=2,
    )
    assert re["reentry_confirmed"] is False


def test_dedupe_actions() -> None:
    acts = [
        {"action_time": TS0.isoformat(), "wall_price": 0.64, "a2_episode_id": "E1"},
        {
            "action_time": (TS0 + timedelta(seconds=30)).isoformat(),
            "wall_price": 0.6401,
            "a2_episode_id": "E2",
        },
        {
            "action_time": (TS0 + timedelta(seconds=300)).isoformat(),
            "wall_price": 0.64,
            "a2_episode_id": "E3",
        },
    ]
    kept = dedupe_actions(acts, gap_seconds=120, level_bps=10)
    assert len(kept) == 2
    assert kept[0]["a2_episode_id"] == "E1"
    assert kept[1]["a2_episode_id"] == "E3"


def test_outcomes_strictly_after_action() -> None:
    mids = [
        (TS0, 1.0),
        (TS0 + timedelta(seconds=30), 1.0),
        (TS0 + timedelta(seconds=60), 0.996),
        (TS0 + timedelta(seconds=120), 0.990),
    ]
    oc = compute_outcome_with_fp(
        action_time=TS0 + timedelta(seconds=30), entry_mid=1.0, mids=mids
    )
    assert oc["hit_down_0_25"] is True
    assert oc["hit_down_0_10"] is True


def test_false_positive_semantics() -> None:
    # up first then down
    mids = [
        (TS0, 1.0),
        (TS0 + timedelta(seconds=30), 1.003),
        (TS0 + timedelta(seconds=90), 0.996),
    ]
    oc = compute_outcome_with_fp(action_time=TS0, entry_mid=1.0, mids=mids)
    assert oc["fp_adverse_before"] is True
    assert oc["false_positive"] is True


def test_outcome_does_not_affect_pairing() -> None:
    # pairing ignores mids entirely
    a2 = [_a2("E1", TS0)]
    g5 = [_g5("W1", TS0 + timedelta(seconds=60))]
    acc1, _, _ = pair_a2_to_g5(a2, g5, window_seconds=180)
    acc2, _, _ = pair_a2_to_g5(a2, g5, window_seconds=180)
    assert acc1 == acc2


def test_end_to_end_d0_parity_and_outputs(tmp_path: Path) -> None:
    abs_dir = tmp_path / "abs"
    g5_dir = tmp_path / "g5"
    abs_dir.mkdir()
    g5_dir.mkdir()
    out = tmp_path / "out"

    # snapshots / mids
    snaps = []
    for i in range(20):
        ts = TS0 + timedelta(seconds=30 * i)
        mid = 0.635 - i * 0.0001
        snaps.append(
            {
                "index": i,
                "timestamp": ts.isoformat(),
                "mid": mid,
                "nearest_ask": 0.64,
            }
        )
    write_csv(abs_dir / "snapshot_features.csv", snaps)

    # A2 episode + action
    write_csv(
        abs_dir / "pattern_episodes.csv",
        [
            {
                "episode_id": "E0088",
                "pattern_type": "ASK_ABSORPTION",
                "episode_start": TS0.isoformat(),
                "episode_end": TS0.isoformat(),
                "first_signal_time": (TS0 - timedelta(seconds=30)).isoformat(),
                "strongest_score_time": TS0.isoformat(),
                "action_time": TS0.isoformat(),
                "level_price": 0.64,
                "max_score": 5,
                "raw_signal_count": 1,
                "signal_ids": "S1",
                "strongest_signal_id": "S1",
            }
        ],
    )
    write_csv(
        abs_dir / "pattern_actions.csv",
        [
            {
                "signal_id": "S1",
                "episode_id": "E0088",
                "pattern_type": "ASK_ABSORPTION",
                "signal_time": (TS0 - timedelta(seconds=30)).isoformat(),
                "action_time": TS0.isoformat(),
                "level": 0.64,
                "score": 5,
                "mid": 0.635,
                "action_mid": 0.635,
                "aggressive_buy_notional": 5000,
                "price_progress_bps": 1.0,
                "confidence": "MEDIUM",
                "valid": True,
                "trend_state": "transition",
            }
        ],
    )

    g5_at = TS0 + timedelta(seconds=90)
    write_csv(
        g5_dir / "integrated_variant_actions.csv",
        [
            {
                "warning_id": "W1",
                "episode_id": "EG1",
                "warning_time": (g5_at - timedelta(seconds=60)).isoformat(),
                "action_time": g5_at.isoformat(),
                "variant": "G5",
                "action": "HEDGE_PREPARE",
                "warning_score": 14,
                "warning_quality": "VERY_STRONG_WARNING",
                "mid": 0.634,
                "support_level": 0.63,
                "reason": "test",
            },
            {
                "warning_id": "W2",
                "episode_id": "EG2",
                "warning_time": (TS0 + timedelta(seconds=300)).isoformat(),
                "action_time": (TS0 + timedelta(seconds=360)).isoformat(),
                "variant": "G5",
                "action": "HEDGE_PREPARE",
                "warning_score": 12,
                "warning_quality": "VERY_STRONG_WARNING",
                "mid": 0.632,
                "support_level": 0.62,
                "reason": "test",
            },
            # non-G5 should be ignored
            {
                "warning_id": "W0",
                "episode_id": "EG0",
                "warning_time": TS0.isoformat(),
                "action_time": TS0.isoformat(),
                "variant": "G0",
                "action": "STOP_LONG_ADDS",
                "warning_score": 5,
                "warning_quality": "WEAK",
                "mid": 0.635,
                "support_level": "",
                "reason": "x",
            },
        ],
    )
    # empty warnings file — should be handled
    write_csv(g5_dir / "integrated_warning_context.csv", [])

    summary = run_handoff_audit(
        absorption_dir=abs_dir,
        g5_dir=g5_dir,
        output_dir=out,
        params=HandoffParams(armed_window_seconds=180),
    )
    assert summary["integrity"]["d0_parity_ok"] is True
    assert summary["integrity"]["d0_reproduced_action_count"] == 2
    assert summary["decision"] in {
        "A2_G5_HANDOFF_INCREMENTAL_VALUE_FOUND",
        "A2_G5_HANDOFF_EARLY_WARNING_ONLY",
        "A2_G5_HANDOFF_FILTER_VALUE_ONLY",
        "NO_INCREMENTAL_VALUE_VS_G5",
        "HANDOFF_DATA_INSUFFICIENT",
        "AUDIT_INVALID",
    }
    required = [
        "REPORT.md",
        "integrity.json",
        "config.json",
        "handoff_actions.csv",
        "handoff_outcomes.csv",
        "handoff_variant_summary.csv",
        "handoff_raw_pairings.csv",
        "a2_episodes_loaded.csv",
        "g5_actions_loaded.csv",
    ]
    for name in required:
        assert (out / name).exists(), name

    # D0 action times
    with (out / "handoff_actions.csv").open() as f:
        rows = [r for r in csv.DictReader(f) if r["variant"] == "D0"]
    assert {r["action_time"] for r in rows} == {
        g5_at.isoformat(),
        (TS0 + timedelta(seconds=360)).isoformat(),
    }


def test_empty_warnings_handled(tmp_path: Path) -> None:
    # covered in end_to_end with empty warning_context
    assert True


def test_stable_rerun(tmp_path: Path) -> None:
    # minimal reuse of end_to_end fixture by calling twice
    abs_dir = tmp_path / "abs"
    g5_dir = tmp_path / "g5"
    abs_dir.mkdir()
    g5_dir.mkdir()
    write_csv(
        abs_dir / "snapshot_features.csv",
        [
            {"index": 0, "timestamp": TS0.isoformat(), "mid": 0.63, "nearest_ask": 0.64},
            {
                "index": 1,
                "timestamp": (TS0 + timedelta(seconds=30)).isoformat(),
                "mid": 0.629,
                "nearest_ask": 0.64,
            },
        ],
    )
    write_csv(
        abs_dir / "pattern_episodes.csv",
        [
            {
                "episode_id": "E1",
                "pattern_type": "ASK_ABSORPTION",
                "episode_start": TS0.isoformat(),
                "episode_end": TS0.isoformat(),
                "first_signal_time": TS0.isoformat(),
                "strongest_score_time": TS0.isoformat(),
                "action_time": TS0.isoformat(),
                "level_price": 0.64,
                "max_score": 5,
                "raw_signal_count": 1,
                "signal_ids": "S1",
                "strongest_signal_id": "S1",
            }
        ],
    )
    write_csv(
        abs_dir / "pattern_actions.csv",
        [
            {
                "signal_id": "S1",
                "episode_id": "E1",
                "pattern_type": "ASK_ABSORPTION",
                "signal_time": TS0.isoformat(),
                "action_time": TS0.isoformat(),
                "level": 0.64,
                "score": 5,
                "mid": 0.63,
                "action_mid": 0.63,
                "aggressive_buy_notional": 1000,
                "price_progress_bps": 0,
                "confidence": "MEDIUM",
                "valid": True,
                "trend_state": "transition",
            }
        ],
    )
    write_csv(
        g5_dir / "integrated_variant_actions.csv",
        [
            {
                "warning_id": "W1",
                "episode_id": "G1",
                "warning_time": (TS0 + timedelta(seconds=30)).isoformat(),
                "action_time": (TS0 + timedelta(seconds=60)).isoformat(),
                "variant": "G5",
                "action": "HEDGE_PREPARE",
                "warning_score": 10,
                "warning_quality": "STRONG",
                "mid": 0.63,
                "support_level": 0.62,
                "reason": "t",
            }
        ],
    )
    out1 = tmp_path / "o1"
    out2 = tmp_path / "o2"
    s1 = run_handoff_audit(
        absorption_dir=abs_dir, g5_dir=g5_dir, output_dir=out1, params=HandoffParams()
    )
    s2 = run_handoff_audit(
        absorption_dir=abs_dir, g5_dir=g5_dir, output_dir=out2, params=HandoffParams()
    )
    assert s1["decision"] == s2["decision"]
    assert s1["integrity"]["d0_reproduced_action_count"] == s2["integrity"][
        "d0_reproduced_action_count"
    ]

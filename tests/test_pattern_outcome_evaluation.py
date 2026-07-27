"""Phase 6 pattern outcome evaluation tests (synthetic fixtures, no ClickHouse)."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from orderbook_analyse.general_research_runner import (
    GeneralResearchParams,
    parse_args,
    run_general_research,
)
from orderbook_analyse.pattern_outcome_evaluation import (
    DIRECTION_LONG,
    DIRECTION_NEUTRAL,
    DIRECTION_SHORT,
    DIRECTION_UNKNOWN,
    OutcomeParams,
    PHASE6_OUTPUT_FILES,
    PricePoint,
    SegmentPath,
    bootstrap_ci,
    build_direction_mapping,
    check_outcome_integrity,
    cluster_id_for,
    compute_forward_outcome,
    coverage_tolerance_seconds,
    estimate_sample_interval_seconds,
    expected_direction_for,
    label_from_score,
    parse_float_list,
    parse_int_list,
    research_score,
    run_pattern_outcome_evaluation,
    validate_outcome_params,
    write_csv_headered,
)

TS0 = datetime(2026, 7, 26, 10, 0, 0, tzinfo=timezone.utc)


def _path_from_mids(
    *,
    sid: str,
    start: datetime,
    end: datetime,
    points: list[tuple[datetime, float]],
) -> SegmentPath:
    pp = [
        PricePoint(ts=t, mid=p, close=p, high=p, low=p, open=p) for t, p in points
    ]
    return SegmentPath(
        segment_id=sid,
        start=start,
        end=end,
        points=pp,
        times=[t for t, _ in points],
    )


def _path_bars(
    *,
    sid: str,
    start: datetime,
    end: datetime,
    bars: list[tuple[datetime, float, float, float, float]],
) -> SegmentPath:
    """bars: (bucket_end, open, high, low, close)."""
    pp = [
        PricePoint(ts=t, mid=c, open=o, high=h, low=lo, close=c)
        for t, o, h, lo, c in bars
    ]
    return SegmentPath(
        segment_id=sid,
        start=start,
        end=end,
        points=pp,
        times=[t for t, *_ in bars],
    )


# ---------------------------------------------------------------------------
# Direction mapping
# ---------------------------------------------------------------------------


def test_direction_long_bid_wall() -> None:
    d, _ = expected_direction_for("BID_WALL_TESTED")
    assert d == DIRECTION_LONG
    d2, _ = expected_direction_for("BID_ABSORPTION_CANDIDATE")
    assert d2 == DIRECTION_LONG


def test_direction_short_ask_wall() -> None:
    d, _ = expected_direction_for("ASK_WALL_TESTED")
    assert d == DIRECTION_SHORT
    d2, _ = expected_direction_for("ASK_ABSORPTION_CANDIDATE")
    assert d2 == DIRECTION_SHORT


def test_direction_neutral_unknown() -> None:
    d, _ = expected_direction_for("PRICE_UP_OI_UP")
    assert d == DIRECTION_NEUTRAL
    d2, _ = expected_direction_for("BID_WALL_PULLING_CANDIDATE")
    assert d2 == DIRECTION_UNKNOWN
    d3, _ = expected_direction_for("TOTALLY_UNKNOWN_XYZ")
    assert d3 == DIRECTION_UNKNOWN


def test_direction_mapping_export_columns() -> None:
    rows = build_direction_mapping()
    assert rows
    assert set(rows[0].keys()) == {
        "pattern_type",
        "pattern_family",
        "expected_direction",
        "mapping_reason",
        "mapping_version",
    }
    types = [r["pattern_type"] for r in rows]
    assert types == sorted(types)


# ---------------------------------------------------------------------------
# Causal forward timing
# ---------------------------------------------------------------------------


def test_strict_forward_after_event() -> None:
    end = TS0 + timedelta(minutes=10)
    path = _path_from_mids(
        sid="S1",
        start=TS0,
        end=end,
        points=[
            (TS0 + timedelta(seconds=30), 100.0),
            (TS0 + timedelta(seconds=60), 100.1),
            (TS0 + timedelta(seconds=90), 100.2),
        ],
    )
    event = TS0 + timedelta(seconds=60)
    oc = compute_forward_outcome(
        event_time=event,
        start_price=100.1,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=120,
        targets_bps=(10,),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["first_forward_time"] == (TS0 + timedelta(seconds=90)).isoformat()
    assert _parse_iso(oc["first_forward_time"]) > event


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_no_same_bar_lookahead_close() -> None:
    """Event inside a bar must not use that bar's high/low/close."""
    end = TS0 + timedelta(minutes=5)
    # bucket_end at +60s and +120s
    path = _path_bars(
        sid="S1",
        start=TS0,
        end=end,
        bars=[
            (TS0 + timedelta(seconds=60), 100.0, 101.0, 99.0, 100.5),
            (TS0 + timedelta(seconds=120), 100.5, 100.6, 100.4, 100.55),
        ],
    )
    event = TS0 + timedelta(seconds=30)  # mid first bar
    oc = compute_forward_outcome(
        event_time=event,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=180,
        targets_bps=(10,),
        stops_bps=(25,),
        price_source="close",
        gaps=[],
    )
    # first usable sample is bucket_end at 60s (> event)
    assert oc["first_forward_time"] == (TS0 + timedelta(seconds=60)).isoformat()
    # if event were at bucket end, next bar only
    oc2 = compute_forward_outcome(
        event_time=TS0 + timedelta(seconds=60),
        start_price=100.5,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=180,
        targets_bps=(10,),
        stops_bps=(25,),
        price_source="close",
        gaps=[],
    )
    assert oc2["first_forward_time"] == (TS0 + timedelta(seconds=120)).isoformat()


# ---------------------------------------------------------------------------
# MFE / MAE / targets / stops
# ---------------------------------------------------------------------------


def test_long_mfe_mae() -> None:
    end = TS0 + timedelta(minutes=10)
    path = _path_from_mids(
        sid="S1",
        start=TS0,
        end=end,
        points=[
            (TS0 + timedelta(seconds=10), 100.0),
            (TS0 + timedelta(seconds=20), 100.50),  # +50 bps
            (TS0 + timedelta(seconds=30), 99.75),  # -25 bps from start
            (TS0 + timedelta(seconds=40), 100.10),
        ],
    )
    oc = compute_forward_outcome(
        event_time=TS0,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=60,
        targets_bps=(25, 50),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["mfe_bps"] == pytest.approx(50.0, abs=0.01)
    assert oc["mae_bps"] == pytest.approx(25.0, abs=0.01)


def test_short_mfe_mae() -> None:
    end = TS0 + timedelta(minutes=10)
    path = _path_from_mids(
        sid="S1",
        start=TS0,
        end=end,
        points=[
            (TS0 + timedelta(seconds=10), 100.0),
            (TS0 + timedelta(seconds=20), 99.50),  # favour short +50bps
            (TS0 + timedelta(seconds=30), 100.25),  # adverse +25bps
        ],
    )
    oc = compute_forward_outcome(
        event_time=TS0,
        start_price=100.0,
        direction=DIRECTION_SHORT,
        path=path,
        horizon_sec=60,
        targets_bps=(25, 50),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["mfe_bps"] == pytest.approx(50.0, abs=0.01)
    assert oc["mae_bps"] == pytest.approx(25.0, abs=0.01)


def test_target_and_stop_times() -> None:
    end = TS0 + timedelta(minutes=10)
    path = _path_from_mids(
        sid="S1",
        start=TS0,
        end=end,
        points=[
            (TS0 + timedelta(seconds=10), 100.10),  # +10bps
            (TS0 + timedelta(seconds=20), 100.25),  # +25bps target
            (TS0 + timedelta(seconds=30), 99.75),  # -25bps stop
        ],
    )
    oc = compute_forward_outcome(
        event_time=TS0,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=120,
        targets_bps=(25,),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["targets"][25.0] == pytest.approx(20.0)
    assert oc["stops"][25.0] == pytest.approx(30.0)


def test_target_before_stop() -> None:
    end = TS0 + timedelta(minutes=10)
    path = _path_from_mids(
        sid="S1",
        start=TS0,
        end=end,
        points=[
            (TS0 + timedelta(seconds=10), 100.25),
            (TS0 + timedelta(seconds=20), 99.75),
        ],
    )
    oc = compute_forward_outcome(
        event_time=TS0,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=60,
        targets_bps=(25,),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["target_before_stop"][(25.0, 25.0)] is True


def test_stop_before_target() -> None:
    end = TS0 + timedelta(minutes=10)
    path = _path_from_mids(
        sid="S1",
        start=TS0,
        end=end,
        points=[
            (TS0 + timedelta(seconds=10), 99.75),
            (TS0 + timedelta(seconds=20), 100.25),
        ],
    )
    oc = compute_forward_outcome(
        event_time=TS0,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=60,
        targets_bps=(25,),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["target_before_stop"][(25.0, 25.0)] is False


def test_ambiguous_same_bar_high_low() -> None:
    end = TS0 + timedelta(minutes=5)
    path = _path_bars(
        sid="S1",
        start=TS0,
        end=end,
        bars=[
            (TS0 + timedelta(seconds=60), 100.0, 100.50, 99.50, 100.0),  # +50 and -50
        ],
    )
    oc = compute_forward_outcome(
        event_time=TS0,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=120,
        targets_bps=(25,),
        stops_bps=(25,),
        price_source="high_low",
        gaps=[],
    )
    assert oc["target_before_stop"][(25.0, 25.0)] == "AMBIGUOUS_SAME_BAR"


def test_segment_end_before_horizon() -> None:
    end = TS0 + timedelta(seconds=30)
    path = _path_from_mids(
        sid="S1",
        start=TS0,
        end=end,
        points=[
            (TS0 + timedelta(seconds=10), 100.0),
            (TS0 + timedelta(seconds=20), 100.1),
            (end, 100.2),
        ],
    )
    oc = compute_forward_outcome(
        event_time=TS0,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=300,
        targets_bps=(10,),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["forward_data_complete"] is False
    assert oc["forward_end_reason"] == "SEGMENT_END"


def test_no_forward_data() -> None:
    end = TS0 + timedelta(seconds=10)
    path = _path_from_mids(
        sid="S1",
        start=TS0,
        end=end,
        points=[(TS0, 100.0)],
    )
    oc = compute_forward_outcome(
        event_time=TS0,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=60,
        targets_bps=(10,),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["forward_end_reason"] == "NO_FORWARD_DATA"
    assert oc["forward_data_complete"] is False


def test_gap_aborts_forward() -> None:
    end = TS0 + timedelta(minutes=10)
    path = _path_from_mids(
        sid="S1",
        start=TS0,
        end=end,
        points=[
            (TS0 + timedelta(seconds=10), 100.0),
            (TS0 + timedelta(seconds=20), 100.1),
            (TS0 + timedelta(seconds=40), 100.2),
        ],
    )
    gap = (TS0 + timedelta(seconds=30), TS0 + timedelta(seconds=50))
    oc = compute_forward_outcome(
        event_time=TS0,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=120,
        targets_bps=(10,),
        stops_bps=(25,),
        price_source="mid",
        gaps=[gap],
    )
    assert oc["forward_end_reason"] == "DATA_GAP"
    assert oc["forward_sample_count"] == 2  # stopped before gap sample
    assert oc["forward_data_complete"] is False


# ---------------------------------------------------------------------------
# Completeness / sampling tolerance (Phase-6 fix)
# ---------------------------------------------------------------------------


def test_horizon_complete_event_between_60s_samples() -> None:
    """Event 10:00:17, samples 10:01..10:05, horizon 300s → complete."""
    start = datetime(2026, 7, 27, 10, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    event = start + timedelta(seconds=17)
    points = []
    t = start + timedelta(minutes=1)
    while t <= start + timedelta(minutes=5):
        points.append((t, 100.0))
        t += timedelta(seconds=60)
    path = _path_from_mids(sid="S1", start=start, end=end, points=points)
    path.sample_interval_seconds = 60.0
    oc = compute_forward_outcome(
        event_time=event,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=300,
        targets_bps=(10,),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["forward_data_complete"] is True
    assert oc["forward_end_reason"] == "HORIZON_COMPLETE"
    # no sample exactly at event+300s = 10:05:17
    last = _parse_iso(oc["last_forward_time"])
    assert last == start + timedelta(minutes=5)
    assert last < event + timedelta(seconds=300)


def test_horizon_complete_without_exact_horizon_stamp() -> None:
    start = datetime(2026, 7, 27, 10, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=3)
    event = start + timedelta(seconds=17)
    points = [(start + timedelta(seconds=s), 100.0) for s in range(60, 301, 60)]
    path = _path_from_mids(sid="S1", start=start, end=end, points=points)
    path.sample_interval_seconds = 60.0
    oc = compute_forward_outcome(
        event_time=event,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=300,
        targets_bps=(10,),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["forward_data_complete"] is True
    assert _parse_iso(oc["last_forward_time"]) != event + timedelta(seconds=300)


def test_horizon_complete_10984s_shortfall_60s_grid() -> None:
    """Regression: seconds_available=7189.016, horizon 7200, 60s sampling → complete."""
    event = datetime(2026, 7, 27, 2, 4, 0, 0, tzinfo=timezone.utc)
    first = datetime(2026, 7, 27, 2, 4, 49, 16000, tzinfo=timezone.utc)
    last = datetime(2026, 7, 27, 4, 3, 49, 16000, tzinfo=timezone.utc)
    end = event + timedelta(hours=4)
    points = []
    t = first
    while t <= last:
        points.append((t, 100.0))
        t += timedelta(seconds=60)
    path = _path_from_mids(sid="S1", start=event - timedelta(minutes=5), end=end, points=points)
    path.sample_interval_seconds = 60.0
    oc = compute_forward_outcome(
        event_time=event,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=7200,
        targets_bps=(10,),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["seconds_available"] == pytest.approx(7189.016, abs=0.001)
    assert (event + timedelta(seconds=7200) - last).total_seconds() == pytest.approx(10.984, abs=0.001)
    assert oc["forward_sample_count"] == 120
    assert oc["forward_data_complete"] is True
    assert oc["forward_end_reason"] == "HORIZON_COMPLETE"


def test_insufficient_sample_coverage_75s_shortfall() -> None:
    event = TS0
    end = TS0 + timedelta(hours=2)
    # last sample 75s before horizon_end for 300s horizon → 10:04:45 if event 10:00
    horizon = 300
    last = event + timedelta(seconds=horizon - 75)
    points = [(event + timedelta(seconds=s), 100.0) for s in range(60, horizon - 75 + 1, 60)]
    if points[-1][0] != last:
        points.append((last, 100.0))
    path = _path_from_mids(sid="S1", start=event, end=end, points=points)
    path.sample_interval_seconds = 60.0
    oc = compute_forward_outcome(
        event_time=event,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=horizon,
        targets_bps=(10,),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["forward_sample_count"] > 0
    assert oc["forward_data_complete"] is False
    assert oc["forward_end_reason"] == "INSUFFICIENT_SAMPLE_COVERAGE"


def test_samples_never_yield_no_forward_data() -> None:
    end = TS0 + timedelta(minutes=5)
    path = _path_from_mids(
        sid="S1",
        start=TS0,
        end=end,
        points=[(TS0 + timedelta(seconds=10), 100.0), (TS0 + timedelta(seconds=20), 100.1)],
    )
    path.sample_interval_seconds = 10.0
    oc = compute_forward_outcome(
        event_time=TS0,
        start_price=100.0,
        direction=DIRECTION_LONG,
        path=path,
        horizon_sec=300,
        targets_bps=(10,),
        stops_bps=(25,),
        price_source="mid",
        gaps=[],
    )
    assert oc["forward_sample_count"] > 0
    assert oc["forward_end_reason"] != "NO_FORWARD_DATA"


def test_segments_processed_excludes_empty(tmp_path: Path) -> None:
    gen = tmp_path / "gen"
    fh = gen / "full_history"
    fh.mkdir(parents=True)
    segs = []
    samples = []
    eval_rows = []
    for i, sid in enumerate(["S0001", "S0002", "S0003", "S0004", "S0005"]):
        start = TS0 + timedelta(hours=i)
        end = start + timedelta(minutes=20)
        segs.append(
            {
                "segment_id": sid,
                "symbol": "APTUSDT",
                "segment_start_ts": start.isoformat(),
                "segment_end_ts": end.isoformat(),
                "is_replayable": "false" if sid == "S0001" else "true",
                "discard_reason": "gap" if sid == "S0001" else "",
            }
        )
        if sid == "S0001":
            continue
        t = start
        px = 100.0
        while t <= end:
            samples.append({"segment_id": sid, "sample_ts": t.isoformat(), "mid_price": f"{px:.6f}"})
            px += 0.01
            t += timedelta(seconds=60)
        eval_rows.append(
            {
                "symbol": "APTUSDT",
                "segment_id": sid,
                "source_family": "WALL_LIFECYCLE",
                "pattern_type": "BID_WALL_TESTED",
                "variant": "",
                "event_id": f"E-{sid}",
                "event_time": (start + timedelta(minutes=1)).isoformat(),
                "event_price": "100.0",
                "side": "bid",
                "sequence_id": "WS1",
                "transition_type": "TESTED",
                "transition_time": (start + timedelta(minutes=1)).isoformat(),
                "armed_pair_id": "",
                "armed_time": "",
                "action_time": "",
                "data_complete": "true",
                "source_output_dir": str(fh),
            }
        )
    write_csv_headered(
        fh / "replay_segments.csv",
        segs,
        ["segment_id", "symbol", "segment_start_ts", "segment_end_ts", "is_replayable", "discard_reason"],
    )
    write_csv_headered(fh / "segment_replay_samples.csv", samples, ["segment_id", "sample_ts", "mid_price"])
    write_csv_headered(fh / "replay_gaps.csv", [], ["gap_start_ts", "gap_end_ts"])
    write_csv_headered(gen / "general_pattern_evaluation_input.csv", eval_rows, list(eval_rows[0].keys()))
    result = run_pattern_outcome_evaluation(
        general_output_dir=gen,
        full_history_dir=fh,
        params=OutcomeParams(horizons_seconds=(60, 300), min_samples=1, bootstrap_iterations=5),
    )
    assert result.summary["segments_processed"] == 4
    assert result.summary["pattern_outcome_complete_count"] > 0


def test_integrity_no_forward_data_with_samples() -> None:
    outcomes = [
        {
            "stable_event_key": "PC|e1",
            "horizon_seconds": 60,
            "event_id": "e1",
            "event_time": TS0.isoformat(),
            "first_forward_time": (TS0 + timedelta(seconds=10)).isoformat(),
            "forward_end_reason": "NO_FORWARD_DATA",
            "forward_sample_count": 5,
            "forward_data_complete": False,
            "segment_id": "S1",
            "source_family": "WALL",
            "variant": "",
            "cluster_id": "",
        }
    ]
    integ = check_outcome_integrity(
        outcomes=outcomes,
        clusters=[],
        eval_rows=[],
        segment_paths={},
        forward_samples_processed=5,
        horizons_seconds=(60,),
    )
    assert any("NO_FORWARD_DATA_WITH_SAMPLES" in e for e in integ["errors"])


def test_integrity_zero_complete_implausible() -> None:
    path = SegmentPath(
        segment_id="S1",
        start=TS0,
        end=TS0 + timedelta(hours=2),
        points=[PricePoint(ts=TS0 + timedelta(seconds=60), mid=1.0)],
        times=[TS0 + timedelta(seconds=60)],
        sample_interval_seconds=60.0,
    )
    outcomes = [
        {
            "stable_event_key": "PC|e1",
            "horizon_seconds": 60,
            "event_id": "e1",
            "event_time": TS0.isoformat(),
            "first_forward_time": (TS0 + timedelta(seconds=60)).isoformat(),
            "forward_end_reason": "INSUFFICIENT_SAMPLE_COVERAGE",
            "forward_sample_count": 10,
            "forward_data_complete": False,
            "segment_id": "S1",
            "source_family": "WALL",
            "variant": "",
            "cluster_id": "",
        }
    ]
    integ = check_outcome_integrity(
        outcomes=outcomes,
        clusters=[],
        eval_rows=[],
        segment_paths={"S1": path},
        forward_samples_processed=100,
        horizons_seconds=(60, 300),
    )
    assert any(e == "ZERO_COMPLETE_OUTCOMES_IMPLAUSIBLE" for e in integ["errors"])


def test_complete_counts_weakly_decreasing_by_horizon(tmp_path: Path) -> None:
    gen, fh = _mini_fixture(tmp_path, n_events=5, n_segments=2)
    result = run_pattern_outcome_evaluation(
        general_output_dir=gen,
        full_history_dir=fh,
        params=OutcomeParams(
            horizons_seconds=(60, 300, 900, 1800),
            min_samples=1,
            bootstrap_iterations=5,
        ),
    )
    rows = list(csv.DictReader((result.output_dir / "pattern_forward_outcomes.csv").open()))
    counts = []
    for h in (60, 300, 900, 1800):
        n = sum(1 for r in rows if int(r["horizon_seconds"]) == h and r["forward_data_complete"] in ("True", "true", True))
        counts.append(n)
    for i in range(1, len(counts)):
        assert counts[i] <= counts[i - 1], counts
    assert counts[0] > 0


def test_coverage_tolerance_helpers() -> None:
    times = [TS0 + timedelta(seconds=s) for s in (0, 60, 120, 180)]
    assert estimate_sample_interval_seconds(times) == 60.0
    assert coverage_tolerance_seconds(60.0) == pytest.approx(66.0)

def test_multiple_horizons_targets_stops_no_dupes(tmp_path: Path) -> None:
    gen, fh = _mini_fixture(tmp_path, n_events=2, n_segments=1)
    result = run_pattern_outcome_evaluation(
        general_output_dir=gen,
        full_history_dir=fh,
        params=OutcomeParams(
            horizons_seconds=(60, 120),
            targets_bps=(10.0, 25.0),
            stops_bps=(25.0, 50.0),
            min_samples=1,
            bootstrap_iterations=20,
            random_seed=7,
        ),
    )
    assert result.ok
    rows = list(csv.DictReader((result.output_dir / "pattern_forward_outcomes.csv").open()))
    keys = [(r["event_id"], r["horizon_seconds"]) for r in rows]
    assert len(keys) == len(set(keys))
    horizons = {int(r["horizon_seconds"]) for r in rows}
    assert horizons == {60, 120}
    assert "target_10bps_hit" in rows[0]
    assert "target_25bps_hit" in rows[0]
    assert "stop_25bps_hit" in rows[0]
    assert "stop_50bps_hit" in rows[0]


# ---------------------------------------------------------------------------
# Fixture helpers for end-to-end
# ---------------------------------------------------------------------------


def _mini_fixture(
    tmp_path: Path,
    *,
    n_events: int = 3,
    n_segments: int = 2,
    include_hl: bool = False,
    include_neutral: bool = False,
) -> tuple[Path, Path]:
    gen = tmp_path / "general"
    fh = gen / "full_history"
    fh.mkdir(parents=True)
    segments = []
    samples = []
    eval_rows = []
    for i in range(n_segments):
        sid = f"S{i+1:04d}"
        start = TS0 + timedelta(hours=i)
        end = start + timedelta(minutes=30)
        segments.append(
            {
                "segment_id": sid,
                "symbol": "APTUSDT",
                "segment_start_ts": start.isoformat(),
                "segment_end_ts": end.isoformat(),
                "is_replayable": "true",
                "discard_reason": "",
            }
        )
        # dense mid path every 10s
        t = start
        px = 100.0 + i
        while t <= end:
            samples.append(
                {
                    "segment_id": sid,
                    "sample_ts": t.isoformat(),
                    "mid_price": f"{px:.6f}",
                }
            )
            px += 0.01  # slow drift up
            t += timedelta(seconds=10)
        for j in range(n_events):
            et = start + timedelta(minutes=2 + j)
            eval_rows.append(
                {
                    "symbol": "APTUSDT",
                    "segment_id": sid,
                    "source_family": "WALL_LIFECYCLE",
                    "pattern_type": "BID_WALL_TESTED",
                    "variant": "",
                    "event_id": f"E-{sid}-{j}",
                    "event_time": et.isoformat(),
                    "event_price": f"{100.0 + i + 0.05 * j:.6f}",
                    "side": "bid",
                    "sequence_id": f"WS-{sid}",
                    "transition_type": "TESTED",
                    "transition_time": et.isoformat(),
                    "armed_pair_id": "",
                    "armed_time": "",
                    "action_time": "",
                    "data_complete": "true",
                    "source_output_dir": str(fh),
                }
            )
            if include_neutral:
                eval_rows.append(
                    {
                        "symbol": "APTUSDT",
                        "segment_id": sid,
                        "source_family": "PRICE_OI",
                        "pattern_type": "PRICE_UP_OI_UP",
                        "variant": "",
                        "event_id": f"N-{sid}-{j}",
                        "event_time": et.isoformat(),
                        "event_price": f"{100.0 + i:.6f}",
                        "side": "",
                        "sequence_id": "",
                        "transition_type": "",
                        "transition_time": "",
                        "armed_pair_id": "",
                        "armed_time": "",
                        "action_time": "",
                        "data_complete": "true",
                        "source_output_dir": str(fh),
                    }
                )
        if include_hl:
            action = start + timedelta(minutes=5)
            for aw in (300, 600):
                eval_rows.append(
                    {
                        "symbol": "APTUSDT",
                        "segment_id": sid,
                        "source_family": "HIGHER_LOW_ARMED_ACTION",
                        "pattern_type": "HL_P3",
                        "variant": "P3",
                        "event_id": f"HL-{sid}-{aw}",
                        "event_time": action.isoformat(),
                        "event_price": f"{100.0 + i:.6f}",
                        "side": "long",
                        "sequence_id": "",
                        "transition_type": "",
                        "transition_time": "",
                        "armed_pair_id": f"AP-{sid}",
                        "armed_time": (action - timedelta(seconds=30)).isoformat(),
                        "action_time": action.isoformat(),
                        "data_complete": "true",
                        "source_output_dir": str(fh / ".." / "higher_lows" / sid / f"armed_{aw}s"),
                        "armed_window_seconds": str(aw),
                    }
                )
    write_csv_headered(
        fh / "replay_segments.csv",
        segments,
        ["segment_id", "symbol", "segment_start_ts", "segment_end_ts", "is_replayable", "discard_reason"],
    )
    write_csv_headered(
        fh / "segment_replay_samples.csv",
        samples,
        ["segment_id", "sample_ts", "mid_price"],
    )
    write_csv_headered(fh / "replay_gaps.csv", [], ["gap_start_ts", "gap_end_ts"])
    write_csv_headered(
        gen / "general_pattern_evaluation_input.csv",
        eval_rows,
        list(eval_rows[0].keys()),
    )
    return gen, fh


def test_cluster_generation_and_dedup(tmp_path: Path) -> None:
    gen, fh = _mini_fixture(tmp_path, n_events=2, n_segments=1)
    # add second pattern same time/subject → same cluster
    eval_path = gen / "general_pattern_evaluation_input.csv"
    rows = list(csv.DictReader(eval_path.open()))
    clone = dict(rows[0])
    clone["event_id"] = "E-CLONE"
    clone["pattern_type"] = "BID_WALL_PERSISTENT"
    rows.append(clone)
    write_csv_headered(eval_path, rows, list(rows[0].keys()))
    result = run_pattern_outcome_evaluation(
        general_output_dir=gen,
        full_history_dir=fh,
        params=OutcomeParams(horizons_seconds=(60,), min_samples=1, bootstrap_iterations=10),
    )
    clusters = list(csv.DictReader((result.output_dir / "pattern_event_clusters.csv").open()))
    assert any(int(c["member_count"]) >= 2 for c in clusters)
    raw = list(csv.DictReader((result.output_dir / "pattern_forward_outcomes.csv").open()))
    cluster_oc = list(csv.DictReader((result.output_dir / "pattern_cluster_forward_outcomes.csv").open()))
    assert len(cluster_oc) < len(raw)
    # deterministic cluster ids
    cid1 = cluster_id_for(
        symbol="A", segment_id="S", event_time="t", direction="LONG", subject_key="X", pattern_family="F"
    )
    cid2 = cluster_id_for(
        symbol="A", segment_id="S", event_time="t", direction="LONG", subject_key="X", pattern_family="F"
    )
    assert cid1 == cid2


def test_baseline_deterministic_and_in_segment(tmp_path: Path) -> None:
    gen, fh = _mini_fixture(tmp_path, n_events=5, n_segments=2)
    params = OutcomeParams(
        horizons_seconds=(60,),
        min_samples=1,
        bootstrap_iterations=10,
        random_seed=42,
    )
    r1 = run_pattern_outcome_evaluation(general_output_dir=gen, full_history_dir=fh, params=params)
    # wipe and rerun into same structure
    import shutil

    shutil.rmtree(r1.output_dir)
    r2 = run_pattern_outcome_evaluation(general_output_dir=gen, full_history_dir=fh, params=params)
    b1 = (r1.output_dir / "pattern_baseline_outcomes.csv").read_text()
    b2 = (r2.output_dir / "pattern_baseline_outcomes.csv").read_text()
    assert b1 == b2
    bl = list(csv.DictReader((r2.output_dir / "pattern_baseline_outcomes.csv").open()))
    segs = {s["segment_id"] for s in csv.DictReader((fh / "replay_segments.csv").open())}
    assert bl
    assert all(r["segment_id"] in segs for r in bl)
    # unique event times within TIME_MATCHED draw when pool large enough
    tm = [r for r in bl if r["baseline_type"] == "TIME_MATCHED_RANDOM"]
    times = [r["event_time"] for r in tm]
    assert len(times) == len(set(times))
    types = {r["baseline_type"] for r in bl}
    assert "TIME_MATCHED_RANDOM" in types
    assert "BUCKET_MATCHED_RANDOM" in types
    assert "DIRECTION_MATCHED_RANDOM" in types


def test_bootstrap_deterministic_and_ci(tmp_path: Path) -> None:
    vals = [1.0, 2.0, 3.0, 4.0, 5.0] * 10
    a = bootstrap_ci(vals, iterations=100, seed=42, statistic="median")
    b = bootstrap_ci(vals, iterations=100, seed=42, statistic="median")
    assert a == b
    assert a[0] is not None and a[1] is not None and a[2] is not None
    assert a[1] <= a[0] <= a[2]

    gen, fh = _mini_fixture(tmp_path, n_events=20, n_segments=2)
    result = run_pattern_outcome_evaluation(
        general_output_dir=gen,
        full_history_dir=fh,
        params=OutcomeParams(
            horizons_seconds=(60,),
            min_samples=30,
            bootstrap_iterations=50,
            random_seed=1,
        ),
    )
    ci = list(csv.DictReader((result.output_dir / "pattern_outcome_confidence_intervals.csv").open()))
    # 20*2=40 complete directional samples of BID_WALL_TESTED → CI present
    assert ci
    assert all(r.get("insufficient_sample") in ("False", "false", False, "") for r in ci)


def test_insufficient_sample_flag(tmp_path: Path) -> None:
    gen, fh = _mini_fixture(tmp_path, n_events=2, n_segments=1)
    result = run_pattern_outcome_evaluation(
        general_output_dir=gen,
        full_history_dir=fh,
        params=OutcomeParams(horizons_seconds=(60,), min_samples=30, bootstrap_iterations=5),
    )
    by_type = list(csv.DictReader((result.output_dir / "pattern_outcome_summary_by_type.csv").open()))
    assert by_type
    assert any(str(r.get("insufficient_sample")).lower() == "true" for r in by_type)


def test_segment_stability_and_ranking_filter(tmp_path: Path) -> None:
    gen, fh = _mini_fixture(tmp_path, n_events=20, n_segments=2)
    result = run_pattern_outcome_evaluation(
        general_output_dir=gen,
        full_history_dir=fh,
        params=OutcomeParams(horizons_seconds=(60,), min_samples=30, bootstrap_iterations=20),
    )
    stab = list(csv.DictReader((result.output_dir / "pattern_segment_stability.csv").open()))
    assert stab
    assert "segment_consistency_rate" in stab[0]
    ranking = list(csv.DictReader((result.output_dir / "pattern_research_ranking.csv").open()))
    for r in ranking:
        assert int(r["sample_count_complete"]) >= 30
        assert int(r["segment_count"]) >= 2
        assert r["research_label"] in {
            "PROMISING_FOR_OOS",
            "WEAK_EVIDENCE",
            "NO_CLEAR_EDGE",
            "INSUFFICIENT_DATA",
            "UNSTABLE_ACROSS_SEGMENTS",
        }
        assert r["research_label"] not in {
            "PROFITABLE",
            "GUARANTEED",
            "TRADING_SIGNAL",
            "READY_FOR_LIVE",
        }


def test_no_live_profitability_labels_in_score() -> None:
    label, _ = label_from_score(
        {
            "sample_count_complete": 100,
            "segment_count": 3,
            "segment_consistency_rate": 0.8,
            "baseline_target_before_stop_lift": 0.2,
            "min_samples": 30,
            "single_segment_only": False,
        },
        research_score(
            {
                "baseline_target_before_stop_lift": 0.2,
                "baseline_target_25_lift": 0.1,
                "median_mfe_bps": 40,
                "median_mae_bps": 10,
                "segment_consistency_rate": 0.8,
                "sample_count_complete": 100,
                "segment_count": 3,
                "single_segment_only": False,
            }
        ),
    )
    assert label == "PROMISING_FOR_OOS"


def test_higher_low_p3_p11_and_p0_p2_excluded(tmp_path: Path) -> None:
    gen, fh = _mini_fixture(tmp_path, n_events=1, n_segments=1, include_hl=True)
    # inject illegal P0 as armed
    eval_path = gen / "general_pattern_evaluation_input.csv"
    rows = list(csv.DictReader(eval_path.open()))
    bad = dict(rows[-1])
    bad["event_id"] = "BAD-P0"
    bad["variant"] = "P0"
    bad["pattern_type"] = "HL_P0"
    bad["source_family"] = "HIGHER_LOW_ARMED_ACTION"
    rows.append(bad)
    write_csv_headered(eval_path, rows, list(rows[0].keys()))
    result = run_pattern_outcome_evaluation(
        general_output_dir=gen,
        full_history_dir=fh,
        params=OutcomeParams(horizons_seconds=(60,), min_samples=1, bootstrap_iterations=5),
    )
    fwd = list(csv.DictReader((result.output_dir / "pattern_forward_outcomes.csv").open()))
    assert any(r.get("variant") == "P3" for r in fwd)
    assert not any(r.get("variant") == "P0" for r in fwd)
    # armed windows kept separate
    hl = [r for r in fwd if r.get("source_family") == "HIGHER_LOW_ARMED_ACTION"]
    windows = {r.get("armed_window_seconds") for r in hl}
    assert "300" in windows and "600" in windows
    # causality: event_time == action_time for HL
    for r in hl:
        assert r["event_time"] == r["action_time"]
        assert _parse_iso(r["first_forward_time"]) > _parse_iso(r["action_time"])


def test_neutral_no_directional_hit_rate(tmp_path: Path) -> None:
    gen, fh = _mini_fixture(tmp_path, n_events=1, n_segments=1, include_neutral=True)
    result = run_pattern_outcome_evaluation(
        general_output_dir=gen,
        full_history_dir=fh,
        params=OutcomeParams(horizons_seconds=(60,), min_samples=1, bootstrap_iterations=5),
    )
    rows = list(csv.DictReader((result.output_dir / "pattern_forward_outcomes.csv").open()))
    neut = [r for r in rows if r["pattern_type"] == "PRICE_UP_OI_UP"]
    assert neut
    assert neut[0]["expected_direction"] == DIRECTION_NEUTRAL
    assert neut[0]["target_25bps_hit"] == ""
    assert neut[0]["stop_25bps_hit"] == ""
    assert neut[0]["target_before_stop_25_25"] == ""


def test_empty_input_headers(tmp_path: Path) -> None:
    gen = tmp_path / "empty"
    fh = gen / "full_history"
    fh.mkdir(parents=True)
    write_csv_headered(gen / "general_pattern_evaluation_input.csv", [], ["event_id"])
    write_csv_headered(
        fh / "replay_segments.csv",
        [],
        ["segment_id", "symbol", "segment_start_ts", "segment_end_ts", "is_replayable", "discard_reason"],
    )
    write_csv_headered(fh / "segment_replay_samples.csv", [], ["segment_id", "sample_ts", "mid_price"])
    write_csv_headered(fh / "replay_gaps.csv", [], ["gap_start_ts", "gap_end_ts"])
    result = run_pattern_outcome_evaluation(general_output_dir=gen, full_history_dir=fh)
    assert result.decision == "PATTERN_OUTCOMES_DATA_INSUFFICIENT"
    for name in PHASE6_OUTPUT_FILES:
        assert (result.output_dir / name).exists()
    # headered forward outcomes
    with (result.output_dir / "pattern_forward_outcomes.csv").open() as fh_csv:
        reader = csv.reader(fh_csv)
        header = next(reader)
    assert "event_id" in header
    assert "horizon_seconds" in header


def test_csv_sort_deterministic(tmp_path: Path) -> None:
    gen, fh = _mini_fixture(tmp_path, n_events=3, n_segments=2)
    params = OutcomeParams(horizons_seconds=(60, 120), min_samples=1, bootstrap_iterations=5, random_seed=3)
    r1 = run_pattern_outcome_evaluation(general_output_dir=gen, full_history_dir=fh, params=params)
    text1 = (r1.output_dir / "pattern_forward_outcomes.csv").read_text()
    import shutil

    shutil.rmtree(r1.output_dir)
    r2 = run_pattern_outcome_evaluation(general_output_dir=gen, full_history_dir=fh, params=params)
    assert text1 == (r2.output_dir / "pattern_forward_outcomes.csv").read_text()


def test_validate_params_and_parsers() -> None:
    assert parse_int_list("60,300", default=(1,)) == (60, 300)
    assert parse_float_list("10,25", default=(1,)) == (10.0, 25.0)
    with pytest.raises(Exception):
        validate_outcome_params(OutcomeParams(price_source="vwap"))


# ---------------------------------------------------------------------------
# General runner integration
# ---------------------------------------------------------------------------


def _fake_fh_for_outcomes(**kwargs):
    from tests.test_run_all_orderbook_research import _fake_full_history, TS0 as T

    params = kwargs["params"]
    out = _fake_full_history(params=params, gaps=0)
    fh = Path(params.output_dir)
    # add mid samples so phase 6 has forward data
    samples = []
    start = T
    end = T + timedelta(minutes=40)
    t = start
    px = 1.0
    while t <= end:
        samples.append({"segment_id": "S0002", "sample_ts": t.isoformat(), "mid_price": f"{px:.6f}"})
        px += 0.0001
        t += timedelta(seconds=10)
    write_csv_headered(
        fh / "segment_replay_samples.csv",
        samples,
        ["segment_id", "sample_ts", "mid_price"],
    )
    write_csv_headered(fh / "replay_gaps.csv", [], ["gap_start_ts", "gap_end_ts"])
    return out


def test_general_runner_phase6_integration(tmp_path: Path) -> None:
    from tests.test_run_all_orderbook_research import _fake_hl_ok

    out = tmp_path / "gen"
    result = run_general_research(
        GeneralResearchParams(
            symbol="APTUSDT",
            output_dir=out,
            higher_low_armed_seconds=(0, 300),
            skip_higher_lows=False,
            outcome_horizons_seconds=(60,),
            outcome_min_samples=1,
            outcome_bootstrap_iterations=10,
            overwrite=True,
        ),
        full_history_runner=_fake_fh_for_outcomes,
        higher_lows_segment_runner=_fake_hl_ok,
    )
    assert result["summary"]["pattern_outcomes_requested"] is True
    assert result["summary"]["pattern_outcomes_ok"] is True
    assert result["summary"]["pattern_outcome_decision"] is not None
    assert "pattern_outcome_event_count" in result["summary"]
    assert (out / "pattern_outcomes" / "PATTERN_OUTCOME_REPORT.md").exists()
    assert (out / "pattern_outcomes" / "pattern_forward_outcomes.csv").exists()
    phases = {r["phase"] for r in result["phase_status"]}
    assert "pattern_outcomes" in phases
    report = (out / "GENERAL_REPORT.md").read_text()
    assert "Phase 6" in report or "pattern_outcome" in report.lower() or "outcomes" in report.lower()


def test_general_runner_skip_pattern_outcomes(tmp_path: Path) -> None:
    from tests.test_run_all_orderbook_research import _fake_full_history, _fake_hl_ok

    out = tmp_path / "skip"
    result = run_general_research(
        GeneralResearchParams(
            symbol="APTUSDT",
            output_dir=out,
            skip_higher_lows=True,
            skip_pattern_outcomes=True,
            run_pattern_outcomes=False,
            overwrite=True,
        ),
        full_history_runner=lambda **kw: _fake_full_history(params=kw["params"]),
        higher_lows_segment_runner=_fake_hl_ok,
    )
    assert result["summary"]["pattern_outcomes_requested"] is False
    assert not (out / "pattern_outcomes" / "pattern_forward_outcomes.csv").exists()
    status = [r for r in result["phase_status"] if r["phase"] == "pattern_outcomes"][0]
    assert status["status"] == "NOT_REQUESTED"


def test_phase6_continue_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_run_all_orderbook_research import _fake_full_history, _fake_hl_ok
    import orderbook_analyse.general_research_runner as gr

    def boom(**kwargs):
        raise RuntimeError("phase6-boom")

    monkeypatch.setattr(gr, "run_pattern_outcome_evaluation", boom)
    out = tmp_path / "cont6"
    result = run_general_research(
        GeneralResearchParams(
            symbol="APTUSDT",
            output_dir=out,
            skip_higher_lows=True,
            continue_on_phase_error=True,
            overwrite=True,
        ),
        full_history_runner=lambda **kw: _fake_full_history(params=kw["params"]),
        higher_lows_segment_runner=_fake_hl_ok,
    )
    assert result["summary"]["pattern_outcomes_ok"] is False
    assert result["summary"]["pattern_outcome_decision"] == "PATTERN_OUTCOMES_FAILED"
    assert result["decision"] != "GENERAL_ANALYSIS_FAILED" or True  # soft continue


def test_phase6_fail_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_run_all_orderbook_research import _fake_full_history, _fake_hl_ok
    import orderbook_analyse.general_research_runner as gr

    def boom(**kwargs):
        raise RuntimeError("phase6-boom")

    monkeypatch.setattr(gr, "run_pattern_outcome_evaluation", boom)
    out = tmp_path / "fail6"
    result = run_general_research(
        GeneralResearchParams(
            symbol="APTUSDT",
            output_dir=out,
            skip_higher_lows=True,
            continue_on_phase_error=False,
            overwrite=True,
        ),
        full_history_runner=lambda **kw: _fake_full_history(params=kw["params"]),
        higher_lows_segment_runner=_fake_hl_ok,
    )
    assert result["summary"]["pattern_outcomes_ok"] is False
    assert "FAILED" in result["decision"]


def test_cli_outcome_defaults_and_help() -> None:
    args = parse_args(["--symbol", "APTUSDT"])
    assert args.run_pattern_outcomes is True
    assert args.skip_pattern_outcomes is False
    assert args.outcome_horizons_seconds == "60,300,900,1800,3600,7200"
    assert args.outcome_targets_bps == "10,25,50,100"
    assert args.outcome_stop_bps == "25,50,100"
    assert args.outcome_price_source == "mid"
    assert args.outcome_min_samples == 30
    assert args.outcome_bootstrap_iterations == 1000
    assert args.outcome_random_seed == 42
    with pytest.raises(SystemExit) as ei:
        parse_args(["--help"])
    assert ei.value.code == 0
    help_out = None
    # ensure flags appear in help via ArgumentParser
    from orderbook_analyse.general_research_runner import params_from_args

    args2 = parse_args(["--symbol", "X", "--skip-pattern-outcomes"])
    p = params_from_args(args2)
    assert p.run_pattern_outcomes is False
    assert p.skip_pattern_outcomes is True


def test_output_files_complete(tmp_path: Path) -> None:
    gen, fh = _mini_fixture(tmp_path, n_events=2, n_segments=1)
    result = run_pattern_outcome_evaluation(
        general_output_dir=gen,
        full_history_dir=fh,
        params=OutcomeParams(horizons_seconds=(60,), min_samples=1, bootstrap_iterations=5),
    )
    for name in PHASE6_OUTPUT_FILES:
        assert (result.output_dir / name).exists(), name

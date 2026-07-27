"""Tests for the central general research runner (mocked I/O, no ClickHouse)."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from orderbook_analyse.general_research_runner import (
    EVAL_INPUT_HEADERS,
    GeneralResearchError,
    GeneralResearchParams,
    build_higher_low_eval_rows,
    build_pattern_eval_rows,
    check_general_integrity,
    decide_general,
    parse_args,
    parse_armed_seconds,
    prepare_output_dir,
    read_csv_rows,
    run_general_research,
    select_replayable_segments,
    write_csv_headered,
)

TS0 = datetime(2026, 7, 26, 10, 0, 0, tzinfo=timezone.utc)


def _write_segments(path: Path, rows: list[dict[str, Any]]) -> None:
    headers = [
        "segment_id",
        "symbol",
        "segment_start_ts",
        "segment_end_ts",
        "is_replayable",
        "discard_reason",
    ]
    write_csv_headered(path, rows, headers)


def _write_replay_results(path: Path, rows: list[dict[str, Any]]) -> None:
    headers = ["segment_id", "replay_status", "error_message"]
    write_csv_headered(path, rows, headers)


def _fake_full_history(*, params, segments=None, patterns=None, gaps=0):
    out = Path(params.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    segs = segments or [
        {
            "segment_id": "S0001",
            "symbol": params.symbol,
            "segment_start_ts": TS0.isoformat(),
            "segment_end_ts": (TS0 + timedelta(minutes=5)).isoformat(),
            "is_replayable": False,
            "discard_reason": "gap",
        },
        {
            "segment_id": "S0002",
            "symbol": params.symbol,
            "segment_start_ts": (TS0 + timedelta(minutes=10)).isoformat(),
            "segment_end_ts": (TS0 + timedelta(minutes=40)).isoformat(),
            "is_replayable": True,
            "discard_reason": "",
        },
    ]
    _write_segments(out / "replay_segments.csv", segs)
    results = []
    for s in segs:
        if str(s["is_replayable"]).lower() in {"true", "1"} or s["is_replayable"] is True:
            results.append({"segment_id": s["segment_id"], "replay_status": "REPLAY_OK", "error_message": ""})
        else:
            results.append(
                {
                    "segment_id": s["segment_id"],
                    "replay_status": "SKIPPED_NOT_REPLAYABLE",
                    "error_message": s.get("discard_reason") or "gap",
                }
            )
    _write_replay_results(out / "segment_replay_results.csv", results)
    pats = patterns or [
        {
            "symbol": params.symbol,
            "segment_id": "S0002",
            "pattern_id": f"{params.symbol}:S0002:1m:BID_WALL_TESTED:20260726T103000:WS1",
            "pattern_ts": (TS0 + timedelta(minutes=30)).isoformat(),
            "pattern_type": "BID_WALL_TESTED",
            "pattern_family": "WALL_LIFECYCLE",
            "pattern_side": "bid",
            "close_price": "1.0",
            "wall_price": "0.99",
            "source_wall_sequence_id": "WS1",
            "source_transition_type": "TESTED",
            "source_transition_ts": (TS0 + timedelta(minutes=29, seconds=50)).isoformat(),
            "data_complete": True,
        }
    ]
    write_csv_headered(
        out / "pattern_candidates.csv",
        pats,
        [
            "symbol",
            "segment_id",
            "pattern_id",
            "pattern_ts",
            "pattern_type",
            "pattern_family",
            "pattern_side",
            "close_price",
            "wall_price",
            "source_wall_sequence_id",
            "source_transition_type",
            "source_transition_ts",
            "data_complete",
        ],
    )
    # empty companion files
    for name in ("replay_gaps.csv", "data_inventory.csv"):
        write_csv_headered(out / name, [], ["x"])
    return {
        "decision": "FULL_HISTORY_ANALYSIS_COMPLETE_WITH_GAPS" if gaps else "FULL_HISTORY_ANALYSIS_COMPLETE",
        "output_dir": str(out),
        "summary": {
            "analysis_start": TS0.isoformat(),
            "analysis_end": (TS0 + timedelta(hours=1)).isoformat(),
            "coverage_pct": 90.0,
            "gap_count": gaps,
            "segment_count": len(segs),
            "replayable_segment_count": sum(1 for s in segs if s["is_replayable"] in (True, "True", "true")),
            "segments_replay_ok": sum(1 for r in results if r["replay_status"].startswith("REPLAY_OK")),
            "market_context_ok": True,
            "wall_history_ok": True,
            "pattern_candidates_ok": True,
            "pattern_candidate_count": len(pats),
            "pattern_integrity_error_count": 0,
            "timeline_rows_1m": 10,
            "wall_sequences_total": 1,
        },
        "integrity": {"ok": True},
    }


def _fake_hl_ok(**kwargs):
    seg_out: Path = kwargs["segment_out_dir"]
    armed_seconds = kwargs["armed_seconds"]
    symbol = kwargs["symbol"]
    start = kwargs["start"]
    for armed_s in armed_seconds:
        armed_dir = seg_out / f"armed_{int(armed_s)}s"
        armed_dir.mkdir(parents=True, exist_ok=True)
        armed_time = start + timedelta(minutes=5)
        action_time = armed_time + timedelta(seconds=min(30, max(0, int(armed_s) // 2 or 0)))
        if int(armed_s) == 0:
            action_time = armed_time
        rows = [
            {
                "signal_id": "S00001",
                "variant": "P0",
                "signal_time": armed_time.isoformat(),
                "action_time": armed_time.isoformat(),
                "signal_price": "1.0",
                "armed_pair_id": "",
                "armed_time": "",
            },
            {
                "signal_id": "S00002",
                "variant": "P3",
                "signal_time": action_time.isoformat(),
                "action_time": action_time.isoformat(),
                "signal_price": "1.01",
                "armed_pair_id": "AP0001",
                "armed_time": armed_time.isoformat(),
            },
            # intentional duplicate same pair/variant — aggregator must dedupe
            {
                "signal_id": "S00003",
                "variant": "P3",
                "signal_time": action_time.isoformat(),
                "action_time": action_time.isoformat(),
                "signal_price": "1.01",
                "armed_pair_id": "AP0001",
                "armed_time": armed_time.isoformat(),
            },
        ]
        write_csv_headered(
            armed_dir / "higher_low_raw_signals.csv",
            rows,
            [
                "signal_id",
                "variant",
                "signal_time",
                "action_time",
                "signal_price",
                "armed_pair_id",
                "armed_time",
            ],
        )
        write_csv_headered(armed_dir / "higher_low_pairs.csv", [{"x": 1}], ["x"])
        write_csv_headered(armed_dir / "confirmed_pullback_lows.csv", [{"x": 1}, {"x": 2}], ["x"])
    return {
        "ok": True,
        "summaries": {},
        "totals": {
            "confirmed_low_count": 2,
            "higher_low_pair_count": 1,
            "armed_pair_count": 1,
            "armed_action_count": 1,
        },
        "error": None,
    }


def test_parse_args_defaults() -> None:
    args = parse_args(["--symbol", "APTUSDT"])
    assert args.pattern_timeframe == "1m"
    assert args.pattern_lookback_bars == 5
    assert args.max_bar_range_pct == 20.0
    assert args.warmup_seconds == 300
    assert args.replay_sample_interval == 60
    assert args.wall_sample_interval == 60
    assert args.higher_low_armed_seconds == "0,300,600,900,1800"
    assert args.max_pullback_duration_seconds == 900
    assert args.max_pullback_depth_bps == 100.0
    assert args.skip_higher_lows is False
    assert args.continue_on_phase_error is False
    assert args.overwrite is False
    assert parse_armed_seconds(args.higher_low_armed_seconds) == (0, 300, 600, 900, 1800)


def test_cli_help_smoke() -> None:
    with pytest.raises(SystemExit) as ei:
        parse_args(["--help"])
    assert ei.value.code == 0


def test_select_replayable_skips_gap_segments(tmp_path: Path) -> None:
    segs = tmp_path / "replay_segments.csv"
    res = tmp_path / "segment_replay_results.csv"
    _write_segments(
        segs,
        [
            {
                "segment_id": "S0001",
                "symbol": "X",
                "segment_start_ts": TS0.isoformat(),
                "segment_end_ts": (TS0 + timedelta(minutes=1)).isoformat(),
                "is_replayable": False,
                "discard_reason": "gap",
            },
            {
                "segment_id": "S0002",
                "symbol": "X",
                "segment_start_ts": TS0.isoformat(),
                "segment_end_ts": (TS0 + timedelta(minutes=30)).isoformat(),
                "is_replayable": True,
                "discard_reason": "",
            },
        ],
    )
    _write_replay_results(
        res,
        [
            {"segment_id": "S0001", "replay_status": "SKIPPED_NOT_REPLAYABLE", "error_message": "gap"},
            {"segment_id": "S0002", "replay_status": "REPLAY_OK", "error_message": ""},
        ],
    )
    selected = select_replayable_segments(replay_segments_path=segs, replay_results_path=res)
    assert [s["segment_id"] for s in selected] == ["S0002"]


def test_full_history_called_and_hl_only_replayable(tmp_path: Path) -> None:
    called = {"fh": 0, "hl_segs": []}

    def fh(**kwargs):
        called["fh"] += 1
        p = kwargs["params"]
        assert p.run_market_context is True
        assert p.run_wall_history is True
        assert p.run_pattern_candidates is True
        return _fake_full_history(params=p, gaps=1)

    def hl(**kwargs):
        called["hl_segs"].append(kwargs["segment_out_dir"].name)
        assert kwargs["segment_out_dir"].name == "S0002"
        # ensure armed folders requested
        assert tuple(kwargs["armed_seconds"]) == (0, 300)
        return _fake_hl_ok(**kwargs)

    out = tmp_path / "gen"
    result = run_general_research(
        GeneralResearchParams(
            symbol="APTUSDT",
            output_dir=out,
            higher_low_armed_seconds=(0, 300),
            overwrite=True,
        ),
        full_history_runner=fh,
        higher_lows_segment_runner=hl,
    )
    assert called["fh"] == 1
    assert called["hl_segs"] == ["S0002"]
    assert (out / "higher_lows" / "S0002" / "armed_0s").is_dir()
    assert (out / "higher_lows" / "S0002" / "armed_300s").is_dir()
    assert not (out / "higher_lows" / "S0001").exists()
    assert result["summary"]["gap_count"] == 1
    assert "WITH_GAPS" in result["decision"] or result["decision"] == "GENERAL_ANALYSIS_COMPLETE_WITH_GAPS"


def test_continue_on_phase_error(tmp_path: Path) -> None:
    def fh(**kwargs):
        return _fake_full_history(
            params=kwargs["params"],
            segments=[
                {
                    "segment_id": "S0002",
                    "symbol": "APTUSDT",
                    "segment_start_ts": TS0.isoformat(),
                    "segment_end_ts": (TS0 + timedelta(minutes=20)).isoformat(),
                    "is_replayable": True,
                    "discard_reason": "",
                },
                {
                    "segment_id": "S0003",
                    "symbol": "APTUSDT",
                    "segment_start_ts": (TS0 + timedelta(minutes=30)).isoformat(),
                    "segment_end_ts": (TS0 + timedelta(minutes=50)).isoformat(),
                    "is_replayable": True,
                    "discard_reason": "",
                },
            ],
        )

    def hl(**kwargs):
        sid = kwargs["segment_out_dir"].name
        if sid == "S0002":
            raise RuntimeError("boom-S0002")
        return _fake_hl_ok(**kwargs)

    out = tmp_path / "cont"
    result = run_general_research(
        GeneralResearchParams(
            symbol="APTUSDT",
            output_dir=out,
            higher_low_armed_seconds=(0,),
            continue_on_phase_error=True,
            overwrite=True,
        ),
        full_history_runner=fh,
        higher_lows_segment_runner=hl,
    )
    assert result["summary"]["higher_lows_segments_failed"] == 1
    assert result["summary"]["higher_lows_segments_ok"] == 1
    assert (out / "higher_lows" / "S0003" / "armed_0s").exists()
    assert any(e.get("segment_id") == "S0002" for e in result["errors"])


def test_fail_fast_without_continue(tmp_path: Path) -> None:
    def fh(**kwargs):
        return _fake_full_history(
            params=kwargs["params"],
            segments=[
                {
                    "segment_id": "S0002",
                    "symbol": "APTUSDT",
                    "segment_start_ts": TS0.isoformat(),
                    "segment_end_ts": (TS0 + timedelta(minutes=20)).isoformat(),
                    "is_replayable": True,
                    "discard_reason": "",
                },
                {
                    "segment_id": "S0003",
                    "symbol": "APTUSDT",
                    "segment_start_ts": (TS0 + timedelta(minutes=30)).isoformat(),
                    "segment_end_ts": (TS0 + timedelta(minutes=50)).isoformat(),
                    "is_replayable": True,
                    "discard_reason": "",
                },
            ],
        )

    def hl(**kwargs):
        if kwargs["segment_out_dir"].name == "S0002":
            raise RuntimeError("boom")
        return _fake_hl_ok(**kwargs)

    out = tmp_path / "failfast"
    result = run_general_research(
        GeneralResearchParams(
            symbol="APTUSDT",
            output_dir=out,
            higher_low_armed_seconds=(0,),
            continue_on_phase_error=False,
            overwrite=True,
        ),
        full_history_runner=fh,
        higher_lows_segment_runner=hl,
    )
    assert result["decision"] == "GENERAL_ANALYSIS_FAILED"
    assert not (out / "higher_lows" / "S0003").exists()


def test_general_summary_and_phase_status(tmp_path: Path) -> None:
    out = tmp_path / "sum"
    result = run_general_research(
        GeneralResearchParams(
            symbol="APTUSDT",
            output_dir=out,
            higher_low_armed_seconds=(0, 600),
            overwrite=True,
        ),
        full_history_runner=lambda **kw: _fake_full_history(params=kw["params"], gaps=0),
        higher_lows_segment_runner=_fake_hl_ok,
    )
    summary = result["summary"]
    for key in (
        "symbol",
        "full_history_decision",
        "pattern_candidate_count",
        "higher_lows_segments_ok",
        "armed_action_count_total",
        "general_integrity_ok",
        "decision",
    ):
        assert key in summary
    assert (out / "general_summary.json").exists()
    assert (out / "GENERAL_REPORT.md").exists()
    status = read_csv_rows(out / "general_phase_status.csv")
    phases = {r["phase"] for r in status}
    assert "full_history" in phases
    assert "pattern_candidates" in phases
    assert "higher_lows_audit" in phases
    assert "general_report" in phases


def test_aggregate_patterns_and_hl_actions(tmp_path: Path) -> None:
    out = tmp_path / "agg"
    result = run_general_research(
        GeneralResearchParams(
            symbol="APTUSDT",
            output_dir=out,
            higher_low_armed_seconds=(0,),
            overwrite=True,
        ),
        full_history_runner=lambda **kw: _fake_full_history(params=kw["params"]),
        higher_lows_segment_runner=_fake_hl_ok,
    )
    rows = read_csv_rows(out / "general_pattern_evaluation_input.csv")
    families = {r["source_family"] for r in rows}
    assert "WALL_LIFECYCLE" in families
    assert "HIGHER_LOW_ARMED_ACTION" in families
    # P0 baseline excluded
    assert not any(r.get("variant") == "P0" for r in rows)
    # pair/variant dedupe → one P3
    p3 = [r for r in rows if r.get("variant") == "P3"]
    assert len(p3) == 1
    assert p3[0]["armed_pair_id"] == "AP0001"


def test_baseline_p0_p2_not_armed_actions(tmp_path: Path) -> None:
    armed = tmp_path / "armed_0s"
    armed.mkdir()
    write_csv_headered(
        armed / "higher_low_raw_signals.csv",
        [
            {
                "signal_id": "S1",
                "variant": "P0",
                "signal_time": TS0.isoformat(),
                "action_time": TS0.isoformat(),
                "signal_price": "1",
                "armed_pair_id": "",
                "armed_time": "",
            },
            {
                "signal_id": "S2",
                "variant": "P1",
                "signal_time": TS0.isoformat(),
                "action_time": TS0.isoformat(),
                "signal_price": "1",
                "armed_pair_id": "AP1",
                "armed_time": TS0.isoformat(),
            },
            {
                "signal_id": "S3",
                "variant": "P2",
                "signal_time": TS0.isoformat(),
                "action_time": TS0.isoformat(),
                "signal_price": "1",
                "armed_pair_id": "AP1",
                "armed_time": TS0.isoformat(),
            },
        ],
        [
            "signal_id",
            "variant",
            "signal_time",
            "action_time",
            "signal_price",
            "armed_pair_id",
            "armed_time",
        ],
    )
    rows = build_higher_low_eval_rows(
        symbol="X", segment_id="S1", armed_dir=armed, armed_seconds=0
    )
    assert rows == []


def test_causality_action_after_armed(tmp_path: Path) -> None:
    out = tmp_path / "cau"
    result = run_general_research(
        GeneralResearchParams(
            symbol="APTUSDT",
            output_dir=out,
            higher_low_armed_seconds=(600,),
            overwrite=True,
        ),
        full_history_runner=lambda **kw: _fake_full_history(params=kw["params"]),
        higher_lows_segment_runner=_fake_hl_ok,
    )
    rows = [r for r in result["evaluation_rows"] if r["source_family"] == "HIGHER_LOW_ARMED_ACTION"]
    assert rows
    for r in rows:
        assert r["action_time"] >= r["armed_time"]


def test_empty_higher_lows_path(tmp_path: Path) -> None:
    def fh(**kwargs):
        return _fake_full_history(
            params=kwargs["params"],
            segments=[
                {
                    "segment_id": "S0001",
                    "symbol": "APTUSDT",
                    "segment_start_ts": TS0.isoformat(),
                    "segment_end_ts": (TS0 + timedelta(minutes=1)).isoformat(),
                    "is_replayable": False,
                    "discard_reason": "gap",
                }
            ],
        )

    out = tmp_path / "emptyhl"
    result = run_general_research(
        GeneralResearchParams(
            symbol="APTUSDT",
            output_dir=out,
            overwrite=True,
        ),
        full_history_runner=fh,
        higher_lows_segment_runner=_fake_hl_ok,
    )
    status = {r["phase"]: r["status"] for r in result["phase_status"]}
    assert status["higher_lows_audit"] == "SKIPPED_NO_DATA"
    # eval still has headers / pattern rows only
    assert (out / "general_pattern_evaluation_input.csv").exists()
    with (out / "general_pattern_evaluation_input.csv").open() as fh:
        header = next(csv.reader(fh))
    assert header == EVAL_INPUT_HEADERS


def test_skip_higher_lows(tmp_path: Path) -> None:
    out = tmp_path / "skip"
    result = run_general_research(
        GeneralResearchParams(
            symbol="APTUSDT",
            output_dir=out,
            skip_higher_lows=True,
            overwrite=True,
        ),
        full_history_runner=lambda **kw: _fake_full_history(params=kw["params"]),
        higher_lows_segment_runner=lambda **kw: (_ for _ in ()).throw(RuntimeError("should not run")),
    )
    assert result["summary"]["higher_lows_segments_requested"] == 0
    assert {r["phase"]: r["status"] for r in result["phase_status"]}["higher_lows_audit"] == "NOT_REQUESTED"


def test_deterministic_output(tmp_path: Path) -> None:
    def run_once(path: Path):
        return run_general_research(
            GeneralResearchParams(
                symbol="APTUSDT",
                output_dir=path,
                higher_low_armed_seconds=(0,),
                overwrite=True,
            ),
            full_history_runner=lambda **kw: _fake_full_history(params=kw["params"]),
            higher_lows_segment_runner=_fake_hl_ok,
        )

    a = run_once(tmp_path / "a")
    b = run_once(tmp_path / "b")

    def _canon(rows):
        out = []
        for r in rows:
            d = {k: v for k, v in r.items() if k != "source_output_dir"}
            out.append(d)
        return out

    assert _canon(a["evaluation_rows"]) == _canon(b["evaluation_rows"])
    run_once(tmp_path / "a")
    text1 = (tmp_path / "a" / "general_pattern_evaluation_input.csv").read_text()
    run_once(tmp_path / "a")
    text2 = (tmp_path / "a" / "general_pattern_evaluation_input.csv").read_text()
    assert text1 == text2
    assert a["summary"]["pattern_candidate_count"] == b["summary"]["pattern_candidate_count"]


def test_overwrite_required(tmp_path: Path) -> None:
    out = tmp_path / "exists"
    out.mkdir()
    (out / "marker.txt").write_text("x", encoding="utf-8")
    with pytest.raises(GeneralResearchError):
        prepare_output_dir(out, overwrite=False)
    prepare_output_dir(out, overwrite=True)
    assert out.exists()
    assert list(out.iterdir()) == []


def test_decide_general() -> None:
    assert (
        decide_general(integrity_ok=True, gap_count=0, hard_failure=False, soft_warnings=False)
        == "GENERAL_ANALYSIS_COMPLETE"
    )
    assert (
        decide_general(integrity_ok=True, gap_count=2, hard_failure=False, soft_warnings=False)
        == "GENERAL_ANALYSIS_COMPLETE_WITH_GAPS"
    )
    assert (
        decide_general(integrity_ok=True, gap_count=0, hard_failure=False, soft_warnings=True)
        == "GENERAL_ANALYSIS_COMPLETE_WITH_WARNINGS"
    )
    assert (
        decide_general(integrity_ok=False, gap_count=0, hard_failure=False, soft_warnings=False)
        == "GENERAL_ANALYSIS_FAILED"
    )


def test_build_pattern_eval_rows(tmp_path: Path) -> None:
    path = tmp_path / "pattern_candidates.csv"
    write_csv_headered(
        path,
        [
            {
                "symbol": "APTUSDT",
                "segment_id": "S1",
                "pattern_id": "PID1",
                "pattern_ts": TS0.isoformat(),
                "pattern_type": "X",
                "pattern_family": "F",
                "pattern_side": "bid",
                "close_price": "1",
                "wall_price": "",
                "source_wall_sequence_id": "W",
                "source_transition_type": "GREW",
                "source_transition_ts": TS0.isoformat(),
                "data_complete": True,
            }
        ],
        [
            "symbol",
            "segment_id",
            "pattern_id",
            "pattern_ts",
            "pattern_type",
            "pattern_family",
            "pattern_side",
            "close_price",
            "wall_price",
            "source_wall_sequence_id",
            "source_transition_type",
            "source_transition_ts",
            "data_complete",
        ],
    )
    rows = build_pattern_eval_rows(
        symbol="APTUSDT", pattern_candidates_path=path, source_output_dir=tmp_path
    )
    assert len(rows) == 1
    assert rows[0]["event_id"] == "PID1"


def test_integrity_detects_duplicate_pattern_ids(tmp_path: Path) -> None:
    fh = tmp_path / "full_history"
    fh.mkdir()
    write_csv_headered(
        fh / "pattern_candidates.csv",
        [
            {"pattern_id": "A", "segment_id": "S1"},
            {"pattern_id": "A", "segment_id": "S1"},
        ],
        ["pattern_id", "segment_id"],
    )
    for name in (
        "GENERAL_REPORT.md",
        "general_summary.json",
        "general_phase_status.csv",
        "general_pattern_evaluation_input.csv",
        "general_errors.csv",
    ):
        (tmp_path / name).write_text("x", encoding="utf-8")
    write_csv_headered(tmp_path / "general_phase_status.csv", [], ["phase"])
    write_csv_headered(tmp_path / "general_pattern_evaluation_input.csv", [], EVAL_INPUT_HEADERS)
    integ = check_general_integrity(
        output_dir=tmp_path,
        full_history_dir=fh,
        eval_rows=[],
        phase_status=[],
        summary={"pattern_candidate_count": 2},
        hl_segment_bounds={},
        skip_higher_lows=True,
    )
    assert integ["ok"] is False
    assert any("duplicate pattern_id" in e for e in integ["errors"])

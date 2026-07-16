"""Tests for trend-state Pine export (research-only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.regime_scanner.trend_pine_export import (
    AUDIT_ANCHOR_PLOT,
    PineExportValidationError,
    build_pine_header,
    build_trend_state_pine,
    escape_pine_string,
    export_audit_pine_artifacts,
    extract_transitions,
    marker_rows_from_events,
    state_code,
    timeline_to_state_runs,
    validate_pine_script,
)


def _timeline() -> list[dict]:
    return [
        {
            "decision_time": "2026-03-01T00:05:00+00:00",
            "state": "bullish_weakening",
            "previous_state": "strong_bullish",
            "close": 1.0,
            "transition": True,
            "reasons": "hold",
        },
        {
            "decision_time": "2026-03-01T00:10:00+00:00",
            "state": "bullish_weakening",
            "previous_state": "bullish_weakening",
            "close": 1.01,
            "transition": False,
            "reasons": "",
        },
        {
            "decision_time": "2026-03-01T00:15:00+00:00",
            "state": "topping",
            "previous_state": "bullish_weakening",
            "close": 1.02,
            "transition": True,
            "reasons": "multi_bar_topping_structure",
        },
    ]


def _sample_pine(*, title: str = "TEST", **kwargs: object) -> str:
    tl = _timeline()
    return build_trend_state_pine(
        title=title,
        symbol="APTUSDT",
        phase="C2_forward_outcome",
        variant="C1_B_loose",
        analyze_start="2026-03-01",
        analyze_end="2026-03-12",
        state_runs=timeline_to_state_runs(tl),
        transitions=extract_transitions(tl),
        markers=marker_rows_from_events(
            [{"timestamp": "2026-03-01T00:15:00+00:00", "new_state": "topping", "h12_direction_hit": True}]
        ),
        **kwargs,
    )


def _expected_header(title: str) -> str:
    return "\n".join(build_pine_header(title)) + "\n"


def test_state_codes_stable() -> None:
    assert state_code("topping") == 5
    assert state_code("bottoming") == 10
    assert state_code("unknown_state") == 0


def test_timeline_to_runs_and_transitions() -> None:
    tl = _timeline()
    runs = timeline_to_state_runs(tl)
    assert len(runs) == 2
    assert runs[0]["state"] == "bullish_weakening"
    assert runs[1]["state"] == "topping"
    tr = extract_transitions(tl)
    assert len(tr) == 2
    assert tr[-1]["new_state"] == "topping"


def test_build_pine_header_multiline_indicator_block() -> None:
    title = 'APTUSDT C2 "quoted" title'
    header = build_pine_header(title)
    assert header[0] == "//@version=6"
    assert header[1] == "indicator("
    assert header[2] == f'    "{escape_pine_string(title)}",'
    assert header[3] == "    overlay=true,"
    assert header[6] == ")"
    assert header[8] == AUDIT_ANCHOR_PLOT


def test_build_pine_v6_contract() -> None:
    pine = _sample_pine()
    assert pine.splitlines()[0] == "//@version=6"
    assert 'timestamp("UTC"' in pine
    assert "box.new" not in pine
    assert "topping" in pine
    assert "✓" in pine


def test_pine_header_exact_fragment_and_anchor_placement() -> None:
    title = "APTUSDT C2_forward_outcome C1_B_loose"
    pine = _sample_pine(title=title)
    assert pine.startswith(_expected_header(title))
    assert pine.count(AUDIT_ANCHOR_PLOT) == 1
    assert "plot(" not in pine[pine.index("indicator(") : pine.index(AUDIT_ANCHOR_PLOT)]
    validate_pine_script(pine)


def test_pine_anchor_before_functions_loops_and_barstate() -> None:
    pine = _sample_pine()
    anchor_at = pine.index(AUDIT_ANCHOR_PLOT)
    assert pine.find("f_ts(") > anchor_at
    assert pine.find("stateColor(") > anchor_at
    assert pine.find("if barstate.isfirst") > anchor_at
    assert pine.find("    for i = 0 to array.size(runStarts) - 1") > anchor_at


def test_validate_pine_script_rejects_ce10156_pattern() -> None:
    broken = "\n".join(
        [
            "//@version=6",
            "indicator(",
            AUDIT_ANCHOR_PLOT,
            '    "APTUSDT",',
            "    overlay=true,",
            "    max_labels_count=500,",
            "    max_lines_count=500",
            ")",
            "",
            "f_ts(y, m, d, h, mi) =>",
            '    timestamp("UTC", y, m, d, h, mi)',
            "",
        ]
    ) + "\n"
    with pytest.raises(PineExportValidationError, match="CE10156"):
        validate_pine_script(broken)


def test_validate_pine_script_rejects_missing_anchor_ce10246_risk() -> None:
    broken = "\n".join(
        [
            "//@version=6",
            "indicator(",
            '    "APTUSDT",',
            "    overlay=true,",
            "    max_labels_count=500,",
            "    max_lines_count=500",
            ")",
            "",
            "f_ts(y, m, d, h, mi) =>",
            '    timestamp("UTC", y, m, d, h, mi)',
            "",
        ]
    ) + "\n"
    with pytest.raises(PineExportValidationError, match="exactly one audit anchor"):
        validate_pine_script(broken)


def test_export_audit_pine_artifacts(tmp_path: Path) -> None:
    meta = export_audit_pine_artifacts(
        output_dir=tmp_path,
        phase="C2_forward_outcome",
        symbol="APTUSDT",
        analyze_start="2026-03-01",
        analyze_end="2026-03-12",
        variants={
            "C1_B_loose": {
                "timeline_rows": _timeline(),
                "marker_rows": marker_rows_from_events(
                    [{"timestamp": "2026-03-01T00:15:00+00:00", "new_state": "topping"}]
                ),
            }
        },
        recommended_variant="C1_B_loose",
    )
    variant_path = tmp_path / "trend_audit_c2_forward_outcome_c1_b_loose.pine"
    recommended_path = tmp_path / "trend_audit_c2_forward_outcome_recommended.pine"
    assert (tmp_path / "trend_pine_export.json").is_file()
    assert variant_path.is_file()
    assert recommended_path.is_file()
    assert meta["recommended_pine"] is not None
    variant_bytes = variant_path.read_bytes()
    recommended_bytes = recommended_path.read_bytes()
    assert variant_bytes == recommended_bytes
    for path in (variant_path, recommended_path):
        text = path.read_text(encoding="utf-8")
        validate_pine_script(text)
        assert text.startswith(_expected_header("APTUSDT C2_forward_outcome C1_B_loose"))

"""HTF-only pine export, dual lifecycle, and touch-marker tests."""

from __future__ import annotations

import re

import pandas as pd

from research.regime_scanner.htf_pivot_level_preview.config import (
    INVALIDATION_BOTH,
    INVALIDATION_CLOSE_BREAK_ONLY,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_REPLACEMENT,
    HtfPivotPreviewConfig,
    invalidation_mode_for_lifecycle,
)
from research.regime_scanner.htf_pivot_level_preview.levels import apply_lifecycle
from research.regime_scanner.htf_pivot_level_preview.pine_export import (
    build_htf_pivot_preview_pine,
    count_nlevels_in_pine,
    filter_htf_only_levels,
    pine_array_lengths,
    select_levels_for_pine,
)
from research.regime_scanner.trend_pine_export import validate_pine_script


def _levels_mixed() -> list[dict]:
    return [
        {
            "level_id": "h1",
            "symbol": "APTUSDT",
            "source_type": "htf_pivot_4h",
            "timeframe": "4h",
            "side": "support",
            "level_price": 5.0,
            "pivot_timestamp": "2026-03-01T00:00:00Z",
            "confirmation_timestamp": "2026-03-02T00:00:00Z",
            "visible_from_timestamp": "2026-03-02T00:00:00Z",
            "invalidated_at": None,
            "invalidation_reason": None,
            "active": True,
            "touch_count": 2,
            "first_touch_timestamp": "2026-03-02T06:00:00Z",
        },
        {
            "level_id": "h2",
            "symbol": "APTUSDT",
            "source_type": "htf_pivot_12h",
            "timeframe": "12h",
            "side": "resistance",
            "level_price": 6.0,
            "pivot_timestamp": "2026-03-03T00:00:00Z",
            "confirmation_timestamp": "2026-03-05T12:00:00Z",
            "visible_from_timestamp": "2026-03-05T12:00:00Z",
            "invalidated_at": "2026-03-10T00:00:00Z",
            "invalidation_reason": "close_break",
            "active": False,
            "touch_count": 0,
            "first_touch_timestamp": None,
        },
        {
            "level_id": "ext",
            "symbol": "APTUSDT",
            "source_type": "external_swing",
            "timeframe": "5m",
            "side": "support",
            "level_price": 4.9,
            "pivot_timestamp": "2026-03-01T01:00:00Z",
            "confirmation_timestamp": "2026-03-01T01:20:00Z",
            "visible_from_timestamp": "2026-03-01T01:20:00Z",
            "invalidated_at": None,
            "invalidation_reason": None,
            "active": True,
            "touch_count": 1,
            "first_touch_timestamp": "2026-03-01T02:00:00Z",
        },
        {
            "level_id": "prot",
            "symbol": "APTUSDT",
            "source_type": "protected",
            "timeframe": "5m",
            "side": "resistance",
            "level_price": 6.1,
            "pivot_timestamp": "2026-03-01T03:00:00Z",
            "confirmation_timestamp": "2026-03-01T03:20:00Z",
            "visible_from_timestamp": "2026-03-01T03:20:00Z",
            "invalidated_at": None,
            "invalidation_reason": None,
            "active": True,
            "touch_count": 0,
            "first_touch_timestamp": None,
        },
    ]


def test_htf_only_filters_external_protected():
    htf = filter_htf_only_levels(_levels_mixed())
    assert len(htf) == 2
    assert all(r["source_type"].startswith("htf_pivot_") for r in htf)


def test_htf_only_pine_contains_no_src_4_or_5():
    cfg = HtfPivotPreviewConfig(htf_only=True, embed_all_htf_levels=True)
    pine = build_htf_pivot_preview_pine(_levels_mixed(), symbol="APTUSDT", cfg=cfg)
    validate_pine_script(pine)
    # srcArr values
    m = re.search(r"srcArr = array.from\((.*)\)", pine)
    assert m
    vals = [int(x.strip()) for x in m.group(1).split(",")]
    assert 4 not in vals and 5 not in vals
    assert set(vals).issubset({1, 2, 3, 6, 7, 8})


def test_15m_trimmed_prefers_nearest_to_reference():
    rows = []
    for i in range(20):
        rows.append(
            {
                "level_id": f"c{i:03d}",
                "symbol": "APTUSDT",
                "source_type": "htf_pivot_4h",
                "timeframe": "4h",
                "side": "support",
                "level_price": 1.0 + i * 0.01,
                "pivot_timestamp": "2026-03-01T00:00:00Z",
                "confirmation_timestamp": "2026-03-02T00:00:00Z",
                "visible_from_timestamp": f"2026-03-{1 + (i % 28):02d}T00:00:00Z",
                "invalidated_at": None,
                "invalidation_reason": None,
                "active": True,
                "touch_count": 0,
                "first_touch_timestamp": None,
            }
        )
    for i in range(100):
        rows.append(
            {
                "level_id": f"m{i:03d}",
                "symbol": "APTUSDT",
                "source_type": "htf_pivot_15m",
                "timeframe": "15m",
                "side": "support" if i % 2 == 0 else "resistance",
                "level_price": 0.50 + i * 0.01,  # 0.50 .. 1.49
                "pivot_timestamp": "2026-04-01T00:00:00Z",
                "confirmation_timestamp": "2026-04-01T00:15:00Z",
                "visible_from_timestamp": f"2026-04-01T{i % 24:02d}:00:00Z",
                "invalidated_at": None if i in (11, 12) else "2026-04-02T00:00:00Z",
                "invalidation_reason": None if i in (11, 12) else "close_break",
                "active": i in (11, 12),  # prices ~0.61 / 0.62 — near reference
                "touch_count": 0,
                "first_touch_timestamp": None,
            }
        )
    cfg = HtfPivotPreviewConfig(htf_only=True, embed_all_htf_levels=True, pine_max_lines=40)
    selected = select_levels_for_pine(rows, cfg, reference_price=0.61)
    assert len(selected) <= 40
    assert sum(1 for r in selected if r["source_type"] == "htf_pivot_4h") >= 1
    m15 = [r for r in selected if r["source_type"] == "htf_pivot_15m"]
    assert len(m15) >= 1
    # nearest-to-0.61 should dominate the 15m slice (active near-ref first, then by distance)
    assert all(abs(float(r["level_price"]) - 0.61) / 0.61 <= 0.20 for r in m15)
    assert {round(float(r["level_price"]), 2) for r in m15 if r.get("active")} >= {0.61, 0.62}

def test_embedded_count_matches_htf_csv_selection():
    cfg = HtfPivotPreviewConfig(htf_only=True, embed_all_htf_levels=True)
    selected = select_levels_for_pine(_levels_mixed(), cfg)
    pine = build_htf_pivot_preview_pine(_levels_mixed(), symbol="APTUSDT", cfg=cfg)
    assert count_nlevels_in_pine(pine) == len(selected) == 2


def test_embed_all_does_not_truncate_below_cap():
    rows = []
    for i in range(61):
        rows.append(
            {
                "level_id": f"id{i:03d}",
                "symbol": "APTUSDT",
                "source_type": "htf_pivot_4h" if i % 3 == 0 else ("htf_pivot_12h" if i % 3 == 1 else "htf_pivot_1d"),
                "timeframe": "4h" if i % 3 == 0 else ("12h" if i % 3 == 1 else "1D"),
                "side": "support" if i % 2 == 0 else "resistance",
                "level_price": 5.0 + i * 0.01,
                "pivot_timestamp": f"2026-03-01T{i % 24:02d}:00:00Z",
                "confirmation_timestamp": f"2026-03-02T{i % 24:02d}:00:00Z",
                "visible_from_timestamp": f"2026-03-02T{i % 24:02d}:{i % 60:02d}:00Z",
                "invalidated_at": None,
                "invalidation_reason": None,
                "active": True,
                "touch_count": 0,
                "first_touch_timestamp": None,
            }
        )
    cfg = HtfPivotPreviewConfig(htf_only=True, embed_all_htf_levels=True)
    pine = build_htf_pivot_preview_pine(rows, symbol="APTUSDT", cfg=cfg)
    assert count_nlevels_in_pine(pine) == 61


def test_touch_marker_uses_first_touch_not_visible_from():
    cfg = HtfPivotPreviewConfig(htf_only=True, embed_all_htf_levels=True)
    pine = build_htf_pivot_preview_pine(_levels_mixed(), symbol="APTUSDT", cfg=cfg)
    assert 'label.new(xTouch, px, "T' in pine
    assert 'label.new(x1, px, "T' not in pine
    assert "firstTouchArr" in pine
    assert "idArr" in pine


def test_level_without_touch_has_na_first_touch_no_t_draw_condition():
    cfg = HtfPivotPreviewConfig(htf_only=True, embed_all_htf_levels=True)
    pine = build_htf_pivot_preview_pine(_levels_mixed(), symbol="APTUSDT", cfg=cfg)
    # code path requires not na(xTouch) and touchArr > 0
    assert "not na(xTouch) and array.get(touchArr, i) > 0" in pine
    # h2 has no first touch → na in array
    m = re.search(r"firstTouchArr = array.from\((.*)\)", pine)
    assert m
    assert "na" in m.group(1)


def test_persistent_mode_no_replacement_invalidation():
    raw = [
        {
            "level_id": "a",
            "symbol": "APTUSDT",
            "source_type": "htf_pivot_4h",
            "timeframe": "4h",
            "side": "support",
            "level_price": 100.0,
            "pivot_timestamp": "2026-03-01T00:00:00Z",
            "confirmation_timestamp": "2026-03-01T08:00:00Z",
            "visible_from_timestamp": "2026-03-01T08:00:00Z",
            "invalidated_at": None,
            "invalidation_reason": None,
            "replacement_level_id": None,
            "active": True,
            "touch_count": 0,
            "sequence_id": 1,
        },
        {
            "level_id": "b",
            "symbol": "APTUSDT",
            "source_type": "htf_pivot_4h",
            "timeframe": "4h",
            "side": "support",
            "level_price": 101.0,
            "pivot_timestamp": "2026-03-03T00:00:00Z",
            "confirmation_timestamp": "2026-03-04T00:00:00Z",
            "visible_from_timestamp": "2026-03-04T00:00:00Z",
            "invalidated_at": None,
            "invalidation_reason": None,
            "replacement_level_id": None,
            "active": True,
            "touch_count": 0,
            "sequence_id": 1,
        },
    ]
    base = pd.Timestamp("2026-03-01", tz="UTC")
    rows = []
    for i in range(2000):
        rows.append(
            {
                "timestamp": base + pd.Timedelta(minutes=5 * i),
                "bucket_start": base + pd.Timedelta(minutes=5 * i),
                "open": 102.0,
                "high": 102.5,
                "low": 101.5,
                "close": 102.0,
                "volume": 1.0,
                "atr_14": 1.0,
                "sequence_id": 1,
            }
        )
    df = pd.DataFrame(rows)
    cfg = HtfPivotPreviewConfig(
        invalidation_mode=invalidation_mode_for_lifecycle(LIFECYCLE_PERSISTENT),
        lifecycle_mode=LIFECYCLE_PERSISTENT,
    )
    assert cfg.invalidation_mode == INVALIDATION_CLOSE_BREAK_ONLY
    out = apply_lifecycle(raw, df, cfg)
    by = {r["level_id"]: r for r in out}
    assert by["a"]["active"] is True
    assert by["a"]["invalidation_reason"] is None
    assert by["b"]["active"] is True


def test_replacement_mode_keeps_replacement_semantics():
    raw = [
        {
            "level_id": "a",
            "symbol": "APTUSDT",
            "source_type": "htf_pivot_4h",
            "timeframe": "4h",
            "side": "support",
            "level_price": 100.0,
            "pivot_timestamp": "2026-03-01T00:00:00Z",
            "confirmation_timestamp": "2026-03-01T08:00:00Z",
            "visible_from_timestamp": "2026-03-01T08:00:00Z",
            "invalidated_at": None,
            "invalidation_reason": None,
            "replacement_level_id": None,
            "active": True,
            "touch_count": 0,
            "sequence_id": 1,
        },
        {
            "level_id": "b",
            "symbol": "APTUSDT",
            "source_type": "htf_pivot_4h",
            "timeframe": "4h",
            "side": "support",
            "level_price": 101.0,
            "pivot_timestamp": "2026-03-03T00:00:00Z",
            "confirmation_timestamp": "2026-03-04T00:00:00Z",
            "visible_from_timestamp": "2026-03-04T00:00:00Z",
            "invalidated_at": None,
            "invalidation_reason": None,
            "replacement_level_id": None,
            "active": True,
            "touch_count": 0,
            "sequence_id": 1,
        },
    ]
    base = pd.Timestamp("2026-03-01", tz="UTC")
    df = pd.DataFrame(
        [
            {
                "timestamp": base + pd.Timedelta(minutes=5 * i),
                "bucket_start": base + pd.Timedelta(minutes=5 * i),
                "open": 100.5,
                "high": 101.0,
                "low": 100.2,
                "close": 100.5,
                "volume": 1.0,
                "atr_14": 1.0,
                "sequence_id": 1,
            }
            for i in range(2000)
        ]
    )
    cfg = HtfPivotPreviewConfig(
        invalidation_mode=invalidation_mode_for_lifecycle(LIFECYCLE_REPLACEMENT),
        lifecycle_mode=LIFECYCLE_REPLACEMENT,
    )
    assert cfg.invalidation_mode == INVALIDATION_BOTH
    out = apply_lifecycle(raw, df, cfg)
    by = {r["level_id"]: r for r in out}
    assert by["a"]["invalidation_reason"] == "replacement"
    assert by["a"]["replacement_level_id"] == "b"


def test_pine_no_extend_both_no_lookahead():
    cfg = HtfPivotPreviewConfig(htf_only=True, embed_all_htf_levels=True)
    pine = build_htf_pivot_preview_pine(_levels_mixed(), symbol="APTUSDT", cfg=cfg)
    assert "barmerge.lookahead_on" not in pine
    assert "extend=extend.both" not in pine
    assert 'timestamp("UTC"' in pine
    assert "DRAW_MODE=bar_time_only" in pine
    assert "forceActiveOnScreen" not in pine
    assert "extendActiveRight = input.bool(false" in pine
    assert "xloc=xloc.bar_index" not in pine


def test_array_lengths_identical():
    cfg = HtfPivotPreviewConfig(htf_only=True, embed_all_htf_levels=True)
    pine = build_htf_pivot_preview_pine(_levels_mixed(), symbol="APTUSDT", cfg=cfg)
    lengths = pine_array_lengths(pine)
    assert lengths
    assert len(set(lengths.values())) == 1
    assert lengths["firstTouchArr"] == count_nlevels_in_pine(pine)
    assert lengths["idArr"] == count_nlevels_in_pine(pine)


def test_pine_f_clear_guards_empty_arrays():
    cfg = HtfPivotPreviewConfig(htf_only=True, embed_all_htf_levels=True)
    pine = build_htf_pivot_preview_pine(_levels_mixed(), symbol="APTUSDT", cfg=cfg)
    assert "if array.size(lines) > 0" in pine
    assert "if array.size(labs) > 0" in pine
    assert "if array.size(markers) > 0" in pine


def test_pine_draw_gate_not_blocked_on_forming_bar():
    cfg = HtfPivotPreviewConfig(htf_only=True, embed_all_htf_levels=True)
    pine = build_htf_pivot_preview_pine(_levels_mixed(), symbol="APTUSDT", cfg=cfg)
    assert "confirmOnClose = input.bool(false" in pine
    assert "barstate.islastconfirmedhistory or (not confirmOnClose and barstate.islast)" in pine
    assert "barstate.islast and canMark" not in pine
    assert "HTF preview OK" in pine
    assert "DRAW_MODE=bar_time_only" in pine
    assert "xloc.bar_time" in pine
    assert "xloc=xloc.bar_index" not in pine
    assert "useBarIndexMapping" not in pine
    assert "clipBarIndex" not in pine
    assert "timeToBarIndex" not in pine
    assert "onlyNearestToPrice = input.bool(true" in pine
    assert "nearAbove = input.int(4" in pine
    assert "nearBelow = input.int(4" in pine
    assert "showInvalidated = input.bool(true" in pine
    assert "show5m = input.bool(true" in pine
    assert "show15m = input.bool(true" in pine
    assert "show1h = input.bool(true" in pine
    assert "maxDistPct = input.float(8.0" in pine
    assert "pickNearestSide" in pine
    assert "sortIdxByPrice" not in pine
    assert "levelPassesFilters" in pine
    assert "src == 7 ? show5m" in pine
    assert "src == 6 ? show15m" in pine
    assert "src == 8 ? show1h" in pine
    assert "v3-bartime" in pine


def test_visible_from_unchanged_in_selection_order():
    cfg = HtfPivotPreviewConfig(htf_only=True, embed_all_htf_levels=True)
    selected = select_levels_for_pine(_levels_mixed(), cfg)
    assert selected[0]["visible_from_timestamp"] <= selected[1]["visible_from_timestamp"]
    pine = build_htf_pivot_preview_pine(_levels_mixed(), symbol="APTUSDT", cfg=cfg)
    assert "visible_from" in pine.lower() or "visArr" in pine

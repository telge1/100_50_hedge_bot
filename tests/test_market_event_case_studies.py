"""Unit tests for market_event_case_studies (no ClickHouse)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from orderbook_analyse.market_event_case_studies.index import INDEX_COLUMNS, row_from_summary, write_case_index
from orderbook_analyse.market_event_case_studies.select import (
    CaseCandidate,
    apply_cooldown,
    cooldown_per_symbol,
    safe_case_dirname,
    select_rare_confluence,
    select_top_n,
)


def test_apply_cooldown_keeps_higher_score():
    t0 = datetime(2026, 8, 12, 10, 0, 0)
    events = [
        (t0, 0.01, "a"),
        (t0 + timedelta(minutes=10), 0.05, "b"),  # better, within 60m of a
        (t0 + timedelta(minutes=70), 0.02, "c"),
    ]
    kept = apply_cooldown(events, cooldown_m=60, prefer_higher_score=True)
    assert [p for _, _, p in kept] == ["b", "c"]


def test_cooldown_per_symbol_independent():
    t0 = datetime(2026, 8, 12, 12, 0, 0)
    cases = [
        CaseCandidate("long_big_move", "AAA", t0, 0.02),
        CaseCandidate("long_big_move", "AAA", t0 + timedelta(minutes=15), 0.03),
        CaseCandidate("long_big_move", "BBB", t0 + timedelta(minutes=5), 0.04),
    ]
    kept = cooldown_per_symbol(cases, cooldown_m=60)
    assert len(kept) == 2
    assert {c.symbol for c in kept} == {"AAA", "BBB"}
    aaa = next(c for c in kept if c.symbol == "AAA")
    assert aaa.score == 0.03


def test_select_top_n():
    t0 = datetime(2026, 8, 12, 0, 0, 0)
    cases = [CaseCandidate("x", "S", t0 + timedelta(minutes=i), float(i)) for i in range(10)]
    top = select_top_n(cases, 3)
    assert [c.score for c in top] == [9.0, 8.0, 7.0]


def test_select_rare_keeps_all_when_small():
    t0 = datetime(2026, 8, 12, 0, 0, 0)
    cases = [
        CaseCandidate("rare_confluence", f"S{i}", t0 + timedelta(hours=i), 0.01 * i) for i in range(5)
    ]
    out = select_rare_confluence(cases, max_all=40)
    assert len(out) == 5


def test_select_rare_caps_and_stratifies():
    t0 = datetime(2026, 8, 11, 0, 0, 0)
    cases = []
    for d in range(7):
        for i in range(10):
            cases.append(
                CaseCandidate(
                    "rare_confluence",
                    f"C{d}_{i}",
                    t0 + timedelta(days=d, minutes=i * 5),
                    score=0.1 * (d + 1) + 0.001 * i,
                    meta={"subtype": "LONG_RARE_IMB_OFI_DELTA_V1"},
                )
            )
    assert len(cases) == 70
    out = select_rare_confluence(cases, max_all=40, top_abs=20, random_fill=20, seed=1)
    assert len(out) == 40
    # top absolute scores included
    top_ids = {c.case_id for c in sorted(cases, key=lambda x: abs(x.score), reverse=True)[:20]}
    out_ids = {c.case_id for c in out}
    assert top_ids.issubset(out_ids)


def test_report_paths_safe_and_unique():
    t0 = datetime(2026, 8, 12, 21, 24, 0)
    a = CaseCandidate(
        "flow_opposed_reversal",
        "ADAUSDT",
        t0,
        1.0,
        meta={"subtype": "flow_opposed_sell_then_up"},
    )
    b = CaseCandidate(
        "long_big_move",
        "ADAUSDT",
        t0,
        1.0,
        meta={"subtype": "long_big_move"},
    )
    assert a.report_relpath() != b.report_relpath()
    assert "ADAUSDT_20260812_2124" in a.report_relpath()
    assert "/" in a.report_relpath()
    assert safe_case_dirname("ADAUSDT", t0) == "ADAUSDT_20260812_2124"
    # no path traversal
    dirty = CaseCandidate("x", "S", t0, 1.0, meta={"subtype": "../evil"})
    assert ".." not in dirty.report_relpath()


def test_index_write(tmp_path: Path):
    summary = {
        "price": {
            "event_minute": {"event_minute_return": -0.01},
            "after_event": {"future_return_60m": 0.02, "future_return_240m": 0.03},
            "path_metrics": {
                "60m": {
                    "LONG": {"mfe": 0.02, "mae": 0.01},
                    "SHORT": {"mfe": 0.01, "mae": 0.02},
                },
                "240m": {
                    "LONG": {"mfe": 0.04, "mae": 0.02},
                    "SHORT": {"mfe": 0.02, "mae": 0.04},
                },
            },
        },
        "trades": {"event_minute": {"delta_ratio": -0.5, "trade_delta": -100.0}},
        "lld": {"distance_upper_bps": 12.0, "distance_lower_bps": -8.0},
        "classification": {"primary": "FLOW_OPPOSED_MOVE"},
    }
    row = row_from_summary(
        case_id="c1",
        case_type="flow_opposed_reversal",
        symbol="ADAUSDT",
        event_time="2026-08-12T21:24:00Z",
        report_path="flow_opposed_sell_then_up/ADAUSDT_20260812_2124",
        summary=summary,
    )
    csv_path, md_path = write_case_index([row], tmp_path)
    assert csv_path.exists() and md_path.exists()
    text = csv_path.read_text(encoding="utf-8")
    for col in INDEX_COLUMNS:
        assert col in text.splitlines()[0]
    assert "ADAUSDT" in text
    assert "FLOW_OPPOSED_MOVE" in text

"""Phase 5 pattern candidate unit tests (synthetic, no ClickHouse)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from orderbook_analyse.full_history_analysis import parse_args, render_report
from orderbook_analyse.pattern_candidates import (
    CANDIDATE_HEADERS,
    FEATURE_HEADERS,
    PatternCandidateError,
    PatternParams,
    check_pattern_integrity,
    compute_rolling_features,
    decide_phase5_patterns,
    detect_patterns_for_segment,
    make_pattern_id,
    run_pattern_candidates,
    validate_pattern_params,
)
from orderbook_analyse.replay_segmentation import ReplayGap, ReplaySegment, discover_replay_segments

TS0 = datetime(2026, 7, 26, 16, 40, 0, tzinfo=timezone.utc)


def _seg(**kwargs) -> ReplaySegment:
    base: dict[str, Any] = dict(
        segment_id="S0002",
        symbol="APTUSDT",
        segment_start_ts=TS0,
        segment_end_ts=TS0 + timedelta(minutes=30),
        bootstrap_snapshot_ts=TS0,
        bootstrap_update_id=1,
        bootstrap_cross_sequence=1,
        first_delta_update_id=2,
        last_update_id=100,
        last_cross_sequence=10,
        message_count=100,
        delta_message_count=99,
        snapshot_message_count=1,
        duration_sec=1800,
        bid_snapshot_levels=20,
        ask_snapshot_levels=20,
        is_replayable=True,
        discard_reason=None,
        end_reason="analysis_end",
    )
    base.update(kwargs)
    return ReplaySegment(**base)


def _bar(
    end: datetime,
    *,
    open_p: float = 0.63,
    close_p: float | None = None,
    high: float | None = None,
    low: float | None = None,
    delta: float | None = 0.0,
    total: float | None = 1000.0,
    oi_open: float | None = 100.0,
    oi_close: float | None = 100.0,
    liq: float | None = 0.0,
    buy_liq: float | None = 0.0,
    sell_liq: float | None = 0.0,
    imb: float | None = 0.0,
    sources: str = "price|trades|oi|liquidations|walls",
    wall_present: bool = True,
    segment_id: str = "S0002",
    **extra: Any,
) -> dict[str, Any]:
    c = close_p if close_p is not None else open_p
    h = high if high is not None else max(open_p, c)
    lo = low if low is not None else min(open_p, c)
    start = end - timedelta(minutes=1)
    row = {
        "symbol": "APTUSDT",
        "wall_segment_id": segment_id,
        "bucket_start": start.isoformat(),
        "bucket_end": end.isoformat(),
        "open_price": open_p,
        "high_price": h,
        "low_price": lo,
        "close_price": c,
        "price_change_pct": ((c - open_p) / open_p * 100.0) if open_p else None,
        "trade_count": 10 if total is not None else None,
        "total_notional": total,
        "buy_notional": (total + (delta or 0)) / 2 if total is not None else None,
        "sell_notional": (total - (delta or 0)) / 2 if total is not None else None,
        "delta_notional": delta,
        "delta_ratio": (delta / total) if (total and delta is not None) else None,
        "oi_open": oi_open,
        "oi_close": oi_close,
        "oi_change_pct": ((oi_close - oi_open) / oi_open * 100.0)
        if oi_open and oi_close is not None
        else None,
        "liquidation_count": 1 if liq else 0,
        "liquidation_notional": liq,
        "buy_liquidation_notional": buy_liq,
        "sell_liquidation_notional": sell_liq,
        "spread_bps_close": 2.0,
        "context_quadrant": "PRICE_FLAT_OI_FLAT",
        "data_sources_present": sources,
        "wall_data_present": wall_present,
        "wall_data_stale": False,
        "wall_sample_ts": end.isoformat(),
        "bid_wall_count": 1,
        "ask_wall_count": 0,
        "bid_wall_total_notional": 50000,
        "ask_wall_total_notional": 10000,
        "wall_notional_imbalance": imb,
        "nearest_bid_wall_distance_bps": 20.0,
        "nearest_ask_wall_distance_bps": 80.0,
        "nearest_bid_wall_multiple": 4.0,
        "nearest_ask_wall_multiple": 2.0,
        "nearest_bid_wall_depth_share": 0.1,
        "nearest_ask_wall_depth_share": 0.02,
        "nearest_bid_wall_age_sec": 180,
        "nearest_ask_wall_age_sec": 60,
        "nearest_bid_wall_price": 0.628,
        "strongest_bid_wall_distance_bps": 20.0,
        "strongest_ask_wall_distance_bps": 80.0,
        "strongest_bid_wall_multiple": 4.0,
        "strongest_ask_wall_multiple": 2.0,
    }
    row.update(extra)
    return row


def _tr(
    ts: datetime,
    *,
    ttype: str,
    side: str = "bid",
    seq: str = "WS001",
    price: float = 0.628,
    notional: float = 50000,
    prev_notional: float | None = None,
    dist: float = 20.0,
    prev_dist: float | None = None,
    segment_id: str = "S0002",
) -> dict[str, Any]:
    return {
        "symbol": "APTUSDT",
        "segment_id": segment_id,
        "transition_ts": ts.isoformat(),
        "transition_type": ttype,
        "wall_sequence_id": seq,
        "side": side,
        "current_price": price,
        "previous_price": price,
        "current_notional": notional,
        "previous_notional": prev_notional if prev_notional is not None else notional,
        "notional_change_pct": (
            ((notional - prev_notional) / abs(prev_notional) * 100.0)
            if prev_notional
            else None
        ),
        "current_distance_bps": dist,
        "previous_distance_bps": prev_dist if prev_dist is not None else dist,
    }


def _run(
    rows: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    *,
    params: PatternParams | None = None,
    segment: ReplaySegment | None = None,
    gaps: list[ReplayGap] | None = None,
):
    seg = segment or _seg()
    p = params or PatternParams(
        timeframe="1m",
        lookback_bars=5,
        min_wall_age_sec=120,
        min_wall_samples=2,
        cooldown_bars=3,
        price_change_threshold_pct=0.05,
        delta_ratio_threshold=0.20,
        oi_change_threshold_pct=0.10,
        wall_growth_threshold_pct=20.0,
        wall_imbalance_threshold=0.5,
    )
    return detect_patterns_for_segment(
        symbol=seg.symbol,
        segment=seg,
        timeline_rows=rows,
        transitions=transitions,
        gaps=gaps or [],
        params=p,
    )


def test_validate_params_invalid_timeframe() -> None:
    with pytest.raises(PatternCandidateError):
        validate_pattern_params(PatternParams(timeframe="15m"))


def test_validate_params_invalid_thresholds() -> None:
    with pytest.raises(PatternCandidateError):
        validate_pattern_params(PatternParams(delta_ratio_threshold=-1))
    with pytest.raises(PatternCandidateError):
        validate_pattern_params(PatternParams(lookback_bars=0))


def test_make_pattern_id_deterministic() -> None:
    ts = datetime(2026, 7, 26, 16, 45, 0, tzinfo=timezone.utc)
    a = make_pattern_id(
        symbol="APTUSDT",
        segment_id="S0002",
        timeframe="1m",
        pattern_type="BID_WALL_CONFIRMED_BREAK",
        pattern_ts=ts,
    )
    b = make_pattern_id(
        symbol="APTUSDT",
        segment_id="S0002",
        timeframe="1m",
        pattern_type="BID_WALL_CONFIRMED_BREAK",
        pattern_ts=ts,
    )
    assert a == b
    assert a == "APTUSDT:S0002:1m:BID_WALL_CONFIRMED_BREAK:20260726T164500"


def test_rolling_price_delta_oi_liq() -> None:
    bars = []
    for i in range(5):
        end = TS0 + timedelta(minutes=i + 1)
        bars.append(
            _bar(
                end,
                open_p=100.0 + i,
                close_p=100.0 + i + 0.5,
                delta=-100.0 * (i + 1),
                total=1000.0,
                oi_open=1000.0,
                oi_close=1000.0 - 10 * (i + 1),
                liq=5.0 * (i + 1),
                buy_liq=1.0,
                sell_liq=4.0 * (i + 1),
            )
        )
    roll = compute_rolling_features(bars, lookback=5)
    assert roll["price_change_1bar_pct"] is not None
    assert roll["price_change_3bar_pct"] is not None
    assert roll["price_change_5bar_pct"] is not None
    # close of last vs open of first: (104.5 - 100) / 100 * 100
    assert abs(roll["rolling_price_change_pct"] - 4.5) < 1e-9
    assert roll["rolling_delta_notional"] == sum(-100.0 * (i + 1) for i in range(5))
    assert roll["rolling_delta_ratio"] is not None
    assert roll["rolling_oi_change_pct"] is not None
    assert roll["liquidation_notional_5bar"] == sum(5.0 * (i + 1) for i in range(5))


def test_rolling_incomplete_lookback_at_segment_start() -> None:
    bars = [_bar(TS0 + timedelta(minutes=1), open_p=10, close_p=10.1, delta=50, total=100)]
    roll = compute_rolling_features(bars, lookback=5)
    assert roll["price_change_5bar_pct"] is not None  # uses available bars
    assert abs(roll["price_change_5bar_pct"] - 1.0) < 1e-9


def test_missing_trades_not_zero_flow() -> None:
    bar = _bar(
        TS0 + timedelta(minutes=1),
        delta=None,
        total=None,
        sources="price|oi|walls",
    )
    bar["trade_count"] = None
    roll = compute_rolling_features([bar], lookback=5)
    assert roll["has_trades"] is False
    assert roll["rolling_delta_notional"] is None
    assert roll["rolling_delta_ratio"] is None


def test_price_down_delta_positive() -> None:
    rows = []
    for i in range(5):
        end = TS0 + timedelta(minutes=i + 1)
        # price drifting down, positive delta
        rows.append(
            _bar(
                end,
                open_p=1.0 - i * 0.01,
                close_p=1.0 - (i + 1) * 0.01,
                delta=400,
                total=1000,
            )
        )
    cands, _, _, _ = _run(rows, [])
    types = {c["pattern_type"] for c in cands}
    assert "PRICE_DOWN_DELTA_POSITIVE" in types


def test_price_up_delta_negative() -> None:
    rows = []
    for i in range(5):
        end = TS0 + timedelta(minutes=i + 1)
        rows.append(
            _bar(
                end,
                open_p=1.0 + i * 0.01,
                close_p=1.0 + (i + 1) * 0.01,
                delta=-400,
                total=1000,
            )
        )
    cands, _, _, _ = _run(rows, [])
    assert "PRICE_UP_DELTA_NEGATIVE" in {c["pattern_type"] for c in cands}


def test_price_flat_delta_positive() -> None:
    rows = []
    for i in range(5):
        end = TS0 + timedelta(minutes=i + 1)
        rows.append(_bar(end, open_p=1.0, close_p=1.0001, delta=400, total=1000))
    cands, _, _, _ = _run(rows, [])
    assert "PRICE_FLAT_DELTA_POSITIVE" in {c["pattern_type"] for c in cands}


def test_price_up_oi_down() -> None:
    rows = []
    for i in range(5):
        end = TS0 + timedelta(minutes=i + 1)
        rows.append(
            _bar(
                end,
                open_p=1.0 + i * 0.01,
                close_p=1.0 + (i + 1) * 0.01,
                delta=0,
                total=1000,
                oi_open=1000,
                oi_close=1000 - 20 * (i + 1),
            )
        )
    cands, _, _, _ = _run(rows, [])
    assert "PRICE_UP_OI_DOWN" in {c["pattern_type"] for c in cands}


def test_wall_grew_and_shrank() -> None:
    rows = [
        _bar(TS0 + timedelta(minutes=1)),
        _bar(TS0 + timedelta(minutes=2)),
        _bar(TS0 + timedelta(minutes=3)),
    ]
    trs = [
        _tr(TS0 + timedelta(seconds=30), ttype="APPEARED", notional=40000),
        _tr(
            TS0 + timedelta(minutes=1, seconds=10),
            ttype="GREW",
            notional=60000,
            prev_notional=40000,
        ),
        _tr(
            TS0 + timedelta(minutes=1, seconds=50),
            ttype="APPEARED",
            side="ask",
            seq="WS_ASK",
            price=0.64,
            notional=40000,
        ),
        _tr(
            TS0 + timedelta(minutes=2, seconds=10),
            ttype="SHRANK",
            side="ask",
            seq="WS_ASK",
            notional=20000,
            prev_notional=40000,
            price=0.64,
        ),
    ]
    cands, _, _, _ = _run(rows, trs)
    types = {c["pattern_type"] for c in cands}
    assert "BID_WALL_GREW" in types
    assert "ASK_WALL_SHRANK" in types


def test_disappeared_untested() -> None:
    rows = [_bar(TS0 + timedelta(minutes=1)), _bar(TS0 + timedelta(minutes=2))]
    trs = [
        _tr(TS0 + timedelta(seconds=10), ttype="APPEARED"),
        _tr(TS0 + timedelta(minutes=1, seconds=30), ttype="DISAPPEARED"),
    ]
    cands, _, _, _ = _run(rows, trs)
    types = {c["pattern_type"] for c in cands}
    assert "BID_WALL_DISAPPEARED_UNTESTED" in types
    assert "BID_WALL_PULLING_CANDIDATE" in types
    pull = next(c for c in cands if c["pattern_type"] == "BID_WALL_PULLING_CANDIDATE")
    assert "Order removal versus execution is unknown" in pull["candidate_reason"]


def test_wall_dominance() -> None:
    rows = [_bar(TS0 + timedelta(minutes=1), imb=0.8)]
    cands, _, _, _ = _run(rows, [_tr(TS0 + timedelta(seconds=5), ttype="APPEARED")])
    assert "BID_WALL_DOMINANCE" in {c["pattern_type"] for c in cands}


def test_tested_broken_only_after_transition_apt_fixture() -> None:
    """APT-like: wall 0.628 appear→persist→break at 16:44:29; no look-ahead."""
    appear = datetime(2026, 7, 26, 16, 40, 29, tzinfo=timezone.utc)
    break_ts = datetime(2026, 7, 26, 16, 44, 29, tzinfo=timezone.utc)
    rows = []
    for m in range(40, 46):
        end = datetime(2026, 7, 26, 16, m, 0, tzinfo=timezone.utc)
        # break bucket has strong sell delta
        delta = -800 if m == 45 else -50
        close = 0.627 if m >= 45 else 0.630
        rows.append(
            _bar(
                end,
                open_p=0.630,
                close_p=close,
                delta=delta,
                total=1000,
                nearest_bid_wall_price=0.628,
            )
        )
    trs = [
        _tr(appear, ttype="APPEARED", price=0.628),
        _tr(appear + timedelta(minutes=1), ttype="PERSISTED", price=0.628),
        _tr(appear + timedelta(minutes=2), ttype="PERSISTED", price=0.628),
        _tr(appear + timedelta(minutes=3), ttype="PERSISTED", price=0.628),
        _tr(break_ts, ttype="TESTED", price=0.628),
        _tr(break_ts, ttype="TRADED_THROUGH", price=0.628),
        _tr(break_ts, ttype="BROKEN", price=0.628),
    ]
    params = PatternParams(
        timeframe="1m",
        lookback_bars=5,
        min_wall_age_sec=120,
        min_wall_samples=2,
        cooldown_bars=3,
        delta_ratio_threshold=0.20,
        price_change_threshold_pct=0.05,
    )
    cands, feats, _, _ = _run(rows, trs, params=params)

    def types_before(ts: datetime) -> set[str]:
        return {
            c["pattern_type"]
            for c in cands
            if datetime.fromisoformat(c["pattern_ts"]) < ts
        }

    early = types_before(break_ts)
    assert "BID_WALL_TESTED" not in early
    assert "BID_WALL_TRADED_THROUGH" not in early
    assert "BID_WALL_CONFIRMED_BREAK" not in early
    assert "BID_WALL_FAILURE_CANDIDATE" not in early

    at_or_after = {
        c["pattern_type"]
        for c in cands
        if datetime.fromisoformat(c["pattern_ts"]) >= break_ts.replace(second=0)
    }
    # bucket_end for 16:45:00 covers transition at 16:44:29
    assert "BID_WALL_TESTED" in at_or_after
    assert "BID_WALL_TRADED_THROUGH" in at_or_after
    assert "BID_WALL_CONFIRMED_BREAK" in at_or_after
    assert "BID_WALL_FAILURE_CANDIDATE" in at_or_after
    assert "BID_WALL_BREAK_WITH_SELL_PRESSURE" in at_or_after

    # causal sample timestamps
    for c in cands:
        if c.get("source_transition_ts"):
            assert datetime.fromisoformat(c["source_transition_ts"]) <= datetime.fromisoformat(
                c["pattern_ts"]
            )
        assert c["is_trading_signal"] is False

    assert len(feats) == len(cands)
    for f in feats:
        keys = {k.lower() for k in f}
        for banned in ("target", "win", "loss", "label_profitable"):
            assert banned not in keys
        assert not any(k.startswith(("return_after", "mfe_", "mae_", "future_")) for k in keys)


def test_no_future_buckets_in_rolling() -> None:
    rows = [
        _bar(TS0 + timedelta(minutes=1), open_p=1.0, close_p=1.0, delta=0, total=100),
        _bar(TS0 + timedelta(minutes=2), open_p=1.0, close_p=1.1, delta=50, total=100),
        _bar(TS0 + timedelta(minutes=3), open_p=1.1, close_p=2.0, delta=50, total=100),  # huge jump later
    ]
    # Only use first two bars via window semantics inside detector; rolling at bar2
    # should not see bar3 close=2.0
    window = rows[:2]
    roll = compute_rolling_features(window, lookback=5)
    assert abs(roll["rolling_price_change_pct"] - 10.0) < 1e-9


def test_wall_sample_after_pattern_ts_ignored() -> None:
    end = TS0 + timedelta(minutes=1)
    row = _bar(end)
    row["wall_sample_ts"] = (end + timedelta(minutes=5)).isoformat()
    trs = [_tr(TS0 + timedelta(seconds=10), ttype="APPEARED")]
    cands, _, _, errs = _run([row], trs)
    # APPEARED may be skipped due to lookahead, or logged as error
    assert all(
        datetime.fromisoformat(c["source_wall_sample_ts"]) <= datetime.fromisoformat(c["pattern_ts"])
        for c in cands
        if c.get("source_wall_sample_ts")
    )
    assert any(e["error_type"] == "LOOKAHEAD_WALL_SAMPLE" for e in errs) or not any(
        c["pattern_type"] == "BID_WALL_APPEARED" for c in cands
    )


def test_gap_resets_state() -> None:
    seg = _seg(segment_end_ts=TS0 + timedelta(minutes=20))
    gap = ReplayGap(
        gap_id="G1",
        symbol="APTUSDT",
        gap_start_ts=TS0 + timedelta(minutes=3),
        gap_end_ts=TS0 + timedelta(minutes=5),
        previous_update_id=1,
        next_update_id=2,
        missing_update_count=1,
        previous_cross_sequence=1,
        next_cross_sequence=2,
        next_message_type="snapshot",
        next_snapshot_complete=True,
        recovered_at_snapshot_ts=TS0 + timedelta(minutes=5),
        discarded_duration_sec=120,
        reason="gap",
    )
    rows = [
        _bar(TS0 + timedelta(minutes=1)),
        _bar(TS0 + timedelta(minutes=2)),
        _bar(TS0 + timedelta(minutes=4)),  # in gap
        _bar(TS0 + timedelta(minutes=6), open_p=1.0, close_p=0.9, delta=500, total=1000),
    ]
    trs = [
        _tr(TS0 + timedelta(seconds=30), ttype="APPEARED"),
        _tr(TS0 + timedelta(minutes=1, seconds=30), ttype="GREW", notional=70000, prev_notional=50000),
    ]
    cands, _, _, _ = _run(rows, trs, segment=seg, gaps=[gap])
    # After gap, pre-gap wall state must not persist → no persistent/grew from old wall at min6
    post = [
        c
        for c in cands
        if datetime.fromisoformat(c["pattern_ts"]) >= TS0 + timedelta(minutes=6)
    ]
    assert not any(c["pattern_type"] == "BID_WALL_GREW" for c in post)
    assert not any(
        c["pattern_type"] == "BID_WALL_PERSISTENT" and c.get("source_wall_sequence_id") == "WS001"
        for c in post
    )


def test_segment_switch_resets_rolling() -> None:
    seg1 = _seg(segment_id="S0001", segment_end_ts=TS0 + timedelta(minutes=5))
    seg2 = _seg(
        segment_id="S0002",
        segment_start_ts=TS0 + timedelta(minutes=10),
        segment_end_ts=TS0 + timedelta(minutes=20),
    )
    rows1 = [
        _bar(TS0 + timedelta(minutes=i + 1), segment_id="S0001", open_p=1.0, close_p=0.9, delta=500, total=1000)
        for i in range(3)
    ]
    rows2 = [
        _bar(
            TS0 + timedelta(minutes=11 + i),
            segment_id="S0002",
            open_p=1.0,
            close_p=1.0,
            delta=0,
            total=1000,
        )
        for i in range(3)
    ]
    c1, _, _, _ = _run(rows1, [], segment=seg1)
    c2, _, _, _ = _run(rows2, [], segment=seg2)
    # Seg2 flat should not inherit seg1 down move for PRICE_DOWN
    assert not any(c["pattern_type"] == "PRICE_DOWN_DELTA_POSITIVE" for c in c2)


def test_dedupe_cooldown_and_once_per_sequence() -> None:
    rows = []
    for i in range(8):
        rows.append(_bar(TS0 + timedelta(minutes=i + 1), imb=0.8))
    trs = [
        _tr(TS0 + timedelta(seconds=10), ttype="APPEARED"),
        _tr(TS0 + timedelta(minutes=1, seconds=10), ttype="TESTED"),
        # duplicate TESTED same sequence later should not re-emit
        _tr(TS0 + timedelta(minutes=4, seconds=10), ttype="TESTED"),
    ]
    params = PatternParams(cooldown_bars=3, wall_imbalance_threshold=0.5, min_wall_age_sec=9999)
    cands, _, _, _ = _run(rows, trs, params=params)
    tested = [c for c in cands if c["pattern_type"] == "BID_WALL_TESTED"]
    assert len(tested) == 1
    dom = [c for c in cands if c["pattern_type"] == "BID_WALL_DOMINANCE"]
    # first + after cooldown (not every bar)
    assert 1 <= len(dom) <= 3
    assert len(dom) < len(rows)


def test_new_sequence_new_candidate() -> None:
    rows = [_bar(TS0 + timedelta(minutes=1)), _bar(TS0 + timedelta(minutes=2))]
    trs = [
        _tr(TS0 + timedelta(seconds=10), ttype="APPEARED", seq="A"),
        _tr(TS0 + timedelta(minutes=1, seconds=10), ttype="APPEARED", seq="B"),
    ]
    cands, _, _, _ = _run(rows, trs)
    appeared = [c for c in cands if c["pattern_type"] == "BID_WALL_APPEARED"]
    assert len(appeared) == 2
    assert {c["source_wall_sequence_id"] for c in appeared} == {"A", "B"}


def test_absorption_and_failure_candidates() -> None:
    appear = TS0 + timedelta(seconds=10)
    rows = []
    for i in range(6):
        end = TS0 + timedelta(minutes=i + 1)
        rows.append(
            _bar(
                end,
                open_p=1.0,
                close_p=1.0,
                delta=-500,
                total=1000,
            )
        )
    # persistent bid + sell pressure + flat price → absorption
    trs = [
        _tr(appear, ttype="APPEARED"),
        _tr(appear + timedelta(minutes=1), ttype="PERSISTED"),
        _tr(appear + timedelta(minutes=2), ttype="PERSISTED"),
        _tr(appear + timedelta(minutes=3), ttype="PERSISTED"),
    ]
    params = PatternParams(min_wall_age_sec=120, min_wall_samples=2, cooldown_bars=10)
    cands, _, _, _ = _run(rows, trs, params=params)
    abs_c = [c for c in cands if c["pattern_type"] == "BID_ABSORPTION_CANDIDATE"]
    assert abs_c
    assert "not proven" in abs_c[0]["candidate_reason"].lower()

    # ask absorption
    trs_ask = [
        _tr(appear, ttype="APPEARED", side="ask", seq="ASK1", price=0.64),
        _tr(appear + timedelta(minutes=1), ttype="PERSISTED", side="ask", seq="ASK1", price=0.64),
        _tr(appear + timedelta(minutes=2), ttype="PERSISTED", side="ask", seq="ASK1", price=0.64),
    ]
    rows_ask = [
        _bar(TS0 + timedelta(minutes=i + 1), open_p=1.0, close_p=1.0, delta=500, total=1000)
        for i in range(6)
    ]
    cands_a, _, _, _ = _run(rows_ask, trs_ask, params=params)
    assert any(c["pattern_type"] == "ASK_ABSORPTION_CANDIDATE" for c in cands_a)

    # failure on break
    trs_fail = trs + [_tr(TS0 + timedelta(minutes=5, seconds=10), ttype="BROKEN")]
    cands_f, _, _, _ = _run(rows, trs_fail, params=params)
    assert any(c["pattern_type"] == "BID_WALL_FAILURE_CANDIDATE" for c in cands_f)

    trs_ask_fail = trs_ask + [
        _tr(TS0 + timedelta(minutes=5, seconds=10), ttype="BROKEN", side="ask", seq="ASK1", price=0.64)
    ]
    cands_af, _, _, _ = _run(rows_ask, trs_ask_fail, params=params)
    assert any(c["pattern_type"] == "ASK_WALL_FAILURE_CANDIDATE" for c in cands_af)


def test_wall_test_with_liquidations() -> None:
    rows = [
        _bar(TS0 + timedelta(minutes=1), liq=1000, buy_liq=100, sell_liq=900),
    ]
    trs = [
        _tr(TS0 + timedelta(seconds=10), ttype="APPEARED"),
        _tr(TS0 + timedelta(seconds=40), ttype="TESTED"),
    ]
    cands, _, _, _ = _run(rows, trs)
    assert "WALL_TEST_WITH_LIQUIDATIONS" in {c["pattern_type"] for c in cands}


def test_outputs_no_outcomes_and_summaries() -> None:
    rows = [_bar(TS0 + timedelta(minutes=i + 1), imb=0.7) for i in range(3)]
    trs = [_tr(TS0 + timedelta(seconds=10), ttype="APPEARED")]
    seg = _seg()
    result = run_pattern_candidates(
        symbol="APTUSDT",
        segments=[seg],
        gaps=[],
        timelines_with_walls={"1m": rows},
        transitions=trs,
        params=PatternParams(cooldown_bars=3),
    )
    assert result.ok
    assert len(result.candidates) == len(result.features)
    assert all(c["is_trading_signal"] is False for c in result.candidates)
    for row in result.candidates + result.features:
        for k in row:
            sk = k.lower()
            assert "target" != sk and "win" != sk and "loss" != sk
            assert not sk.startswith(("return_after", "mfe_", "mae_", "future_", "max_profit_", "max_adverse_"))
    assert result.summary_by_symbol
    assert result.summary_by_type
    total = sum(int(r["candidate_count"]) for r in result.summary_by_type)
    assert total == len(result.candidates)
    # empty headers still defined
    assert "pattern_id" in CANDIDATE_HEADERS
    assert "pattern_id" in FEATURE_HEADERS
    assert "is_trading_signal" in CANDIDATE_HEADERS


def test_empty_outputs_and_integrity_file_shape() -> None:
    seg = _seg()
    result = run_pattern_candidates(
        symbol="APTUSDT",
        segments=[seg],
        gaps=[],
        timelines_with_walls={"1m": []},
        transitions=[],
        params=PatternParams(),
    )
    assert result.candidates == []
    assert result.features == []
    assert isinstance(result.integrity_errors, list)
    integ = check_pattern_integrity(
        candidates=[], features=[], segments=[seg], gaps=[]
    )
    assert integ["ok"] is True


def test_decide_phase5() -> None:
    assert (
        decide_phase5_patterns(ok=True, gap_count=0, has_failures=False, has_success=True)
        == "FULL_HISTORY_PATTERN_CANDIDATES_COMPLETE"
    )
    assert (
        decide_phase5_patterns(ok=True, gap_count=2, has_failures=False, has_success=True)
        == "FULL_HISTORY_PATTERN_CANDIDATES_COMPLETE_WITH_GAPS"
    )
    assert (
        decide_phase5_patterns(ok=True, gap_count=0, has_failures=True, has_success=True)
        == "FULL_HISTORY_PATTERN_CANDIDATES_PARTIAL"
    )
    assert (
        decide_phase5_patterns(ok=False, gap_count=0, has_failures=False, has_success=False)
        == "FULL_HISTORY_PATTERN_CANDIDATES_FAILED"
    )


def test_final_sequence_flags_not_used_retroactively() -> None:
    """Timeline may carry final was_broken; transitions as-of must gate TESTED/BROKEN."""
    rows = []
    for i in range(5):
        end = TS0 + timedelta(minutes=i + 1)
        row = _bar(end)
        # poisoned future flags on early rows (must be ignored)
        row["nearest_bid_wall_tested"] = True
        row["nearest_bid_wall_broken"] = True
        rows.append(row)
    trs = [_tr(TS0 + timedelta(seconds=10), ttype="APPEARED")]
    # no TESTED/BROKEN transitions
    cands, _, _, _ = _run(rows, trs)
    types = {c["pattern_type"] for c in cands}
    assert "BID_WALL_TESTED" not in types
    assert "BID_WALL_CONFIRMED_BREAK" not in types
    assert "BID_WALL_FAILURE_CANDIDATE" not in types


def test_parse_args_phase5_and_defaults_unchanged() -> None:
    base = parse_args(["--symbol", "VANRYUSDT"])
    assert base.run_pattern_candidates is False
    assert base.run_wall_history is False
    args = parse_args(
        [
            "--symbol",
            "APTUSDT",
            "--run-pattern-candidates",
            "--pattern-timeframe",
            "1m",
            "--pattern-lookback-bars",
            "5",
            "--pattern-cooldown-bars",
            "3",
            "--pattern-wall-imbalance-threshold",
            "0.5",
        ]
    )
    assert args.run_pattern_candidates is True
    assert args.pattern_timeframe == "1m"
    assert args.pattern_lookback_bars == 5
    assert args.pattern_cooldown_bars == 3


def test_render_report_phase5_section() -> None:
    report = render_report(
        decision="FULL_HISTORY_PATTERN_CANDIDATES_COMPLETE",
        symbol="APTUSDT",
        analysis_start=TS0,
        analysis_end=TS0 + timedelta(hours=1),
        inventory=[],
        seg=discover_replay_segments([], symbol="APTUSDT"),
        quality=[],
        health={},
        coverage_pct=0.0,
        limitations=["phase5"],
        pattern_stats={
            "pattern_candidates_ok": True,
            "pattern_timeframe": "1m",
            "pattern_lookback_bars": 5,
            "pattern_candidate_count": 12,
            "pattern_type_count": 4,
            "pattern_family_count": 3,
            "pattern_segments_count": 1,
            "pattern_data_complete_count": 10,
            "pattern_data_incomplete_count": 2,
            "pattern_wall_lifecycle_count": 5,
            "pattern_price_delta_count": 2,
            "pattern_price_oi_count": 1,
            "pattern_wall_flow_count": 1,
            "pattern_liquidation_count": 1,
            "pattern_absorption_candidate_count": 1,
            "pattern_wall_failure_candidate_count": 1,
            "pattern_integrity_error_count": 0,
            "pattern_runtime_sec": 0.01,
            "top_pattern_types": [{"pattern_type": "BID_WALL_TESTED", "count": 3}],
        },
    )
    assert "Phase 5" in report
    assert "No forward outcomes" in report
    assert "Absorption is not proven" in report


def test_duplicate_grew_same_event_emits_once() -> None:
    """Same GREW event reaching emit twice (duplicate transition rows) → one candidate."""
    end = TS0 + timedelta(minutes=1)
    rows = [_bar(end)]
    grew_ts = TS0 + timedelta(seconds=30)
    # Identical GREW twice + trailing PERSISTED (mirrors APT ASK_WALL_GREW finding)
    trs = [
        _tr(grew_ts, ttype="APPEARED", side="ask", seq="APTUSDT:S0005:ASK:W000214", price=0.633),
        _tr(
            grew_ts + timedelta(seconds=10),
            ttype="GREW",
            side="ask",
            seq="APTUSDT:S0005:ASK:W000214",
            price=0.633,
            notional=60000,
            prev_notional=40000,
        ),
        _tr(
            grew_ts + timedelta(seconds=10),
            ttype="GREW",
            side="ask",
            seq="APTUSDT:S0005:ASK:W000214",
            price=0.633,
            notional=60000,
            prev_notional=40000,
        ),
        _tr(
            grew_ts + timedelta(seconds=19),
            ttype="PERSISTED",
            side="ask",
            seq="APTUSDT:S0005:ASK:W000214",
            price=0.633,
        ),
    ]
    cands, feats, _, _ = _run(rows, trs)
    grew = [c for c in cands if c["pattern_type"] == "ASK_WALL_GREW"]
    assert len(grew) == 1
    assert grew[0]["source_wall_sequence_id"] == "APTUSDT:S0005:ASK:W000214"
    assert grew[0]["source_transition_type"] == "GREW"
    assert len(feats) == len(cands)
    ids = [c["pattern_id"] for c in cands]
    assert len(ids) == len(set(ids))


def test_ask_wall_grew_same_bucket_same_transition_one_row() -> None:
    end = datetime(2026, 7, 27, 9, 36, 0, tzinfo=timezone.utc)
    seg = _seg(
        segment_id="S0005",
        segment_start_ts=end - timedelta(minutes=10),
        segment_end_ts=end + timedelta(minutes=10),
    )
    rows = [_bar(end, segment_id="S0005")]
    seq = "APTUSDT:S0005:ASK:W000214"
    trs = [
        _tr(end - timedelta(minutes=2), ttype="APPEARED", side="ask", seq=seq, price=0.633, segment_id="S0005"),
        _tr(
            datetime(2026, 7, 27, 9, 35, 49, 16000, tzinfo=timezone.utc),
            ttype="GREW",
            side="ask",
            seq=seq,
            price=0.633,
            notional=60000,
            prev_notional=40000,
            segment_id="S0005",
        ),
        _tr(
            datetime(2026, 7, 27, 9, 35, 49, 16000, tzinfo=timezone.utc),
            ttype="GREW",
            side="ask",
            seq=seq,
            price=0.633,
            notional=60000,
            prev_notional=40000,
            segment_id="S0005",
        ),
    ]
    cands, _, _, _ = _run(rows, trs, segment=seg)
    grew = [c for c in cands if c["pattern_type"] == "ASK_WALL_GREW"]
    assert len(grew) == 1


def test_two_sequences_same_bucket_two_grew() -> None:
    end = TS0 + timedelta(minutes=1)
    rows = [_bar(end)]
    trs = [
        _tr(TS0 + timedelta(seconds=10), ttype="APPEARED", side="ask", seq="A", price=0.64),
        _tr(TS0 + timedelta(seconds=10), ttype="APPEARED", side="ask", seq="B", price=0.65),
        _tr(TS0 + timedelta(seconds=20), ttype="GREW", side="ask", seq="A", price=0.64, notional=60, prev_notional=40),
        _tr(TS0 + timedelta(seconds=20), ttype="GREW", side="ask", seq="B", price=0.65, notional=60, prev_notional=40),
    ]
    cands, _, _, _ = _run(rows, trs)
    grew = [c for c in cands if c["pattern_type"] == "ASK_WALL_GREW"]
    assert len(grew) == 2
    assert {c["source_wall_sequence_id"] for c in grew} == {"A", "B"}


def test_same_sequence_two_grew_different_timestamps() -> None:
    rows = [
        _bar(TS0 + timedelta(minutes=1)),
        _bar(TS0 + timedelta(minutes=2)),
    ]
    trs = [
        _tr(TS0 + timedelta(seconds=10), ttype="APPEARED", seq="WS_G"),
        _tr(TS0 + timedelta(seconds=30), ttype="GREW", seq="WS_G", notional=60, prev_notional=40),
        _tr(TS0 + timedelta(minutes=1, seconds=30), ttype="GREW", seq="WS_G", notional=80, prev_notional=60),
    ]
    cands, _, _, _ = _run(rows, trs)
    grew = [c for c in cands if c["pattern_type"] == "BID_WALL_GREW"]
    assert len(grew) == 2
    assert grew[0]["source_transition_ts"] != grew[1]["source_transition_ts"]


def test_grew_and_persistent_same_bucket_both_allowed() -> None:
    end = TS0 + timedelta(minutes=5)
    rows = [_bar(end)]
    # appear early enough for age>=120 and samples via PERSISTED
    trs = [
        _tr(TS0 + timedelta(seconds=10), ttype="APPEARED", seq="WS_P"),
        _tr(TS0 + timedelta(minutes=1), ttype="PERSISTED", seq="WS_P"),
        _tr(TS0 + timedelta(minutes=2), ttype="PERSISTED", seq="WS_P"),
        _tr(TS0 + timedelta(minutes=4, seconds=30), ttype="GREW", seq="WS_P", notional=70, prev_notional=50),
    ]
    params = PatternParams(min_wall_age_sec=120, min_wall_samples=2, cooldown_bars=10)
    cands, _, _, _ = _run(rows, trs, params=params)
    types = {c["pattern_type"] for c in cands if c["source_wall_sequence_id"] == "WS_P"}
    assert "BID_WALL_GREW" in types
    assert "BID_WALL_PERSISTENT" in types


def test_tested_and_traded_through_same_ts_both_allowed() -> None:
    end = TS0 + timedelta(minutes=1)
    rows = [_bar(end)]
    ts = TS0 + timedelta(seconds=40)
    trs = [
        _tr(TS0 + timedelta(seconds=10), ttype="APPEARED"),
        _tr(ts, ttype="TESTED"),
        _tr(ts, ttype="TRADED_THROUGH"),
    ]
    cands, _, _, _ = _run(rows, trs)
    types = {c["pattern_type"] for c in cands}
    assert "BID_WALL_TESTED" in types
    assert "BID_WALL_TRADED_THROUGH" in types


def test_tested_duplicate_paths_once() -> None:
    end = TS0 + timedelta(minutes=1)
    rows = [_bar(end)]
    ts = TS0 + timedelta(seconds=40)
    trs = [
        _tr(TS0 + timedelta(seconds=10), ttype="APPEARED"),
        _tr(ts, ttype="TESTED"),
        _tr(ts, ttype="TESTED"),  # duplicate path
    ]
    cands, _, _, _ = _run(rows, trs)
    assert len([c for c in cands if c["pattern_type"] == "BID_WALL_TESTED"]) == 1


def test_confirmed_break_duplicate_once() -> None:
    end = TS0 + timedelta(minutes=1)
    rows = [_bar(end)]
    ts = TS0 + timedelta(seconds=40)
    trs = [
        _tr(TS0 + timedelta(seconds=10), ttype="APPEARED"),
        _tr(ts, ttype="BROKEN"),
        _tr(ts, ttype="BROKEN"),
    ]
    cands, _, _, _ = _run(rows, trs)
    assert len([c for c in cands if c["pattern_type"] == "BID_WALL_CONFIRMED_BREAK"]) == 1


def test_defensive_dedupe_keeps_feature_count_equal() -> None:
    end = TS0 + timedelta(minutes=1)
    rows = [_bar(end)]
    grew = TS0 + timedelta(seconds=20)
    trs = [
        _tr(TS0 + timedelta(seconds=5), ttype="APPEARED", side="ask", seq="DUP"),
        _tr(grew, ttype="GREW", side="ask", seq="DUP", notional=60, prev_notional=40),
        _tr(grew, ttype="GREW", side="ask", seq="DUP", notional=60, prev_notional=40),
    ]
    result = run_pattern_candidates(
        symbol="APTUSDT",
        segments=[_seg()],
        gaps=[],
        timelines_with_walls={"1m": rows},
        transitions=trs,
        params=PatternParams(),
    )
    assert len(result.candidates) == len(result.features)
    assert len([c for c in result.candidates if c["pattern_type"] == "ASK_WALL_GREW"]) == 1


def test_timeline_pattern_ids_unique() -> None:
    end = TS0 + timedelta(minutes=1)
    rows = [_bar(end)]
    grew = TS0 + timedelta(seconds=20)
    trs = [
        _tr(TS0 + timedelta(seconds=5), ttype="APPEARED", side="ask", seq="TL"),
        _tr(grew, ttype="GREW", side="ask", seq="TL", notional=60, prev_notional=40),
        _tr(grew, ttype="GREW", side="ask", seq="TL", notional=60, prev_notional=40),
    ]
    result = run_pattern_candidates(
        symbol="APTUSDT",
        segments=[_seg()],
        gaps=[],
        timelines_with_walls={"1m": rows},
        transitions=trs,
        params=PatternParams(),
    )
    tl = result.timelines["1m"]
    for row in tl:
        ids = [x for x in str(row.get("pattern_ids") or "").split("|") if x]
        assert len(ids) == len(set(ids))
        assert int(row.get("pattern_count") or 0) == len(set(ids))


def test_rerun_deterministic_and_input_order_independent() -> None:
    rows = [
        _bar(TS0 + timedelta(minutes=1)),
        _bar(TS0 + timedelta(minutes=2)),
    ]
    trs = [
        _tr(TS0 + timedelta(seconds=10), ttype="APPEARED", seq="ORD1"),
        _tr(TS0 + timedelta(seconds=40), ttype="GREW", seq="ORD1", notional=60, prev_notional=40),
        _tr(TS0 + timedelta(minutes=1, seconds=10), ttype="TESTED", seq="ORD1"),
    ]
    p = PatternParams(cooldown_bars=3)
    seg = _seg()

    def _ids(transitions, timeline):
        r = run_pattern_candidates(
            symbol="APTUSDT",
            segments=[seg],
            gaps=[],
            timelines_with_walls={"1m": timeline},
            transitions=transitions,
            params=p,
        )
        return [c["pattern_id"] for c in r.candidates]

    a = _ids(trs, rows)
    b = _ids(trs, rows)
    assert a == b
    c = _ids(list(reversed(trs)), list(reversed(rows)))
    assert a == c


def test_integrity_still_flags_manual_duplicate_rows() -> None:
    end = TS0 + timedelta(minutes=1)
    rows = [_bar(end)]
    trs = [_tr(TS0 + timedelta(seconds=10), ttype="APPEARED")]
    cands, feats, _, _ = _run(rows, trs)
    assert cands
    dup_cands = list(cands) + [dict(cands[0])]
    dup_feats = list(feats) + [dict(feats[0])]
    integ = check_pattern_integrity(
        candidates=dup_cands,
        features=dup_feats,
        segments=[_seg()],
        gaps=[],
    )
    assert integ["ok"] is False
    assert any("duplicate pattern_id" in e for e in integ["errors"])

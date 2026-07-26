"""Unit tests for causal liquidation analysis (no live ClickHouse required)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import orjson

from orderbook_analyse.liquidation_analysis import (
    DOWNSIDE_CONTINUATION_AFTER_LIQUIDATION,
    HORIZONS_SEC,
    LIQUIDATED_LONG,
    LIQUIDATED_SHORT,
    LIQUIDATION_ABSORBED,
    LIQUIDATION_BREAKDOWN_ACCELERATION,
    LIQUIDATION_BREAKOUT_ACCELERATION,
    LIQUIDATION_REJECTION,
    LIQUIDATION_SIDE_UNKNOWN,
    LIQUIDATION_THROUGH_ASK,
    LIQUIDATION_THROUGH_BID,
    LiquidationAnalysisParams,
    PRICE_TYPE_BANKRUPTCY,
    ReactionThresholds,
    UPSIDE_CONTINUATION_AFTER_LIQUIDATION,
    book_state_before_event,
    classify_reaction,
    cluster_events,
    compute_notional,
    dedupe_liquidations,
    interpret_liquidation_side,
    liquidation_from_row,
    make_event_key,
    path_stats,
    run_liquidation_analysis,
)
from orderbook_analyse.orderbook_replay import BookLevelEvent


TS0 = datetime(2026, 7, 26, 11, 0, 0, tzinfo=timezone.utc)


def _evt(
    *,
    ts: datetime,
    side: str,
    price: str,
    qty: str,
    msg: str = "delta",
    u: int = 2,
    seq: int = 2,
    idx: int = 0,
) -> BookLevelEvent:
    return BookLevelEvent(
        exchange_ts=ts,
        side=side,
        price=Decimal(price),
        quantity=Decimal(qty),
        message_type=msg,
        update_id=u,
        cross_sequence=seq,
        level_index=idx,
    )


def _snapshot_book(ts: datetime = TS0) -> list[BookLevelEvent]:
    """Simple two-sided book around 0.636."""
    return [
        _evt(ts=ts, side="bid", price="0.635", qty="1000", msg="snapshot", u=1, seq=1, idx=0),
        _evt(ts=ts, side="bid", price="0.634", qty="5000", msg="snapshot", u=1, seq=1, idx=1),
        _evt(ts=ts, side="bid", price="0.630", qty="8000", msg="snapshot", u=1, seq=1, idx=2),
        _evt(ts=ts, side="ask", price="0.637", qty="1000", msg="snapshot", u=1, seq=1, idx=0),
        _evt(ts=ts, side="ask", price="0.638", qty="5000", msg="snapshot", u=1, seq=1, idx=1),
        _evt(ts=ts, side="ask", price="0.642", qty="8000", msg="snapshot", u=1, seq=1, idx=2),
    ]


def _liq(
    *,
    ts: datetime,
    side: str = "Sell",
    price: str = "0.636",
    qty: str = "815.65",
    notional: str | None = None,
    symbol: str = "APTUSDT",
):
    row = {
        "liquidation_ts": ts,
        "received_ts": ts + timedelta(milliseconds=400),
        "symbol": symbol,
        "side": side,
        "price": Decimal(price),
        "quantity": Decimal(qty),
        "notional": None if notional is None else Decimal(notional),
    }
    return liquidation_from_row(row)


def test_exact_liquidation_price_from_source() -> None:
    ev = _liq(ts=TS0, price="0.63600000", qty="815.65000000", notional="518.75340000")
    assert ev.liquidation_price == Decimal("0.63600000")


def test_exact_exchange_timestamp() -> None:
    ts = datetime(2026, 7, 26, 11, 3, 34, 383000, tzinfo=timezone.utc)
    ev = _liq(ts=ts)
    assert ev.exchange_timestamp == ts


def test_notional_price_times_qty_when_missing() -> None:
    assert compute_notional(Decimal("0.636"), Decimal("815.65"), None) == Decimal("0.636") * Decimal(
        "815.65"
    )
    stored = compute_notional(Decimal("1"), Decimal("2"), Decimal("99"))
    assert stored == Decimal("99")


def test_sell_maps_to_liquidated_short() -> None:
    assert interpret_liquidation_side("Sell") == LIQUIDATED_SHORT
    ev = _liq(ts=TS0, side="Sell")
    assert ev.interpreted_position_side == LIQUIDATED_SHORT
    assert ev.price_type == PRICE_TYPE_BANKRUPTCY
    assert ev.bankruptcy_price == Decimal("0.636")


def test_buy_maps_to_liquidated_long() -> None:
    assert interpret_liquidation_side("Buy") == LIQUIDATED_LONG
    ev = _liq(ts=TS0, side="Buy")
    assert ev.interpreted_position_side == LIQUIDATED_LONG


def test_unknown_side_maps_to_unknown() -> None:
    assert interpret_liquidation_side("Nope") == LIQUIDATION_SIDE_UNKNOWN
    assert interpret_liquidation_side("") == LIQUIDATION_SIDE_UNKNOWN
    ev = liquidation_from_row(
        {
            "liquidation_ts": TS0,
            "received_ts": TS0,
            "symbol": "APTUSDT",
            "side": "Maybe",
            "price": Decimal("1"),
            "quantity": Decimal("1"),
            "notional": Decimal("1"),
        }
    )
    assert ev.interpreted_position_side == LIQUIDATION_SIDE_UNKNOWN


def test_p_exported_as_bankruptcy_price() -> None:
    ev = _liq(ts=TS0, price="0.63600000")
    row = ev.to_row()
    assert row["bankruptcy_price"] == "0.63600000"
    assert row["price_type"] == PRICE_TYPE_BANKRUPTCY
    assert row["liquidation_price"] == row["bankruptcy_price"]


def test_deduplication() -> None:
    a = _liq(ts=TS0, side="Sell", price="0.636", qty="10")
    b = _liq(ts=TS0, side="Sell", price="0.636", qty="10")
    c = _liq(ts=TS0 + timedelta(seconds=1), side="Sell", price="0.636", qty="10")
    out = dedupe_liquidations([a, b, c])
    assert len(out) == 2
    assert out[0].event_key == make_event_key(
        "APTUSDT", TS0, "Sell", Decimal("0.636"), Decimal("10")
    )


def test_single_event_cluster() -> None:
    ev = _liq(ts=TS0)
    clusters = cluster_events([ev], window_seconds=60, price_bps=10)
    assert len(clusters) == 1
    assert clusters[0]["event_count"] == 1


def test_two_events_inside_cluster_window() -> None:
    a = _liq(ts=TS0, qty="10")
    b = _liq(ts=TS0 + timedelta(seconds=30), qty="20")
    clusters = cluster_events([a, b], window_seconds=60, price_bps=10)
    assert len(clusters) == 1
    assert clusters[0]["event_count"] == 2


def test_two_events_outside_cluster_window() -> None:
    a = _liq(ts=TS0, qty="10")
    b = _liq(ts=TS0 + timedelta(seconds=90), qty="20")
    clusters = cluster_events([a, b], window_seconds=60, price_bps=10)
    assert len(clusters) == 2


def test_pre_event_state_no_lookahead() -> None:
    events = _snapshot_book(TS0)
    events.append(
        _evt(
            ts=TS0 + timedelta(seconds=10),
            side="ask",
            price="0.637",
            qty="0",
            msg="delta",
            u=2,
            seq=2,
        )
    )
    events.append(
        _evt(
            ts=TS0 + timedelta(seconds=10),
            side="ask",
            price="0.650",
            qty="100",
            msg="delta",
            u=2,
            seq=2,
            idx=1,
        )
    )
    book = book_state_before_event(events, event_ts=TS0 + timedelta(seconds=5), strict=True)
    assert book.best_ask() == Decimal("0.637")
    assert Decimal("0.650") not in book.asks


def _path_from(start: Decimal, moves: list[tuple[int, str]]) -> list[tuple[datetime, Decimal]]:
    path = [(TS0, start)]
    for sec, px in moves:
        path.append((TS0 + timedelta(seconds=sec), Decimal(px)))
    return path


def test_forward_horizons_30s_1m_2m_5m_10m() -> None:
    start = Decimal("1.00")
    path = _path_from(
        start,
        [
            (30, "1.01"),
            (60, "1.02"),
            (120, "1.03"),
            (300, "1.04"),
            (600, "1.05"),
        ],
    )
    expected = {
        30: Decimal("1.01"),
        60: Decimal("1.02"),
        120: Decimal("1.03"),
        300: Decimal("1.04"),
        600: Decimal("1.05"),
    }
    for h in HORIZONS_SEC:
        stats = path_stats(
            path,
            start_ts=TS0,
            end_ts=TS0 + timedelta(seconds=h),
            start_price=start,
        )
        assert stats["end_price"] == expected[h]


def test_upside_continuation() -> None:
    cls = classify_reaction(
        return_pct=0.20,
        mfe_up_pct=0.22,
        mae_down_pct=0.01,
        trade_delta_after=Decimal("100"),
        liquidation_notional=Decimal("50"),
        wall_labels=[],
        bid_floor_change="HIGHER",
        near_ask_change="HIGHER",
        near_ask_notional_before=Decimal("1000"),
        near_ask_notional_after=Decimal("1000"),
        thresholds=ReactionThresholds(),
    )
    assert cls == UPSIDE_CONTINUATION_AFTER_LIQUIDATION


def test_downside_continuation() -> None:
    cls = classify_reaction(
        return_pct=-0.20,
        mfe_up_pct=0.01,
        mae_down_pct=0.22,
        trade_delta_after=Decimal("-100"),
        liquidation_notional=Decimal("50"),
        wall_labels=[],
        bid_floor_change="LOWER",
        near_ask_change="LOWER",
        near_ask_notional_before=Decimal("1000"),
        near_ask_notional_after=Decimal("1000"),
        thresholds=ReactionThresholds(),
    )
    assert cls == DOWNSIDE_CONTINUATION_AFTER_LIQUIDATION


def test_rejection() -> None:
    cls = classify_reaction(
        return_pct=0.02,
        mfe_up_pct=0.20,
        mae_down_pct=0.01,
        trade_delta_after=Decimal("10"),
        liquidation_notional=Decimal("50"),
        wall_labels=[],
        bid_floor_change="STABLE",
        near_ask_change="STABLE",
        near_ask_notional_before=Decimal("1000"),
        near_ask_notional_after=Decimal("1000"),
        thresholds=ReactionThresholds(),
    )
    assert cls == LIQUIDATION_REJECTION


def test_absorption() -> None:
    cls = classify_reaction(
        return_pct=0.01,
        mfe_up_pct=0.02,
        mae_down_pct=0.02,
        trade_delta_after=Decimal("500"),
        liquidation_notional=Decimal("50"),
        wall_labels=[],
        bid_floor_change="STABLE",
        near_ask_change="STABLE",
        near_ask_notional_before=Decimal("1000"),
        near_ask_notional_after=Decimal("1000"),
        thresholds=ReactionThresholds(),
    )
    assert cls == LIQUIDATION_ABSORBED


def test_breakout_acceleration() -> None:
    cls = classify_reaction(
        return_pct=0.15,
        mfe_up_pct=0.18,
        mae_down_pct=0.01,
        trade_delta_after=Decimal("100"),
        liquidation_notional=Decimal("50"),
        wall_labels=[LIQUIDATION_THROUGH_ASK],
        bid_floor_change="HIGHER",
        near_ask_change="HIGHER",
        near_ask_notional_before=Decimal("1000"),
        near_ask_notional_after=Decimal("700"),
        thresholds=ReactionThresholds(),
    )
    assert cls == LIQUIDATION_BREAKOUT_ACCELERATION


def test_breakdown_acceleration() -> None:
    cls = classify_reaction(
        return_pct=-0.15,
        mfe_up_pct=0.01,
        mae_down_pct=0.18,
        trade_delta_after=Decimal("-100"),
        liquidation_notional=Decimal("50"),
        wall_labels=[LIQUIDATION_THROUGH_BID],
        bid_floor_change="LOWER",
        near_ask_change="LOWER",
        near_ask_notional_before=Decimal("1000"),
        near_ask_notional_after=Decimal("1000"),
        thresholds=ReactionThresholds(),
    )
    assert cls == LIQUIDATION_BREAKDOWN_ACCELERATION


def test_deterministic_outputs(tmp_path: Path) -> None:
    events = _snapshot_book(TS0)
    u = 2
    for i, sec in enumerate(range(30, 661, 30), start=0):
        events.append(
            _evt(
                ts=TS0 + timedelta(seconds=sec),
                side="bid",
                price="0.635",
                qty=str(1000 + i),
                msg="delta",
                u=u,
                seq=u,
            )
        )
        u += 1
    liq = _liq(ts=TS0 + timedelta(seconds=5), price="0.636", qty="815.65", notional="518.7534")
    params = LiquidationAnalysisParams(sample_seconds=30)
    price_path = [
        (TS0 + timedelta(seconds=s), Decimal("0.636") + Decimal(s) / Decimal("100000"))
        for s in range(0, 601, 10)
    ]

    def td(_a: datetime, _b: datetime) -> Decimal:
        return Decimal("10")

    def oi(_a: datetime, _b: datetime) -> Decimal | None:
        return Decimal("0")

    out1 = tmp_path / "a"
    out2 = tmp_path / "b"
    s1 = run_liquidation_analysis(
        db=None,
        symbol="APTUSDT",
        start=TS0,
        end=TS0 + timedelta(minutes=15),
        params=params,
        output_dir=out1,
        book_events=events,
        liquidation_events=[liq],
        price_path=price_path,
        trade_delta_fn=td,
        oi_change_fn=oi,
    )
    s2 = run_liquidation_analysis(
        db=None,
        symbol="APTUSDT",
        start=TS0,
        end=TS0 + timedelta(minutes=15),
        params=params,
        output_dir=out2,
        book_events=events,
        liquidation_events=[liq],
        price_path=price_path,
        trade_delta_fn=td,
        oi_change_fn=oi,
    )
    assert s1["event_count"] == 1
    assert s2["event_count"] == 1
    j1 = orjson.loads((out1 / "liquidation_summary.json").read_bytes())
    j2 = orjson.loads((out2 / "liquidation_summary.json").read_bytes())
    assert j1["events"] == j2["events"]
    assert (out1 / "liquidation_events.csv").read_text() == (out2 / "liquidation_events.csv").read_text()
    assert (out1 / "liquidation_forward_outcomes.csv").read_text() == (
        out2 / "liquidation_forward_outcomes.csv"
    ).read_text()


def test_bankruptcy_distance_from_mid() -> None:
    mid = Decimal("0.62965")
    bp = Decimal("0.636")
    dist = float((bp - mid) / mid * 100)
    assert round(dist, 6) == round(1.008497, 6)
    assert bp - mid == Decimal("0.00635")


def test_forward_uses_mid_not_bankruptcy(tmp_path: Path) -> None:
    """Reaction start_price must be mid_before, not bankruptcy_price."""
    events = _snapshot_book(TS0)
    u = 2
    for i, sec in enumerate(range(30, 661, 30), start=0):
        events.append(
            _evt(
                ts=TS0 + timedelta(seconds=sec),
                side="bid",
                price="0.635",
                qty=str(1000 + i),
                msg="delta",
                u=u,
                seq=u,
            )
        )
        u += 1
    # Bankruptcy far above mid (~0.636 book mid)
    liq = _liq(ts=TS0 + timedelta(seconds=5), price="0.650", qty="10", notional="6.5")
    # Market path stays near mid
    price_path = [(TS0 + timedelta(seconds=s), Decimal("0.636")) for s in range(0, 601, 10)]

    summary = run_liquidation_analysis(
        db=None,
        symbol="APTUSDT",
        start=TS0,
        end=TS0 + timedelta(minutes=15),
        params=LiquidationAnalysisParams(sample_seconds=30),
        output_dir=tmp_path / "fwd",
        book_events=events,
        liquidation_events=[liq],
        price_path=price_path,
        trade_delta_fn=lambda _a, _b: Decimal("0"),
        oi_change_fn=lambda _a, _b: Decimal("0"),
    )
    ev = summary["events"][0]
    assert ev["bankruptcy_price"] == "0.650"
    assert ev["price_type"] == PRICE_TYPE_BANKRUPTCY
    assert Decimal(ev["mid_before"]) != Decimal(ev["bankruptcy_price"])
    csv_text = (tmp_path / "fwd" / "liquidation_forward_outcomes.csv").read_text()
    import csv
    from io import StringIO

    reader = csv.DictReader(StringIO(csv_text))
    for row in reader:
        assert row["start_price"] == ev["mid_before"]
        assert row["start_price"] != ev["bankruptcy_price"]
        assert row["start_price_basis"] == "mid_before"
    assert summary["liquidated_short_event_count"] == 1
    assert summary["liquidated_long_event_count"] == 0


def test_summary_counts_long_and_short(tmp_path: Path) -> None:
    events = _snapshot_book(TS0)
    u = 2
    for i, sec in enumerate(range(30, 120, 30), start=0):
        events.append(
            _evt(
                ts=TS0 + timedelta(seconds=sec),
                side="bid",
                price="0.635",
                qty=str(1000 + i),
                msg="delta",
                u=u,
                seq=u,
            )
        )
        u += 1
    buy = _liq(ts=TS0 + timedelta(seconds=5), side="Buy", price="0.636", qty="10", notional="6.36")
    sell = _liq(ts=TS0 + timedelta(seconds=70), side="Sell", price="0.636", qty="20", notional="12.72")
    summary = run_liquidation_analysis(
        db=None,
        symbol="APTUSDT",
        start=TS0,
        end=TS0 + timedelta(minutes=5),
        params=LiquidationAnalysisParams(sample_seconds=30, cluster_window_seconds=1),
        output_dir=tmp_path / "counts",
        book_events=events,
        liquidation_events=[buy, sell],
        price_path=[(TS0 + timedelta(seconds=s), Decimal("0.636")) for s in range(0, 301, 10)],
        trade_delta_fn=lambda _a, _b: Decimal("0"),
        oi_change_fn=lambda _a, _b: None,
    )
    assert summary["liquidated_long_event_count"] == 1
    assert summary["liquidated_short_event_count"] == 1
    assert summary["unknown_side_event_count"] == 0
    assert summary["liquidated_long_notional"] == "6.36"
    assert summary["liquidated_short_notional"] == "12.72"


def test_known_sell_event_is_liquidated_short() -> None:
    ts = datetime(2026, 7, 26, 11, 3, 34, 383000, tzinfo=timezone.utc)
    ev = _liq(ts=ts, side="Sell", price="0.636", qty="815.65", notional="518.7534")
    assert ev.exchange_timestamp == ts
    assert ev.raw_side == "Sell"
    assert ev.interpreted_position_side == LIQUIDATED_SHORT
    assert ev.bankruptcy_price == Decimal("0.636")
    assert ev.price_type == PRICE_TYPE_BANKRUPTCY
    assert ev.liquidation_qty == Decimal("815.65")
    assert ev.liquidation_notional == Decimal("518.7534")

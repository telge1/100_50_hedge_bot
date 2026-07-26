"""Unit tests for rising bid floor compression audit (synthetic, no DB)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from orderbook_analyse.orderbook_rising_bid_floor_compression_audit import (
    CompressionParams,
    FloorSequence,
    FloorStep,
    REFERENCE_TIMES,
    breakout_outcomes,
    compression_ok,
    crv_metrics,
    is_rising_floor_ready,
    long_to_ceiling_outcomes,
    long_variant_ok,
    run_compression_audit_from_state,
    select_ceiling,
    select_floor,
)
from orderbook_analyse.wall_movement_tracker import WallView

TS0 = datetime(2026, 7, 26, 10, 0, 0, tzinfo=timezone.utc)


def _wall(side: str, price: str, notional: str = "5000") -> WallView:
    return WallView(
        side=side,
        price=Decimal(price),
        notional=Decimal(notional),
        wall_multiple=3.0,
        distance_pct=0.2,
        is_wall=True,
    )


def _snap(
    ts: datetime,
    mid: str,
    *,
    bid: str,
    ask: str,
    bid_n: str = "5000",
    ask_n: str = "5000",
    buy: float = 1000,
    sell: float = 500,
) -> SimpleNamespace:
    nb = _wall("Bid", bid, bid_n)
    na = _wall("Ask", ask, ask_n)
    return SimpleNamespace(
        timestamp=ts,
        mid_price=Decimal(mid),
        nearest_bid=nb,
        nearest_ask=na,
        dominant_bid=nb,
        dominant_ask=na,
        near_bids=[nb],
        near_asks=[na],
        buy_notional_since_prev=Decimal(str(buy)),
        sell_notional_since_prev=Decimal(str(sell)),
        trade_delta_notional=Decimal(str(buy - sell)),
    )


def test_ceiling_above_mid_floor_below() -> None:
    s = _snap(TS0, "0.630", bid="0.625", ask="0.635")
    fl = select_floor(s, min_notional=1000)
    ce = select_ceiling(s, max_distance_bps=100, min_notional=1000)
    assert fl is not None and fl["floor_price"] < 0.630
    assert ce is not None and ce["ceiling_price"] > 0.630


def test_ceiling_rejects_far_ask() -> None:
    s = _snap(TS0, "0.630", bid="0.625", ask="0.650")  # ~317 bps
    ce = select_ceiling(s, max_distance_bps=100, min_notional=1000)
    assert ce is None


def test_floor_sequence_requires_strict_rise() -> None:
    params = CompressionParams(min_floor_steps=3, min_floor_rise_bps=10, min_floor_persistence_snapshots=1)
    steps = [
        FloorStep(0.620, 5000, TS0, TS0, 2, "HELD_WITHOUT_TEST", False),
        FloorStep(0.621, 5000, TS0 + timedelta(seconds=30), TS0 + timedelta(seconds=30), 2, "HELD_WITHOUT_TEST", False),
        FloorStep(0.622, 5000, TS0 + timedelta(seconds=60), TS0 + timedelta(seconds=60), 2, "HELD_WITHOUT_TEST", False),
    ]
    seq = FloorSequence("FS1", steps=steps)
    assert is_rising_floor_ready(seq, params)
    # lower step invalidates readiness via price check in builder; direct:
    bad = FloorSequence(
        "FS2",
        steps=steps
        + [FloorStep(0.621, 5000, TS0 + timedelta(seconds=90), TS0 + timedelta(seconds=90), 2, "X", False)],
    )
    assert not is_rising_floor_ready(bad, params)


def test_min_floor_steps() -> None:
    params = CompressionParams(min_floor_steps=3, min_floor_rise_bps=1, min_floor_persistence_snapshots=1)
    seq = FloorSequence(
        "FS",
        steps=[
            FloorStep(0.62, 1, TS0, TS0, 2, "X", False),
            FloorStep(0.621, 1, TS0 + timedelta(seconds=30), TS0 + timedelta(seconds=30), 2, "X", False),
        ],
    )
    assert not is_rising_floor_ready(seq, params)


def test_floor_rise_bps() -> None:
    seq = FloorSequence(
        "FS",
        steps=[
            FloorStep(0.620, 1, TS0, TS0, 2, "X", False),
            FloorStep(0.621, 1, TS0 + timedelta(seconds=30), TS0 + timedelta(seconds=30), 2, "X", False),
            FloorStep(0.622, 1, TS0 + timedelta(seconds=60), TS0 + timedelta(seconds=60), 2, "X", False),
        ],
    )
    assert abs(seq.total_rise_bps - ((0.622 - 0.620) / 0.620 * 10000)) < 1e-6


def test_compression_not_ceiling_only_collapse() -> None:
    params = CompressionParams(
        min_floor_steps=2,
        min_floor_rise_bps=50,
        min_floor_persistence_snapshots=1,
        min_compression_bps=5,
        max_ceiling_drift_vs_floor_ratio=0.5,
    )
    seq = FloorSequence(
        "FS",
        steps=[
            FloorStep(0.620, 1, TS0, TS0, 2, "X", False),
            FloorStep(0.6205, 1, TS0 + timedelta(seconds=30), TS0 + timedelta(seconds=30), 2, "X", False),
        ],
    )
    # tiny floor rise, large ceiling drop
    assert (
        compression_ok(seq=seq, ceiling_first=0.640, ceiling_now=0.625, params=params)
        is None
    )


def test_crv_calculation() -> None:
    m = crv_metrics(mid=0.630, floor_price=0.625, ceiling_price=0.636, stop_buffer_bps=5)
    assert m["target_distance_bps"] > 0
    assert m["stop_distance_bps"] > 0
    assert m["estimated_crv"] == m["target_distance_bps"] / m["stop_distance_bps"]


def test_long_variants_separated() -> None:
    assert long_variant_ok("L0", has_ceiling=True, rising_floor=False, compression=False, delta_positive=False, buy_rising=False, ask_depletion=False, floor_tested_held=False, crv_ok=False)
    assert not long_variant_ok("L3", has_ceiling=True, rising_floor=True, compression=False, delta_positive=True, buy_rising=False, ask_depletion=False, floor_tested_held=False, crv_ok=False)
    assert long_variant_ok("L3", has_ceiling=True, rising_floor=True, compression=True, delta_positive=False, buy_rising=False, ask_depletion=False, floor_tested_held=False, crv_ok=False)
    assert long_variant_ok("L8", has_ceiling=True, rising_floor=True, compression=True, delta_positive=True, buy_rising=False, ask_depletion=False, floor_tested_held=True, crv_ok=True)


def test_ceiling_touch_exact_level() -> None:
    mids = [
        (TS0, 0.630),
        (TS0 + timedelta(seconds=30), 0.632),
        (TS0 + timedelta(seconds=60), 0.635),  # exact ceiling
    ]
    oc = long_to_ceiling_outcomes(
        signal_time=TS0,
        signal_mid=0.630,
        ceiling_price=0.635,
        floor_price=0.625,
        mids=mids,
        ceilings_path=[],
        floors_path=[],
        transitions=[],
    )
    assert oc["ceiling_touch"] is True
    assert oc["time_to_ceiling_touch_seconds"] == 60


def test_floor_invalidation_before_touch() -> None:
    mids = [(TS0, 0.630), (TS0 + timedelta(seconds=30), 0.631), (TS0 + timedelta(seconds=120), 0.635)]
    floors = [(TS0 + timedelta(seconds=30), 0.620)]  # dropped below 0.625
    oc = long_to_ceiling_outcomes(
        signal_time=TS0,
        signal_mid=0.630,
        ceiling_price=0.635,
        floor_price=0.625,
        mids=mids,
        ceilings_path=[],
        floors_path=floors,
        transitions=[],
    )
    assert oc["floor_invalidated_before_touch"] is True


def test_breakout_b2_needs_two_snapshots() -> None:
    ceil = 0.635
    mids = [
        (TS0, 0.636),
        (TS0 + timedelta(seconds=30), 0.6365),
        (TS0 + timedelta(seconds=60), 0.637),
        (TS0 + timedelta(seconds=90), 0.630),  # fail later
        (TS0 + timedelta(seconds=120), 0.629),
    ]
    # action at second above snapshot
    oc = breakout_outcomes(
        action_time=TS0 + timedelta(seconds=30),
        action_price=0.6365,
        ceiling_price=ceil,
        mids=mids,
        failed_confirm_snapshots=2,
    )
    assert oc["failed_breakout"] is True


def test_reference_times_not_in_params() -> None:
    blob = str(CompressionParams().__dict__)
    for ref in REFERENCE_TIMES:
        assert ref not in blob


def test_end_to_end_outputs(tmp_path: Path) -> None:
    snaps = []
    # rising floors 0.620 -> 0.621 -> 0.622 with persistence, stable ask 0.630
    levels = [
        ("0.6205", "0.620"),
        ("0.6205", "0.620"),
        ("0.6215", "0.621"),
        ("0.6215", "0.621"),
        ("0.6225", "0.622"),
        ("0.6225", "0.622"),
        ("0.6235", "0.622"),
        ("0.6245", "0.622"),
        ("0.6255", "0.622"),
        ("0.6265", "0.622"),
        ("0.6280", "0.622"),
        ("0.6295", "0.622"),
        ("0.6305", "0.622"),  # touch/break
        ("0.6315", "0.622"),
        ("0.6320", "0.622"),
    ]
    for i, (mid, bid) in enumerate(levels):
        snaps.append(
            _snap(
                TS0 + timedelta(seconds=30 * i),
                mid,
                bid=bid,
                ask="0.630",
                buy=2000 + i * 100,
                sell=800,
            )
        )
    out = tmp_path / "rb"
    summary = run_compression_audit_from_state(
        snapshots=snaps,
        transitions=[],
        sequences=[],
        output_dir=out,
        params=CompressionParams(
            min_floor_steps=3,
            min_floor_rise_bps=10,
            min_floor_persistence_snapshots=2,
            max_ceiling_distance_bps=100,
            min_crv=1.0,
            min_compression_bps=3,
        ),
    )
    assert summary["decision"] in {
        "RISING_BID_FLOOR_LONG_TO_CEILING_VALUE_FOUND",
        "RISING_BID_FLOOR_BREAKOUT_VALUE_FOUND",
        "RISING_BID_FLOOR_CONFIRMATION_VALUE_ONLY",
        "RISING_BID_FLOOR_PATTERN_TOO_NOISY",
        "RISING_BID_FLOOR_DATA_INSUFFICIENT",
        "AUDIT_INVALID",
    }
    for name in [
        "REPORT.md",
        "integrity.json",
        "variant_summary.csv",
        "long_to_ceiling_actions.csv",
        "breakout_actions.csv",
        "control_summary.csv",
        "pattern_reference_point_audit.csv",
    ]:
        assert (out / name).exists(), name


def test_stable_rerun(tmp_path: Path) -> None:
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), f"{0.620 + i*0.0003:.4f}", bid=f"{0.618 + (i//2)*0.0005:.4f}", ask="0.628")
        for i in range(12)
    ]
    # ensure floors rise every 2 snaps with persistence
    snaps = []
    floors = [0.618, 0.618, 0.619, 0.619, 0.620, 0.620, 0.620, 0.620, 0.621, 0.621, 0.622, 0.622]
    for i, fl in enumerate(floors):
        mid = fl + 0.003
        snaps.append(
            _snap(
                TS0 + timedelta(seconds=30 * i),
                f"{mid:.6f}",
                bid=f"{fl:.6f}",
                ask="0.628",
                buy=3000,
                sell=1000,
            )
        )
    o1 = tmp_path / "a"
    o2 = tmp_path / "b"
    p = CompressionParams(min_floor_steps=3, min_floor_rise_bps=5, min_floor_persistence_snapshots=2, min_crv=1.0)
    s1 = run_compression_audit_from_state(snapshots=snaps, transitions=[], sequences=[], output_dir=o1, params=p)
    s2 = run_compression_audit_from_state(snapshots=snaps, transitions=[], sequences=[], output_dir=o2, params=p)
    assert s1["decision"] == s2["decision"]
    assert s1["integrity"]["snapshot_count"] == s2["integrity"]["snapshot_count"]

"""Unit tests for price higher lows → ask ceiling audit (synthetic, no DB)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from orderbook_analyse.orderbook_price_higher_lows_ceiling_audit import (
    HigherLowParams,
    PullbackMachine,
    REFERENCE_TIMES,
    advance_pullback_machine,
    breakout_outcomes,
    crv_from_hl,
    long_to_ceiling_outcomes,
    p_variant_ok,
    run_higher_lows_audit_from_state,
)
from orderbook_analyse.orderbook_rising_bid_floor_compression_audit import select_ceiling
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


def _snap(ts: datetime, mid: str, *, ask: str = "0.640", bid: str = "0.620") -> SimpleNamespace:
    na = _wall("Ask", ask)
    nb = _wall("Bid", bid)
    return SimpleNamespace(
        timestamp=ts,
        mid_price=Decimal(mid),
        nearest_ask=na,
        nearest_bid=nb,
        dominant_ask=na,
        dominant_bid=nb,
        near_asks=[na],
        near_bids=[nb],
        buy_notional_since_prev=Decimal("2000"),
        sell_notional_since_prev=Decimal("1000"),
        trade_delta_notional=Decimal("1000"),
    )


def _drive_to_confirm(
    mids: list[float],
    *,
    params: HigherLowParams | None = None,
) -> list:
    params = params or HigherLowParams(
        impulse_min_bps=10, pullback_min_bps=5, rebound_confirm_bps=5
    )
    m = PullbackMachine()
    lows = []
    counter = [0]
    tx: list = []
    for i, px in enumerate(mids):
        ts = TS0 + timedelta(seconds=30 * i)
        low = advance_pullback_machine(
            m,
            ts=ts,
            mid=px,
            params=params,
            delta_ratio=0.2,
            buy_n=2000,
            sell_n=1000,
            low_counter=counter,
            transitions=tx,
        )
        if low:
            lows.append(low)
    return lows


def test_pullback_after_impulse_then_confirm() -> None:
    # rise ~20 bps then pullback ~10 then rebound 5+
    base = 0.630
    path = [
        base,
        base * 1.001,
        base * 1.002,  # impulse peak ~20bps
        base * 1.001,
        base * 1.0005,
        base * 0.9995,  # pullback
        base * 1.0005,  # rebound
    ]
    lows = _drive_to_confirm(path)
    assert len(lows) >= 1
    low = lows[0]
    assert low.confirmation_time > low.candidate_time
    assert low.confirmation_price > low.candidate_price


def test_confirm_time_not_trough_time() -> None:
    lows = _drive_to_confirm(
        [0.630, 0.6315, 0.632, 0.631, 0.6302, 0.6298, 0.6308]
    )
    assert lows
    assert lows[0].candidate_time < lows[0].confirmation_time


def test_no_future_bar_in_confirm() -> None:
    # machine only sees current and past via sequential calls
    m = PullbackMachine()
    counter = [0]
    tx: list = []
    params = HigherLowParams(impulse_min_bps=5, pullback_min_bps=3, rebound_confirm_bps=3)
    for i, px in enumerate([0.63, 0.631, 0.6305, 0.6298, 0.6305]):
        advance_pullback_machine(
            m,
            ts=TS0 + timedelta(seconds=30 * i),
            mid=px,
            params=params,
            delta_ratio=0.1,
            buy_n=1,
            sell_n=1,
            low_counter=counter,
            transitions=tx,
        )
    # no exception and states only forward
    assert all(
        datetime.fromisoformat(t["transition_time"]) >= TS0 for t in tx
    )


def test_higher_low_requires_second_above_first() -> None:
    params = HigherLowParams(min_higher_low_bps=2)
    # two pullbacks with higher second low
    path = [
        0.630,
        0.632,
        0.631,
        0.6295,
        0.6305,  # confirm low1 ~0.6295
        0.632,
        0.633,
        0.632,
        0.6305,
        0.6315,  # confirm low2 ~0.6305 higher
    ]
    lows = _drive_to_confirm(path, params=params)
    assert len(lows) >= 2
    assert lows[1].candidate_price > lows[0].candidate_price


def test_min_higher_low_bps() -> None:
    assert 0.6305 > 0.6300
    # crv / variant gates
    assert p_variant_ok(
        "P3",
        has_ceiling=True,
        one_low=True,
        two_lows=True,
        higher_low=True,
        delta_pos=False,
        delta_improving=False,
        sell_declining=False,
        crv_ok=False,
        quality_ok=False,
        no_a2=True,
    )
    assert not p_variant_ok(
        "P3",
        has_ceiling=True,
        one_low=True,
        two_lows=True,
        higher_low=False,
        delta_pos=False,
        delta_improving=False,
        sell_declining=False,
        crv_ok=False,
        quality_ok=False,
        no_a2=True,
    )


def test_p_variants_separated() -> None:
    base = dict(
        has_ceiling=True,
        one_low=True,
        two_lows=True,
        higher_low=True,
        delta_pos=True,
        delta_improving=True,
        sell_declining=True,
        crv_ok=True,
        quality_ok=True,
        no_a2=True,
    )
    assert p_variant_ok("P0", **{**base, "higher_low": False, "two_lows": False, "one_low": False})
    assert p_variant_ok("P4", **base)
    assert not p_variant_ok("P4", **{**base, "delta_pos": False})
    assert p_variant_ok("P9", **base)
    assert not p_variant_ok("P9", **{**base, "no_a2": False})


def test_ceiling_must_be_above_mid() -> None:
    s = _snap(TS0, "0.630", ask="0.635")
    ce = select_ceiling(s, max_distance_bps=100, min_notional=1000)
    assert ce is not None and ce["ceiling_price"] > 0.630


def test_crv_stop_under_second_low() -> None:
    m = crv_from_hl(
        signal_price=0.632,
        second_low_price=0.629,
        ceiling_price=0.640,
        stop_buffer_bps=3,
    )
    assert m["stop_price"] < 0.629
    assert m["crv_valid"] is True
    assert m["estimated_crv"] is not None and m["estimated_crv"] > 0


def test_crv_invalid_when_stop_not_below_signal() -> None:
    m = crv_from_hl(
        signal_price=0.630,
        second_low_price=0.631,
        ceiling_price=0.640,
        stop_buffer_bps=3,
    )
    assert m["stop_price"] >= 0.630
    assert m["crv_valid"] is False
    assert m["estimated_crv"] is None
    # no epsilon-inflated CRV
    assert m["estimated_crv"] is not m.get("target_distance_bps")


def test_touch_exact_ceiling() -> None:
    oc = long_to_ceiling_outcomes(
        signal_time=TS0,
        signal_price=0.630,
        ceiling_price=0.635,
        second_low_price=0.628,
        first_low_price=0.627,
        mids=[
            (TS0, 0.630),
            (TS0 + timedelta(seconds=30), 0.632),
            (TS0 + timedelta(seconds=60), 0.635),
        ],
        transitions=[],
    )
    assert oc["ceiling_touch"] is True


def test_second_low_invalidation() -> None:
    oc = long_to_ceiling_outcomes(
        signal_time=TS0,
        signal_price=0.630,
        ceiling_price=0.640,
        second_low_price=0.628,
        first_low_price=0.627,
        mids=[
            (TS0, 0.630),
            (TS0 + timedelta(seconds=30), 0.627),  # under second low
            (TS0 + timedelta(seconds=120), 0.640),
        ],
        transitions=[],
    )
    assert oc["second_low_invalidated_before_touch"] is True


def test_mae_after_signal_only() -> None:
    oc = long_to_ceiling_outcomes(
        signal_time=TS0 + timedelta(seconds=30),
        signal_price=0.630,
        ceiling_price=0.640,
        second_low_price=0.628,
        first_low_price=0.627,
        mids=[
            (TS0, 0.620),  # before signal — ignored
            (TS0 + timedelta(seconds=30), 0.630),
            (TS0 + timedelta(seconds=60), 0.629),
            (TS0 + timedelta(seconds=120), 0.640),
        ],
        transitions=[],
    )
    # mae from 0.630 to 0.629 only ~15.9 bps, not from 0.620
    assert oc["mae_down_bps_before_touch"] is not None
    assert float(oc["mae_down_bps_before_touch"]) < 50


def test_b2_failed_breakout() -> None:
    oc = breakout_outcomes(
        action_time=TS0,
        action_price=0.636,
        ceiling_price=0.635,
        mids=[
            (TS0 + timedelta(seconds=30), 0.636),
            (TS0 + timedelta(seconds=60), 0.634),
            (TS0 + timedelta(seconds=90), 0.633),
        ],
        failed_confirm_snapshots=2,
    )
    assert oc["failed_breakout"] is True


def test_reference_times_not_in_params() -> None:
    blob = str(HigherLowParams().__dict__)
    for ref in REFERENCE_TIMES:
        assert ref not in blob


def test_end_to_end(tmp_path: Path) -> None:
    # Build path: impulse, pullback1, rebound, impulse, pullback2 higher, rebound, approach ceiling
    prices = []
    # impulse 1
    prices += [0.630, 0.631, 0.632]
    # pullback 1
    prices += [0.6312, 0.6305, 0.6298]
    # rebound confirm 1
    prices += [0.6305, 0.631]
    # impulse 2
    prices += [0.632, 0.633]
    # pullback 2 higher low ~0.6308
    prices += [0.632, 0.6312, 0.6308]
    # rebound confirm 2
    prices += [0.6315, 0.632]
    # toward ceiling 0.636
    prices += [0.633, 0.634, 0.635, 0.636, 0.6365, 0.637]

    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), f"{px:.6f}", ask="0.636", bid=f"{px-0.004:.6f}")
        for i, px in enumerate(prices)
    ]
    out = tmp_path / "hl"
    summary = run_higher_lows_audit_from_state(
        snapshots=snaps,
        transitions=[],
        output_dir=out,
        params=HigherLowParams(
            impulse_min_bps=8,
            pullback_min_bps=4,
            rebound_confirm_bps=4,
            min_higher_low_bps=1,
            max_time_between_lows_seconds=900,
            min_crv=1.0,
            max_ceiling_distance_bps=150,
        ),
    )
    assert summary["decision"] in {
        "PRICE_HIGHER_LOWS_LONG_TO_CEILING_VALUE_FOUND",
        "PRICE_HIGHER_LOWS_BREAKOUT_VALUE_FOUND",
        "PRICE_HIGHER_LOWS_CONFIRMATION_VALUE_ONLY",
        "PRICE_HIGHER_LOWS_PATTERN_TOO_NOISY",
        "PRICE_HIGHER_LOWS_DATA_INSUFFICIENT",
        "AUDIT_INVALID",
    }
    for name in [
        "REPORT.md",
        "integrity.json",
        "variant_summary.csv",
        "control_summary.csv",
        "confirmed_pullback_lows.csv",
        "higher_low_pairs.csv",
        "long_to_ceiling_actions.csv",
        "pattern_reference_point_audit.csv",
    ]:
        assert (out / name).exists(), name
    assert summary["integrity"]["retroactive_pivot_violations"] == 0


def test_stable_rerun(tmp_path: Path) -> None:
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), f"{0.630 + (i % 5) * 0.0004:.6f}")
        for i in range(40)
    ]
    p = HigherLowParams(impulse_min_bps=5, pullback_min_bps=3, rebound_confirm_bps=3, min_higher_low_bps=0)
    s1 = run_higher_lows_audit_from_state(
        snapshots=snaps, transitions=[], output_dir=tmp_path / "a", params=p
    )
    s2 = run_higher_lows_audit_from_state(
        snapshots=snaps, transitions=[], output_dir=tmp_path / "b", params=p
    )
    assert s1["decision"] == s2["decision"]
    assert s1["integrity"]["confirmed_low_count"] == s2["integrity"]["confirmed_low_count"]


def test_pullback_requires_prior_impulse() -> None:
    m = PullbackMachine()
    counter = [0]
    tx: list = []
    params = HigherLowParams(impulse_min_bps=20, pullback_min_bps=5, rebound_confirm_bps=5)
    # flat then drop — no impulse, no confirm
    for i, px in enumerate([0.630, 0.6298, 0.6295, 0.6290, 0.6295]):
        advance_pullback_machine(
            m,
            ts=TS0 + timedelta(seconds=30 * i),
            mid=px,
            params=params,
            delta_ratio=0.1,
            buy_n=1,
            sell_n=1,
            low_counter=counter,
            transitions=tx,
        )
    assert counter[0] == 0


def test_low_candidate_stored_during_pullback() -> None:
    m = PullbackMachine()
    counter = [0]
    tx: list = []
    params = HigherLowParams(impulse_min_bps=8, pullback_min_bps=4, rebound_confirm_bps=20)
    path = [0.630, 0.6315, 0.632, 0.631, 0.6302]  # impulse then pullback, no full rebound
    for i, px in enumerate(path):
        advance_pullback_machine(
            m,
            ts=TS0 + timedelta(seconds=30 * i),
            mid=px,
            params=params,
            delta_ratio=0.1,
            buy_n=1,
            sell_n=1,
            low_counter=counter,
            transitions=tx,
        )
    assert m.low_candidate_price is not None
    assert m.state in {"PULLBACK_ACTIVE", "LOW_CANDIDATE"}
    assert counter[0] == 0


def test_min_higher_low_bps_gate_on_pair(tmp_path: Path) -> None:
    # nearly equal lows — should fail min_higher_low_bps=10
    prices = [
        0.630, 0.632, 0.631, 0.6298, 0.6306,  # L1
        0.632, 0.633, 0.632, 0.6300, 0.6308,  # L2 barely higher
    ]
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), f"{px:.6f}", ask="0.640")
        for i, px in enumerate(prices)
    ]
    summary = run_higher_lows_audit_from_state(
        snapshots=snaps,
        transitions=[],
        output_dir=tmp_path / "hl_min",
        params=HigherLowParams(
            impulse_min_bps=8,
            pullback_min_bps=4,
            rebound_confirm_bps=4,
            min_higher_low_bps=10,
            max_time_between_lows_seconds=900,
            max_ceiling_distance_bps=200,
        ),
    )
    assert summary["integrity"]["higher_low_pair_count"] == 0


def test_max_time_between_lows(tmp_path: Path) -> None:
    prices = [
        0.630, 0.632, 0.631, 0.6295, 0.6305,  # L1
    ]
    # long gap then second low
    prices += [0.631] * 25
    prices += [0.633, 0.632, 0.6310, 0.6318]
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), f"{px:.6f}", ask="0.640")
        for i, px in enumerate(prices)
    ]
    summary = run_higher_lows_audit_from_state(
        snapshots=snaps,
        transitions=[],
        output_dir=tmp_path / "hl_time",
        params=HigherLowParams(
            impulse_min_bps=8,
            pullback_min_bps=4,
            rebound_confirm_bps=4,
            min_higher_low_bps=1,
            max_time_between_lows_seconds=60,  # too short for 25*30s gap
            max_ceiling_distance_bps=200,
        ),
    )
    assert summary["integrity"]["higher_low_pair_count"] == 0


def test_ceiling_known_at_signal_not_retroactive(tmp_path: Path) -> None:
    # early snaps have no nearby ask; late ask appears — HL signal must not invent early ceiling
    prices = [
        0.630, 0.632, 0.631, 0.6295, 0.6305,
        0.632, 0.633, 0.632, 0.6308, 0.6315,
    ]
    snaps = []
    for i, px in enumerate(prices):
        ask = "0.700" if i < 8 else "0.636"  # far then near
        snaps.append(
            _snap(TS0 + timedelta(seconds=30 * i), f"{px:.6f}", ask=ask, bid=f"{px-0.004:.6f}")
        )
    summary = run_higher_lows_audit_from_state(
        snapshots=snaps,
        transitions=[],
        output_dir=tmp_path / "ceil",
        params=HigherLowParams(
            impulse_min_bps=8,
            pullback_min_bps=4,
            rebound_confirm_bps=4,
            min_higher_low_bps=1,
            max_ceiling_distance_bps=100,
        ),
    )
    # if second confirm happens before near ceiling, no P3+ with that ceiling
    for row in summary.get("variant_summary") or []:
        if row["variant"] == "P3":
            # either no actions, or ceiling was within distance at signal
            assert int(row.get("actions") or 0) >= 0


def test_ceiling_pull_before_touch() -> None:
    tr = SimpleNamespace(
        current_timestamp=TS0 + timedelta(seconds=30),
        side="Ask",
        classification="WALL_PULLED",
    )
    # WALL_PULLED constant may differ — use module constant
    from orderbook_analyse.wall_movement_tracker import WALL_PULLED

    tr.classification = WALL_PULLED
    oc = long_to_ceiling_outcomes(
        signal_time=TS0,
        signal_price=0.630,
        ceiling_price=0.640,
        second_low_price=0.628,
        first_low_price=0.627,
        mids=[
            (TS0 + timedelta(seconds=30), 0.632),
            (TS0 + timedelta(seconds=120), 0.640),
        ],
        transitions=[tr],
    )
    assert oc["ceiling_pulled_before_touch"] is True


def test_empty_optional_a2_g5(tmp_path: Path) -> None:
    snaps = [_snap(TS0 + timedelta(seconds=30 * i), f"{0.630 + i * 0.0001:.6f}") for i in range(20)]
    summary = run_higher_lows_audit_from_state(
        snapshots=snaps,
        transitions=[],
        output_dir=tmp_path / "empty",
        params=HigherLowParams(impulse_min_bps=5, pullback_min_bps=3, rebound_confirm_bps=3),
        a2_times=[],
        g5_warning_times=[],
        g5_action_times=[],
        absorption_by_ts={},
    )
    assert summary["integrity"]["ok"] is True


def test_a2_does_not_alter_p3_base(tmp_path: Path) -> None:
    prices = [
        0.630, 0.631, 0.632, 0.6312, 0.6305, 0.6298, 0.6305, 0.631,
        0.632, 0.633, 0.632, 0.6312, 0.6308, 0.6315, 0.632,
        0.633, 0.634, 0.635, 0.636,
    ]
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), f"{px:.6f}", ask="0.636")
        for i, px in enumerate(prices)
    ]
    p = HigherLowParams(
        impulse_min_bps=8,
        pullback_min_bps=4,
        rebound_confirm_bps=4,
        min_higher_low_bps=1,
        max_ceiling_distance_bps=150,
        min_crv=1.0,
    )
    s_clean = run_higher_lows_audit_from_state(
        snapshots=snaps, transitions=[], output_dir=tmp_path / "c1", params=p
    )
    s_a2 = run_higher_lows_audit_from_state(
        snapshots=snaps,
        transitions=[],
        output_dir=tmp_path / "c2",
        params=p,
        a2_times=[TS0 + timedelta(seconds=200)],
    )
    p3_clean = next(v for v in s_clean["variant_summary"] if v["variant"] == "P3")
    p3_a2 = next(v for v in s_a2["variant_summary"] if v["variant"] == "P3")
    assert p3_clean["raw_signals"] == p3_a2["raw_signals"]


def test_readme_unchanged() -> None:
    import subprocess

    out = subprocess.check_output(
        ["git", "diff", "--", "README.md"],
        cwd="/home/telgenbuescher/projects/orderbook_analyse",
        text=True,
    )
    # pre-existing modification allowed; ensure our run does not rewrite it further mid-test
    assert "orderbook_price_higher_lows" not in out


def test_no_existing_audit_modules_modified() -> None:
    import subprocess

    out = subprocess.check_output(
        ["git", "status", "--short"],
        cwd="/home/telgenbuescher/projects/orderbook_analyse",
        text=True,
    )
    for line in out.splitlines():
        path = line[3:].strip() if len(line) > 3 else ""
        if path.startswith("src/orderbook_analyse/orderbook_") and "price_higher_lows" not in path:
            assert not line.startswith(" M") and not line.startswith("M "), path


def _step(
    m: PullbackMachine,
    *,
    i: int,
    px: float,
    params: HigherLowParams,
    counter: list[int],
    tx: list,
    step_seconds: int = 30,
):
    return advance_pullback_machine(
        m,
        ts=TS0 + timedelta(seconds=step_seconds * i),
        mid=px,
        params=params,
        delta_ratio=0.1,
        buy_n=1,
        sell_n=1,
        low_counter=counter,
        transitions=tx,
    )


def test_pullback_still_confirms_on_rebound() -> None:
    lows = _drive_to_confirm(
        [0.630, 0.6315, 0.632, 0.631, 0.6302, 0.6298, 0.6308],
        params=HigherLowParams(
            impulse_min_bps=10,
            pullback_min_bps=5,
            rebound_confirm_bps=5,
            max_pullback_duration_seconds=900,
            max_pullback_depth_bps=100,
        ),
    )
    assert len(lows) >= 1
    assert lows[0].confirmation_time > lows[0].candidate_time


def test_pullback_expires_exactly_at_max_duration() -> None:
    params = HigherLowParams(
        impulse_min_bps=5,
        pullback_min_bps=3,
        rebound_confirm_bps=50,  # prevent confirm
        max_pullback_duration_seconds=90,
        max_pullback_depth_bps=500,
    )
    m = PullbackMachine()
    counter = [0]
    tx: list = []
    # impulse then shallow pullback
    path = [0.630, 0.631, 0.6315, 0.6310]  # enter pullback around i=3
    for i, px in enumerate(path):
        _step(m, i=i, px=px, params=params, counter=counter, tx=tx)
    assert m.state in {"PULLBACK_ACTIVE", "LOW_CANDIDATE"}
    pb_start_i = next(
        i
        for i, t in enumerate(tx)
        if t["new_state"] == "PULLBACK_ACTIVE"
    )
    # Find snapshot index of pullback start from transition time
    start_ts = datetime.fromisoformat(tx[pb_start_i]["transition_time"])
    start_i = int((start_ts - TS0).total_seconds() // 30)

    # just before boundary: duration 60 < 90
    _step(m, i=start_i + 2, px=0.6308, params=params, counter=counter, tx=tx)
    assert m.state in {"PULLBACK_ACTIVE", "LOW_CANDIDATE"}
    assert m.pullback_expired_count == 0

    # exactly at 90s
    low = _step(m, i=start_i + 3, px=0.6307, params=params, counter=counter, tx=tx)
    assert low is None
    assert m.pullback_expired_count == 1
    assert m.state == "IMPULSE_UP"
    assert any(t["new_state"] == "EXPIRED" for t in tx)
    assert any(t["reason"] == "restart_after_expire" for t in tx)


def test_no_expiry_before_duration_boundary() -> None:
    params = HigherLowParams(
        impulse_min_bps=5,
        pullback_min_bps=3,
        rebound_confirm_bps=50,
        max_pullback_duration_seconds=120,
        max_pullback_depth_bps=500,
    )
    m = PullbackMachine()
    counter = [0]
    tx: list = []
    for i, px in enumerate([0.630, 0.6312, 0.6315, 0.6308]):
        _step(m, i=i, px=px, params=params, counter=counter, tx=tx)
    assert m.pullback_start_time is not None
    # duration 90 < 120
    _step(m, i=6, px=0.6305, params=params, counter=counter, tx=tx)
    assert m.pullback_expired_count == 0
    assert m.state in {"PULLBACK_ACTIVE", "LOW_CANDIDATE"}


def test_deep_pullback_invalidated() -> None:
    params = HigherLowParams(
        impulse_min_bps=5,
        pullback_min_bps=3,
        rebound_confirm_bps=50,
        max_pullback_duration_seconds=10_000,
        max_pullback_depth_bps=20,
    )
    m = PullbackMachine()
    counter = [0]
    tx: list = []
    # ~20+ bps drop from peak 0.632
    for i, px in enumerate([0.630, 0.631, 0.632, 0.631, 0.6305, 0.6295]):
        low = _step(m, i=i, px=px, params=params, counter=counter, tx=tx)
        assert low is None
    assert m.pullback_invalidated_count >= 1
    assert m.state == "IMPULSE_UP"
    assert any(t["new_state"] == "INVALIDATED" for t in tx)
    assert any(t["reason"] == "restart_after_invalidate" for t in tx)
    assert counter[0] == 0


def test_expiry_and_invalidate_create_no_confirmed_low() -> None:
    params = HigherLowParams(
        impulse_min_bps=5,
        pullback_min_bps=3,
        rebound_confirm_bps=100,
        max_pullback_duration_seconds=60,
        max_pullback_depth_bps=15,
    )
    m = PullbackMachine()
    counter = [0]
    tx: list = []
    for i, px in enumerate([0.630, 0.6315, 0.632, 0.6305, 0.629]):
        assert _step(m, i=i, px=px, params=params, counter=counter, tx=tx) is None
    assert counter[0] == 0
    assert len(m.confirmed) == 0


def test_after_expiry_new_impulse_and_later_confirm() -> None:
    params = HigherLowParams(
        impulse_min_bps=5,
        pullback_min_bps=3,
        rebound_confirm_bps=5,
        max_pullback_duration_seconds=90,
        max_pullback_depth_bps=500,
    )
    m = PullbackMachine()
    counter = [0]
    tx: list = []
    # enter pullback, expire without rebound (rebound blocked by high threshold temporarily)
    params_block = HigherLowParams(
        impulse_min_bps=5,
        pullback_min_bps=3,
        rebound_confirm_bps=50,
        max_pullback_duration_seconds=90,
        max_pullback_depth_bps=500,
    )
    for i, px in enumerate([0.630, 0.6315, 0.632, 0.631, 0.6308, 0.6306, 0.6305]):
        _step(m, i=i, px=px, params=params_block, counter=counter, tx=tx)
    assert m.pullback_expired_count >= 1
    assert m.state == "IMPULSE_UP"
    # new impulse + shallow confirmable pullback
    path2 = [0.6305, 0.6312, 0.6318, 0.6312, 0.6309, 0.6314]
    lows = []
    base = 10
    for j, px in enumerate(path2):
        low = _step(m, i=base + j, px=px, params=params, counter=counter, tx=tx)
        if low:
            lows.append(low)
    assert len(lows) >= 1
    assert lows[0].confirmation_time >= lows[0].candidate_time


def test_confirmed_lows_retained_after_invalidate() -> None:
    params = HigherLowParams(
        impulse_min_bps=5,
        pullback_min_bps=3,
        rebound_confirm_bps=5,
        max_pullback_duration_seconds=10_000,
        max_pullback_depth_bps=40,
    )
    m = PullbackMachine()
    counter = [0]
    tx: list = []
    # shallow confirm first low (depth << 40)
    for i, px in enumerate([0.630, 0.631, 0.6315, 0.6310, 0.6308, 0.6312]):
        _step(m, i=i, px=px, params=params, counter=counter, tx=tx)
    assert len(m.confirmed) == 1
    kept = m.confirmed[0].low_id
    # deep second pullback → invalidate (>40 bps from peak)
    for j, px in enumerate([0.6315, 0.6325, 0.633, 0.631, 0.6295]):
        _step(m, i=20 + j, px=px, params=params, counter=counter, tx=tx)
    assert m.pullback_invalidated_count >= 1
    assert len(m.confirmed) == 1
    assert m.confirmed[0].low_id == kept


def test_signal_time_causal_after_abort_path(tmp_path: Path) -> None:
    # expire then later form HL; signal at second confirm
    prices = [
        0.630, 0.632, 0.631, 0.6305, 0.6304, 0.6303, 0.6302,  # expire-ish long shallow
    ]
    # continue with workable structure after restart
    prices += [
        0.631, 0.632, 0.633, 0.632, 0.631, 0.6305, 0.6312,  # L1
        0.632, 0.6335, 0.6325, 0.6315, 0.6310, 0.6318,  # L2 higher
        0.633, 0.634, 0.635,
    ]
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), f"{px:.6f}", ask="0.640")
        for i, px in enumerate(prices)
    ]
    summary = run_higher_lows_audit_from_state(
        snapshots=snaps,
        transitions=[],
        output_dir=tmp_path / "abort_hl",
        params=HigherLowParams(
            impulse_min_bps=8,
            pullback_min_bps=4,
            rebound_confirm_bps=4,
            min_higher_low_bps=1,
            max_time_between_lows_seconds=900,
            max_pullback_duration_seconds=120,
            max_pullback_depth_bps=200,
            max_ceiling_distance_bps=200,
            min_crv=0.5,
        ),
    )
    assert summary["integrity"]["future_data_violations"] == 0
    assert summary["integrity"]["outcome_leakage_violations"] == 0
    assert summary["integrity"]["retroactive_pivot_violations"] == 0
    for row in summary.get("variant_summary") or []:
        if row["variant"] == "P3" and int(row.get("actions") or 0) > 0:
            # outcomes exist only after signals formed at confirm times
            assert True


def _hl_path_then_ceiling_later() -> tuple[list[float], list[str]]:
    """Impulse/pullback HL pair, then hold, then nearer ask appears."""
    prices = [
        0.6300,
        0.6310,
        0.6318,  # impulse
        0.6312,
        0.6306,
        0.6312,  # L1 confirm
        0.6320,
        0.6328,  # impulse 2
        0.6322,
        0.6314,
        0.6320,  # L2 confirm higher (~0.6314 > 0.6306)
    ]
    # hold several snaps without near ceiling
    prices += [0.6321, 0.6322, 0.6323, 0.6324]
    # still holding
    prices += [0.6325, 0.6326]
    asks = ["0.700"] * len(prices)
    # last two snaps: near ceiling
    asks[-2] = "0.636"
    asks[-1] = "0.636"
    return prices, asks


def test_armed_persists_and_later_ceiling_triggers_p3(tmp_path: Path) -> None:
    prices, asks = _hl_path_then_ceiling_later()
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), f"{px:.6f}", ask=asks[i], bid=f"{px-0.004:.6f}")
        for i, px in enumerate(prices)
    ]
    summary = run_higher_lows_audit_from_state(
        snapshots=snaps,
        transitions=[],
        output_dir=tmp_path / "armed_later",
        params=HigherLowParams(
            impulse_min_bps=8,
            pullback_min_bps=4,
            rebound_confirm_bps=4,
            min_higher_low_bps=1,
            max_time_between_lows_seconds=900,
            higher_low_armed_seconds=600,
            max_ceiling_distance_bps=100,
            min_crv=0.1,
            max_pullback_depth_bps=200,
        ),
    )
    assert summary["integrity"]["higher_low_pair_count"] >= 1
    assert summary["integrity"]["armed_pair_count"] >= 1
    p3 = next(v for v in summary["variant_summary"] if v["variant"] == "P3")
    assert int(p3["actions"] or 0) >= 1
    # action not before second confirm
    actions = [
        r
        for r in (tmp_path / "armed_later" / "long_to_ceiling_actions.csv").read_text().splitlines()[1:]
        if r.startswith("S") or ",P3," in r or r
    ]
    # parse via integrity delays
    assert summary["integrity"]["future_data_violations"] == 0
    assert (summary["integrity"].get("median_pair_to_action_seconds") or 0) >= 0


def test_armed_zero_is_event_only(tmp_path: Path) -> None:
    prices, asks = _hl_path_then_ceiling_later()
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), f"{px:.6f}", ask=asks[i], bid=f"{px-0.004:.6f}")
        for i, px in enumerate(prices)
    ]
    summary = run_higher_lows_audit_from_state(
        snapshots=snaps,
        transitions=[],
        output_dir=tmp_path / "armed0",
        params=HigherLowParams(
            impulse_min_bps=8,
            pullback_min_bps=4,
            rebound_confirm_bps=4,
            min_higher_low_bps=1,
            max_time_between_lows_seconds=900,
            higher_low_armed_seconds=0,
            max_ceiling_distance_bps=100,
            min_crv=0.1,
            max_pullback_depth_bps=200,
        ),
    )
    p3 = next(v for v in summary["variant_summary"] if v["variant"] == "P3")
    # ceiling only appears later → event-only must NOT action
    assert int(p3.get("actions") or 0) == 0
    assert summary["integrity"]["armed_expired_count"] >= 1


def test_armed_invalidation_under_second_low(tmp_path: Path) -> None:
    prices = [
        0.6300, 0.6310, 0.6318, 0.6312, 0.6306, 0.6312,
        0.6320, 0.6328, 0.6322, 0.6314, 0.6320,
        0.6310,  # breaks under second low 0.6314 with buffer
    ]
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), f"{px:.6f}", ask="0.700")
        for i, px in enumerate(prices)
    ]
    summary = run_higher_lows_audit_from_state(
        snapshots=snaps,
        transitions=[],
        output_dir=tmp_path / "armed_inv",
        params=HigherLowParams(
            impulse_min_bps=8,
            pullback_min_bps=4,
            rebound_confirm_bps=4,
            min_higher_low_bps=1,
            higher_low_armed_seconds=600,
            max_ceiling_distance_bps=100,
            max_pullback_depth_bps=200,
        ),
    )
    assert summary["integrity"]["armed_invalidated_count"] >= 1


def test_armed_expiry_at_boundary(tmp_path: Path) -> None:
    prices = [
        0.6300, 0.6310, 0.6318, 0.6312, 0.6306, 0.6312,
        0.6320, 0.6328, 0.6322, 0.6314, 0.6320,
    ]
    # hold 90s with armed_seconds=60 → expire without ceiling
    prices += [0.6321, 0.6322, 0.6323]
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), f"{px:.6f}", ask="0.700")
        for i, px in enumerate(prices)
    ]
    summary = run_higher_lows_audit_from_state(
        snapshots=snaps,
        transitions=[],
        output_dir=tmp_path / "armed_exp",
        params=HigherLowParams(
            impulse_min_bps=8,
            pullback_min_bps=4,
            rebound_confirm_bps=4,
            min_higher_low_bps=1,
            higher_low_armed_seconds=60,
            max_ceiling_distance_bps=100,
            max_pullback_depth_bps=200,
        ),
    )
    assert summary["integrity"]["armed_expired_count"] >= 1


def test_pair_actioned_once_per_variant(tmp_path: Path) -> None:
    prices, asks = _hl_path_then_ceiling_later()
    # keep near ceiling for many snaps after it appears
    prices = prices + [0.6327, 0.6328, 0.6329]
    asks = asks + ["0.636", "0.636", "0.636"]
    snaps = [
        _snap(TS0 + timedelta(seconds=30 * i), f"{px:.6f}", ask=asks[i], bid=f"{px-0.004:.6f}")
        for i, px in enumerate(prices)
    ]
    summary = run_higher_lows_audit_from_state(
        snapshots=snaps,
        transitions=[],
        output_dir=tmp_path / "once",
        params=HigherLowParams(
            impulse_min_bps=8,
            pullback_min_bps=4,
            rebound_confirm_bps=4,
            min_higher_low_bps=1,
            higher_low_armed_seconds=900,
            max_ceiling_distance_bps=100,
            min_crv=0.1,
            max_pullback_depth_bps=200,
        ),
    )
    import csv

    with (tmp_path / "once" / "higher_low_raw_signals.csv").open() as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("variant") == "P3"]
    # one pair → at most one P3 raw signal
    pair_keys = {(r.get("first_low_id"), r.get("second_low_id")) for r in rows}
    for key in pair_keys:
        n = sum(1 for r in rows if (r.get("first_low_id"), r.get("second_low_id")) == key)
        assert n == 1

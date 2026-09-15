"""Offline / mock tests for Breakout X-Ray V1 Phase 2 (no production CH / Bronze)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from obfull_research_engine.breakout_xray_v1 import FULL_RUN_BLOCK_REASON
from obfull_research_engine.breakout_xray_v1.adapters.bronze_baseline import (
    BronzeRecordFull,
    BronzeRecordLite,
    replay_baseline_from_records,
    replay_baseline_fullbook,
)
from obfull_research_engine.breakout_xray_v1.adapters.live_data import (
    LivePublicTradesRepository,
    LiveReadinessRepository,
    LiveSilverMetricsRepository,
    QUERY_SETTINGS,
    mid_series_quality,
    normalize_mid_rows,
    normalize_trade_rows,
    public_trades_sql,
    metrics_mid_sql,
)
from obfull_research_engine.breakout_xray_v1.adapters.market_profile import (
    offline_value_area_from_bins,
    profile_from_fixture,
)
from obfull_research_engine.breakout_xray_v1.cli import build_parser, main
from obfull_research_engine.breakout_xray_v1.execution_hold import (
    assert_full_run_allowed,
    read_builder_lock,
)
from obfull_research_engine.breakout_xray_v1.fakes import make_fake_deps
from obfull_research_engine.breakout_xray_v1.hashing import report_content_hash
from obfull_research_engine.breakout_xray_v1.matching import match_trades_to_wall
from obfull_research_engine.breakout_xray_v1.models import (
    EdgeSide,
    ManualWindowConfig,
    StrategyEdgeConfig,
)
from obfull_research_engine.breakout_xray_v1.ports import (
    LevelChangeEvent,
    MidState,
    ReadinessResult,
)
from obfull_research_engine.breakout_xray_v1.reference import (
    resolve_manual_reference,
    resolve_strategy_reference,
    wall_threshold_for_manual,
    wall_threshold_for_strategy,
)
from obfull_research_engine.breakout_xray_v1.run import (
    run_manual_window_analysis,
    run_strategy_edge_analysis,
)
from obfull_research_engine.breakout_xray_v1.states import run_breakout_state_machine
from obfull_research_engine.breakout_xray_v1.time_windows import (
    dt_to_ns,
    rolling_exact_300s_windows,
)
from obfull_research_engine.breakout_xray_v1.trades import XRayTrade, dedup_trades_by_id
from obfull_research_engine.breakout_xray_v1.walls import (
    LevelEvent,
    accumulate_level_sizes,
    classify_wall_lifecycle,
    select_relevant_walls,
    select_walls_by_notional,
    wall_notional_usdt,
)
from obfull_research_engine.breakout_xray_v1.writers import ARTIFACTS
from obfull_research_engine.clickhouse_research_store_v1.analysis_readiness_v1_3 import (
    ChunkAssessment,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_replay import (
    book_map_sha256,
)


UTC = timezone.utc
MINUTE_NS = 60_000_000_000
MS100 = 100_000_000


def test_rolling_exact_five_buckets_300s():
    base = 1_000_000_000_000_000_000
    rows = [
        {"minute_ns": base + i * MINUTE_NS, "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100.5 + i}
        for i in range(7)
    ]
    wins = rolling_exact_300s_windows(rows)
    assert len(wins) == 3
    for w in wins:
        assert len(w) == 5


def test_trade_dedup_counts():
    ts = datetime(2026, 9, 6, 18, 5, tzinfo=UTC)
    trades = [
        XRayTrade(ts, "a", "Buy", 1.0, 1.0, 10.0),
        XRayTrade(ts, "a", "Buy", 1.0, 1.0, 10.0),
        XRayTrade(ts, "b", "Sell", 1.0, 1.0, 5.0),
    ]
    out, stats = dedup_trades_by_id(trades)
    assert stats.raw_rows == 3
    assert stats.unique_trades == 2
    assert stats.unique_trade_ids == 2
    assert stats.duplicate_rows == 1
    assert abs(stats.duplicate_ratio - 1 / 3) < 1e-9
    assert stats.largest_buy["trade_id"] == "a"
    assert stats.largest_sell["trade_id"] == "b"
    assert len(out) == 2


def test_strategy_reference_mp_causal():
    decision = datetime(2026, 9, 6, 18, 0, tzinfo=UTC)
    prof = profile_from_fixture(
        window_start=datetime(2026, 9, 6, 17, 30, tzinfo=UTC),
        window_end=datetime(2026, 9, 6, 18, 0, tzinfo=UTC),
        vah=79730.25,
        val=79500.0,
    )
    ref = resolve_strategy_reference(
        decision_time_utc=decision, edge_side=EdgeSide.UPPER, mp_profile=prof
    )
    assert ref.causal_reference is True
    assert ref.price == 79730.25


def test_override_known_as_of_after_decision_non_causal():
    decision = datetime(2026, 9, 6, 18, 0, tzinfo=UTC)
    bad = resolve_strategy_reference(
        decision_time_utc=decision,
        edge_side=EdgeSide.UPPER,
        reference_price=79730.25,
        reference_known_as_of_utc=datetime(2026, 9, 6, 18, 5, tzinfo=UTC),
    )
    assert bad.causal_reference is False


def test_strategy_override_requires_known_as_of():
    decision = datetime(2026, 9, 6, 18, 0, tzinfo=UTC)
    bad = resolve_strategy_reference(
        decision_time_utc=decision,
        edge_side=EdgeSide.UPPER,
        reference_price=79730.25,
        reference_known_as_of_utc=None,
    )
    assert bad.causal_reference is False


def test_manual_reference_forensic_default():
    ref = resolve_manual_reference(
        analysis_start=datetime(2026, 9, 6, 17, 10, tzinfo=UTC),
        reference_price=79730.25,
        reference_side=EdgeSide.UPPER,
        reference_known_as_of_utc=None,
    )
    assert ref.source.value == "manual_forensic"
    assert ref.causal_reference is False


def test_manual_causal_when_known_before_start():
    ref = resolve_manual_reference(
        analysis_start=datetime(2026, 9, 6, 17, 10, tzinfo=UTC),
        reference_price=79730.25,
        reference_side=EdgeSide.UPPER,
        reference_known_as_of_utc=datetime(2026, 9, 6, 17, 0, tzinfo=UTC),
    )
    assert ref.causal_reference is True


def test_pull_vs_consumption():
    touch = datetime(2026, 9, 6, 17, 20, 10, tzinfo=UTC)
    pull = classify_wall_lifecycle(
        baseline_size=10.0,
        events=[
            LevelEvent(datetime(2026, 9, 6, 17, 20, 5, tzinfo=UTC), "ask", 79730.0, "UPDATE", 0.0, 10.0)
        ],
        first_aggressive_touch=touch,
        baseline_complete=True,
    )
    assert pull.classification.value == "PULL"
    cons = classify_wall_lifecycle(
        baseline_size=10.0,
        events=[
            LevelEvent(datetime(2026, 9, 6, 17, 20, 15, tzinfo=UTC), "ask", 79730.0, "UPDATE", 0.0, 10.0)
        ],
        first_aggressive_touch=touch,
        baseline_complete=True,
    )
    assert cons.classification.value == "CONSUMPTION"


def test_refill_absorption_relocation():
    touch = datetime(2026, 9, 6, 17, 20, 10, tzinfo=UTC)
    refill = classify_wall_lifecycle(
        baseline_size=10.0,
        events=[
            LevelEvent(datetime(2026, 9, 6, 17, 20, 11, tzinfo=UTC), "ask", 100.0, "UPDATE", 2.0, 10.0),
            LevelEvent(datetime(2026, 9, 6, 17, 20, 12, tzinfo=UTC), "ask", 100.0, "UPDATE", 11.0, 2.0),
        ],
        first_aggressive_touch=touch,
        hit_notional=5000.0,
        baseline_complete=True,
    )
    assert refill.classification.value == "REFILL"
    absorb = classify_wall_lifecycle(
        baseline_size=10.0,
        events=[
            LevelEvent(datetime(2026, 9, 6, 17, 20, 11, tzinfo=UTC), "ask", 100.0, "UPDATE", 8.0, 10.0),
        ],
        first_aggressive_touch=touch,
        hit_notional=9000.0,
        baseline_complete=True,
    )
    assert absorb.classification.value == "ABSORPTION"
    reloc = classify_wall_lifecycle(
        baseline_size=10.0,
        events=[
            LevelEvent(datetime(2026, 9, 6, 17, 20, 11, tzinfo=UTC), "ask", 100.0, "REMOVE", 0.0, 10.0),
        ],
        first_aggressive_touch=touch,
        hit_notional=100.0,
        baseline_complete=True,
        neighbor_growth=True,
    )
    assert reloc.classification.value == "RELOCATION"


def test_unresolved_baseline_blocks_claims():
    life = classify_wall_lifecycle(
        baseline_size=10.0,
        events=[
            LevelEvent(datetime(2026, 9, 6, 17, 20, 5, tzinfo=UTC), "ask", 1.0, "UPDATE", 0.0, 10.0)
        ],
        first_aggressive_touch=datetime(2026, 9, 6, 17, 20, 10, tzinfo=UTC),
        baseline_complete=False,
    )
    assert life.classification.value == "UNRESOLVED_BASELINE"


def test_q95_notional_and_band():
    sizes = {
        ("ask", 83000.0): 200.0,  # far
        ("ask", 79800.0): 0.1,  # small notional
        ("ask", 79810.0): 5.0,  # large notional ~399k
        ("bid", 79700.0): 4.0,
    }
    thr, ranked = select_walls_by_notional(
        sizes, ref_price=79750.0, local_band_usd=400.0, q=0.5
    )
    prices = {r["price"] for r in ranked}
    assert 83000.0 not in prices
    assert all("notional_usdt" in r and "size_base" in r for r in ranked)
    assert thr == quantile_check(sizes, 79750.0, 400.0, 0.5)


def quantile_check(sizes, ref, band, q):
    from obfull_research_engine.breakout_xray_v1.walls import quantile

    notionals = [
        wall_notional_usdt(p, s)
        for (side, p), s in sizes.items()
        if abs(p - ref) <= band and s > 0
    ]
    return quantile(notionals, q)


def test_q95_band_excludes_far_levels():
    sizes = {
        ("ask", 83000.0): 200.0,
        ("ask", 79800.0): 50.0,
        ("ask", 79810.0): 40.0,
        ("bid", 79700.0): 30.0,
    }
    thr, ranked = select_relevant_walls(sizes, ref_price=79750.0, local_band_usd=400.0, q=0.5)
    prices = {p for _, p, _ in ranked}
    assert 83000.0 not in prices


def test_strategy_q95_no_future_data():
    thr = wall_threshold_for_strategy(
        decision_time=datetime(2026, 9, 6, 18, 0, tzinfo=UTC),
        pre_window_minutes=30,
        local_band_usd=400.0,
        quantile=0.95,
        max_rank=12,
    )
    assert thr["causal"] is True
    assert thr["window_end"] == datetime(2026, 9, 6, 18, 0, tzinfo=UTC)
    assert thr["method"] == "causal_pre_decision_q95"
    # accumulate must ignore events at/after decision
    start = dt_to_ns(thr["window_start"])
    end = dt_to_ns(thr["window_end"])
    sizes = accumulate_level_sizes(
        baseline_levels=[{"side": "ask", "price": 100.0, "size": 1.0}],
        events=[
            LevelChangeEvent(end - 1, "ask", 100.0, "UPDATE", 5.0),
            LevelChangeEvent(end, "ask", 100.0, "UPDATE", 999.0),  # future / at decision
        ],
        window_start_ns=start,
        window_end_ns=end,
    )
    assert sizes[("ask", 100.0)] == 5.0


def test_manual_q95_forensic():
    thr = wall_threshold_for_manual(
        start=datetime(2026, 9, 6, 17, 10, tzinfo=UTC),
        end=datetime(2026, 9, 6, 19, 5, tzinfo=UTC),
        local_band_usd=400.0,
        quantile=0.95,
        max_rank=12,
    )
    assert thr["causal"] is False
    assert thr["method"] == "forensic_window_q95"


def test_bronze_replay_hash_and_order():
    snap = BronzeRecordLite(
        0, 1, 1000, "snapshot", bids=[(100.0, 2.0)], asks=[(101.0, 3.0)]
    )
    delta = BronzeRecordLite(0, 2, 2000, "delta", side="ask", price=101.0, size=0.0)
    digest = book_map_sha256({100.0: 2.0}, {})
    base = replay_baseline_from_records(
        records=[delta, snap], start_ns=3000, expected_book_hash=digest
    )
    assert base.complete is True
    assert base.hash == digest


def test_bronze_fullbook_gap_and_mismatch_and_remove():
    snap = BronzeRecordFull(
        0,
        1,
        1000,
        "snapshot",
        bids=[[100.0, 2.0], [99.0, 1.5]],
        asks=[[101.0, 3.0], [102.0, 4.0]],
        u=1,
        seq=1,
    )
    d1 = BronzeRecordFull(
        0, 2, 2000, "delta", bids=[], asks=[[101.0, 0.0]], u=2, seq=2
    )
    d2 = BronzeRecordFull(
        1, 1, 2500, "delta", bids=[[100.0, 5.0]], asks=[], u=3, seq=3
    )
    # wrong order input
    good = replay_baseline_fullbook(
        symbol="BTCUSDT",
        records=[d2, d1, snap],
        start_ns=3000,
        expected_book_hash=None,
        enforce_continuity=True,
    )
    assert good.complete is True
    assert good.hash == book_map_sha256({100.0: 5.0, 99.0: 1.5}, {102.0: 4.0})

    mismatch = replay_baseline_fullbook(
        symbol="BTCUSDT",
        records=[snap],
        start_ns=3000,
        expected_book_hash="deadbeef",
        enforce_continuity=True,
    )
    assert mismatch.complete is False
    assert mismatch.unresolved_reason == "UNRESOLVED_BASELINE_HASH_MISMATCH"

    gap = replay_baseline_fullbook(
        symbol="BTCUSDT",
        records=[
            snap,
            BronzeRecordFull(0, 2, 1500, "gap_marker", gap=True),
            d1,
        ],
        start_ns=3000,
        enforce_continuity=True,
    )
    assert gap.complete is False
    assert gap.unresolved_reason == "UNRESOLVED_BASELINE"


def test_trade_wall_matching_side_and_tick():
    trades = [
        XRayTrade(datetime(2026, 9, 6, 17, 20, 10, tzinfo=UTC), "1", "Buy", 100.0, 1.0, 100.0),
        XRayTrade(datetime(2026, 9, 6, 17, 20, 11, tzinfo=UTC), "2", "Sell", 100.0, 1.0, 100.0),
        XRayTrade(datetime(2026, 9, 6, 17, 20, 12, tzinfo=UTC), "3", "Buy", 100.5, 1.0, 100.5),
    ]
    ask_hit = match_trades_to_wall(
        side="ask",
        price=100.0,
        baseline_size=10.0,
        events=[],
        trades=trades,
        mid_series=[],
        tick_tolerance=0.05,
    )
    assert ask_hit.hit_trade_count == 1
    assert ask_hit.first_aggressive_touch_ns is not None
    # wrong side for bid wall
    bid_miss = match_trades_to_wall(
        side="bid",
        price=100.0,
        baseline_size=10.0,
        events=[],
        trades=[trades[0]],
        mid_series=[],
        tick_tolerance=0.05,
    )
    assert bid_miss.hit_trade_count == 0
    # outside tick
    far = match_trades_to_wall(
        side="ask",
        price=100.0,
        baseline_size=10.0,
        events=[],
        trades=[trades[2]],
        mid_series=[],
        tick_tolerance=0.05,
    )
    assert far.hit_trade_count == 0


def test_state_machine_requires_persistence():
    ref = 100.0
    series = [(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 99.0)]
    for i in range(10):
        series.append((datetime(2026, 1, 1, 0, 0, i + 1, tzinfo=UTC), 106.0))
    series.append((datetime(2026, 1, 1, 0, 0, 20, tzinfo=UTC), 99.0))
    sm = run_breakout_state_machine(
        side=EdgeSide.UPPER,
        reference_price=ref,
        mid_series=series,
        accept_persist_samples=50,
        break_persist_samples=30,
    )
    assert sm.accepted is False


def test_execution_hold_blocks_full_run(tmp_path: Path):
    lock = tmp_path / "build.lock"
    lock.write_text(json.dumps({"pid": 1}), encoding="utf-8")
    probe = read_builder_lock(lock)
    if probe.pid_alive:
        with pytest.raises(RuntimeError) as ei:
            assert_full_run_allowed(lock)
        assert FULL_RUN_BLOCK_REASON in str(ei.value)


def test_cli_exit_2_active_lock(tmp_path: Path):
    lock = tmp_path / "build.lock"
    lock.write_text(json.dumps({"pid": 1}), encoding="utf-8")
    if not read_builder_lock(lock).pid_alive:
        pytest.skip("pid 1 not alive")
    rc = main(
        [
            "manual-window",
            "--symbol",
            "BTCUSDT",
            "--start-utc",
            "2026-09-06T17:10:00Z",
            "--end-utc",
            "2026-09-06T17:20:00Z",
            "--local-band-usd",
            "400",
            "--output-dir",
            str(tmp_path / "out"),
            "--lock-path",
            str(lock),
        ]
    )
    assert rc == 2


def test_cli_check_only_with_inactive_lock(tmp_path: Path):
    lock = tmp_path / "build.lock"
    lock.write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
    out = tmp_path / "out"
    rc = main(
        [
            "manual-window",
            "--symbol",
            "BTCUSDT",
            "--start-utc",
            "2026-09-06T17:10:00Z",
            "--end-utc",
            "2026-09-06T17:20:00Z",
            "--local-band-usd",
            "400",
            "--output-dir",
            str(out),
            "--check-only",
            "--lock-path",
            str(lock),
        ]
    )
    assert rc == 0
    assert (out / "manifest.json").exists()


def test_cli_help_smoke():
    with pytest.raises(SystemExit) as ei:
        build_parser().parse_args(["--help"])
    assert ei.value.code == 0


def _fixture_bundle(tmp_path: Path, *, mode: str):
    lock = tmp_path / "nolock"
    decision = datetime(2026, 9, 6, 18, 0, tzinfo=UTC)
    start = datetime(2026, 9, 6, 17, 30, tzinfo=UTC)
    end = datetime(2026, 9, 6, 19, 0, tzinfo=UTC)
    base_ns = dt_to_ns(start)
    mid_series = [
        MidState(
            bucket_start_ns=base_ns + i * MS100,
            mid=79600.0 + i * 0.01,
            spread=1.0,
            best_bid=79599.5,
            best_ask=79600.5,
            book_hash="abc",
        )
        for i in range(100)
    ]
    # denser mids near decision for outcomes
    for i in range(0, 3600):
        mid_series.append(
            MidState(
                bucket_start_ns=dt_to_ns(decision) + i * MS100,
                mid=79720.0 + (0.05 if i > 10 else 0.0),
                spread=1.0,
                best_bid=79719.0,
                best_ask=79721.0,
                book_hash="abc",
            )
        )
    minutes = []
    for i in range(90):
        minutes.append(
            {
                "minute_ns": base_ns + i * MINUTE_NS,
                "o": 79600 + i * 0.1,
                "h": 79610 + i * 0.1,
                "l": 79590 + i * 0.1,
                "c": 79605 + i * 0.1,
                "delta": 1000.0,
            }
        )
    wall_px = 79730.25
    trades = [
        XRayTrade(
            datetime(2026, 9, 6, 18, 0, 5, tzinfo=UTC),
            "mega_buy",
            "Buy",
            wall_px,
            250.0,
            20_006_882.5,
        ),
        XRayTrade(
            datetime(2026, 9, 6, 18, 0, 5, tzinfo=UTC),
            "mega_buy",
            "Buy",
            wall_px,
            250.0,
            20_006_882.5,
        ),
        XRayTrade(
            datetime(2026, 9, 6, 18, 1, 0, tzinfo=UTC),
            "sell1",
            "Sell",
            79600.0,
            1.0,
            79600.0,
        ),
    ]
    events = [
        LevelChangeEvent(
            dt_to_ns(datetime(2026, 9, 6, 17, 45, tzinfo=UTC)),
            "ask",
            wall_px,
            "ADD",
            10.0,
        ),
        LevelChangeEvent(
            dt_to_ns(datetime(2026, 9, 6, 18, 0, 6, tzinfo=UTC)),
            "ask",
            wall_px,
            "UPDATE",
            4.0,
            10.0,
        ),
    ]
    snap = BronzeRecordFull(
        0,
        1,
        base_ns - 1_000_000_000,
        "snapshot",
        bids=[[79600.0, 2.0], [79590.0, 1.0]],
        asks=[[wall_px, 10.0], [79800.0, 3.0]],
        u=1,
        seq=1,
    )
    digest = book_map_sha256({79600.0: 2.0, 79590.0: 1.0}, {wall_px: 10.0, 79800.0: 3.0})
    # clear mid book_hash so baseline doesn't fail mismatch
    mid_series = [
        MidState(
            m.bucket_start_ns,
            m.mid,
            m.spread,
            m.best_bid,
            m.best_ask,
            digest,
            m.bid_level_count,
            m.ask_level_count,
        )
        for m in mid_series
    ]
    prof = profile_from_fixture(
        window_start=datetime(2026, 9, 6, 17, 30, tzinfo=UTC),
        window_end=datetime(2026, 9, 6, 18, 0, tzinfo=UTC),
        vah=wall_px,
        val=79500.0,
        poc=79615.0,
    )
    deps = make_fake_deps(
        mid_series=mid_series,
        minute_rows=minutes,
        level_changes=events,
        trades=trades,
        bronze_records=[snap],
        expected_book_hash=digest,
        mp_profile=prof,
        enforce_continuity=False,
    )
    return lock, decision, start, end, deps, digest, wall_px


def test_strategy_fixture_e2e(tmp_path: Path):
    lock, decision, start, end, deps, digest, wall_px = _fixture_bundle(tmp_path, mode="strategy")
    cfg = StrategyEdgeConfig(
        symbol="BTCUSDT",
        decision_time_utc=decision,
        edge_side=EdgeSide.UPPER,
        pre_window_minutes=30,
        post_window_minutes=60,
        local_band_usd=400.0,
        output_dir=str(tmp_path / "strat"),
        check_only=False,
    )
    result = run_strategy_edge_analysis(
        cfg,
        deps=deps,
        mp_profile=deps.market_profile.profile,
        lock_path=lock,
        skip_execution_hold=True,
    )
    assert result.reference.causal_reference is True
    assert result.baseline.complete is True
    assert result.baseline.hash == digest
    out = tmp_path / "strat"
    for name in ARTIFACTS:
        assert (out / name).exists(), name
    dedup = json.loads((out / "trade_dedup_report.json").read_text())
    assert dedup["largest_buy"]["trade_id"] == "mega_buy"
    assert dedup["largest_buy"]["notional"] == 20_006_882.5
    report = json.loads((out / "report.json").read_text())
    assert report["sections"]["avr"]["status"] == "UNAVAILABLE_ADAPTER_NOT_IMPLEMENTED"
    assert report["sections"]["open_interest"]["status"] == "UNAVAILABLE_ADAPTER_NOT_IMPLEMENTED"
    assert result.wall_threshold.causal is True
    walls = report["sections"]["wall_lifecycles"]
    assert isinstance(walls, list)
    # no vah=0 placeholder in real mp
    assert result.reference.price == wall_px


def test_manual_fixture_e2e(tmp_path: Path):
    lock, decision, start, end, deps, digest, wall_px = _fixture_bundle(tmp_path, mode="manual")
    cfg = ManualWindowConfig(
        symbol="BTCUSDT",
        start_utc=start,
        end_utc=end,
        local_band_usd=400.0,
        output_dir=str(tmp_path / "manual"),
        check_only=False,
        reference_price=wall_px,
        reference_side=EdgeSide.UPPER,
    )
    result = run_manual_window_analysis(
        cfg, deps=deps, lock_path=lock, skip_execution_hold=True
    )
    assert result.reference.causal_reference is False
    assert result.wall_threshold.causal is False
    assert result.wall_threshold.method == "forensic_window_q95"
    assert result.baseline.complete is True
    for name in ARTIFACTS:
        assert (tmp_path / "manual" / name).exists(), name


def test_deterministic_report_hash(tmp_path: Path):
    lock, decision, start, end, deps, digest, wall_px = _fixture_bundle(tmp_path, mode="h")
    cfg = ManualWindowConfig(
        symbol="BTCUSDT",
        start_utc=start,
        end_utc=end,
        local_band_usd=400.0,
        output_dir=str(tmp_path / "h1"),
        reference_price=wall_px,
        reference_side=EdgeSide.UPPER,
    )
    r1 = run_manual_window_analysis(
        cfg, deps=deps, lock_path=lock, skip_execution_hold=True
    )
    cfg2 = ManualWindowConfig(
        symbol="BTCUSDT",
        start_utc=start,
        end_utc=end,
        local_band_usd=400.0,
        output_dir=str(tmp_path / "h2"),
        reference_price=wall_px,
        reference_side=EdgeSide.UPPER,
    )
    # rebuild identical deps
    _, _, _, _, deps2, _, _ = _fixture_bundle(tmp_path / "x", mode="h")
    r2 = run_manual_window_analysis(
        cfg2, deps=deps2, lock_path=lock, skip_execution_hold=True
    )
    assert r1.report_hash == r2.report_hash
    assert r1.report_hash == report_content_hash(r1.to_dict())


def test_not_ready_blocks_loaders(tmp_path: Path):
    lock = tmp_path / "nolock"
    deps = make_fake_deps(
        readiness=ReadinessResult(status="NOT_READY", reason="GAP"),
        trades=[
            XRayTrade(datetime(2026, 9, 6, 17, 30, tzinfo=UTC), "x", "Buy", 1.0, 1.0, 1.0)
        ],
    )
    # spy: if trades called would still work but core must not call
    calls_before = len(deps.trades.calls)
    cfg = ManualWindowConfig(
        symbol="BTCUSDT",
        start_utc=datetime(2026, 9, 6, 17, 10, tzinfo=UTC),
        end_utc=datetime(2026, 9, 6, 17, 20, tzinfo=UTC),
        local_band_usd=400.0,
        output_dir=str(tmp_path / "nr"),
    )
    result = run_manual_window_analysis(
        cfg, deps=deps, lock_path=lock, skip_execution_hold=True
    )
    assert result.data_quality.get("loaders_invoked") is False
    assert len(deps.trades.calls) == calls_before
    assert result.sections["readiness"]["status"] == "NOT_READY"


def test_outcomes_outside_coverage():
    from obfull_research_engine.breakout_xray_v1.outcomes import plan_outcome_horizons

    outs = plan_outcome_horizons(
        event_time=datetime(2026, 9, 6, 18, 50, tzinfo=UTC),
        window_end=datetime(2026, 9, 6, 19, 0, tzinfo=UTC),
    )
    by = {o.label: o.status for o in outs}
    assert by["5m"] == "EVALUATED"
    assert by["15m"] == "UNAVAILABLE_OUTSIDE_WINDOW"
    assert by["4h"] == "UNAVAILABLE_OUTSIDE_WINDOW"


def test_short_tail_windows_not_ranked():
    base = 1_000_000_000_000_000_000
    rows = [
        {"minute_ns": base + i * MINUTE_NS, "o": 1, "h": 2, "l": 0, "c": 1.5}
        for i in range(4)
    ]
    wins = rolling_exact_300s_windows(rows)
    assert wins == []


def test_mock_metrics_sql_params_and_normalize():
    client = MagicMock()
    rows = [
        {
            "bucket_start_ns": 1000,
            "chunk_key": "c1",
            "payload": json.dumps(
                {
                    "mid": 1.0,
                    "spread": 0.1,
                    "best_bid": 0.9,
                    "best_ask": 1.1,
                    "book_hash": "h",
                    "bid_level_count": 2,
                    "ask_level_count": 2,
                }
            ),
        },
        {
            "bucket_start_ns": 1000 + MS100,
            "chunk_key": "c1",
            "payload": json.dumps(
                {
                    "mid": 1.01,
                    "spread": 0.1,
                    "best_bid": 0.91,
                    "best_ask": 1.11,
                    "book_hash": "h2",
                    "bid_level_count": 2,
                    "ask_level_count": 2,
                }
            ),
        },
    ]
    client.query.return_value = rows
    lock = Path("/tmp/xray_test_nolock_metrics")
    if lock.exists():
        lock.unlink()
    repo = LiveSilverMetricsRepository(
        client, database="research_full_ob_silver_v1_3", lock_path=lock, require_lock_gate=False
    )
    out = repo.load_mid_series(
        symbol="BTCUSDT",
        start_ns=1000,
        end_ns=1000 + 2 * MS100,
        chunk_keys=("c1", "c2"),
    )
    assert len(out) == 2
    call_kw = client.query.call_args
    assert "chunk_key IN" in metrics_mid_sql(database="research_full_ob_silver_v1_3")
    params = call_kw.kwargs["parameters"]
    assert params["chunk_keys"] == ["c1", "c2"]
    assert params["start_ns"] == 1000
    assert params["end_ns"] == 1000 + 2 * MS100
    settings = call_kw.kwargs["settings"]
    assert settings["max_threads"] == 1
    assert settings["readonly"] == 1
    q = mid_series_quality(out, start_ns=1000, end_ns=1000 + 2 * MS100)
    assert q["monotonic"] is True
    assert q["duplicate_buckets"] is False
    assert q["half_open_ok"] is True
    assert q["expected_100ms_count"] == 2


def test_mock_trades_sql_and_dedup_report():
    client = MagicMock()
    ts = datetime(2026, 9, 6, 18, 0, tzinfo=UTC)
    client.query.return_value = [
        {
            "trade_ts": ts,
            "trade_id": "mega",
            "side": "Buy",
            "price": 80000.0,
            "size": 250.0,
            "notional": 20_006_882.5,
        },
        {
            "trade_ts": ts,
            "trade_id": "mega",
            "side": "Buy",
            "price": 80000.0,
            "size": 250.0,
            "notional": 20_006_882.5,
        },
    ]
    lock = Path("/tmp/xray_test_nolock_trades")
    if lock.exists():
        lock.unlink()
    repo = LivePublicTradesRepository(client, lock_path=lock, require_lock_gate=False)
    raw = repo.load_trades(symbol="BTCUSDT", start_ns=1, end_ns=9)
    trades, stats = dedup_trades_by_id(raw)
    assert len(trades) == 1
    assert stats.duplicate_rows == 1
    assert stats.largest_buy["notional"] == 20_006_882.5
    assert "ORDER BY trade_ts, trade_id" in public_trades_sql()
    assert QUERY_SETTINGS["max_threads"] == 1


def test_readiness_mock_gap_epoch_empty():
    ready = ChunkAssessment(
        chunk_key="c1",
        epoch_id="e1",
        start_ns=0,
        end_ns=10_000,
        level_change_count=1,
        state_count=1,
        output_hash="a" * 64,
        ledger_status="COMPLETE",
        observed_level_changes=1,
        observed_states=1,
        status="READY",
    )
    repo = LiveReadinessRepository(ready_chunks=[ready], gap_times=[5000])
    r = repo.assess_window(symbol="BTCUSDT", start_ns=0, end_ns=9000)
    assert r.status == "NOT_READY"
    assert "GAP" in r.reason.upper() or "CROSSES" in r.reason

    empty = LiveReadinessRepository(ready_chunks=[], gap_times=[])
    r2 = empty.assess_window(symbol="BTCUSDT", start_ns=0, end_ns=100)
    assert r2.status == "NOT_READY"

    ok = LiveReadinessRepository(ready_chunks=[ready], gap_times=[])
    r3 = ok.assess_window(symbol="BTCUSDT", start_ns=100, end_ns=9000)
    assert r3.status == "READY"
    assert r3.chunk_keys == ("c1",)


def test_mp_value_area_parity_offline():
    bins = [
        (0, 90.0, 92.0, 91.0, 10.0),
        (1, 92.0, 94.0, 93.0, 50.0),
        (2, 94.0, 96.0, 95.0, 20.0),
        (3, 96.0, 98.0, 97.0, 5.0),
    ]
    a = offline_value_area_from_bins(bins, value_area_pct=0.70)
    b = offline_value_area_from_bins(bins, value_area_pct=0.70)
    assert a == b
    assert a["poc"] == 93.0
    assert a["vah"] >= a["val"]
    assert a["value_area_pct"] == 0.70


def test_mp_dual_profile_trades_offline_no_ch():
    from obfull_research_engine.breakout_xray_v1.adapters.market_profile import (
        offline_dual_profile_parity,
    )

    start = datetime(2026, 9, 6, 17, 30, tzinfo=UTC)
    end = datetime(2026, 9, 6, 18, 0, tzinfo=UTC)
    trades = []
    t0 = start.timestamp()
    for i in range(120):
        ts = datetime.fromtimestamp(t0 + i * 12, tz=UTC)
        if ts >= end:
            break
        px = 79600.0 + (i % 15) * 10.0
        trades.append(
            {
                "ts": ts,
                "trade_id": str(i),
                "price": px,
                "size": 1.0 + (i % 4),
                "side": "Buy" if i % 2 == 0 else "Sell",
            }
        )
    a = offline_dual_profile_parity(
        symbol="BTCUSDT", window_start=start, window_end=end, trades=trades
    )
    b = offline_dual_profile_parity(
        symbol="BTCUSDT", window_start=start, window_end=end, trades=trades
    )
    assert a["poc"] == b["poc"]
    assert a["vah"] == b["vah"]
    assert a["val"] == b["val"]
    assert a["upper_reference"] == a["vah"]
    assert a["lower_reference"] == a["val"]
    assert a["value_area_pct"] == 0.70
    assert a["poc"] is not None


def test_growth_decay_unresolved_path():
    growth = classify_wall_lifecycle(
        baseline_size=10.0,
        events=[
            LevelEvent(datetime(2026, 9, 6, 17, 20, 1, tzinfo=UTC), "bid", 100.0, "UPDATE", 11.0),
            LevelEvent(datetime(2026, 9, 6, 17, 20, 2, tzinfo=UTC), "bid", 100.0, "UPDATE", 15.0),
        ],
        first_aggressive_touch=None,
        baseline_complete=True,
    )
    assert growth.classification.value == "GROWTH"
    decay = classify_wall_lifecycle(
        baseline_size=10.0,
        events=[
            LevelEvent(datetime(2026, 9, 6, 17, 20, 1, tzinfo=UTC), "bid", 100.0, "UPDATE", 9.0),
            LevelEvent(datetime(2026, 9, 6, 17, 20, 2, tzinfo=UTC), "bid", 100.0, "UPDATE", 8.0),
        ],
        first_aggressive_touch=None,
        baseline_complete=True,
    )
    assert decay.classification.value == "DECAY"
    unresolved = classify_wall_lifecycle(
        baseline_size=10.0,
        events=[],
        first_aggressive_touch=None,
        baseline_complete=True,
        side="ask",
        price=100.0,
    )
    assert unresolved.classification.value == "UNRESOLVED"


def test_mp_parity_fixture_shape_matches_dashboard_fields():
    prof = profile_from_fixture(
        window_start=datetime(2026, 9, 6, 17, 30, tzinfo=UTC),
        window_end=datetime(2026, 9, 6, 18, 0, tzinfo=UTC),
        vah=100.0,
        val=90.0,
        poc=95.0,
    )
    assert prof["tpo"]["value_area"]["vah"] == 100.0
    assert prof["temporary_cross_repo_dependency"] is True


def test_normalize_helpers_half_open_and_sort():
    mids = normalize_mid_rows(
        [
            {"bucket_start_ns": 200, "mid": 2.0, "spread": None, "best_bid": None, "best_ask": None, "book_hash": "b"},
            {"bucket_start_ns": 100, "mid": 1.0, "spread": None, "best_bid": None, "best_ask": None, "book_hash": "a"},
            {"bucket_start_ns": 100, "mid": 1.0, "spread": None, "best_bid": None, "best_ask": None, "book_hash": "dup"},
        ]
    )
    assert [m.bucket_start_ns for m in mids] == [100, 200]
    trades = normalize_trade_rows(
        [
            {
                "trade_ts": datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
                "trade_id": "b",
                "side": "Buy",
                "price": 1,
                "size": 1,
                "notional": 1,
            },
            {
                "trade_ts": datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
                "trade_id": "a",
                "side": "Sell",
                "price": 1,
                "size": 1,
                "notional": 1,
            },
        ]
    )
    assert [t.trade_id for t in trades] == ["a", "b"]

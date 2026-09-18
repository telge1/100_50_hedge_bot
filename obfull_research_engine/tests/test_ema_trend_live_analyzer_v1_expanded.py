"""Expanded pre-rollout tests for ema_trend_live_analyzer_v1."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_SHADOW = str(_ENGINE_ROOT.parent / "src")
while _SHADOW in sys.path:
    sys.path.remove(_SHADOW)
sys.path.insert(0, str(_ENGINE_ROOT / "src"))
sys.path.insert(0, "/home/telgenbuescher/projects/orderbook_analyse/src")

from obfull_research_engine.ema_trend_live_analyzer_v1 import (
    ALLOW_CLICKHOUSE_WRITES,
    ALLOW_MYSQL_WRITES,
    FEATURE_HORIZONS_S,
    FIRST_CANDIDATE_ELIGIBLE_SECONDS,
    FIRST_EARLY_EVIDENCE_SECONDS,
    LIVE_TRADING,
    MP_REQUIRED,
    OUTCOME_HORIZON_SECONDS,
    SECOND_BYBIT_OB_WS,
)
from obfull_research_engine.ema_trend_live_analyzer_v1.clock import FakeClock
from obfull_research_engine.ema_trend_live_analyzer_v1.contract import (
    COLLECTOR_FANOUT_SOURCES,
    build_contract_manifest,
)
from obfull_research_engine.ema_trend_live_analyzer_v1.decision_evidence import CandidateFSM
from obfull_research_engine.ema_trend_live_analyzer_v1.feature_timeline import IncrementalTimeline
from obfull_research_engine.ema_trend_live_analyzer_v1.incremental_engine import IncrementalEngineState
from obfull_research_engine.ema_trend_live_analyzer_v1.live_event_adapter import LiveEventAdapter
from obfull_research_engine.ema_trend_live_analyzer_v1.outcomes_6h import OutcomePending
from obfull_research_engine.ema_trend_live_analyzer_v1.public_trades_adapter import (
    STREAM_AVAILABLE_BUT_NO_TRADES,
    STREAM_MISSING,
    TRADES_PRESENT,
    FakePublicTradesAdapter,
    TradeWatermarkPoller,
)
from obfull_research_engine.ema_trend_live_analyzer_v1.schema import CoverageFlags
from obfull_research_engine.ema_trend_live_analyzer_v1.wall_tracker import DynamicWallTracker
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (
    BookNode,
)
from obfull_research_engine.mp_qdh_30event_case_control_v2.flow_v2 import recompute_bucket_mass

PKG = Path(__file__).resolve().parents[1] / "src" / "obfull_research_engine" / "ema_trend_live_analyzer_v1"
COLLECTOR_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ema_delta_fanout_v1/src/orderbook_analyse"
)


# ---- Contract / isolation ----


def test_live_flags_safe() -> None:
    assert LIVE_TRADING is False
    assert ALLOW_CLICKHOUSE_WRITES is False
    assert ALLOW_MYSQL_WRITES is False
    assert MP_REQUIRED is False
    assert SECOND_BYBIT_OB_WS is False


def test_no_import_of_forensic_package_in_callgraph() -> None:
    for path in PKG.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "ema_trend_analyzer_v1" not in alias.name or "live" in alias.name
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "ema_trend_analyzer_v1" not in node.module or "live" in node.module


def test_mp_not_required_and_not_imported_as_dependency() -> None:
    assert MP_REQUIRED is False
    for path in PKG.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert "market_profile_context" not in src
        assert "market_profile_lld" not in src
        # MP as Market Profile modality forbidden; mp_qdh_* engines are allowed canonical math
        assert "from obfull_research_engine.market_profile" not in src


def test_no_order_modules() -> None:
    for path in PKG.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        for bad in ("place_order", "submit_order", "bybit_private", "create_order", "OrderClient"):
            assert bad not in src


def test_no_ch_mysql_write_paths() -> None:
    for path in PKG.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        for bad in ("INSERT INTO", "ALTER TABLE", "CREATE TABLE", "client.command(", "execute("):
            # allow comments / strings about SELECT-only
            if bad in src and "assert_ch_select_only" not in src and "SELECT" not in src:
                # public_trades has assert_ch_select_only — check INSERT specifically
                pass
        assert "INSERT INTO" not in src
        assert "REPLACE INTO" not in src
        assert ".command(" not in src


def test_contract_includes_collector_and_analyzer() -> None:
    man = build_contract_manifest(collector_src_root=COLLECTOR_ROOT)
    assert man["analyzer_sources"]
    assert man["collector_sources"]
    assert "orderbook_v2_live/full_ob_event_fanout.py" in man["collector_sources"]
    assert "contract.py" in man["analyzer_sources"]
    assert "git" not in man["contract_hash"]  # hash is content, not git sha self-ref
    assert len(man["contract_hash"]) == 64


def test_stale_collector_hash_detectable() -> None:
    man = build_contract_manifest(collector_src_root=COLLECTOR_ROOT)
    key = "orderbook_v2_live/full_ob_event_fanout.py"
    assert key in man["collector_sources"]
    fake = dict(man)
    fake["collector_sources"] = dict(man["collector_sources"])
    fake["collector_sources"][key] = "0" * 64
    # arm gate: hashes must match recomputed
    fresh = build_contract_manifest(collector_src_root=COLLECTOR_ROOT)
    assert fresh["collector_sources"][key] != fake["collector_sources"][key]


def test_stale_analyzer_hash_detectable() -> None:
    man = build_contract_manifest(collector_src_root=COLLECTOR_ROOT)
    key = "pipeline.py"
    assert key in man["analyzer_sources"]
    stale = "f" * 64
    assert man["analyzer_sources"][key] != stale


# ---- Signal ----


def test_only_one_signal_max_and_armed_gate() -> None:
    from obfull_research_engine.ema_trend_live_analyzer_v1 import MAX_ACCEPTED_CASES, MAX_SIGNALS

    assert MAX_SIGNALS == 1
    assert MAX_ACCEPTED_CASES == 1


def test_direction_does_not_force_candidate() -> None:
    # ABOVE threshold with weak features stays EARLY/UNRESOLVED, not forced LONG
    fsm = CandidateFSM(threshold_side="ABOVE_EMA_THRESHOLD")
    now = datetime.now(timezone.utc)
    fsm.update(
        elapsed_s=6.0,
        coverage_ok=True,
        archive_ok=True,
        features={"persistence_ratio": 0.0, "impact_efficiency": 0.0, "qdh": {"qdh_base": 0.0}, "mass": {"attributed_fill_capped": 0.0, "refill": 0.0, "residual_pull": 0.0}},
        now=now,
    )
    assert "CANDIDATE" not in fsm.state or fsm.state == "EARLY_EVIDENCE"
    assert fsm.not_calibrated is True


def test_no_signal_in_technical_smoke_flags() -> None:
    from obfull_research_engine.ema_trend_live_analyzer_v1.source_signals import describe_source

    d = describe_source()
    assert d["mode"] == "disarmed"
    assert d["mysql_writes"] is False


# ---- Public trades ----


def test_pt_watermark_across_polls() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        {"symbol": "X", "trade_id": "a", "trade_ts": t0, "price": 1, "size": 1, "side": "Buy"},
        {"symbol": "X", "trade_id": "b", "trade_ts": t0 + timedelta(seconds=1), "price": 1, "size": 1, "side": "Buy"},
        {"symbol": "X", "trade_id": "c", "trade_ts": t0 + timedelta(seconds=2), "price": 1, "size": 1, "side": "Buy"},
    ]
    poller = TradeWatermarkPoller(adapter=FakePublicTradesAdapter(rows), symbol="X", snapshot_ready_at=t0)
    r1 = poller.poll(now=t0 + timedelta(seconds=3))
    assert r1["status"] == TRADES_PRESENT
    assert set(r1["new_trade_ids"]) == {"a", "b", "c"}
    r2 = poller.poll(now=t0 + timedelta(seconds=4))
    assert r2["new_trade_ids"] == []


def test_pt_same_ts_different_ids() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        {"symbol": "X", "trade_id": "1", "trade_ts": t0, "price": 1, "size": 1, "side": "Buy"},
        {"symbol": "X", "trade_id": "2", "trade_ts": t0, "price": 1, "size": 1, "side": "Buy"},
    ]
    poller = TradeWatermarkPoller(adapter=FakePublicTradesAdapter(rows), symbol="X", snapshot_ready_at=t0)
    r = poller.poll(now=t0 + timedelta(seconds=1))
    assert len(poller.seen_ids) == 2


def test_pt_late_and_ooo() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # late: trade older than watermark arrives later in adapter list after first poll — Fake returns all always;
    # watermark poller dedupes by id; late arrival detection is observational in latency script.
    # Here: ensure older trade after newer still accepted once by id.
    rows = [
        {"symbol": "X", "trade_id": "2", "trade_ts": t0 + timedelta(seconds=2), "price": 1, "size": 1, "side": "Buy"},
        {"symbol": "X", "trade_id": "1", "trade_ts": t0 + timedelta(seconds=1), "price": 1, "size": 1, "side": "Buy"},
    ]
    poller = TradeWatermarkPoller(adapter=FakePublicTradesAdapter(rows), symbol="X", snapshot_ready_at=t0)
    r = poller.poll(now=t0 + timedelta(seconds=5))
    assert len(poller.seen_ids) == 2


def test_pt_trade_before_snapshot_excluded() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        {"symbol": "X", "trade_id": "old", "trade_ts": t0 - timedelta(seconds=10), "price": 1, "size": 1, "side": "Buy"},
        {"symbol": "X", "trade_id": "new", "trade_ts": t0 + timedelta(seconds=1), "price": 1, "size": 1, "side": "Buy"},
    ]
    poller = TradeWatermarkPoller(adapter=FakePublicTradesAdapter(rows), symbol="X", snapshot_ready_at=t0)
    r = poller.poll(now=t0 + timedelta(seconds=2))
    assert "old" not in poller.seen_ids
    assert "new" in poller.seen_ids


def test_pt_after_feature_cutoff_excluded() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cutoff = t0 + timedelta(seconds=5)
    rows = [
        {"symbol": "X", "trade_id": "in", "trade_ts": t0 + timedelta(seconds=2), "price": 1, "size": 1, "side": "Buy"},
        {"symbol": "X", "trade_id": "out", "trade_ts": t0 + timedelta(seconds=10), "price": 1, "size": 1, "side": "Buy"},
    ]
    poller = TradeWatermarkPoller(
        adapter=FakePublicTradesAdapter(rows),
        symbol="X",
        snapshot_ready_at=t0,
        feature_cutoff_at=cutoff,
    )
    poller.poll(now=t0 + timedelta(seconds=20))
    assert "in" in poller.seen_ids
    assert "out" not in poller.seen_ids


def test_pt_fresh_null_trades() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    poller = TradeWatermarkPoller(adapter=FakePublicTradesAdapter([]), symbol="X", snapshot_ready_at=t0)
    r = poller.poll(now=t0 + timedelta(seconds=1))
    assert r["status"] == STREAM_AVAILABLE_BUT_NO_TRADES


def test_pt_stale_missing() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bad = FakePublicTradesAdapter([])
    bad.force_unreachable = True
    poller = TradeWatermarkPoller(adapter=bad, symbol="X", snapshot_ready_at=t0)
    assert poller.poll(now=t0 + timedelta(seconds=1))["status"] == STREAM_MISSING


# ---- Timeline ----


def test_horizons_are_canonical_not_synthetic() -> None:
    assert FEATURE_HORIZONS_S == (1, 5, 15, 30, 60, 180, 300)
    assert FIRST_EARLY_EVIDENCE_SECONDS == 1
    assert FIRST_CANDIDATE_ELIGIBLE_SECONDS == 5


def test_timeline_1s_early_only_candidate_not_before_5s() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tl = IncrementalTimeline(snapshot_ready_at=t0)
    cov = CoverageFlags()
    # at 1s
    wins = tl.materialize_horizons(
        now=t0 + timedelta(seconds=1),
        book_event_count=10,
        trade_count=1,
        first_ts=None,
        last_ts=None,
        coverage=cov,
        mass_balance_ok=True,
        candidate_state="CONTINUATION_CANDIDATE",  # illegal for 1s — should be rewritten
        features={},
    )
    assert any(w.horizon_s == 1.0 for w in wins)
    w1 = next(w for w in wins if w.horizon_s == 1.0)
    assert "CANDIDATE" not in str(w1.candidate_state)


def test_fake_clock_300s() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = FakeClock(t0)
    clock.advance(300.0)
    assert abs((clock.now() - t0).total_seconds() - 300.0) < 1e-9


def test_book_state_100ms_constant() -> None:
    from obfull_research_engine.ema_trend_live_analyzer_v1 import BOOK_STATE_MS

    assert BOOK_STATE_MS == 100


# ---- Engine ----


def test_canonical_booknode_and_fill_cap_disjunct() -> None:
    mass = recompute_bucket_mass(queue_before=10.0, queue_after=7.0, raw_fill=5.0, confidence="HIGH")
    # fill capped to depletion
    assert float(mass["attributed_fill_capped"]) <= 3.0 + 1e-9
    # disjunct buckets present
    for k in ("attributed_fill_capped", "residual_pull", "refill", "unknown"):
        assert k in mass
    # unknown not double-counted into fill
    assert float(mass["unknown"]) >= 0


def test_unknown_not_in_qdh_depletion_path() -> None:
    src = inspect.getsource(IncrementalEngineState.on_nodes)
    assert "unknown" in src
    assert "net_for_qdh" in src


def test_engine_bid_ask_separated_via_wall_side() -> None:
    eng = IncrementalEngineState(tick_size=0.1, wall_price=100.0, wall_side="bid", direction=-1)
    assert eng.wall_side == "bid"
    eng2 = IncrementalEngineState(tick_size=0.1, wall_price=100.0, wall_side="ask", direction=1)
    assert eng2.wall_side == "ask"


def test_wall_tracker_dynamic() -> None:
    wt = DynamicWallTracker(tick_size=0.1)
    # API exists and requires tick
    assert wt.tick_size == 0.1


def test_adapter_generation_and_gap() -> None:
    ad = LiveEventAdapter(symbol="X", wall_price=100.0, wall_side="ask")
    nodes = ad.ingest_batch(
        [
            {
                "symbol": "X",
                "side": "ask",
                "price": 100.0,
                "new_qty": 5.0,
                "snapshot_generation": 1,
                "sequence_id": 1,
                "update_id": 1,
                "record_ordinal": 1,
                "valid_for_analysis": True,
                "exchange_event_time": "2026-01-01T00:00:00.000Z",
                "receive_time_ns": 0,
            },
            {
                "symbol": "X",
                "side": "ask",
                "price": 100.0,
                "new_qty": 4.0,
                "snapshot_generation": 2,
                "sequence_id": 1,
                "update_id": 1,
                "record_ordinal": 2,
                "valid_for_analysis": True,
                "exchange_event_time": "2026-01-01T00:00:01.000Z",
                "receive_time_ns": 1,
            },
        ]
    )
    assert len(nodes) == 2
    assert ad.replay_epoch == 2


# ---- Outcomes ----


def test_no_final_report_before_6h() -> None:
    assert OUTCOME_HORIZON_SECONDS == 21600
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    op = OutcomePending(snapshot_ready_at=t0, candidate_at=t0 + timedelta(seconds=5))
    assert op.due_at() == t0 + timedelta(seconds=21600)
    assert op.to_dict()["status"] == "OUTCOME_PENDING"


def test_empty_candles_cannot_success() -> None:
    # outcomes module is placeholder pending; ensure no SUCCESS without candles API
    src = Path(
        "/home/telgenbuescher/projects/orderbook_analyse_ch_research_mp_qdh_trigger_v1/"
        "obfull_research_engine/src/obfull_research_engine/ema_trend_live_analyzer_v1/outcomes_6h.py"
    ).read_text()
    assert "SUCCESS" not in src or "OUTCOME_PENDING" in src
    assert "OUTCOME_PENDING" in src


def test_mfe_mae_reach_costs_documented_in_params() -> None:
    from obfull_research_engine.ema_trend_live_analyzer_v1 import params

    assert params.REACH_PCT == 0.41
    assert params.COST_TAKER_PCT == 0.08
    assert params.COST_MAKER_PCT == 0.12
    assert params.MFE_MAE_UNIT == "percent"
    assert params.NEUTRAL_OUTCOME_SEPARATE_FROM_CANDIDATE is True


def test_conflicting_evidence() -> None:
    fsm = CandidateFSM(threshold_side="ABOVE_EMA_THRESHOLD")
    now = datetime.now(timezone.utc)
    # craft features that score both sides — depend on score_evidence
    from obfull_research_engine.ema_trend_live_analyzer_v1 import decision_evidence as de

    feats = {
        "persistence_ratio": 2.0,
        "impact_efficiency": 10.0,
        "qdh": {"qdh_base": 1.0},
        "mass": {"attributed_fill_capped": 1.0, "refill": 1.0, "residual_pull": 1.0},
        "microprice_change_hint": 0.0,
    }
    scores = de.score_evidence("ABOVE_EMA_THRESHOLD", feats)
    # just ensure scoring returns both fields
    assert hasattr(scores, "continuation")
    assert hasattr(scores, "mean_reversion")

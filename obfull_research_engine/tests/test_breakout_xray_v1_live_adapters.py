"""Phase-3 live adapter / dual-gate / mock E2E tests (no real ClickHouse)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from obfull_research_engine.breakout_xray_v1 import (
    EXPLICIT_EXECUTION_REQUIRED,
    FULL_RUN_BLOCK_REASON,
)
from obfull_research_engine.breakout_xray_v1.adapters.bronze_baseline import (
    BronzeRecordFull,
    LiveBaselineBookRepository,
    XRAY_BRONZE_STREAM_SETTINGS,
    replay_baseline_fullbook,
)
from obfull_research_engine.breakout_xray_v1.adapters.live_data import (
    LiveMarketProfileRepository,
    LivePublicTradesRepository,
    LiveReadinessRepository,
    LiveSilverLevelChangesRepository,
    LiveSilverMetricsRepository,
    assert_select_only_bounded,
    level_changes_sql,
    metrics_mid_sql,
    mid_series_quality,
    public_trades_sql,
)
from obfull_research_engine.breakout_xray_v1.adapters.live_factory import (
    LiveAnalysisConfig,
    build_live_analysis_dependencies,
    build_live_bundle,
)
from obfull_research_engine.breakout_xray_v1.adapters.market_profile import (
    profile_from_fixture,
)
from obfull_research_engine.breakout_xray_v1.baseline_hash import (
    START_NOT_ALIGNED_TO_100MS,
    UNRESOLVED_BASELINE_HASH_MISMATCH,
    UNRESOLVED_BASELINE_MISSING_SILVER_HASH,
    predecessor_bucket_start_ns,
    resolve_lc_price_band,
)
from obfull_research_engine.breakout_xray_v1.cli import build_parser, main
from obfull_research_engine.breakout_xray_v1.execution_hold import (
    STOP_ACTIVE_SILVER_BUILDER_DURING_ANALYSIS,
    ExecutionSentinel,
    assert_live_execution_allowed,
)
from obfull_research_engine.breakout_xray_v1.models import (
    EdgeSide,
    ManualWindowConfig,
    StrategyEdgeConfig,
)
from obfull_research_engine.breakout_xray_v1.ports import MidState, ReadinessResult
from obfull_research_engine.breakout_xray_v1.resource_preflight import (
    STOP_RESOURCE_PREFLIGHT_RAM,
    ResourcePreflightConfig,
    run_host_resource_preflight,
)
from obfull_research_engine.breakout_xray_v1.run import (
    run_manual_window_analysis,
    run_strategy_edge_analysis,
)
from obfull_research_engine.breakout_xray_v1.trades import dedup_trades_by_id
from obfull_research_engine.breakout_xray_v1.writers import (
    ARTIFACTS,
    OUTPUT_DIR_ALREADY_EXISTS,
    STATUS_COMPLETE,
    STATUS_FAILED,
    RunOutputSession,
)
from obfull_research_engine.clickhouse_research_store_v1.analysis_readiness_v1_3 import (
    ChunkAssessment,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_replay import (
    book_map_sha256,
)


UTC = timezone.utc
MS100 = 100_000_000


def test_dual_gate_requires_execute_live(tmp_path: Path):
    lock = tmp_path / "nolock"
    with pytest.raises(RuntimeError) as ei:
        assert_live_execution_allowed(execute_live=False, lock_path=lock)
    assert EXPLICIT_EXECUTION_REQUIRED in str(ei.value)


def test_dual_gate_lock_never_overridable(tmp_path: Path):
    lock = tmp_path / "build.lock"
    lock.write_text(json.dumps({"pid": 1}), encoding="utf-8")
    from obfull_research_engine.breakout_xray_v1.execution_hold import read_builder_lock

    if not read_builder_lock(lock).pid_alive:
        pytest.skip("pid 1 not alive")
    with pytest.raises(RuntimeError) as ei:
        assert_live_execution_allowed(execute_live=True, lock_path=lock)
    assert FULL_RUN_BLOCK_REASON in str(ei.value)


def test_cli_without_execute_live_blocks_no_client(tmp_path: Path):
    lock = tmp_path / "nolock"
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


def test_cli_check_only_not_alias_for_execute_live():
    p = build_parser()
    args = p.parse_args(
        [
            "manual-window",
            "--symbol",
            "BTCUSDT",
            "--start-utc",
            "2026-09-06T17:10:00Z",
            "--end-utc",
            "2026-09-06T17:20:00Z",
            "--output-dir",
            "/tmp/x",
            "--check-only",
        ]
    )
    assert args.check_only is True
    assert args.execute_live is False


def test_forbidden_sql_rejected():
    with pytest.raises(RuntimeError):
        assert_select_only_bounded("INSERT INTO t VALUES (1)")
    with pytest.raises(RuntimeError):
        assert_select_only_bounded(
            "SELECT * FROM research_full_ob_silver_v1_3.ob_metrics_100ms_v1_3"
        )
    # leading comment + CTE ok
    assert_select_only_bounded(
        """
        -- comment
        WITH x AS (SELECT 1 AS a)
        SELECT a FROM x WHERE chunk_key IN {chunk_keys:Array(String)}
          AND bucket_start_ns >= {start_ns:UInt64}
        """
    )
    with pytest.raises(RuntimeError):
        assert_select_only_bounded(
            """
            WITH x AS (SELECT 1 AS a)
            DELETE FROM t WHERE id = 1
            """
        )
    with pytest.raises(RuntimeError):
        assert_select_only_bounded("select 1; drop table t")
    # DML word only in comment/string must pass when otherwise SELECT-bounded
    assert_select_only_bounded(
        """
        SELECT 1 AS delete_col /* drop table */
        FROM research_full_ob_silver_v1_3.ob_metrics_100ms_v1_3
        WHERE chunk_key IN {chunk_keys:Array(String)}
          AND bucket_start_ns >= {start_ns:UInt64}
          AND 'insert into' = 'insert into'
        """
    )


def test_metrics_sql_bounds_and_normalize():
    sql = metrics_mid_sql(database="research_full_ob_silver_v1_3")
    assert_select_only_bounded(sql)
    assert "chunk_key IN" in sql
    assert "ob_metrics_100ms_v1_3" in sql
    client = MagicMock()
    client.query.return_value = [
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
                }
            ),
        },
    ]
    lock = Path("/tmp/xray_p3_metrics_nolock")
    if lock.exists():
        lock.unlink()
    repo = LiveSilverMetricsRepository(
        client, database="research_full_ob_silver_v1_3", lock_path=lock, require_lock_gate=False
    )
    out = repo.load_mid_series(
        symbol="BTCUSDT", start_ns=1000, end_ns=1000 + 2 * MS100, chunk_keys=("c1",)
    )
    assert len(out) == 2
    params = client.query.call_args.kwargs["parameters"]
    assert params["chunk_keys"] == ["c1"]
    assert client.query.call_args.kwargs["settings"]["readonly"] == 1
    q = mid_series_quality(out, start_ns=1000, end_ns=1000 + 2 * MS100)
    assert q["monotonic"] is True
    assert q["book_hash_semantics"].startswith("state_at_bucket_end")


def test_public_trades_raw_dedup_in_core_20m_case():
    ts = datetime(2026, 9, 6, 18, 0, 0, 123000, tzinfo=UTC)
    client = MagicMock()
    client.query.return_value = [
        {
            "trade_ts": ts,
            "trade_id": "mega",
            "side": "Buy",
            "price": 80027.53,
            "size": 250.0,
            "notional": 20_006_882.5,
        },
        {
            "trade_ts": ts,
            "trade_id": "mega",
            "side": "Buy",
            "price": 80027.53,
            "size": 250.0,
            "notional": 20_006_882.5,
        },
        {
            "trade_ts": ts,
            "trade_id": "other",
            "side": "Sell",
            "price": 80000.0,
            "size": 1.0,
            "notional": 80000.0,
        },
    ]
    lock = Path("/tmp/xray_p3_trades_nolock")
    if lock.exists():
        lock.unlink()
    repo = LivePublicTradesRepository(client, lock_path=lock, require_lock_gate=False)
    raw = repo.load_trades(symbol="BTCUSDT", start_ns=1, end_ns=9)
    assert len(raw) == 3
    deduped, stats = dedup_trades_by_id(raw)
    assert len(deduped) == 2
    assert stats.duplicate_rows == 1
    assert stats.largest_buy["trade_id"] == "mega"
    assert stats.largest_buy["notional"] == 20_006_882.5
    assert "ORDER BY trade_ts, trade_id" in public_trades_sql()


def test_level_changes_sql_price_band_bound():
    sql = level_changes_sql(database="research_full_ob_silver_v1_3")
    assert_select_only_bounded(sql)
    assert "JSONExtractFloat(payload, 'price')" in sql
    assert "price_min" in sql and "price_max" in sql
    assert "GROUP BY" not in sql.upper()
    client = MagicMock()
    client.query.return_value = [
        {
            "event_time_ns": 100,
            "chunk_key": "c1",
            "apply_order": 1,
            "record_ordinal": 10,
            "payload": json.dumps(
                {
                    "side": "ask",
                    "price": 100.0,
                    "old_size": 5.0,
                    "new_size": 4.0,
                    "change_type": "UPDATE",
                    "event_time_ns": 100,
                }
            ),
        }
    ]
    lock = Path("/tmp/xray_p3_lc_nolock")
    if lock.exists():
        lock.unlink()
    repo = LiveSilverLevelChangesRepository(
        client,
        database="research_full_ob_silver_v1_3",
        lock_path=lock,
        require_lock_gate=False,
    )
    evs = repo.load_level_changes(
        symbol="BTCUSDT",
        start_ns=0,
        end_ns=200,
        chunk_keys=("c1",),
        price_min=50.0,
        price_max=150.0,
    )
    assert len(evs) == 1
    params = client.query.call_args.kwargs["parameters"]
    assert params["price_min"] == 50.0
    assert params["price_max"] == 150.0
    with pytest.raises(RuntimeError) as ei:
        repo.load_level_changes(
            symbol="BTCUSDT",
            start_ns=0,
            end_ns=200,
            chunk_keys=("c1",),
            price_min=0.0,
            price_max=10_000.0,
        )
    assert "STOP_LC_PRICE_BAND_TOO_WIDE" in str(ei.value)


def test_lc_price_band_modes():
    band = resolve_lc_price_band(
        reference_price=100.0, local_band_usd=400.0, mid_series=[]
    )
    assert band == ( -300.0, 500.0)
    mids = [
        MidState(0, 10.0, None, None, None, "h"),
        MidState(MS100, 50.0, None, None, None, "h"),
    ]
    lo, hi = resolve_lc_price_band(
        reference_price=None, local_band_usd=5.0, mid_series=mids
    )
    assert lo == 5.0 and hi == 55.0


def test_not_ready_stops_before_other_repos(tmp_path: Path):
    from obfull_research_engine.breakout_xray_v1.fakes import make_fake_deps

    deps = make_fake_deps(
        readiness=ReadinessResult(status="NOT_READY", reason="GAP"),
    )
    metrics_calls = []

    class Boom:
        def load_mid_series(self, **kwargs):
            metrics_calls.append(kwargs)
            raise AssertionError("should not load metrics")

        def load_book_hash_at_bucket(self, **kwargs):
            raise AssertionError("no")

        def load_minute_rows(self, **kwargs):
            raise AssertionError("no")

    deps.metrics = Boom()  # type: ignore[assignment]
    out = tmp_path / "nr"
    cfg = ManualWindowConfig(
        symbol="BTCUSDT",
        start_utc=datetime(2026, 9, 6, 17, 10, tzinfo=UTC),
        end_utc=datetime(2026, 9, 6, 17, 20, tzinfo=UTC),
        local_band_usd=400.0,
        output_dir=str(out),
    )
    result = run_manual_window_analysis(
        cfg, deps=deps, lock_path=tmp_path / "nolock", skip_execution_hold=True
    )
    assert result.data_quality.get("loaders_invoked") is False
    assert metrics_calls == []
    assert not out.exists()
    failed = list(tmp_path.glob("nr.failed.*"))
    assert len(failed) == 1
    man = json.loads((failed[0] / "manifest.json").read_text())
    assert man["status"] == STATUS_FAILED


def test_manual_without_reference_skips_mp(tmp_path: Path):
    from obfull_research_engine.breakout_xray_v1.fakes import _StaticBaseline, make_fake_deps
    from obfull_research_engine.breakout_xray_v1.models import BaselineBookState

    deps = make_fake_deps(expected_book_hash="dead")
    deps.baseline = _StaticBaseline(  # type: ignore[assignment]
        BaselineBookState(
            source="fake",
            timestamp=None,
            age_ms=None,
            complete=True,
            hash="dead",
            unresolved_reason=None,
        )
    )
    deps.metrics.default_book_hash = "dead"  # type: ignore[attr-defined]
    deps.metrics.mid_series = [  # type: ignore[attr-defined]
        MidState(0, 100.0, None, None, None, "dead"),
        MidState(MS100, 120.0, None, None, None, "dead"),
    ]
    mp = LiveMarketProfileRepository(enabled=True, require_lock_gate=False)
    deps.market_profile = mp  # type: ignore[assignment]
    cfg = ManualWindowConfig(
        symbol="BTCUSDT",
        start_utc=datetime(2026, 9, 6, 17, 10, tzinfo=UTC),
        end_utc=datetime(2026, 9, 6, 17, 20, tzinfo=UTC),
        local_band_usd=400.0,
        output_dir=str(tmp_path / "man"),
        reference_price=None,
    )
    run_manual_window_analysis(
        cfg, deps=deps, lock_path=tmp_path / "nolock", skip_execution_hold=True
    )
    assert mp.calls == []


def test_baseline_hash_mismatch_unresolved(tmp_path: Path):
    snap = BronzeRecordFull(
        0,
        1,
        1000,
        "snapshot",
        bids=[[100.0, 1.0]],
        asks=[[101.0, 1.0]],
        u=1,
        seq=1,
    )

    class Ep:
        epoch_id = "e1"
        safe_start_ns = 0
        safe_end_ns = 10_000
        anchor_segment_chain_index = 0
        anchor_record_ordinal = 1

    repo = LiveBaselineBookRepository(
        require_lock_gate=False,
        epochs=[Ep()],
        record_source=lambda **kwargs: [snap],
    )
    bad = repo.load_baseline(
        symbol="BTCUSDT", start_ns=3000, epoch_id="e1", expected_book_hash="deadbeef"
    )
    assert bad.complete is False
    assert bad.unresolved_reason == UNRESOLVED_BASELINE_HASH_MISMATCH


def test_event_at_start_ns_not_in_baseline():
    snap = BronzeRecordFull(
        0, 1, 1000, "snapshot", bids=[[100.0, 1.0]], asks=[[101.0, 1.0]], u=1, seq=1
    )
    at_start = BronzeRecordFull(
        0,
        2,
        3000,
        "delta",
        bids=[[100.0, 9.0]],
        asks=[],
        u=2,
        seq=2,
    )
    digest_before = book_map_sha256({100.0: 1.0}, {101.0: 1.0})
    st = replay_baseline_fullbook(
        symbol="BTCUSDT",
        records=[snap, at_start],
        start_ns=3000,
        expected_book_hash=digest_before,
        enforce_continuity=False,
    )
    assert st.complete is True
    assert st.hash == digest_before


def test_start_not_aligned_rejected(tmp_path: Path):
    from obfull_research_engine.breakout_xray_v1.fakes import make_fake_deps

    deps = make_fake_deps(expected_book_hash="x")
    cfg = ManualWindowConfig(
        symbol="BTCUSDT",
        start_utc=datetime(2026, 9, 6, 17, 10, 0, 123000, tzinfo=UTC),
        end_utc=datetime(2026, 9, 6, 17, 20, tzinfo=UTC),
        local_band_usd=400.0,
        output_dir=str(tmp_path / "badstart"),
    )
    result = run_manual_window_analysis(
        cfg, deps=deps, lock_path=tmp_path / "nolock", skip_execution_hold=True
    )
    assert result.data_quality.get("failure", {}).get("failure_reason") == START_NOT_ALIGNED_TO_100MS


def test_factory_gates_before_client(tmp_path: Path):
    calls = []

    def factory():
        calls.append("client")
        return MagicMock()

    with pytest.raises(RuntimeError) as ei:
        build_live_analysis_dependencies(
            LiveAnalysisConfig(
                symbol="BTCUSDT",
                chain_version="cv",
                chain_hash="ch",
                lock_path=tmp_path / "nolock",
                execute_live=False,
                client_factory=factory,
                output_dir=tmp_path / "out",
                skip_host_preflight=True,
            )
        )
    assert EXPLICIT_EXECUTION_REQUIRED in str(ei.value)
    assert calls == []


def test_preflight_failure_skips_client_factory(tmp_path: Path):
    calls = []

    def factory():
        calls.append("client")
        return MagicMock()

    with pytest.raises(RuntimeError) as ei:
        build_live_bundle(
            LiveAnalysisConfig(
                symbol="BTCUSDT",
                chain_version="cv",
                chain_hash="ch",
                lock_path=tmp_path / "nolock",
                execute_live=True,
                client_factory=factory,
                output_dir=tmp_path / "out",
                preflight_config=ResourcePreflightConfig(
                    min_available_ram_bytes=10**18,  # impossible
                    min_free_disk_bytes=1 * 1024**3,
                ),
            )
        )
    assert STOP_RESOURCE_PREFLIGHT_RAM in str(ei.value)
    assert calls == []


def test_execution_sentinel_mid_run(tmp_path: Path):
    lock = tmp_path / "build.lock"
    sentinel = ExecutionSentinel(lock)
    client = MagicMock()
    client.query.return_value = []
    repo = LiveSilverMetricsRepository(
        client,
        database="research_full_ob_silver_v1_3",
        lock_path=lock,
        require_lock_gate=False,
        sentinel=sentinel,
    )
    repo.load_mid_series(
        symbol="BTCUSDT", start_ns=0, end_ns=MS100, chunk_keys=("c1",)
    )
    # Simulate active builder lock mid-run
    lock.write_text(json.dumps({"pid": 1}), encoding="utf-8")
    from obfull_research_engine.breakout_xray_v1.execution_hold import read_builder_lock

    if not read_builder_lock(lock).pid_alive:
        pytest.skip("pid 1 not alive")
    with pytest.raises(RuntimeError) as ei:
        repo.load_book_hash_at_bucket(
            symbol="BTCUSDT", bucket_start_ns=0, chunk_keys=("c1",)
        )
    assert STOP_ACTIVE_SILVER_BUILDER_DURING_ANALYSIS in str(ei.value)
    assert client.query.call_count == 1  # second call blocked


def test_atomic_writer_success_and_exists(tmp_path: Path):
    from obfull_research_engine.breakout_xray_v1.fakes import make_fake_deps
    from obfull_research_engine.breakout_xray_v1.models import BaselineBookState
    from obfull_research_engine.breakout_xray_v1.fakes import _StaticBaseline

    digest = "abc"
    deps = make_fake_deps(expected_book_hash=digest)
    deps.baseline = _StaticBaseline(  # type: ignore[assignment]
        BaselineBookState(
            source="fake",
            timestamp=None,
            age_ms=None,
            complete=True,
            hash=digest,
            unresolved_reason=None,
        )
    )
    deps.metrics.mid_series = [MidState(0, 1.0, None, None, None, digest)]  # type: ignore[attr-defined]
    out = tmp_path / "ok"
    cfg = ManualWindowConfig(
        symbol="BTCUSDT",
        start_utc=datetime(2026, 9, 6, 17, 10, tzinfo=UTC),
        end_utc=datetime(2026, 9, 6, 17, 20, tzinfo=UTC),
        local_band_usd=400.0,
        output_dir=str(out),
        reference_price=100.0,
        reference_side=EdgeSide.UPPER,
    )
    result = run_manual_window_analysis(
        cfg, deps=deps, lock_path=tmp_path / "nolock", skip_execution_hold=True
    )
    assert out.exists()
    man = json.loads((out / "manifest.json").read_text())
    assert man["status"] == STATUS_COMPLETE
    assert not list(tmp_path.glob("ok.running.*"))
    assert result.baseline.complete is True
    with pytest.raises(RuntimeError) as ei:
        RunOutputSession(out)
    assert OUTPUT_DIR_ALREADY_EXISTS in str(ei.value)


def test_bronze_stream_settings_hardened():
    assert XRAY_BRONZE_STREAM_SETTINGS["readonly"] == 1
    assert XRAY_BRONZE_STREAM_SETTINGS["max_threads"] == 1
    assert "max_memory_usage" in XRAY_BRONZE_STREAM_SETTINGS
    assert "max_execution_time" in XRAY_BRONZE_STREAM_SETTINGS
    assert "max_result_rows" in XRAY_BRONZE_STREAM_SETTINGS


def test_bronze_bound_mode_provenance(tmp_path: Path):
    """Apply-keys vs chain-index fallback must be explicit in baseline provenance."""
    from typing import Any

    from obfull_research_engine.breakout_xray_v1.adapters.bronze_baseline import (
        BASELINE_VALIDATION_REQUIRES_LIVE_PARITY,
        BRONZE_BOUND_APPLY_KEYS,
        BRONZE_BOUND_CHAIN_INDEX_FALLBACK,
    )

    snap = BronzeRecordFull(
        0, 1, 1000, "snapshot", bids=[[100.0, 1.0]], asks=[[101.0, 1.0]], u=1, seq=1
    )
    digest = book_map_sha256({100.0: 1.0}, {101.0: 1.0})

    class EpApply:
        epoch_id = "e1"
        safe_start_ns = 0
        safe_end_ns = 10_000
        anchor_segment_chain_index = 0
        anchor_record_ordinal = 1
        apply_end_segment_chain_index = 0
        apply_end_record_ordinal = 99

    class EpFallback:
        epoch_id = "e1"
        safe_start_ns = 0
        safe_end_ns = 10_000
        anchor_segment_chain_index = 0
        anchor_record_ordinal = 1
        apply_end_segment_chain_index = None
        apply_end_record_ordinal = None

    class FakeCfg:
        symbol = "BTCUSDT"
        chain_version = "cv"
        input_database = "research_full_ob_continuous_v1_3"
        start_chain_index = 0
        end_chain_index = 10

    streamed: list[dict[str, Any]] = []

    class FakeClient:
        def query_row_block_stream(self, sql, parameters=None, settings=None):
            streamed.append(
                {
                    "sql": sql,
                    "parameters": dict(parameters or {}),
                    "settings": dict(settings or {}),
                }
            )

            class _Ctx:
                def __enter__(self_inner):
                    return []

                def __exit__(self_inner, *a):
                    return False

            return _Ctx()

    repo_a = LiveBaselineBookRepository(
        client=FakeClient(),
        build_config=FakeCfg(),
        require_lock_gate=False,
        epochs=[EpApply()],
    )
    repo_a._resource_guard_preflight = lambda: None  # type: ignore[method-assign]
    repo_a._load_records(epoch=EpApply(), start_ns=3000)
    assert repo_a.last_bronze_bound_mode == BRONZE_BOUND_APPLY_KEYS
    assert "start_rank" in streamed[-1]["parameters"]
    assert streamed[-1]["settings"].get("readonly") == 1

    streamed.clear()
    repo_f = LiveBaselineBookRepository(
        client=FakeClient(),
        build_config=FakeCfg(),
        require_lock_gate=False,
        epochs=[EpFallback()],
    )
    repo_f._resource_guard_preflight = lambda: None  # type: ignore[method-assign]
    repo_f._load_records(epoch=EpFallback(), start_ns=3000)
    assert repo_f.last_bronze_bound_mode == BRONZE_BOUND_CHAIN_INDEX_FALLBACK
    params = streamed[-1]["parameters"]
    assert "start_index" in params and "end_index" in params
    assert "event_time_ns_to" in params
    assert "symbol" in params and "chain_version" in params
    assert "ORDER BY e.canonical_segment_chain_index, e.record_ordinal" in streamed[-1]["sql"]

    st = replay_baseline_fullbook(
        symbol="BTCUSDT",
        records=[snap],
        start_ns=3000,
        expected_book_hash=digest,
        enforce_continuity=False,
    )
    repo_src = LiveBaselineBookRepository(require_lock_gate=False, epochs=[EpApply()])
    repo_src.last_bronze_bound_mode = BRONZE_BOUND_CHAIN_INDEX_FALLBACK
    tagged = repo_src._attach_bound_provenance(st)
    d = tagged.to_dict()
    assert d["bronze_bound_mode"] == BRONZE_BOUND_CHAIN_INDEX_FALLBACK
    assert d["baseline_validation_status"] == BASELINE_VALIDATION_REQUIRES_LIVE_PARITY

    repo_src.last_bronze_bound_mode = BRONZE_BOUND_APPLY_KEYS
    tagged2 = repo_src._attach_bound_provenance(st)
    d2 = tagged2.to_dict()
    assert d2["bronze_bound_mode"] == BRONZE_BOUND_APPLY_KEYS
    assert "baseline_validation_status" not in d2


def test_live_mock_e2e_both_modes(tmp_path: Path):
    """Full live-adapter path with mocked client; no network."""
    lock = tmp_path / "nolock"
    start = datetime(2026, 9, 6, 17, 30, tzinfo=UTC)
    end = datetime(2026, 9, 6, 18, 30, tzinfo=UTC)
    decision = datetime(2026, 9, 6, 18, 0, tzinfo=UTC)
    from obfull_research_engine.breakout_xray_v1.time_windows import dt_to_ns

    start_ns = dt_to_ns(start)
    end_ns = dt_to_ns(end)
    wall_px = 79730.25
    digest = book_map_sha256({79600.0: 2.0}, {wall_px: 10.0})
    pred = predecessor_bucket_start_ns(start_ns)

    ready_chunk = ChunkAssessment(
        chunk_key="c1",
        epoch_id="e1",
        start_ns=start_ns - MS100 * 20,
        end_ns=end_ns + 1000,
        level_change_count=2,
        state_count=10,
        output_hash="a" * 64,
        ledger_status="COMPLETE",
        observed_level_changes=2,
        observed_states=10,
        status="READY",
    )

    client = MagicMock()

    def query(sql, parameters=None, settings=None):
        s = sql.lower()
        parameters = parameters or {}
        if "ob_metrics_100ms" in s:
            if parameters.get("bucket_start_ns") is not None and "start_ns" not in parameters:
                b = int(parameters["bucket_start_ns"])
                return [
                    {
                        "bucket_start_ns": b,
                        "chunk_key": "c1",
                        "payload": json.dumps(
                            {
                                "mid": 79600.0,
                                "spread": 1.0,
                                "best_bid": 79599.0,
                                "best_ask": 79601.0,
                                "book_hash": digest,
                            }
                        ),
                    }
                ]
            rows = []
            for i in range(20):
                ns = start_ns + i * MS100
                rows.append(
                    {
                        "bucket_start_ns": ns,
                        "chunk_key": "c1",
                        "payload": json.dumps(
                            {
                                "mid": 79600.0 + i * 0.1,
                                "spread": 1.0,
                                "best_bid": 79599.0,
                                "best_ask": 79601.0,
                                "book_hash": digest,
                            }
                        ),
                    }
                )
            for i in range(100):
                ns = dt_to_ns(decision) + i * MS100
                rows.append(
                    {
                        "bucket_start_ns": ns,
                        "chunk_key": "c1",
                        "payload": json.dumps(
                            {
                                "mid": 79720.0,
                                "spread": 1.0,
                                "best_bid": 79719.0,
                                "best_ask": 79721.0,
                                "book_hash": digest,
                            }
                        ),
                    }
                )
            return rows
        if "ob_level_changes" in s:
            assert "price_min" in parameters and "price_max" in parameters
            return [
                {
                    "event_time_ns": dt_to_ns(datetime(2026, 9, 6, 17, 45, tzinfo=UTC)),
                    "chunk_key": "c1",
                    "apply_order": 1,
                    "record_ordinal": 1,
                    "payload": json.dumps(
                        {
                            "side": "ask",
                            "price": wall_px,
                            "old_size": 0.0,
                            "new_size": 10.0,
                            "change_type": "ADD",
                            "event_time_ns": dt_to_ns(
                                datetime(2026, 9, 6, 17, 45, tzinfo=UTC)
                            ),
                        }
                    ),
                }
            ]
        if "public_trades_canonical" in s:
            return [
                {
                    "trade_ts": datetime(2026, 9, 6, 18, 0, 5, tzinfo=UTC),
                    "trade_id": "mega",
                    "side": "Buy",
                    "price": wall_px,
                    "size": 250.0,
                    "notional": 20_006_882.5,
                },
                {
                    "trade_ts": datetime(2026, 9, 6, 18, 0, 5, tzinfo=UTC),
                    "trade_id": "mega",
                    "side": "Buy",
                    "price": wall_px,
                    "size": 250.0,
                    "notional": 20_006_882.5,
                },
            ]
        return []

    client.query.side_effect = query

    snap = BronzeRecordFull(
        0,
        1,
        start_ns - 1_000_000_000,
        "snapshot",
        bids=[[79600.0, 2.0]],
        asks=[[wall_px, 10.0]],
        u=1,
        seq=1,
    )

    class Ep:
        epoch_id = "e1"
        safe_start_ns = start_ns - 10_000
        safe_end_ns = end_ns + 10_000
        anchor_segment_chain_index = 0
        anchor_record_ordinal = 1
        apply_end_segment_chain_index = None
        apply_end_record_ordinal = None

    cfg = LiveAnalysisConfig(
        symbol="BTCUSDT",
        chain_version="cv",
        chain_hash="ch",
        lock_path=lock,
        execute_live=True,
        client=client,
        require_lock_gate=False,
        enable_market_profile=True,
        verify_readiness_counts=False,
        output_dir=tmp_path / "live_strat",
        skip_host_preflight=True,
    )
    deps = build_live_analysis_dependencies(cfg)
    deps.readiness = LiveReadinessRepository(  # type: ignore[assignment]
        ready_chunks=[ready_chunk],
        gap_times=[],
        chain_version="cv",
        chain_hash="ch",
        require_lock_gate=False,
    )
    deps.baseline = LiveBaselineBookRepository(  # type: ignore[assignment]
        require_lock_gate=False,
        epochs=[Ep()],
        record_source=lambda **k: [snap],
    )
    order: list[str] = []
    _assess = deps.readiness.assess_window

    def assess_wrap(**kw):
        order.append("readiness")
        return _assess(**kw)

    deps.readiness.assess_window = assess_wrap  # type: ignore[method-assign]
    _mid = deps.metrics.load_mid_series

    def mid_wrap(**kw):
        order.append("metrics")
        return _mid(**kw)

    deps.metrics.load_mid_series = mid_wrap  # type: ignore[method-assign]

    prof = profile_from_fixture(
        window_start=datetime(2026, 9, 6, 17, 30, tzinfo=UTC),
        window_end=decision,
        vah=wall_px,
        val=79500.0,
    )

    strat = StrategyEdgeConfig(
        symbol="BTCUSDT",
        decision_time_utc=decision,
        edge_side=EdgeSide.UPPER,
        pre_window_minutes=30,
        post_window_minutes=30,
        local_band_usd=400.0,
        output_dir=str(tmp_path / "live_strat"),
    )
    r1 = run_strategy_edge_analysis(
        strat,
        deps=deps,
        mp_profile=prof,
        lock_path=lock,
        execute_live=True,
        skip_execution_hold=False,
    )
    assert order[0] == "readiness"
    assert "metrics" in order
    assert r1.sections["avr"]["status"] == "UNAVAILABLE_ADAPTER_NOT_IMPLEMENTED"
    assert r1.sections["open_interest"]["status"] == "UNAVAILABLE_ADAPTER_NOT_IMPLEMENTED"
    assert r1.baseline.complete is True, getattr(deps.baseline, "last_error", None)
    for name in ARTIFACTS:
        assert (tmp_path / "live_strat" / name).exists()
    dedup = json.loads((tmp_path / "live_strat" / "trade_dedup_report.json").read_text())
    assert dedup["largest_buy"]["notional"] == 20_006_882.5
    man = json.loads((tmp_path / "live_strat" / "manifest.json").read_text())
    assert man["status"] == STATUS_COMPLETE

    cfg2 = LiveAnalysisConfig(
        symbol="BTCUSDT",
        chain_version="cv",
        chain_hash="ch",
        lock_path=lock,
        execute_live=True,
        client=client,
        require_lock_gate=False,
        enable_market_profile=False,
        output_dir=tmp_path / "live_man",
        skip_host_preflight=True,
    )
    deps2 = build_live_analysis_dependencies(cfg2)
    deps2.readiness = LiveReadinessRepository(  # type: ignore[assignment]
        ready_chunks=[ready_chunk],
        gap_times=[],
        chain_version="cv",
        chain_hash="ch",
        require_lock_gate=False,
    )
    deps2.baseline = LiveBaselineBookRepository(  # type: ignore[assignment]
        require_lock_gate=False,
        epochs=[Ep()],
        record_source=lambda **k: [snap],
    )
    man_cfg = ManualWindowConfig(
        symbol="BTCUSDT",
        start_utc=start,
        end_utc=end,
        local_band_usd=400.0,
        output_dir=str(tmp_path / "live_man"),
        reference_price=wall_px,
        reference_side=EdgeSide.UPPER,
    )
    r2 = run_manual_window_analysis(
        man_cfg,
        deps=deps2,
        lock_path=lock,
        execute_live=True,
        skip_execution_hold=False,
    )
    assert r2.baseline.complete is True
    assert (tmp_path / "live_man" / "manifest.json").exists()


def test_chunk_boundary_predecessor_keys(tmp_path: Path):
    """Start exactly on chunk boundary uses predecessor chunk for hash."""
    from obfull_research_engine.breakout_xray_v1.fakes import make_fake_deps
    from obfull_research_engine.breakout_xray_v1.time_windows import dt_to_ns

    start = datetime(2026, 9, 6, 17, 30, tzinfo=UTC)
    end = datetime(2026, 9, 6, 17, 45, tzinfo=UTC)
    start_ns = dt_to_ns(start)
    pred = predecessor_bucket_start_ns(start_ns)
    digest = book_map_sha256({1.0: 1.0}, {2.0: 1.0})
    snap = BronzeRecordFull(
        0, 1, start_ns - 10, "snapshot", bids=[[1.0, 1.0]], asks=[[2.0, 1.0]], u=1, seq=1
    )
    prev_chunk = ChunkAssessment(
        chunk_key="prev",
        epoch_id="e1",
        start_ns=start_ns - MS100 * 10,
        end_ns=start_ns,
        level_change_count=1,
        state_count=1,
        output_hash="b" * 64,
        ledger_status="COMPLETE",
        observed_level_changes=1,
        observed_states=1,
        status="READY",
    )
    curr_chunk = ChunkAssessment(
        chunk_key="curr",
        epoch_id="e1",
        start_ns=start_ns,
        end_ns=dt_to_ns(end),
        level_change_count=1,
        state_count=1,
        output_hash="c" * 64,
        ledger_status="COMPLETE",
        observed_level_changes=1,
        observed_states=1,
        status="READY",
    )

    class ReadyRepo:
        def assess_window(self, *, symbol, start_ns, end_ns):
            from obfull_research_engine.clickhouse_research_store_v1.analysis_readiness_v1_3 import (
                assess_analysis_window,
            )

            a = assess_analysis_window(
                start_ns=start_ns,
                end_ns=end_ns,
                ready_chunks=[prev_chunk, curr_chunk],
                gap_times=[],
            )
            return ReadinessResult(
                status=a.status,
                reason=a.reason or a.status,
                epoch_id=a.epoch_id,
                chunk_keys=tuple(a.chunk_keys),
            )

    deps = make_fake_deps(
        bronze_records=[snap],
        expected_book_hash=digest,
        book_hashes={pred: digest},
        mid_series=[MidState(start_ns, 1.5, None, None, None, digest)],
        enforce_continuity=False,
    )
    deps.readiness = ReadyRepo()  # type: ignore[assignment]
    cfg = ManualWindowConfig(
        symbol="BTCUSDT",
        start_utc=start,
        end_utc=end,
        local_band_usd=400.0,
        output_dir=str(tmp_path / "chunkb"),
        reference_price=1.5,
        reference_side=EdgeSide.UPPER,
    )
    result = run_manual_window_analysis(
        cfg, deps=deps, lock_path=tmp_path / "nolock", skip_execution_hold=True
    )
    assert result.baseline.complete is True
    assert "prev" in result.data_quality["coverage"]["predecessor_chunk_keys"]
    assert "curr" in result.data_quality["coverage"]["analysis_chunk_keys"]


def test_missing_silver_hash_fails(tmp_path: Path):
    from obfull_research_engine.breakout_xray_v1.fakes import make_fake_deps
    from obfull_research_engine.breakout_xray_v1.time_windows import dt_to_ns

    start = datetime(2026, 9, 6, 17, 10, tzinfo=UTC)
    end = datetime(2026, 9, 6, 17, 20, tzinfo=UTC)
    start_ns = dt_to_ns(start)
    deps = make_fake_deps(
        mid_series=[MidState(start_ns, 100.0, None, None, None, "")],
    )
    deps.metrics.default_book_hash = None  # type: ignore[attr-defined]
    deps.metrics.book_hashes = {}  # type: ignore[attr-defined]
    cfg = ManualWindowConfig(
        symbol="BTCUSDT",
        start_utc=start,
        end_utc=end,
        local_band_usd=400.0,
        output_dir=str(tmp_path / "miss"),
        reference_price=100.0,
        reference_side=EdgeSide.UPPER,
    )
    result = run_manual_window_analysis(
        cfg, deps=deps, lock_path=tmp_path / "nolock", skip_execution_hold=True
    )
    assert (
        result.data_quality.get("failure", {}).get("failure_reason")
        == UNRESOLVED_BASELINE_MISSING_SILVER_HASH
    )
    assert not (tmp_path / "miss").exists()

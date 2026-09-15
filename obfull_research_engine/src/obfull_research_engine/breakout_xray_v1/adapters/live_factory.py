"""Live AnalysisDependencies factory (gated; no import-time connections)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..execution_hold import (
    DEFAULT_SILVER_LOCK,
    ExecutionSentinel,
    assert_full_run_allowed,
    assert_live_execution_allowed,
)
from ..ports import AnalysisDependencies
from ..resource_preflight import (
    ResourcePreflightConfig,
    ResourcePreflightResult,
    run_host_resource_preflight,
)
from .bronze_baseline import LiveBaselineBookRepository
from .live_data import (
    LiveMarketProfileRepository,
    LivePublicTradesRepository,
    LiveReadinessRepository,
    LiveSilverLevelChangesRepository,
    LiveSilverMetricsRepository,
    StubAvrLiveRepository,
    StubOpenInterestLiveRepository,
    validate_database_name,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 import (
    validate_input_database,
    validate_output_database,
)


@dataclass
class LiveAnalysisConfig:
    symbol: str
    chain_version: str
    chain_hash: str
    output_dir: Path | str | None = None
    input_database: str = "research_full_ob_continuous_v1_3"
    output_database: str = "research_full_ob_silver_v1_3"
    local_band_usd: float = 400.0
    lock_path: Path = DEFAULT_SILVER_LOCK
    execute_live: bool = False
    expected_bronze_records: int = 2_638_997
    verify_readiness_counts: bool = True
    enable_market_profile: bool = True
    # Injected client / factory for mocks — never used unless execute_live gates pass
    client: Any | None = None
    client_factory: Callable[[], Any] | None = None
    require_lock_gate: bool = True
    preflight_config: ResourcePreflightConfig | None = None
    skip_host_preflight: bool = False  # tests only when resources already asserted


@dataclass
class LiveBundle:
    deps: AnalysisDependencies
    sentinel: ExecutionSentinel
    preflight: ResourcePreflightResult | None
    client: Any


def _build_config(cfg: LiveAnalysisConfig) -> Any:
    from pathlib import Path as PathType

    from obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 import (
        BuildConfig,
    )

    validate_input_database(cfg.input_database)
    validate_output_database(cfg.output_database)

    return BuildConfig(
        symbol=cfg.symbol.upper(),
        input_database=cfg.input_database,
        output_database=cfg.output_database,
        chain_version=cfg.chain_version,
        expected_chain_hash=cfg.chain_hash,
        expected_bronze_records=cfg.expected_bronze_records,
        resume=True,
        start_chain_index=0,
        end_chain_index=163,
        chunk_market_minutes=15,
        warmup_minutes=5,
        max_rss_mib=1536,
        min_free_disk_gib=200.0,
        min_available_memory_mib=4096,
        progress_every_chunks=1,
        report_path=PathType("/tmp/xray_v1_unused_report.json"),
        lock_path=PathType(cfg.lock_path),
    )


def open_live_clickhouse_client(
    *,
    execute_live: bool,
    lock_path: Path,
    output_dir: Path | None = None,
    preflight_config: ResourcePreflightConfig | None = None,
    skip_host_preflight: bool = False,
) -> Any:
    """Open CH client only after dual gates + host preflight + lock recheck."""
    assert_live_execution_allowed(execute_live=execute_live, lock_path=lock_path)
    if not skip_host_preflight:
        if output_dir is None:
            raise RuntimeError("STOP_XRAY_OUTPUT_DIR_REQUIRED_FOR_PREFLIGHT")
        run_host_resource_preflight(output_dir=Path(output_dir), config=preflight_config)
    assert_full_run_allowed(lock_path)
    from obfull_research_engine.clickhouse_research_store_v1.helpers import (
        get_clickhouse_client,
    )

    return get_clickhouse_client(role="xray-read")


def build_live_analysis_dependencies(
    config: LiveAnalysisConfig,
) -> AnalysisDependencies:
    """Concrete live repos. Raises if gates fail or a required source is missing."""
    bundle = build_live_bundle(config)
    return bundle.deps


def build_live_bundle(config: LiveAnalysisConfig) -> LiveBundle:
    """Full live open sequence with sentinel + preflight retained for the run."""
    # 1) --execute-live + Silver-Lock free
    assert_live_execution_allowed(
        execute_live=config.execute_live, lock_path=config.lock_path
    )

    preflight: ResourcePreflightResult | None = None
    # 2) Host-Resource-Preflight (no DB)
    if not config.skip_host_preflight:
        if config.output_dir is None:
            raise RuntimeError("STOP_XRAY_OUTPUT_DIR_REQUIRED_FOR_PREFLIGHT")
        preflight = run_host_resource_preflight(
            output_dir=Path(config.output_dir),
            config=config.preflight_config,
        )

    # 3) Silver-Lock recheck
    assert_full_run_allowed(config.lock_path)
    sentinel = ExecutionSentinel(config.lock_path)
    sentinel.check("before_client_factory")

    # 4) Client factory
    client = config.client
    if client is None:
        if config.client_factory is not None:
            client = config.client_factory()
        else:
            # Nested open skips preflight (already done) but re-asserts gates.
            assert_live_execution_allowed(
                execute_live=True, lock_path=config.lock_path
            )
            assert_full_run_allowed(config.lock_path)
            from obfull_research_engine.clickhouse_research_store_v1.helpers import (
                get_clickhouse_client,
            )

            client = get_clickhouse_client(role="xray-read")
    if client is None:
        raise RuntimeError("DATA_SOURCE_UNAVAILABLE:clickhouse_client")

    validate_database_name(config.output_database)
    validate_input_database(config.input_database)
    build_cfg = _build_config(config)
    gate = config.require_lock_gate
    swap_baseline = (
        None if preflight is None else int(preflight.swap_used_baseline_bytes)
    )
    max_swap = (
        None
        if preflight is None
        else int(preflight.max_additional_swap_bytes)
    )

    deps = AnalysisDependencies(
        readiness=LiveReadinessRepository(
            client,
            build_config=build_cfg,
            lock_path=config.lock_path,
            require_lock_gate=gate,
            chain_version=config.chain_version,
            chain_hash=config.chain_hash,
            verify_counts=config.verify_readiness_counts,
            sentinel=sentinel,
        ),
        metrics=LiveSilverMetricsRepository(
            client,
            database=config.output_database,
            lock_path=config.lock_path,
            require_lock_gate=gate,
            sentinel=sentinel,
        ),
        level_changes=LiveSilverLevelChangesRepository(
            client,
            database=config.output_database,
            local_band_usd=config.local_band_usd,
            lock_path=config.lock_path,
            require_lock_gate=gate,
            sentinel=sentinel,
        ),
        trades=LivePublicTradesRepository(
            client,
            lock_path=config.lock_path,
            require_lock_gate=gate,
            sentinel=sentinel,
        ),
        baseline=LiveBaselineBookRepository(
            client,
            build_config=build_cfg,
            lock_path=config.lock_path,
            require_lock_gate=gate,
            sentinel=sentinel,
            swap_baseline_bytes=swap_baseline,
            max_additional_swap_bytes=max_swap,
        ),
        market_profile=LiveMarketProfileRepository(
            client,
            lock_path=config.lock_path,
            require_lock_gate=gate,
            enabled=config.enable_market_profile,
            sentinel=sentinel,
        ),
        avr=StubAvrLiveRepository(),
        open_interest=StubOpenInterestLiveRepository(),
    )
    return LiveBundle(
        deps=deps, sentinel=sentinel, preflight=preflight, client=client
    )

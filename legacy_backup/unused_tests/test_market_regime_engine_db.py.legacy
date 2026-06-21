import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from strategy.market_regime import (
    CoinProfile,
    FeatureProfileStats,
    MarketRegimeDBConfig,
    MarketRegimeStore,
    MarketSignalEngine,
    RawMarketSnapshot,
    RoutedRegimeSnapshot,
    StateMachineSnapshot,
)
from strategy.market_regime.models import StateMachineSnapshot


def make_profile_row(symbol: str = "BTCUSDT") -> dict:
    row = {
        "symbol": symbol,
        "updated_at": datetime.now(timezone.utc),
        "sample_size": 1234,
        "profile_version": 1,
        "window_start": datetime.now(timezone.utc),
        "window_end": datetime.now(timezone.utc),
        "threshold_orderflow_long": 0.15,
        "threshold_orderflow_short": -0.15,
        "threshold_buy_sell_imbalance_long": 0.20,
        "threshold_buy_sell_imbalance_short": -0.20,
        "decay_alpha": 0.3,
        "profile_mode": "rolling",
        "notes": "test-profile",
    }
    for feature_name in (
        "price_change_1m",
        "price_change_5m",
        "price_change_15m",
        "oi_change_ratio",
        "trade_volume_1m",
        "volume_spike_ratio",
        "orderflow_ratio",
        "delta_ratio",
        "microburst_score",
        "liquidation_density_5m",
        "liquidation_cluster_score",
        "spread_ratio",
        "trade_count_1m",
        "avg_trade_size",
        "atr_1m",
    ):
        row[f"{feature_name}_mean"] = 0.0
        row[f"{feature_name}_std"] = 1.0
        row[f"{feature_name}_p50"] = 0.0
        row[f"{feature_name}_p75"] = 0.75
        row[f"{feature_name}_p90"] = 0.9
        row[f"{feature_name}_p95"] = 1.5
        row[f"{feature_name}_p99"] = 2.0
        row[f"{feature_name}_median"] = 0.0
        row[f"{feature_name}_mad"] = 1.0
        if feature_name.startswith("price_change"):
            row[f"{feature_name}_n95"] = -1.5
            row[f"{feature_name}_n99"] = -2.0
    return row


class FakeCursor:
    def __init__(
        self,
        fetchall_result=None,
        fetchone_result=None,
        fetchall_results=None,
        fetchone_results=None,
    ) -> None:
        self.fetchall_result = fetchall_result or []
        self.fetchall_results = list(fetchall_results or [])
        self.fetchone_result = fetchone_result
        self.fetchone_results = list(fetchone_results or [])
        self.executed = []
        self.executemany_calls = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def executemany(self, query, params):
        self.executemany_calls.append((query, params))

    def fetchall(self):
        if self.fetchall_results:
            return self.fetchall_results.pop(0)
        return self.fetchall_result

    def fetchone(self):
        if self.fetchone_results:
            return self.fetchone_results.pop(0)
        return self.fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def make_profile() -> CoinProfile:
    features = {
        name: FeatureProfileStats(
            mean=0.0,
            std=1.0,
            p50=0.0,
            p75=0.75,
            p90=0.9,
            p95=1.5,
            p99=2.0,
            n95=-1.5 if name.startswith("price_change") else None,
            n99=-2.0 if name.startswith("price_change") else None,
        )
        for name in (
            "price_change_1m",
            "price_change_5m",
            "price_change_15m",
            "oi_change_ratio",
            "trade_volume_1m",
            "volume_spike_ratio",
            "orderflow_ratio",
            "delta_ratio",
            "microburst_score",
            "liquidation_density_5m",
            "liquidation_cluster_score",
            "spread_ratio",
            "trade_count_1m",
            "avg_trade_size",
            "atr_1m",
        )
    }
    return CoinProfile(symbol="BTCUSDT", features=features)


class FakeStore:
    def __init__(self, profile: CoinProfile | None) -> None:
        self.profile = profile
        self.insert_calls = []

    def load_coin_profile(self, symbol: str):
        return self.profile

    def insert_market_state_live(self, **kwargs):
        self.insert_calls.append(kwargs)


class FakeLiveStore:
    def __init__(self) -> None:
        self.profile = make_profile()
        self.ensure_schema_called = False

    def load_active_symbols(self):
        return ["BTCUSDT"]

    def load_recent_raw_snapshots(self, symbol: str, limit: int = 2):
        return [
            RawMarketSnapshot(
                symbol=symbol,
                ts=datetime.now(timezone.utc),
                price=100.0,
                price_change_1m=0.2,
                price_change_5m=0.3,
                trade_volume_1m=10.0,
                volume_spike_ratio=1.1,
                buy_volume=6.0,
                sell_volume=4.0,
                delta=2.0,
                orderflow_ratio=0.6,
                spread=0.01,
            ),
            RawMarketSnapshot(
                symbol=symbol,
                ts=datetime.now(timezone.utc),
                price=101.0,
                price_change_1m=0.4,
                price_change_5m=0.5,
                trade_volume_1m=12.0,
                volume_spike_ratio=1.2,
                buy_volume=7.0,
                sell_volume=5.0,
                delta=2.0,
                orderflow_ratio=0.58,
                spread=0.01,
            ),
        ]

    def load_latest_state_machine(self, symbol: str):
        return StateMachineSnapshot(previous_state="neutral", current_state="neutral")

    def load_coin_profile(self, symbol: str):
        return self.profile

    def insert_market_state_live(self, **kwargs):
        return None

    def ensure_schema(self):
        self.ensure_schema_called = True
        return None

    def backfill_market_state_live_oi_price_state(self, dry_run: bool = True, *, batch_size: int = 1000):
        return {
            "dry_run": dry_run,
            "total_rows_scanned": 4,
            "rows_with_source_data": 3,
            "rows_missing_source_data": 1,
            "rows_needing_update": 2,
            "rows_already_correct": 2,
            "rows_updated": 0 if dry_run else 2,
            "target_state_counts": {
                "neutral": 1,
                "price_down_oi_up": 1,
                "price_up_oi_down": 1,
                "price_up_oi_up": 1,
            },
            "written_state_counts": {} if dry_run else {
                "price_down_oi_up": 1,
                "price_up_oi_down": 1,
            },
        }

    def backfill_market_state_live_oi_price_flags(self, *, dry_run: bool = True):
        return {
            "dry_run": dry_run,
            "total_rows_scanned": 4,
            "rows_needing_update": 2,
            "rows_updated": 0 if dry_run else 2,
            "quadrant_counts": {
                "price_up_oi_up": 1,
                "price_up_oi_down": 1,
                "price_down_oi_up": 1,
                "price_down_oi_down": 1,
            },
        }

    def load_market_state_live_oi_performance_rows(self, *, symbols=None):
        return [
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                "state": "trend_continuation_long",
                "oi_price_state": "price_up_oi_up",
                "future_return_5m": 0.8,
                "future_return_15m": 1.4,
            },
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
                "state": "trend_continuation_long",
                "oi_price_state": "price_up_oi_up",
                "future_return_5m": 0.4,
                "future_return_15m": 0.7,
            },
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 2, tzinfo=timezone.utc),
                "state": "mid_reversal_setup_short",
                "oi_price_state": "price_down_oi_up",
                "future_return_5m": -0.6,
                "future_return_15m": -1.1,
            },
        ]

    def audit_data_coverage(self, *, symbols=None):
        return {
            "database": "test_db",
            "selected_symbols": symbols or [],
            "discovered_tables": [
                "coin_profiles_current",
                "coin_profiles_history",
                "liquidation_data",
                "market_state_live",
            ],
            "tables": [
                {
                    "table_name": "liquidation_data",
                    "role": "raw_market_data",
                    "exists": True,
                    "row_count": 100,
                    "symbol_count": 1,
                    "first_ts": "2026-01-01T00:00:00+00:00",
                    "last_ts": "2026-01-01T01:39:00+00:00",
                    "estimated_granularity_minutes": 1,
                    "duplicate_symbol_ts_rows": 0,
                    "unique_symbol_ts": True,
                    "existing_relevant_fields": ["price_change_1m", "oi_change"],
                    "missing_expected_fields": [],
                    "field_quality": [],
                    "per_symbol_coverage": [],
                }
            ],
            "join_compatibility": {
                "ready": True,
                "join_pairs": [
                    {
                        "left_table": "market_state_live",
                        "right_table": "liquidation_data",
                        "left_key": "(symbol, ts)",
                        "right_key": "(symbol, timestamp)",
                        "total_left_rows": 50,
                        "matched_rows": 48,
                        "unmatched_rows": 2,
                        "match_pct": 96.0,
                    }
                ],
                "systematic_time_shift_check": {
                    "candidate_offsets_minutes": [],
                    "best_offset_minutes": 0,
                    "best_match_pct": 96.0,
                },
                "state_rows_missing_required_raw_source": 2,
            },
            "oi_backfill_readiness": {
                "ready": True,
                "source_table": "liquidation_data",
                "required_source_fields": ["price_change_1m", "oi_change"],
                "earliest_backfill_ready_ts_any_symbol": "2026-01-01T00:00:00+00:00",
                "earliest_backfill_ready_ts_all_symbols": "2026-01-01T00:00:00+00:00",
                "symbols_ready_for_backfill": ["BTCUSDT"],
                "symbols_with_gaps_or_missing_source": [],
                "raw_source_coverage_per_symbol": [],
                "market_state_alignment_per_symbol": [],
            },
            "analysis_readiness": {
                "ready": True,
                "source_table": "liquidation_data",
                "future_horizons_minutes": [5, 15],
                "total_state_rows": 50,
                "rows_with_future_5m": 45,
                "rows_with_future_15m": 40,
                "rows_missing_future_5m": 5,
                "rows_missing_future_15m": 10,
                "per_symbol": [],
            },
            "summary": {
                "tables_complete_enough": ["liquidation_data"],
                "tables_incomplete": [],
                "problematic_tables": [],
                "state_raw_exact_join_match_pct": 96.0,
                "oi_backfill_reliable_from_any_symbol": "2026-01-01T00:00:00+00:00",
                "oi_backfill_reliable_from_all_symbols": "2026-01-01T00:00:00+00:00",
                "symbols_ready_for_oi_backfill": ["BTCUSDT"],
                "symbols_with_oi_backfill_gaps": [],
                "analysis_rows_with_future_5m": 45,
                "analysis_rows_with_future_15m": 40,
                "analysis_rows_missing_future_5m": 5,
                "analysis_rows_missing_future_15m": 10,
            },
        }

    def analyze_market_state_live_quality(self, *, symbols=None, limit=None):
        self.ensure_schema_called = True
        return {
            "row_count": 1,
            "selected_symbols": symbols or [],
            "limit": limit,
            "horizons": [5, 15, 30],
            "confidence_coverage": {
                "rows_with_confidence": 1,
                "rows_without_confidence": 0,
                "source_counts": {"stored": 1},
            },
            "state_quality": {
                "trend_continuation_long": {
                    "row_count": 1,
                    "avg_confidence": 0.9,
                    "conflict_rate": 0.0,
                    "instability_rate": 0.0,
                    "return_stats": {
                        "5": {"count": 1, "avg": 0.05, "positive_pct": 100.0},
                        "15": {"count": 1, "avg": 0.08, "positive_pct": 100.0},
                        "30": {"count": 1, "avg": 0.12, "positive_pct": 100.0},
                    },
                }
            },
            "confidence_buckets": {
                "lt_0_33": {"row_count": 0, "avg_confidence": None, "confidence_source_counts": {}, "return_stats": {}},
                "between_0_33_and_0_66": {"row_count": 0, "avg_confidence": None, "confidence_source_counts": {}, "return_stats": {}},
                "gt_0_66": {
                    "row_count": 1,
                    "avg_confidence": 0.9,
                    "confidence_source_counts": {"stored": 1},
                    "return_stats": {
                        "5": {"count": 1, "avg": 0.05, "positive_pct": 100.0, "median": 0.05},
                        "15": {"count": 1, "avg": 0.08, "positive_pct": 100.0, "median": 0.08},
                        "30": {"count": 1, "avg": 0.12, "positive_pct": 100.0, "median": 0.12},
                    },
                },
                "none": {"row_count": 0, "avg_confidence": None, "confidence_source_counts": {}, "return_stats": {}},
            },
            "conflict_flags": {},
            "instability_flags": {},
            "transition_reason_stats": {},
            "routed_transition_reason_stats": {},
            "range_unclear_diagnosis": {},
        }

    def analyze_market_state_live_quality_review(self, *, symbols=None, limit=None):
        self.ensure_schema_called = True
        return {
            "overview": {
                "row_count": 1,
                "selected_symbols": symbols or [],
                "limit": limit,
                "horizons": [5, 15, 30],
                "horizon_return_counts": {"5": 1, "15": 1, "30": 1},
                "confidence_coverage": {"rows_with_confidence": 1, "rows_without_confidence": 0, "source_counts": {"stored": 1}},
                "range_unclear_rows": 0,
                "range_unclear_share_pct": 0.0,
            },
            "confidence_coverage": {"rows_with_confidence": 1, "rows_without_confidence": 0, "source_counts": {"stored": 1}},
            "strongest_state_contexts": [
                {
                    "state": "trend_continuation_long",
                    "row_count": 1,
                    "low_sample_warning": True,
                    "avg_confidence": 0.9,
                    "conflict_rate": 0.0,
                    "instability_rate": 0.0,
                    "return_stats": {
                        "5": {"count": 1, "avg": 0.05, "positive_pct": 100.0},
                        "15": {"count": 1, "avg": 0.08, "positive_pct": 100.0},
                        "30": {"count": 1, "avg": 0.12, "positive_pct": 100.0},
                    },
                    "ranking_score": 0.08333333333333333,
                }
            ],
            "weakest_state_contexts": [],
            "top_positive_states": {"5": [], "15": [], "30": []},
            "weakest_states": {"5": [], "15": [], "30": []},
            "conflict_review": [],
            "instability_review": [],
            "confidence_review": {"buckets": {}, "coverage": {"rows_with_confidence": 1}},
            "transition_review": {"transition_reason": [], "routed_transition_reason": []},
            "range_unclear_diagnosis": {},
            "review_warnings": [],
        }

    def materialize_market_state_history_signal_validation(self, *, symbols=None, limit=None, horizons=(5, 15, 30, 60)):
        self.ensure_schema_called = True
        return {
            "selected_symbols": symbols or [],
            "limit": limit,
            "horizons": horizons,
            "candidate_rows": 2,
            "inserted_rows": 2,
            "summary_rows": 2,
        }

    def load_signal_validation_summary_rows(self, *, symbols=None, min_count: int = 1, limit=None):
        return [
            {
                "routed_state": "mid_exhaustion_long",
                "decision": "ALLOW",
                "symbol": "BTCUSDT",
                "signal_count": 2,
                "hit_rate_pct": 50.0,
                "avg_return_5m_pct": 0.5,
                "avg_return_15m_pct": 1.0,
                "avg_return_30m_pct": 1.5,
                "avg_return_60m_pct": 2.0,
                "avg_mfe_pct": 3.0,
                "avg_mae_pct": -1.0,
                "expectancy_pct": 2.0,
                "updated_at": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            }
        ][: (limit or 1)]

    def analyze_market_state_live_telemetry(self, *, symbols=None, limit=None):
        return {
            "row_count": 3,
            "selected_symbols": symbols or [],
            "limit": limit,
            "state_distributions": {
                "routed_state": {"trend_continuation_long": 2, "mid_exhaustion_long": 1},
                "slow_state": {"slow_trend_long": 2, "none": 1},
                "mid_state": {"mid_exhaustion_long": 1, "none": 2},
                "fast_state": {"fast_impulse_long": 2, "fast_exhaustion_long": 1},
            },
            "confidence_stats": {
                "rows_with_confidence": 2,
                "rows_without_confidence": 1,
                "min": 0.4,
                "max": 0.75,
                "avg": 0.575,
                "buckets": {
                    "lt_0_33": 0,
                    "between_0_33_and_0_66": 1,
                    "gt_0_66": 1,
                },
            },
            "conflict_flag_stats": {
                "rows_with_any_conflict": 1,
                "rows_without_conflict": 2,
                "flag_counts": {"fast_exhaustion_ambiguous": 1},
                "flag_counts_by_routed_state": {
                    "mid_exhaustion_long": {"fast_exhaustion_ambiguous": 1},
                },
            },
            "instability_flag_stats": {
                "rows_with_any_instability": 1,
                "rows_without_instability": 2,
                "flag_counts": {"fast_exhaustion_ambiguous": 1},
                "flag_counts_by_routed_state": {
                    "mid_exhaustion_long": {"fast_exhaustion_ambiguous": 1},
                },
            },
            "transition_stats": {
                "transition_reason_counts": {"router_aligned_with_slow_long": 2},
                "routed_transition_reason_counts": {"slow_fast_alignment_long": 2},
            },
        }

    def load_live_state_debug_rows(self, symbols=None, limit: int = 20):
        return [
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                "state": "trend_continuation_long",
                "routed_state": "trend_continuation_long",
                "slow_state": "slow_trend_long",
                "mid_state": None,
                "fast_state": "fast_impulse_long",
                "confidence": 0.75,
                "confidence_source": "stored",
                "conflict_flags": {},
                "instability_flags": {"fast_exhaustion_ambiguous": False},
                "decision": "WATCHLIST",
                "decision_reason": "state_not_whitelisted",
                "entry_allowed": False,
                "range_unclear_diagnosis": None,
                "transition_reason": ["router_aligned_with_slow_long"],
                "routed_transition_reason": ["slow_fast_alignment_long"],
                "oi_price_state": "price_up_oi_up",
                "oi_price_build_long": True,
                "oi_price_short_covering": False,
                "oi_price_build_short": False,
                "oi_price_long_unwinding": False,
            }
        ][:limit]


class MarketRegimeDBEngineTests(unittest.TestCase):
    def test_db_config_autodetects_running_collector_credentials(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "strategy.market_regime.db._autodetect_mysql_from_running_collector",
                return_value={
                    "MYSQL_HOST": "10.0.0.5",
                    "MYSQL_PORT": "4406",
                    "MYSQL_USER": "collector",
                    "MYSQL_PASSWORD": "secret",
                    "MYSQL_DATABASE": "market_data",
                    "MYSQL_TABLE": "liquidation_data",
                },
            ):
                config = MarketRegimeDBConfig()

        self.assertEqual(config.host, "10.0.0.5")
        self.assertEqual(config.port, 4406)
        self.assertEqual(config.user, "collector")
        self.assertEqual(config.password, "secret")
        self.assertEqual(config.database, "market_data")
        self.assertEqual(config.raw_table, "liquidation_data")

    def test_db_ensure_schema_executes_create_statements(self) -> None:
        cursor = FakeCursor()
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        store.ensure_schema()

        self.assertGreaterEqual(len(cursor.executed), 3)
        self.assertIn("CREATE TABLE IF NOT EXISTS", cursor.executed[0][0])

    def test_market_state_schema_contains_oi_price_state_column(self) -> None:
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(FakeCursor()))

        create_sql = store._build_create_market_state_live_sql()

        self.assertIn("oi_price_state VARCHAR(64) NULL", create_sql)
        self.assertIn("confidence DOUBLE NULL", create_sql)
        self.assertIn("confidence_source VARCHAR(32) NULL", create_sql)
        self.assertIn("conflict_flags_json JSON NULL", create_sql)
        self.assertIn("instability_flags_json JSON NULL", create_sql)
        self.assertIn("decision VARCHAR(16) NULL", create_sql)
        self.assertIn("decision_reason VARCHAR(128) NULL", create_sql)
        self.assertIn("range_unclear_diagnosis VARCHAR(64) NULL", create_sql)
        self.assertIn("entry_allowed TINYINT(1) NOT NULL DEFAULT 0", create_sql)
        self.assertIn("UNIQUE KEY ux_symbol_ts (symbol, ts)", create_sql)
        self.assertIn("oi_price_build_long TINYINT(1) NOT NULL DEFAULT 0", create_sql)
        self.assertIn("oi_price_short_covering TINYINT(1) NOT NULL DEFAULT 0", create_sql)
        self.assertIn("oi_price_build_short TINYINT(1) NOT NULL DEFAULT 0", create_sql)
        self.assertIn("oi_price_long_unwinding TINYINT(1) NOT NULL DEFAULT 0", create_sql)

    def test_signal_validation_schema_contains_expected_columns(self) -> None:
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(FakeCursor()))

        results_sql = store._build_create_signal_validation_results_sql()
        summary_sql = store._build_create_signal_validation_summary_sql()

        self.assertIn("signal_history_id BIGINT NOT NULL", results_sql)
        self.assertIn("price_60m DOUBLE NULL", results_sql)
        self.assertIn("return_60m_pct DOUBLE NULL", results_sql)
        self.assertIn("evaluation_return_pct DOUBLE NULL", results_sql)
        self.assertIn("max_favorable_move_pct DOUBLE NULL", results_sql)
        self.assertIn("UNIQUE KEY ux_signal_history_id (signal_history_id)", results_sql)
        self.assertIn("signal_count BIGINT NOT NULL", summary_sql)
        self.assertIn("expectancy_pct DOUBLE NULL", summary_sql)
        self.assertIn("PRIMARY KEY (routed_state, decision, symbol)", summary_sql)

    def test_schema_sync_adds_oi_price_state_column_when_missing(self) -> None:
        cursor = FakeCursor(
            fetchall_result=[
                {"Field": "symbol"},
                {"Field": "ts"},
                {"Field": "slow_state"},
                {"Field": "mid_state"},
                {"Field": "fast_state"},
                {"Field": "routed_state"},
            ]
        )
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        store._ensure_market_state_live_v2_columns(cursor)

        executed_sql = " ".join(query for query, _ in cursor.executed)
        self.assertIn("ADD COLUMN confidence DOUBLE NULL", executed_sql)
        self.assertIn("ADD COLUMN confidence_source VARCHAR(32) NULL", executed_sql)
        self.assertIn("ADD COLUMN conflict_flags_json JSON NULL", executed_sql)
        self.assertIn("ADD COLUMN instability_flags_json JSON NULL", executed_sql)
        self.assertIn("ADD COLUMN decision VARCHAR(16) NULL", executed_sql)
        self.assertIn("ADD COLUMN decision_reason VARCHAR(128) NULL", executed_sql)
        self.assertIn("ADD COLUMN range_unclear_diagnosis VARCHAR(64) NULL", executed_sql)
        self.assertIn("ADD COLUMN entry_allowed TINYINT(1) NOT NULL DEFAULT 0", executed_sql)
        self.assertIn("ADD COLUMN oi_price_state VARCHAR(64) NULL", executed_sql)
        self.assertIn("ADD COLUMN oi_price_build_long TINYINT(1) NOT NULL DEFAULT 0", executed_sql)
        self.assertIn("ADD COLUMN oi_price_short_covering TINYINT(1) NOT NULL DEFAULT 0", executed_sql)
        self.assertIn("ADD COLUMN oi_price_build_short TINYINT(1) NOT NULL DEFAULT 0", executed_sql)
        self.assertIn("ADD COLUMN oi_price_long_unwinding TINYINT(1) NOT NULL DEFAULT 0", executed_sql)

    def test_market_state_uniqueness_sync_dedupes_and_adds_unique_constraint(self) -> None:
        cursor = FakeCursor(
            fetchall_results=[
                [{"Tables_in_test": "market_state_live"}],
                [
                    {"Key_name": "idx_symbol_ts", "Non_unique": 1, "Seq_in_index": 1, "Column_name": "symbol"},
                    {"Key_name": "idx_symbol_ts", "Non_unique": 1, "Seq_in_index": 2, "Column_name": "ts"},
                ],
            ]
        )
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        store._ensure_market_state_live_uniqueness(cursor)

        executed_sql = " ".join(query for query, _ in cursor.executed)
        self.assertIn("DELETE older", executed_sql)
        self.assertIn("ADD CONSTRAINT ux_symbol_ts UNIQUE (symbol, ts)", executed_sql)

    def test_market_state_uniqueness_sync_noops_when_unique_exists(self) -> None:
        cursor = FakeCursor(
            fetchall_results=[
                [{"Tables_in_test": "market_state_live"}],
                [
                    {"Key_name": "ux_symbol_ts", "Non_unique": 0, "Seq_in_index": 1, "Column_name": "symbol"},
                    {"Key_name": "ux_symbol_ts", "Non_unique": 0, "Seq_in_index": 2, "Column_name": "ts"},
                ],
            ]
        )
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        store._ensure_market_state_live_uniqueness(cursor)

        executed_sql = " ".join(query for query, _ in cursor.executed)
        self.assertNotIn("DELETE older", executed_sql)
        self.assertNotIn("ADD CONSTRAINT ux_symbol_ts UNIQUE (symbol, ts)", executed_sql)

    def test_insert_market_state_live_batch_uses_upsert(self) -> None:
        cursor = FakeCursor()
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        store.insert_market_state_live_batch(
            [
                {
                    "symbol": "BTCUSDT",
                    "ts": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                    "price": 100.0,
                    "active_state": "trend_continuation_long",
                    "primitive_events_json": "{}",
                    "pressure_score": 1.0,
                    "participation_score": 2.0,
                    "instability_score": 0.5,
                    "exhaustion_score": 0.25,
                }
            ]
        )

        self.assertEqual(len(cursor.executemany_calls), 1)
        query, _ = cursor.executemany_calls[0]
        self.assertIn("ON DUPLICATE KEY UPDATE", query)
        self.assertIn("symbol=VALUES(symbol)", query)
        self.assertNotIn("created_at=VALUES(created_at)", query)

    def test_derive_oi_price_flags_is_deterministic_one_hot(self) -> None:
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(FakeCursor()))
        cases = {
            "price_up_oi_up": "oi_price_build_long",
            "price_up_oi_down": "oi_price_short_covering",
            "price_down_oi_up": "oi_price_build_short",
            "price_down_oi_down": "oi_price_long_unwinding",
        }

        for oi_price_state, expected_true_key in cases.items():
            with self.subTest(oi_price_state=oi_price_state):
                flags = store._derive_oi_price_flags(oi_price_state)
                self.assertEqual(sum(flags.values()), 1)
                self.assertEqual(flags[expected_true_key], 1)
                for key, value in flags.items():
                    if key != expected_true_key:
                        self.assertEqual(value, 0)

    def test_derive_oi_price_flags_unknown_or_null_is_all_zero(self) -> None:
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(FakeCursor()))

        for value in [None, "", "neutral", "weird_state"]:
            with self.subTest(value=value):
                flags = store._derive_oi_price_flags(value)
                self.assertEqual(flags, {
                    "oi_price_build_long": 0,
                    "oi_price_short_covering": 0,
                    "oi_price_build_short": 0,
                    "oi_price_long_unwinding": 0,
                })

    def test_backfill_oi_price_flags_mixed_old_rows(self) -> None:
        cursor = FakeCursor(
            fetchone_results=[
                {
                    "total_rows_scanned": 6,
                    "count_price_up_oi_up": 2,
                    "count_price_up_oi_down": 1,
                    "count_price_down_oi_up": 1,
                    "count_price_down_oi_down": 1,
                    "rows_needing_update": 4,
                },
                {
                    "total_rows_scanned": 6,
                    "count_price_up_oi_up": 2,
                    "count_price_up_oi_down": 1,
                    "count_price_down_oi_up": 1,
                    "count_price_down_oi_down": 1,
                    "rows_needing_update": 0,
                },
            ]
        )
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        result = store.backfill_market_state_live_oi_price_flags(dry_run=False)

        self.assertFalse(result["dry_run"])
        self.assertEqual(result["total_rows_scanned"], 6)
        self.assertEqual(result["rows_needing_update"], 4)
        self.assertEqual(result["rows_updated"], 4)
        self.assertEqual(result["quadrant_counts"]["price_up_oi_up"], 2)
        update_queries = [query for query, _ in cursor.executed if query.strip().startswith("UPDATE")]
        self.assertEqual(len(update_queries), 1)

    def test_backfill_oi_price_flags_rerun_is_safe_noop(self) -> None:
        cursor = FakeCursor(
            fetchone_results=[
                {
                    "total_rows_scanned": 6,
                    "count_price_up_oi_up": 2,
                    "count_price_up_oi_down": 1,
                    "count_price_down_oi_up": 1,
                    "count_price_down_oi_down": 1,
                    "rows_needing_update": 0,
                },
                {
                    "total_rows_scanned": 6,
                    "count_price_up_oi_up": 2,
                    "count_price_up_oi_down": 1,
                    "count_price_down_oi_up": 1,
                    "count_price_down_oi_down": 1,
                    "rows_needing_update": 0,
                },
            ]
        )
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        result = store.backfill_market_state_live_oi_price_flags(dry_run=False)

        self.assertEqual(result["rows_needing_update"], 0)
        self.assertEqual(result["rows_updated"], 0)
        update_queries = [query for query, _ in cursor.executed if query.strip().startswith("UPDATE")]
        self.assertEqual(update_queries, [])

    def test_backfill_oi_price_flags_dry_run_handles_unknown_states(self) -> None:
        cursor = FakeCursor(
            fetchone_results=[
                {
                    "total_rows_scanned": 5,
                    "count_price_up_oi_up": 1,
                    "count_price_up_oi_down": 0,
                    "count_price_down_oi_up": 1,
                    "count_price_down_oi_down": 0,
                    "rows_needing_update": 3,
                },
                {
                    "total_rows_scanned": 5,
                    "count_price_up_oi_up": 1,
                    "count_price_up_oi_down": 0,
                    "count_price_down_oi_up": 1,
                    "count_price_down_oi_down": 0,
                    "rows_needing_update": 3,
                },
            ]
        )
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        result = store.backfill_market_state_live_oi_price_flags(dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["rows_updated"], 0)
        self.assertEqual(result["rows_needing_update"], 3)
        self.assertEqual(result["quadrant_counts"], {
            "price_up_oi_up": 1,
            "price_up_oi_down": 0,
            "price_down_oi_up": 1,
            "price_down_oi_down": 0,
        })

    def test_backfill_oi_price_state_updates_only_mismatches(self) -> None:
        cursor = FakeCursor(
            fetchall_results=[
                [
                    {
                        "id": 1,
                        "oi_price_state": None,
                        "oi_price_build_long": 0,
                        "oi_price_short_covering": 0,
                        "oi_price_build_short": 0,
                        "oi_price_long_unwinding": 0,
                        "source_price_change_1m": 0.5,
                        "source_oi_change": 10.0,
                    },
                    {
                        "id": 2,
                        "oi_price_state": "price_up_oi_up",
                        "oi_price_build_long": 1,
                        "oi_price_short_covering": 0,
                        "oi_price_build_short": 0,
                        "oi_price_long_unwinding": 0,
                        "source_price_change_1m": 0.2,
                        "source_oi_change": 1.0,
                    },
                    {
                        "id": 3,
                        "oi_price_state": "neutral",
                        "oi_price_build_long": 1,
                        "oi_price_short_covering": 0,
                        "oi_price_build_short": 0,
                        "oi_price_long_unwinding": 0,
                        "source_price_change_1m": -0.3,
                        "source_oi_change": 5.0,
                    },
                    {
                        "id": 4,
                        "oi_price_state": "price_up_oi_down",
                        "oi_price_build_long": 0,
                        "oi_price_short_covering": 1,
                        "oi_price_build_short": 0,
                        "oi_price_long_unwinding": 0,
                        "source_price_change_1m": None,
                        "source_oi_change": None,
                    },
                ],
                [],
            ]
        )
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        result = store.backfill_market_state_live_oi_price_state(dry_run=False, batch_size=10)

        self.assertFalse(result["dry_run"])
        self.assertEqual(result["total_rows_scanned"], 4)
        self.assertEqual(result["rows_with_source_data"], 3)
        self.assertEqual(result["rows_missing_source_data"], 1)
        self.assertEqual(result["rows_needing_update"], 3)
        self.assertEqual(result["rows_already_correct"], 1)
        self.assertEqual(result["rows_updated"], 3)
        self.assertEqual(result["target_state_counts"]["price_up_oi_up"], 2)
        self.assertEqual(result["target_state_counts"]["price_down_oi_up"], 1)
        self.assertEqual(result["target_state_counts"]["neutral"], 1)
        self.assertEqual(result["written_state_counts"]["price_up_oi_up"], 1)
        self.assertEqual(result["written_state_counts"]["price_down_oi_up"], 1)
        self.assertEqual(result["written_state_counts"]["neutral"], 1)
        self.assertEqual(len(cursor.executemany_calls), 1)
        _, update_params = cursor.executemany_calls[0]
        self.assertEqual(len(update_params), 3)

    def test_backfill_oi_price_state_dry_run_is_noop_when_all_rows_match(self) -> None:
        cursor = FakeCursor(
            fetchall_results=[
                [
                    {
                        "id": 10,
                        "oi_price_state": "price_down_oi_down",
                        "oi_price_build_long": 0,
                        "oi_price_short_covering": 0,
                        "oi_price_build_short": 0,
                        "oi_price_long_unwinding": 1,
                        "source_price_change_1m": -0.2,
                        "source_oi_change": -3.0,
                    }
                ],
                [],
            ]
        )
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        result = store.backfill_market_state_live_oi_price_state(dry_run=True, batch_size=5)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["rows_needing_update"], 0)
        self.assertEqual(result["rows_already_correct"], 1)
        self.assertEqual(result["rows_updated"], 0)
        self.assertEqual(cursor.executemany_calls, [])

    def test_load_market_state_live_oi_performance_rows_joins_forward_returns(self) -> None:
        cursor = FakeCursor(
            fetchall_result=[
                {
                    "symbol": "BTCUSDT",
                    "ts": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                    "state": "trend_continuation_long",
                    "oi_price_state": "price_up_oi_up",
                    "future_return_5m": 0.5,
                    "future_return_15m": 1.0,
                }
            ]
        )
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        rows = store.load_market_state_live_oi_performance_rows(symbols=["BTCUSDT"])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["oi_price_state"], "price_up_oi_up")
        executed_query, executed_params = cursor.executed[0]
        self.assertIn("DATE_ADD(m.ts, INTERVAL 5 MINUTE)", executed_query)
        self.assertEqual(executed_params, ["BTCUSDT"])

    def test_analyze_market_state_live_telemetry_counts_meta_flags_and_confidence(self) -> None:
        show_columns_rows = [
            {"Field": "symbol"},
            {"Field": "ts"},
            {"Field": "active_state"},
            {"Field": "routed_state"},
            {"Field": "slow_state"},
            {"Field": "mid_state"},
            {"Field": "fast_state"},
            {"Field": "confidence"},
            {"Field": "conflict_flags_json"},
            {"Field": "instability_flags_json"},
            {"Field": "transition_reason"},
            {"Field": "routed_transition_reason"},
        ]
        telemetry_rows = [
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                "active_state": "trend_continuation_long",
                "routed_state": "trend_continuation_long",
                "slow_state": "slow_trend_long",
                "mid_state": None,
                "fast_state": "fast_impulse_long",
                "confidence": 0.75,
                "conflict_flags_json": "{\"slow_fast_direction_conflict\": true, \"ignored\": false}",
                "instability_flags_json": "{}",
                "transition_reason": "state_machine_confirmed;router_aligned_with_slow_long",
                "routed_transition_reason": "slow_fast_alignment_long",
            },
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
                "active_state": "mid_exhaustion_long",
                "routed_state": "mid_exhaustion_long",
                "slow_state": "slow_trend_long",
                "mid_state": "mid_exhaustion_long",
                "fast_state": "fast_exhaustion_long",
                "confidence": 0.4,
                "conflict_flags_json": "{}",
                "instability_flags_json": "{\"fast_exhaustion_ambiguous\": true}",
                "transition_reason": "",
                "routed_transition_reason": "mid_state_priority;fast_exhaustion_ambiguous",
            },
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 2, tzinfo=timezone.utc),
                "active_state": "range_unclear",
                "routed_state": "range_unclear",
                "slow_state": None,
                "mid_state": None,
                "fast_state": None,
                "confidence": None,
                "conflict_flags_json": None,
                "instability_flags_json": "not_json",
                "transition_reason": None,
                "routed_transition_reason": None,
            },
        ]
        cursor = FakeCursor(fetchall_results=[show_columns_rows, telemetry_rows])
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        payload = store.analyze_market_state_live_telemetry(symbols=["BTCUSDT"], limit=50)

        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(payload["selected_symbols"], ["BTCUSDT"])
        self.assertEqual(payload["limit"], 50)
        self.assertEqual(payload["state_distributions"]["routed_state"]["trend_continuation_long"], 1)
        self.assertEqual(payload["state_distributions"]["mid_state"]["none"], 2)
        self.assertEqual(payload["confidence_stats"]["rows_with_confidence"], 3)
        self.assertEqual(payload["confidence_stats"]["rows_without_confidence"], 0)
        self.assertEqual(payload["confidence_stats"]["min"], 0.4)
        self.assertEqual(payload["confidence_stats"]["max"], 0.75)
        self.assertAlmostEqual(payload["confidence_stats"]["avg"], 1.55 / 3.0)
        self.assertEqual(payload["confidence_stats"]["buckets"]["between_0_33_and_0_66"], 2)
        self.assertEqual(payload["confidence_stats"]["buckets"]["gt_0_66"], 1)
        self.assertEqual(payload["confidence_stats"]["source_counts"]["stored"], 2)
        self.assertEqual(payload["confidence_stats"]["source_counts"]["derived_fallback"], 1)
        self.assertEqual(payload["conflict_flag_stats"]["rows_with_any_conflict"], 1)
        self.assertEqual(payload["conflict_flag_stats"]["flag_counts"]["slow_fast_direction_conflict"], 1)
        self.assertEqual(
            payload["conflict_flag_stats"]["flag_counts_by_routed_state"]["trend_continuation_long"]["slow_fast_direction_conflict"],
            1,
        )
        self.assertEqual(payload["instability_flag_stats"]["rows_with_any_instability"], 1)
        self.assertEqual(payload["instability_flag_stats"]["flag_counts"]["fast_exhaustion_ambiguous"], 1)
        self.assertEqual(payload["transition_stats"]["transition_reason_counts"]["router_aligned_with_slow_long"], 1)
        self.assertEqual(payload["transition_stats"]["routed_transition_reason_counts"]["slow_fast_alignment_long"], 1)
        executed_query, executed_params = cursor.executed[1]
        self.assertIn("LIMIT %s", executed_query)
        self.assertEqual(executed_params, ["BTCUSDT", 50])

    def test_load_market_state_live_telemetry_rows_handles_missing_meta_columns(self) -> None:
        show_columns_rows = [
            {"Field": "symbol"},
            {"Field": "ts"},
            {"Field": "active_state"},
        ]
        telemetry_rows = [
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                "active_state": "trend_continuation_long",
            }
        ]
        cursor = FakeCursor(fetchall_results=[show_columns_rows, telemetry_rows])
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        rows = store.load_market_state_live_telemetry_rows(limit=1)

        self.assertEqual(rows[0]["routed_state"], "trend_continuation_long")
        self.assertEqual(rows[0]["confidence"], 0.75)
        self.assertEqual(rows[0]["confidence_source"], "derived_fallback")
        self.assertEqual(rows[0]["conflict_flags"], {})
        self.assertEqual(rows[0]["instability_flags"], {})
        self.assertEqual(rows[0]["transition_reason"], [])
        self.assertEqual(rows[0]["routed_transition_reason"], [])

    def test_load_market_state_live_return_rows_parses_future_returns(self) -> None:
        show_columns = [
            {"Field": "symbol"},
            {"Field": "ts"},
            {"Field": "price"},
            {"Field": "active_state"},
            {"Field": "routed_state"},
            {"Field": "slow_state"},
            {"Field": "mid_state"},
            {"Field": "fast_state"},
            {"Field": "confidence"},
            {"Field": "conflict_flags_json"},
            {"Field": "instability_flags_json"},
            {"Field": "transition_reason"},
            {"Field": "routed_transition_reason"},
        ]
        rows = [
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                "price": 100.0,
                "active_state": "trend_continuation_long",
                "routed_state": "trend_continuation_long",
                "slow_state": "slow_trend_long",
                "mid_state": None,
                "fast_state": "fast_impulse_long",
                "confidence": 0.75,
                "conflict_flags_json": "{\"slow_fast_direction_conflict\": true}",
                "instability_flags_json": "{}",
                "transition_reason": "router_aligned_with_slow_long",
                "routed_transition_reason": "slow_fast_alignment_long",
                "future_price_5": 105.0,
                "future_price_15": 110.0,
                "future_price_30": 115.0,
            },
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
                "price": 100.0,
                "active_state": "mid_exhaustion_long",
                "routed_state": "mid_exhaustion_long",
                "slow_state": "slow_trend_long",
                "mid_state": "mid_exhaustion_long",
                "fast_state": "fast_exhaustion_long",
                "confidence": 0.5,
                "conflict_flags_json": "{}",
                "instability_flags_json": "{\"fast_exhaustion_ambiguous\": true}",
                "transition_reason": "",
                "routed_transition_reason": "mid_state_priority",
                "future_price_5": 95.0,
                "future_price_15": 90.0,
                "future_price_30": 93.0,
            },
        ]
        cursor = FakeCursor(fetchall_results=[show_columns, rows])
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        payload = store.analyze_market_state_live_quality(symbols=["BTCUSDT"], limit=2)

        self.assertEqual(payload["row_count"], 2)
        self.assertIn("trend_continuation_long", payload["state_quality"])
        self.assertAlmostEqual(payload["state_quality"]["trend_continuation_long"]["avg_confidence"], 0.75)
        self.assertEqual(payload["state_quality"]["trend_continuation_long"]["conflict_rate"], 100.0)
        self.assertIn("mid_exhaustion_long", payload["state_quality"])
        self.assertEqual(payload["state_quality"]["mid_exhaustion_long"]["instability_rate"], 100.0)
        self.assertEqual(payload["confidence_coverage"]["source_counts"]["stored"], 2)
        self.assertEqual(payload["conflict_flags"]["slow_fast_direction_conflict"]["row_count"], 1)
        self.assertAlmostEqual(
            payload["conflict_flags"]["slow_fast_direction_conflict"]["return_stats"]["5"]["avg"],
            0.05,
        )
        self.assertEqual(
            payload["instability_flags"]["fast_exhaustion_ambiguous"]["routed_states"]["mid_exhaustion_long"],
            1,
        )
        self.assertEqual(
            payload["transition_reason_stats"]["router_aligned_with_slow_long"]["row_count"],
            1,
        )
        self.assertEqual(
            payload["routed_transition_reason_stats"]["mid_state_priority"]["row_count"],
            1,
        )
        executed_query, executed_params = cursor.executed[1]
        self.assertIn("LIMIT %s", executed_query)
        self.assertEqual(executed_params, [5, 15, 30, "BTCUSDT", 2])

    def test_load_market_state_live_return_rows_derives_confidence_fallback(self) -> None:
        show_columns = [
            {"Field": "symbol"},
            {"Field": "ts"},
            {"Field": "price"},
            {"Field": "active_state"},
            {"Field": "routed_state"},
            {"Field": "mid_state"},
            {"Field": "transition_reason"},
            {"Field": "routed_transition_reason"},
        ]
        rows = [
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                "price": 100.0,
                "active_state": "mid_exhaustion_long",
                "routed_state": "mid_exhaustion_long",
                "mid_state": "mid_exhaustion_long",
                "transition_reason": "awaiting_confirmation:mid_exhaustion_long:1/2",
                "routed_transition_reason": "mid_state_priority",
                "future_price_5": 101.0,
                "future_price_15": 102.0,
                "future_price_30": 103.0,
            },
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
                "price": 100.0,
                "active_state": "range_unclear",
                "routed_state": "range_unclear",
                "mid_state": None,
                "transition_reason": "true_range_or_unclear_context",
                "routed_transition_reason": "true_range_or_unclear_context",
                "future_price_5": 99.0,
                "future_price_15": 98.0,
                "future_price_30": 97.0,
            },
        ]
        cursor = FakeCursor(fetchall_results=[show_columns, rows])
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        result_rows = store.load_market_state_live_return_rows(symbols=["BTCUSDT"], limit=2)

        self.assertEqual(result_rows[0]["confidence"], 0.85)
        self.assertEqual(result_rows[0]["confidence_source"], "derived_fallback")
        self.assertEqual(result_rows[1]["confidence"], 0.40)
        self.assertEqual(result_rows[1]["confidence_source"], "derived_fallback")

    def test_build_signal_validation_result_row_normalizes_short_returns(self) -> None:
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(FakeCursor()))

        row = store._build_signal_validation_result_row(
            {
                "signal_history_id": 1,
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                "routed_state": "mid_reversal_setup_short",
                "decision": "ALLOW",
                "entry_allowed": 1,
                "confidence": 0.8,
                "confidence_source": "stored",
                "price_at_signal": 100.0,
                "price_5m": 95.0,
                "price_15m": 90.0,
                "price_30m": 92.0,
                "price_60m": 85.0,
                "max_price_in_window": 101.0,
                "min_price_in_window": 80.0,
            },
            horizons=(5, 15, 30, 60),
        )

        self.assertEqual(row["signal_direction"], "SHORT")
        self.assertAlmostEqual(row["return_5m_pct"], 5.0)
        self.assertAlmostEqual(row["return_60m_pct"], 15.0)
        self.assertAlmostEqual(row["max_favorable_move_pct"], 20.0)
        self.assertAlmostEqual(row["max_adverse_move_pct"], -1.0)
        self.assertAlmostEqual(row["evaluation_return_pct"], 15.0)

    def test_materialize_market_state_history_signal_validation_uses_incremental_query(self) -> None:
        cursor = FakeCursor(
            fetchall_result=[
                {
                    "signal_history_id": 7,
                    "symbol": "BTCUSDT",
                    "ts": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                    "routed_state": "mid_exhaustion_long",
                    "decision": "ALLOW",
                    "entry_allowed": 1,
                    "confidence": 0.9,
                    "confidence_source": "stored",
                    "price_at_signal": 100.0,
                    "price_5m": 101.0,
                    "price_15m": 102.0,
                    "price_30m": 103.0,
                    "price_60m": 104.0,
                    "max_price_in_window": 110.0,
                    "min_price_in_window": 99.0,
                }
            ],
            fetchone_result={"summary_rows": 1},
        )
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        summary = store.materialize_market_state_history_signal_validation(symbols=["BTCUSDT"], limit=5)

        self.assertEqual(summary["candidate_rows"], 1)
        self.assertEqual(summary["summary_rows"], 1)
        self.assertEqual(len(cursor.executemany_calls), 1)
        executed_query, executed_params = cursor.executed[0]
        self.assertIn("LEFT JOIN liquidation_data AS future_60", executed_query)
        self.assertIn("results.signal_history_id IS NULL", executed_query)
        self.assertIn("h.entry_allowed = 1", executed_query)
        self.assertIn("DATE_ADD(h.ts, INTERVAL 60 MINUTE)", executed_query)
        self.assertEqual(executed_params, [5, 15, 30, 60, "BTCUSDT", 5])

    def test_analyze_market_state_live_quality_review_produces_overview_and_rankings(self) -> None:
        payload = {
            "row_count": 3,
            "selected_symbols": ["BTCUSDT"],
            "limit": 5,
            "horizons": [5, 15],
            "confidence_coverage": {
                "rows_with_confidence": 2,
                "rows_without_confidence": 1,
                "source_counts": {"stored": 1, "derived_fallback": 1, "missing": 1},
            },
            "state_quality": {
                "trend_continuation_long": {
                    "row_count": 3,
                    "avg_confidence": 0.8,
                    "conflict_rate": 0.0,
                    "instability_rate": 0.0,
                    "return_stats": {
                        "5": {"count": 3, "avg": 0.04, "positive_pct": 100.0},
                        "15": {"count": 3, "avg": 0.05, "positive_pct": 100.0},
                    },
                },
                "range_unclear": {
                    "row_count": 2,
                    "avg_confidence": 0.3,
                    "conflict_rate": 50.0,
                    "instability_rate": 0.0,
                    "return_stats": {
                        "5": {"count": 2, "avg": -0.02, "positive_pct": 0.0},
                        "15": {"count": 2, "avg": -0.01, "positive_pct": 0.0},
                    },
                },
            },
            "confidence_buckets": {
                "lt_0_33": {
                    "row_count": 2,
                    "avg_confidence": 0.2,
                    "confidence_source_counts": {"stored": 2},
                    "return_stats": {
                        "5": {"count": 2, "avg": -0.01, "positive_pct": 0.0, "median": -0.01},
                        "15": {"count": 2, "avg": -0.015, "positive_pct": 0.0, "median": -0.015},
                    },
                },
                "gt_0_66": {
                    "row_count": 3,
                    "avg_confidence": 0.85,
                    "confidence_source_counts": {"stored": 2, "derived_fallback": 1},
                    "return_stats": {
                        "5": {"count": 3, "avg": 0.06, "positive_pct": 100.0, "median": 0.06},
                        "15": {"count": 3, "avg": 0.07, "positive_pct": 100.0, "median": 0.07},
                    },
                },
                "between_0_33_and_0_66": {"row_count": 0, "avg_confidence": None, "confidence_source_counts": {}, "return_stats": {}},
                "none": {"row_count": 0, "avg_confidence": None, "confidence_source_counts": {}, "return_stats": {}},
            },
            "conflict_flags": {
                "slow_fast_direction_conflict": {
                    "row_count": 1,
                    "routed_states": {"trend_continuation_long": 1},
                    "return_stats": {
                        "5": {"count": 1, "avg": 0.03, "positive_pct": 100.0},
                        "15": {"count": 1, "avg": 0.02, "positive_pct": 100.0},
                    },
                }
            },
            "instability_flags": {},
            "transition_reason_stats": {
                "router_aligned_with_slow_long": {
                    "row_count": 2,
                    "routed_state_distribution": {"trend_continuation_long": 2},
                    "return_stats": {
                        "5": {"count": 2, "avg": 0.05, "positive_pct": 100.0},
                        "15": {"count": 2, "avg": 0.04, "positive_pct": 100.0},
                    },
                }
            },
            "routed_transition_reason_stats": {},
            "range_unclear_diagnosis": {"waiting_for_confirmation": 1, "true_range_context": 1},
        }
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(FakeCursor()))
        with patch.object(
            MarketRegimeStore,
            "analyze_market_state_live_quality",
            return_value=payload,
        ):
            with patch.object(store, "ensure_schema", return_value=None):
                review = store.analyze_market_state_live_quality_review(limit=5)

        self.assertEqual(review["overview"]["row_count"], 3)
        self.assertEqual(review["overview"]["horizons"], (5, 15))
        self.assertEqual(review["confidence_coverage"]["source_counts"]["derived_fallback"], 1)
        self.assertEqual(len(review["strongest_state_contexts"]), 1)
        self.assertEqual(review["strongest_state_contexts"][0]["state"], "trend_continuation_long")
        self.assertEqual(review["weakest_state_contexts"][0]["state"], "range_unclear")
        self.assertEqual(review["conflict_review"][0]["name"], "slow_fast_direction_conflict")
        self.assertEqual(review["transition_review"]["transition_reason"][0]["name"], "router_aligned_with_slow_long")
        self.assertEqual(review["transition_review"]["transition_reason"][0]["top_routed_state"]["state"], "trend_continuation_long")
        self.assertEqual(review["range_unclear_diagnosis"]["waiting_for_confirmation"], 1)
        self.assertIn("review_warnings", review)

    def test_db_profile_mapping(self) -> None:
        cursor = FakeCursor(fetchall_result=[make_profile_row()])
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        profile = store.load_coin_profile("btcusdt")

        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.symbol, "BTCUSDT")
        self.assertEqual(profile.sample_size, 1234)
        self.assertAlmostEqual(profile.get_stats("price_change_1m").p99, 2.0)
        self.assertAlmostEqual(profile.get_stats("price_change_1m").n99 or 0.0, -2.0)

    def test_market_signal_engine_process_symbol_basic(self) -> None:
        store = FakeStore(make_profile())
        engine = MarketSignalEngine(store)
        current = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=100.0,
            price_change_1m=1.8,
            price_change_5m=1.2,
            price_change_15m=0.5,
            oi_change_ratio=1.8,
            trade_volume_1m=2.0,
            volume_spike_ratio=1.7,
            buy_volume=80.0,
            sell_volume=20.0,
            delta=60.0,
            orderflow_ratio=1.8,
            trade_count_1m=2.2,
            avg_trade_size=1.4,
            spread=0.02,
        )

        result = engine.process_symbol("BTCUSDT", current, None, None, persist=True)

        self.assertTrue(result.profile_found)
        self.assertFalse(result.skipped)
        self.assertIsNotNone(result.state_machine)
        self.assertEqual(len(store.insert_calls), 1)

    def test_market_signal_engine_persists_oi_price_state_into_live_row(self) -> None:
        store = FakeStore(make_profile())
        engine = MarketSignalEngine(store)
        db_store = MarketRegimeStore(connection_factory=lambda: FakeConnection(FakeCursor()))
        current = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=100.0,
            price_change_1m=1.8,
            price_change_5m=1.2,
            price_change_15m=0.5,
            oi_change=12.0,
            oi_change_ratio=1.8,
            trade_volume_1m=2.0,
            volume_spike_ratio=1.7,
            buy_volume=80.0,
            sell_volume=20.0,
            delta=60.0,
            orderflow_ratio=1.8,
            trade_count_1m=2.2,
            avg_trade_size=1.4,
            spread=0.02,
        )

        engine.process_symbol("BTCUSDT", current, None, None, persist=True)

        self.assertEqual(len(store.insert_calls), 1)
        insert_kwargs = store.insert_calls[0]
        row = db_store._build_market_state_live_row(
            symbol=insert_kwargs["symbol"],
            ts=insert_kwargs["ts"],
            raw_snapshot=insert_kwargs["raw_snapshot"],
            normalized_snapshot=insert_kwargs["normalized_snapshot"],
            events=insert_kwargs["events"],
            scores=insert_kwargs["scores"],
            slow_regime=insert_kwargs["slow_regime"],
            mid_regime=insert_kwargs["mid_regime"],
            fast_trigger=insert_kwargs["fast_trigger"],
            routed_regime=insert_kwargs["routed_regime"],
            regime=insert_kwargs["regime"],
            state_machine=insert_kwargs["state_machine"],
            decision=insert_kwargs["decision"],
            engine_version=insert_kwargs["engine_version"],
        )
        self.assertEqual(row["oi_price_state"], "price_up_oi_up")
        self.assertEqual(row["oi_price_build_long"], 1)
        self.assertEqual(row["oi_price_short_covering"], 0)
        self.assertEqual(row["oi_price_build_short"], 0)
        self.assertEqual(row["oi_price_long_unwinding"], 0)
        self.assertEqual(row["slow_state"], insert_kwargs["slow_regime"].state)
        self.assertEqual(row["mid_state"], insert_kwargs["mid_regime"].state)
        self.assertEqual(row["fast_state"], insert_kwargs["fast_trigger"].state)
        self.assertEqual(row["routed_state"], insert_kwargs["state_machine"].current_state)
        self.assertEqual(row["confidence"], insert_kwargs["routed_regime"].confidence)
        self.assertEqual(row["confidence_source"], "stored")
        self.assertEqual(row["decision"], insert_kwargs["decision"].decision)
        self.assertEqual(row["decision_reason"], insert_kwargs["decision"].decision_reason)
        self.assertEqual(row["entry_allowed"], int(insert_kwargs["decision"].entry_allowed))
        self.assertEqual(
            json.loads(row["conflict_flags_json"]),
            insert_kwargs["routed_regime"].conflict_flags,
        )
        self.assertEqual(
            json.loads(row["instability_flags_json"]),
            insert_kwargs["routed_regime"].instability_flags,
        )
        self.assertEqual(row["active_state"], insert_kwargs["state_machine"].current_state)

    def test_live_state_debug_rows_read_back_all_quadrants(self) -> None:
        show_columns_rows = [
            {"Field": "symbol"},
            {"Field": "ts"},
            {"Field": "active_state"},
            {"Field": "slow_state"},
            {"Field": "mid_state"},
            {"Field": "fast_state"},
            {"Field": "routed_state"},
            {"Field": "confidence"},
            {"Field": "confidence_source"},
            {"Field": "conflict_flags_json"},
            {"Field": "instability_flags_json"},
            {"Field": "decision"},
            {"Field": "decision_reason"},
            {"Field": "range_unclear_diagnosis"},
            {"Field": "entry_allowed"},
            {"Field": "transition_reason"},
            {"Field": "routed_transition_reason"},
            {"Field": "oi_price_state"},
            {"Field": "oi_price_build_long"},
            {"Field": "oi_price_short_covering"},
            {"Field": "oi_price_build_short"},
            {"Field": "oi_price_long_unwinding"},
        ]
        quadrant_rows = [
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                "state": "trend_continuation_long",
                "slow_state": "slow_trend_long",
                "mid_state": None,
                "fast_state": "fast_impulse_long",
                "routed_state": "trend_continuation_long",
                "confidence": 0.75,
                "confidence_source": "stored",
                "conflict_flags_json": "{\"slow_fast_direction_conflict\": false}",
                "instability_flags_json": "{\"fast_exhaustion_ambiguous\": false}",
                "decision": "WATCHLIST",
                "decision_reason": "state_not_whitelisted",
                "range_unclear_diagnosis": None,
                "entry_allowed": 0,
                "transition_reason": "state_machine_confirmed;router_aligned_with_slow_long",
                "routed_transition_reason": "slow_fast_alignment_long",
                "oi_price_state": "price_up_oi_up",
                "oi_price_build_long": 1,
                "oi_price_short_covering": 0,
                "oi_price_build_short": 0,
                "oi_price_long_unwinding": 0,
            },
            {
                "symbol": "ETHUSDT",
                "ts": datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
                "state": "trend_continuation_long",
                "slow_state": "slow_trend_long",
                "mid_state": None,
                "fast_state": "fast_impulse_long",
                "routed_state": "trend_continuation_long",
                "confidence": 0.75,
                "confidence_source": "stored",
                "conflict_flags_json": "{}",
                "instability_flags_json": "{}",
                "decision": "WATCHLIST",
                "decision_reason": "state_not_whitelisted",
                "range_unclear_diagnosis": None,
                "entry_allowed": 0,
                "transition_reason": "router_aligned_with_slow_long",
                "routed_transition_reason": "slow_fast_alignment_long",
                "oi_price_state": "price_up_oi_down",
                "oi_price_build_long": 0,
                "oi_price_short_covering": 1,
                "oi_price_build_short": 0,
                "oi_price_long_unwinding": 0,
            },
            {
                "symbol": "SOLUSDT",
                "ts": datetime(2026, 1, 1, 12, 2, tzinfo=timezone.utc),
                "state": "mid_reversal_setup_short",
                "slow_state": "slow_transition_long_to_neutral",
                "mid_state": "mid_reversal_setup_short",
                "fast_state": "fast_reversal_attempt_short",
                "routed_state": "mid_reversal_setup_short",
                "confidence": 0.85,
                "confidence_source": "stored",
                "conflict_flags_json": "{}",
                "instability_flags_json": "{\"mid_reversal_context\": true}",
                "decision": "WATCHLIST",
                "decision_reason": "state_not_whitelisted",
                "range_unclear_diagnosis": None,
                "entry_allowed": 0,
                "transition_reason": "mid_state_priority",
                "routed_transition_reason": "mid_state_priority",
                "oi_price_state": "price_down_oi_up",
                "oi_price_build_long": 0,
                "oi_price_short_covering": 0,
                "oi_price_build_short": 1,
                "oi_price_long_unwinding": 0,
            },
            {
                "symbol": "DOGEUSDT",
                "ts": datetime(2026, 1, 1, 12, 3, tzinfo=timezone.utc),
                "state": "mid_exhaustion_long",
                "slow_state": "slow_transition_long_to_neutral",
                "mid_state": "mid_exhaustion_long",
                "fast_state": "fast_exhaustion_long",
                "routed_state": "mid_exhaustion_long",
                "confidence": 0.85,
                "confidence_source": "stored",
                "conflict_flags_json": "{\"fast_exhaustion_ambiguous\": true}",
                "instability_flags_json": "{\"fast_exhaustion_ambiguous\": true}",
                "decision": "ALLOW",
                "decision_reason": "allowed_state_mid_exhaustion_long",
                "range_unclear_diagnosis": None,
                "entry_allowed": 1,
                "transition_reason": "mid_state_priority;fast_exhaustion_context",
                "routed_transition_reason": "mid_state_priority;fast_exhaustion_ambiguous",
                "oi_price_state": "price_down_oi_down",
                "oi_price_build_long": 0,
                "oi_price_short_covering": 0,
                "oi_price_build_short": 0,
                "oi_price_long_unwinding": 1,
            },
        ]
        cursor = FakeCursor(fetchall_results=[show_columns_rows, quadrant_rows])
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        rows = store.load_live_state_debug_rows(limit=10)

        self.assertEqual([row["oi_price_state"] for row in rows], [
            "price_up_oi_up",
            "price_up_oi_down",
            "price_down_oi_up",
            "price_down_oi_down",
        ])
        self.assertEqual([row["oi_price_build_long"] for row in rows], [True, False, False, False])
        self.assertEqual([row["oi_price_short_covering"] for row in rows], [False, True, False, False])
        self.assertEqual([row["oi_price_build_short"] for row in rows], [False, False, True, False])
        self.assertEqual([row["oi_price_long_unwinding"] for row in rows], [False, False, False, True])
        self.assertEqual(rows[0]["state"], "trend_continuation_long")
        self.assertEqual(rows[0]["routed_state"], "trend_continuation_long")
        self.assertEqual(rows[0]["confidence"], 0.75)
        self.assertEqual(rows[0]["confidence_source"], "stored")
        self.assertEqual(rows[0]["conflict_flags"], {"slow_fast_direction_conflict": False})
        self.assertEqual(rows[0]["decision"], "WATCHLIST")
        self.assertEqual(rows[0]["decision_reason"], "state_not_whitelisted")
        self.assertFalse(rows[0]["entry_allowed"])
        self.assertEqual(rows[2]["instability_flags"], {"mid_reversal_context": True})
        self.assertEqual(rows[3]["transition_reason"], ["mid_state_priority", "fast_exhaustion_context"])
        self.assertEqual(rows[3]["routed_transition_reason"], ["mid_state_priority", "fast_exhaustion_ambiguous"])

    def test_live_state_debug_rows_fallback_when_oi_price_state_column_missing(self) -> None:
        show_columns_rows = [
            {"Field": "symbol"},
            {"Field": "ts"},
            {"Field": "active_state"},
            {"Field": "slow_state"},
            {"Field": "mid_state"},
            {"Field": "fast_state"},
        ]
        result_rows = [
            {
                "symbol": "BTCUSDT",
                "ts": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                "state": "trend_continuation_long",
                "slow_state": "slow_trend_long",
                "mid_state": None,
                "fast_state": "fast_impulse_long",
                "routed_state": "trend_continuation_long",
                "confidence": None,
                "confidence_source": "missing",
                "conflict_flags_json": "{}",
                "instability_flags_json": "{}",
                "decision": "WATCHLIST",
                "decision_reason": "state_not_whitelisted",
                "range_unclear_diagnosis": None,
                "entry_allowed": 0,
                "transition_reason": "",
                "routed_transition_reason": "",
                "oi_price_state": "neutral",
                "oi_price_build_long": 0,
                "oi_price_short_covering": 0,
                "oi_price_build_short": 0,
                "oi_price_long_unwinding": 0,
            }
        ]
        cursor = FakeCursor(fetchall_results=[show_columns_rows, result_rows])
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        rows = store.load_live_state_debug_rows(limit=1)

        self.assertEqual(rows[0]["oi_price_state"], "neutral")
        self.assertFalse(rows[0]["oi_price_build_long"])
        self.assertFalse(rows[0]["oi_price_short_covering"])
        self.assertFalse(rows[0]["oi_price_build_short"])
        self.assertFalse(rows[0]["oi_price_long_unwinding"])
        self.assertEqual(rows[0]["routed_state"], "trend_continuation_long")
        self.assertEqual(rows[0]["confidence"], 0.75)
        self.assertEqual(rows[0]["confidence_source"], "derived_fallback")
        self.assertEqual(rows[0]["decision"], "WATCHLIST")
        self.assertEqual(rows[0]["decision_reason"], "state_not_whitelisted")
        self.assertFalse(rows[0]["entry_allowed"])
        self.assertEqual(rows[0]["conflict_flags"], {})
        self.assertEqual(rows[0]["instability_flags"], {})
        self.assertEqual(rows[0]["transition_reason"], [])
        self.assertEqual(rows[0]["routed_transition_reason"], [])

    def test_market_signal_engine_rebound_transition(self) -> None:
        store = FakeStore(make_profile())
        engine = MarketSignalEngine(store)
        previous_raw = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=100.0,
            price_change_1m=-1.2,
            price_change_5m=-1.6,
            oi_change_ratio=0.8,
            trade_volume_1m=0.2,
            volume_spike_ratio=0.4,
            buy_volume=10.0,
            sell_volume=40.0,
            delta=-30.0,
            orderflow_ratio=-1.0,
            trade_count_1m=0.3,
            spread=0.01,
        )
        current_raw = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=101.0,
            price_change_1m=0.9,
            price_change_5m=0.3,
            oi_change_ratio=-2.0,
            trade_volume_1m=2.0,
            volume_spike_ratio=2.5,
            buy_volume=90.0,
            sell_volume=10.0,
            delta=80.0,
            orderflow_ratio=2.0,
            trade_count_1m=2.5,
            spread=0.02,
        )
        previous_state = StateMachineSnapshot(
            previous_state="trend_short",
            current_state="trend_exhaustion_short",
            confirmation_counters={"rebound_start_long": 1},
            cooldown_remaining_fast_updates=0,
        )

        result = engine.process_symbol(
            "BTCUSDT",
            current_raw,
            previous_raw,
            previous_state,
            persist=False,
        )

        self.assertIsNotNone(result.slow_regime)
        self.assertIsNotNone(result.fast_trigger)
        self.assertIsNotNone(result.routed_regime)
        self.assertIsNotNone(result.state_machine)
        assert result.state_machine is not None
        self.assertEqual(result.state_machine.current_state, result.routed_regime.routed_state)

    def test_market_signal_engine_missing_profile(self) -> None:
        store = FakeStore(None)
        engine = MarketSignalEngine(store, strict_missing_profile=False)
        current = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=100.0,
        )

        result = engine.process_symbol("BTCUSDT", current, None, None, persist=False)

        self.assertTrue(result.skipped)
        self.assertFalse(result.profile_found)
        self.assertIn("missing", (result.skip_reason or "").lower())

    def test_watchlist_state_does_not_allow_entry(self) -> None:
        store = FakeStore(make_profile())
        engine = MarketSignalEngine(store)
        current = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=100.0,
            price_change_1m=0.1,
        )
        routed = RoutedRegimeSnapshot(
            slow_state="slow_trend_long",
            mid_state=None,
            fast_state="fast_impulse_long",
            routed_state="trend_continuation_long",
            confidence=0.75,
            transition_reason=["slow_long_context_meta_support"],
        )
        machine = StateMachineSnapshot(
            previous_state="range_unclear",
            current_state="trend_continuation_long",
            routed_state="trend_continuation_long",
            transition_reason=["same_ts_guard"],
        )
        with patch("strategy.market_regime.market_signal_engine.route_regime", return_value=routed), patch(
            "strategy.market_regime.market_signal_engine.apply_routed_state_machine",
            return_value=machine,
        ):
            result = engine.process_symbol("BTCUSDT", current, None, None, persist=False)

        self.assertEqual(result.decision, "WATCHLIST")
        self.assertEqual(result.decision_reason, "state_not_whitelisted")
        self.assertFalse(result.entry_allowed)

    def test_skip_state_does_not_allow_entry(self) -> None:
        store = FakeStore(make_profile())
        engine = MarketSignalEngine(store)
        current = RawMarketSnapshot(
            symbol="BTCUSDT",
            ts=datetime.now(timezone.utc),
            price=100.0,
            price_change_1m=0.1,
        )
        routed = RoutedRegimeSnapshot(
            slow_state="slow_range_neutral",
            mid_state=None,
            fast_state="fast_neutral",
            routed_state="range_unclear",
            confidence=0.40,
            transition_reason=["true_range_or_unclear_context"],
        )
        machine = StateMachineSnapshot(
            previous_state="range_unclear",
            current_state="range_unclear",
            routed_state="range_unclear",
            transition_reason=["awaiting_confirmation:trend_continuation_long:1/2"],
        )
        with patch("strategy.market_regime.market_signal_engine.route_regime", return_value=routed), patch(
            "strategy.market_regime.market_signal_engine.apply_routed_state_machine",
            return_value=machine,
        ):
            result = engine.process_symbol("BTCUSDT", current, None, None, persist=False)

        self.assertEqual(result.decision, "SKIP")
        self.assertEqual(result.decision_reason, "range_unclear_waiting_for_confirmation")
        self.assertFalse(result.entry_allowed)

    def test_cli_ensure_schema_dispatch(self) -> None:
        from strategy.market_regime import cli

        class FakeStoreForCLI:
            def __init__(self) -> None:
                self.called = False

            def ensure_schema(self):
                self.called = True

        fake_store = FakeStoreForCLI()
        with patch("strategy.market_regime.cli._make_store", return_value=fake_store):
            exit_code = cli.run_cli(["ensure-schema"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(fake_store.called)

    def test_cli_run_live_signal_dispatch(self) -> None:
        from strategy.market_regime import cli

        fake_store = FakeLiveStore()
        with patch("strategy.market_regime.cli._make_store", return_value=fake_store):
            exit_code = cli.run_cli(["run-live-signal", "--symbols", "BTCUSDT", "--no-persist"])

        self.assertEqual(exit_code, 0)

    def test_cli_debug_live_state_dispatch(self) -> None:
        from strategy.market_regime import cli

        fake_store = FakeLiveStore()
        with patch("strategy.market_regime.cli._make_store", return_value=fake_store):
            exit_code = cli.run_cli(["debug-live-state", "--symbols", "BTCUSDT", "--limit", "1"])

        self.assertEqual(exit_code, 0)

    def test_cli_run_live_signal_output_contains_router_meta_fields(self) -> None:
        from strategy.market_regime import cli

        payload = cli._run_live_signal_command(
            FakeLiveStore(),
            symbols=["BTCUSDT"],
            limit=1,
            persist=False,
        )

        self.assertTrue(payload["ok"])
        result = payload["results"][0]
        self.assertIn("confidence", result)
        self.assertIn("conflict_flags", result)
        self.assertIn("instability_flags", result)
        self.assertIn("routed_state", result)
        self.assertIn("routed_transition_reason", result)
        self.assertIn("decision", result)
        self.assertIn("decision_reason", result)
        self.assertIn("entry_allowed", result)
        self.assertIn("confidence_source", result)
        self.assertIn("range_unclear_diagnosis", result)
        self.assertIsInstance(result["conflict_flags"], dict)
        self.assertIsInstance(result["instability_flags"], dict)
        self.assertIsInstance(result["routed_transition_reason"], list)

    def test_cli_debug_live_state_output_contains_router_meta_fields(self) -> None:
        from strategy.market_regime import cli

        payload = cli._run_debug_live_state_command(
            FakeLiveStore(),
            symbols=["BTCUSDT"],
            limit=1,
        )

        self.assertTrue(payload["ok"])
        row = payload["rows"][0]
        self.assertEqual(row["routed_state"], "trend_continuation_long")
        self.assertEqual(row["confidence"], 0.75)
        self.assertEqual(row["confidence_source"], "stored")
        self.assertEqual(row["conflict_flags"], {})
        self.assertEqual(row["instability_flags"], {"fast_exhaustion_ambiguous": False})
        self.assertEqual(row["decision"], "WATCHLIST")
        self.assertEqual(row["decision_reason"], "state_not_whitelisted")
        self.assertFalse(row["entry_allowed"])
        self.assertEqual(row["transition_reason"], ["router_aligned_with_slow_long"])
        self.assertEqual(row["routed_transition_reason"], ["slow_fast_alignment_long"])

    def test_cli_analyze_live_state_quality_dispatch(self) -> None:
        from strategy.market_regime import cli

        fake_store = FakeLiveStore()
        with patch("strategy.market_regime.cli._make_store", return_value=fake_store):
            exit_code = cli.run_cli(["analyze-live-state-quality", "--symbol", "BTCUSDT", "--limit", "100"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(fake_store.ensure_schema_called)

    def test_cli_analyze_live_state_quality_output_contains_core_blocks(self) -> None:
        from strategy.market_regime import cli

        payload = cli._run_analyze_live_state_quality_command(
            FakeLiveStore(),
            symbols=["BTCUSDT"],
            limit=100,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "analyze-live-state-quality")
        self.assertIn("state_quality", payload)
        self.assertIn("confidence_buckets", payload)
        self.assertIn("conflict_flags", payload)
        self.assertIn("instability_flags", payload)
        self.assertIn("transition_reason_stats", payload)

    def test_cli_review_live_state_quality_dispatch(self) -> None:
        from strategy.market_regime import cli

        fake_store = FakeLiveStore()
        with patch("strategy.market_regime.cli._make_store", return_value=fake_store):
            exit_code = cli.run_cli(["review-live-state-quality", "--symbol", "BTCUSDT", "--limit", "50"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(fake_store.ensure_schema_called)

    def test_cli_review_live_state_quality_output_is_structured(self) -> None:
        from strategy.market_regime import cli

        payload = cli._run_review_live_state_quality_command(
            FakeLiveStore(),
            symbols=["BTCUSDT"],
            limit=50,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "review-live-state-quality")
        self.assertIn("overview", payload)
        self.assertIn("strongest_state_contexts", payload)
        self.assertIn("weakest_state_contexts", payload)
        self.assertIn("conflict_review", payload)
        self.assertIn("instability_review", payload)
        self.assertIn("confidence_review", payload)
        self.assertIn("transition_review", payload)

    def test_cli_backfill_oi_price_flags_dispatch(self) -> None:
        from strategy.market_regime import cli

        fake_store = FakeLiveStore()
        with patch("strategy.market_regime.cli._make_store", return_value=fake_store):
            exit_code = cli.run_cli(["backfill-oi-price-flags", "--dry-run"])

        self.assertEqual(exit_code, 0)

    def test_cli_backfill_oi_price_state_dispatch(self) -> None:
        from strategy.market_regime import cli

        fake_store = FakeLiveStore()
        with patch("strategy.market_regime.cli._make_store", return_value=fake_store):
            exit_code = cli.run_cli(["backfill-oi-price-state", "--dry-run", "--batch-size", "500"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(fake_store.ensure_schema_called)

    def test_cli_analyze_oi_price_performance_dispatch(self) -> None:
        from strategy.market_regime import cli

        fake_store = FakeLiveStore()
        with patch("strategy.market_regime.cli._make_store", return_value=fake_store):
            exit_code = cli.run_cli(["analyze-oi-price-performance", "--symbols", "BTCUSDT", "--min-rows", "2"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(fake_store.ensure_schema_called)

    def test_cli_analyze_live_state_dispatch(self) -> None:
        from strategy.market_regime import cli

        fake_store = FakeLiveStore()
        with patch("strategy.market_regime.cli._make_store", return_value=fake_store):
            exit_code = cli.run_cli(["analyze-live-state", "--symbol", "BTCUSDT", "--limit", "100"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(fake_store.ensure_schema_called)

    def test_cli_analyze_live_state_output_contains_core_blocks(self) -> None:
        from strategy.market_regime import cli

        payload = cli._run_analyze_live_state_command(
            FakeLiveStore(),
            symbols=["BTCUSDT"],
            limit=100,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "analyze-live-state")
        self.assertIn("row_count", payload)
        self.assertIn("state_distributions", payload)
        self.assertIn("confidence_stats", payload)
        self.assertIn("conflict_flag_stats", payload)
        self.assertIn("instability_flag_stats", payload)
        self.assertIn("transition_stats", payload)
        json.dumps(payload)

    def test_merge_symbol_filters_combines_single_and_multi(self) -> None:
        from strategy.market_regime import cli

        merged = cli._merge_symbol_filters("btcusdt", "ethusdt,BTCUSDT")

        self.assertEqual(merged, ["BTCUSDT", "ETHUSDT"])

    def test_cli_audit_data_coverage_dispatch(self) -> None:
        from strategy.market_regime import cli

        fake_store = FakeLiveStore()
        with patch("strategy.market_regime.cli._make_store", return_value=fake_store):
            exit_code = cli.run_cli(["audit-data-coverage", "--symbol", "BTCUSDT", "--json"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(fake_store.ensure_schema_called)

    def test_cli_validate_history_signals_dispatch(self) -> None:
        from strategy.market_regime import cli

        fake_store = FakeLiveStore()
        with patch("strategy.market_regime.cli._make_store", return_value=fake_store):
            exit_code = cli.run_cli(["validate-history-signals", "--symbol", "BTCUSDT", "--limit", "25"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(fake_store.ensure_schema_called)

    def test_cli_validate_history_signals_output_contains_summary_preview(self) -> None:
        from strategy.market_regime import cli

        payload = cli._run_validate_history_signals_command(
            FakeLiveStore(),
            symbols=["BTCUSDT"],
            limit=25,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "validate-history-signals")
        self.assertIn("candidate_rows", payload)
        self.assertIn("inserted_rows", payload)
        self.assertIn("summary_preview", payload)

    def test_cli_review_signal_validation_dispatch(self) -> None:
        from strategy.market_regime import cli

        fake_store = FakeLiveStore()
        with patch("strategy.market_regime.cli._make_store", return_value=fake_store):
            exit_code = cli.run_cli(["review-signal-validation", "--symbol", "BTCUSDT", "--min-count", "1"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(fake_store.ensure_schema_called)

    def test_cli_review_signal_validation_output_contains_rows(self) -> None:
        from strategy.market_regime import cli

        payload = cli._run_review_signal_validation_command(
            FakeLiveStore(),
            symbols=["BTCUSDT"],
            min_count=1,
            limit=10,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "review-signal-validation")
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["rows"][0]["decision"], "ALLOW")

    def test_load_latest_state_machine_maps_row(self) -> None:
        cursor = FakeCursor(
            fetchone_result={
                "active_state": "trend_long",
                "confirmation_counters_json": '{"trend_long": 2, "rebound_start_short": 0}',
                "cooldown_remaining_fast_updates": 3,
                "transition_reason": "candidate:trend_long;confirmed",
            }
        )
        store = MarketRegimeStore(connection_factory=lambda: FakeConnection(cursor))

        state = store.load_latest_state_machine("BTCUSDT")

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.current_state, "trend_continuation_long")
        self.assertEqual(state.confirmation_counters["trend_long"], 2)
        self.assertEqual(state.cooldown_remaining_fast_updates, 3)
        self.assertEqual(state.transition_reason, ["candidate:trend_long", "confirmed"])


if __name__ == "__main__":
    unittest.main()

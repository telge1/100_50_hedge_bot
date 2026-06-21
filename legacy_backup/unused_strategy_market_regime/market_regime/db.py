from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import glob
import json
import os
from typing import Any, Callable, Iterable, Mapping, Sequence
from statistics import median

import pymysql

from .decision_policy import DecisionPolicyResult, classify_range_unclear_diagnosis
from .feature_normalizer import compute_oi_price_state
from .models import (
    CoinProfile,
    FastTriggerSnapshot,
    FeatureProfileStats,
    MidRegimeSnapshot,
    NormalizedSnapshot,
    PrimitiveEvents,
    RawMarketSnapshot,
    RegimeSnapshot,
    RoutedRegimeSnapshot,
    ScoreSnapshot,
    SlowRegimeSnapshot,
    StateMachineSnapshot,
)
from .state_machine import _sanitize_routed_state

PROFILED_FEATURES = (
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

RAW_AUDIT_FIELDS = (
    "price",
    "price_change_1m",
    "price_change_5m",
    "price_change_15m",
    "oi_change",
    "oi_change_ratio",
    "trade_volume_1m",
    "volume_spike_ratio",
    "buy_volume",
    "sell_volume",
    "delta",
    "orderflow_ratio",
    "liquidation_density_5m",
    "liquidation_cluster_score",
    "microburst_score",
    "spread",
    "trade_count_1m",
    "avg_trade_size",
    "atr_1m",
    "funding_rate",
    "funding",
)

MARKET_STATE_AUDIT_FIELDS = (
    "price",
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
    "pressure_score",
    "participation_score",
    "instability_score",
    "exhaustion_score",
    "slow_pressure_score",
    "slow_participation_score",
    "slow_exhaustion_score",
    "fast_pressure_score",
    "fast_participation_score",
    "fast_instability_score",
    "fast_exhaustion_score",
    "slow_state",
    "mid_state",
    "fast_state",
    "routed_state",
    "confidence",
    "confidence_source",
    "decision",
    "decision_reason",
    "range_unclear_diagnosis",
    "entry_allowed",
    "active_state",
    "oi_price_state",
    "oi_price_build_long",
    "oi_price_short_covering",
    "oi_price_build_short",
    "oi_price_long_unwinding",
)

LONG_SIGNAL_STATES = {
    "trend_continuation_long",
    "pullback_in_long_context",
    "mid_pullback_in_long",
    "mid_exhaustion_long",
    "mid_reversal_setup_long",
    "reversal_building_long",
    "reversal_confirmed_long",
}

SHORT_SIGNAL_STATES = {
    "trend_continuation_short",
    "pullback_in_short_context",
    "mid_pullback_in_short",
    "mid_exhaustion_short",
    "mid_reversal_setup_short",
    "reversal_building_short",
    "reversal_confirmed_short",
}

PROFILES_CURRENT_AUDIT_FIELDS = (
    "sample_size",
    "profile_version",
    "window_start",
    "window_end",
    "updated_at",
    "threshold_orderflow_long",
    "threshold_orderflow_short",
    "threshold_buy_sell_imbalance_long",
    "threshold_buy_sell_imbalance_short",
    "decay_alpha",
    "profile_mode",
)

PROFILES_HISTORY_AUDIT_FIELDS = (
    "snapshot_time",
    "sample_size",
    "profile_version",
    "window_start",
    "window_end",
    "profile_json",
    "created_at",
)


def _autodetect_mysql_from_running_collector() -> dict[str, str] | None:
    """Best-effort autodetection of MYSQL_* values from a running collector."""
    for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
        proc_dir = Path(cmdline_path).parent
        try:
            cmdline = proc_dir.joinpath("cmdline").read_bytes()
        except OSError:
            continue

        cmd = cmdline.replace(b"\x00", b" ").decode("utf-8", "ignore")
        if "collector.py" not in cmd:
            continue

        try:
            environ_entries = proc_dir.joinpath("environ").read_bytes().split(b"\x00")
        except OSError:
            continue

        found: dict[str, str] = {}
        for entry in environ_entries:
            if not entry.startswith(b"MYSQL_"):
                continue
            try:
                key, value = entry.decode("utf-8", "ignore").split("=", 1)
            except ValueError:
                continue
            if key in {
                "MYSQL_HOST",
                "MYSQL_PORT",
                "MYSQL_USER",
                "MYSQL_PASSWORD",
                "MYSQL_DATABASE",
                "MYSQL_TABLE",
            }:
                found[key] = value

        if {"MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE"}.issubset(found):
            return found
    return None


@dataclass(slots=True)
class MarketRegimeDBConfig:
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    raw_table: str | None = None
    profiles_table: str = "coin_profiles_current"
    profiles_history_table: str = "coin_profiles_history"
    market_state_live_table: str = "market_state_live"
    market_state_history_table: str = "market_state_history"
    signal_validation_results_table: str = "signal_validation_results"
    signal_validation_summary_table: str = "signal_validation_summary"

    def __post_init__(self) -> None:
        detected = _autodetect_mysql_from_running_collector()
        self.host = self.host or os.getenv("MYSQL_HOST") or (detected or {}).get("MYSQL_HOST") or "127.0.0.1"
        self.port = int(
            self.port
            or os.getenv("MYSQL_PORT")
            or (detected or {}).get("MYSQL_PORT")
            or "3306"
        )
        self.user = self.user or os.getenv("MYSQL_USER") or (detected or {}).get("MYSQL_USER") or "root"
        password = self.password if self.password is not None else os.getenv("MYSQL_PASSWORD")
        if password is None and detected is not None:
            password = detected.get("MYSQL_PASSWORD")
        self.password = password if password is not None else ""
        self.database = (
            self.database
            or os.getenv("MYSQL_DATABASE")
            or (detected or {}).get("MYSQL_DATABASE")
            or "liquidation_research"
        )
        self.raw_table = self.raw_table or os.getenv("MYSQL_TABLE") or (detected or {}).get("MYSQL_TABLE") or "liquidation_data"


class MarketRegimeStore:
    def __init__(
        self,
        config: MarketRegimeDBConfig | None = None,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config or MarketRegimeDBConfig()
        self._connection_factory = connection_factory

    def _connect(self):
        if self._connection_factory is not None:
            return self._connection_factory()
        return pymysql.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            database=self.config.database,
            autocommit=True,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )

    def ensure_schema(self) -> None:
        statements = [
            self._build_create_coin_profiles_current_sql(),
            self._build_create_coin_profiles_history_sql(),
            self._build_create_market_state_live_sql(),
            self._build_create_signal_validation_results_sql(),
            self._build_create_signal_validation_summary_sql(),
        ]
        with self._connect() as connection:
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)
                self._ensure_market_state_live_v2_columns(cursor)
                self._ensure_market_state_live_uniqueness(cursor)

    def load_coin_profile(self, symbol: str) -> CoinProfile | None:
        profiles = self.load_coin_profiles([symbol])
        return profiles.get(symbol.upper())

    def load_coin_profiles(self, symbols: list[str]) -> dict[str, CoinProfile]:
        clean_symbols = [symbol.upper() for symbol in symbols if str(symbol).strip()]
        if not clean_symbols:
            return {}

        placeholders = ", ".join(["%s"] * len(clean_symbols))
        query = (
            f"SELECT * FROM {self.config.profiles_table} "
            f"WHERE symbol IN ({placeholders})"
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, clean_symbols)
                rows = cursor.fetchall() or []
        profiles: dict[str, CoinProfile] = {}
        for row in rows:
            profile = self._row_to_coin_profile(row)
            profiles[profile.symbol] = profile
        return profiles

    def load_active_symbols(self) -> list[str]:
        query = f"SELECT DISTINCT symbol FROM {self.config.raw_table} ORDER BY symbol"
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall() or []
        return [str(row.get("symbol") or "").upper() for row in rows if str(row.get("symbol") or "").strip()]

    def load_raw_feature_rows(
        self,
        *,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        clean_symbols = [symbol.upper() for symbol in symbols if str(symbol).strip()]
        if not clean_symbols:
            return {}

        where_clauses = ["symbol IN ({})".format(", ".join(["%s"] * len(clean_symbols)))]
        params: list[Any] = list(clean_symbols)
        if start_time is not None:
            where_clauses.append("timestamp >= %s")
            params.append(start_time)
        if end_time is not None:
            where_clauses.append("timestamp <= %s")
            params.append(end_time)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                query = f"""
                    SELECT
                        {self._build_raw_feature_select_clause(cursor)}
                    FROM {self.config.raw_table}
                    WHERE {" AND ".join(where_clauses)}
                    ORDER BY symbol, timestamp
                """
                cursor.execute(query, params)
                rows = cursor.fetchall() or []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            grouped.setdefault(symbol, []).append(dict(row))
        return grouped

    def load_recent_raw_snapshots(self, symbol: str, limit: int = 2) -> list[RawMarketSnapshot]:
        clean_symbol = str(symbol).strip().upper()
        if not clean_symbol or limit <= 0:
            return []
        with self._connect() as connection:
            with connection.cursor() as cursor:
                query = f"""
                    SELECT
                        {self._build_raw_feature_select_clause(cursor)}
                    FROM {self.config.raw_table}
                    WHERE symbol = %s
                    ORDER BY timestamp DESC
                    LIMIT %s
                """
                cursor.execute(query, [clean_symbol, int(limit)])
                rows = cursor.fetchall() or []
        ordered_rows = list(reversed(rows))
        return [self._row_to_raw_market_snapshot(row) for row in ordered_rows]

    def _build_raw_feature_select_clause(self, cursor: Any) -> str:
        existing_columns = self._load_table_columns(cursor, self.config.raw_table)
        atr_1m_select = "atr_1m" if "atr_1m" in existing_columns else "NULL AS atr_1m"
        return f"""
                symbol,
                timestamp,
                price,
                price_change_1m,
                price_change_5m,
                price_change_15m,
                oi_change,
                oi_change_ratio,
                trade_volume_1m,
                volume_spike_ratio,
                buy_volume,
                sell_volume,
                delta,
                orderflow_ratio,
                liquidation_density_5m,
                liquidation_cluster_score,
                microburst_score,
                spread,
                trade_count_1m,
                avg_trade_size,
                {atr_1m_select}
        """.strip()

    def load_latest_state_machine(self, symbol: str) -> StateMachineSnapshot | None:
        clean_symbol = str(symbol).strip().upper()
        if not clean_symbol:
            return None
        query = f"""
            SELECT *
            FROM {self.config.market_state_live_table}
            WHERE symbol = %s
            ORDER BY id DESC
            LIMIT 1
        """
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, [clean_symbol])
                row = cursor.fetchone()
        if not row:
            return None
        counters = self._safe_json_loads(row.get("confirmation_counters_json"), {})
        if not isinstance(counters, dict):
            counters = {}
        reason = str(row.get("transition_reason") or "")
        active_state = _sanitize_routed_state(row.get("routed_state") or row.get("active_state") or "range_unclear")
        current_ts = self._safe_datetime(row.get("last_processed_ts")) or self._safe_datetime(row.get("ts"))
        return StateMachineSnapshot(
            previous_state=active_state,
            current_state=active_state,
            confirmation_counters={str(k): int(v) for k, v in counters.items()},
            cooldown_remaining_fast_updates=int(row.get("cooldown_remaining_fast_updates") or 0),
            transition_reason=[part for part in reason.split(";") if part],
            transition_applied=False,
            slow_state=str(row.get("slow_state") or "") or None,
            slow_state_memory=str(row.get("slow_state_memory") or "") or None,
            slow_transition_counter=int(row.get("slow_transition_counter") or 0),
            slow_bias=int(row.get("slow_bias") or 0),
            mid_state=str(row.get("mid_state") or "") or None,
            fast_state=str(row.get("fast_state") or "") or None,
            routed_state=active_state,
            current_ts=current_ts,
            last_confirmed_ts=self._safe_datetime(row.get("last_confirmed_ts")),
        )

    def load_live_state_debug_rows(
        self,
        symbols: list[str] | None = None,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clean_symbols = [symbol.upper() for symbol in (symbols or []) if str(symbol).strip()]
        where_clause = ""
        params: list[Any] = []
        if clean_symbols:
            placeholders = ", ".join(["%s"] * len(clean_symbols))
            where_clause = f"WHERE m.symbol IN ({placeholders})"
            params.extend(clean_symbols)
        oi_price_state_select = "'neutral' AS oi_price_state"
        oi_price_build_long_select = "0 AS oi_price_build_long"
        oi_price_short_covering_select = "0 AS oi_price_short_covering"
        oi_price_build_short_select = "0 AS oi_price_build_short"
        oi_price_long_unwinding_select = "0 AS oi_price_long_unwinding"
        routed_state_select = "active_state AS routed_state"
        confidence_select = "NULL AS confidence"
        confidence_source_select = "'missing' AS confidence_source"
        conflict_flags_select = "'{}' AS conflict_flags_json"
        instability_flags_select = "'{}' AS instability_flags_json"
        decision_select = "'WATCHLIST' AS decision"
        decision_reason_select = "'' AS decision_reason"
        range_unclear_diagnosis_select = "NULL AS range_unclear_diagnosis"
        entry_allowed_select = "0 AS entry_allowed"
        transition_reason_select = "'' AS transition_reason"
        routed_transition_reason_select = "'' AS routed_transition_reason"
        query = f"""
            SELECT
                m.symbol,
                m.ts,
                m.active_state AS state,
                m.slow_state,
                m.mid_state,
                m.fast_state,
                {routed_state_select},
                {confidence_select},
                {confidence_source_select},
                {conflict_flags_select},
                {instability_flags_select},
                {decision_select},
                {decision_reason_select},
                {range_unclear_diagnosis_select},
                {entry_allowed_select},
                {transition_reason_select},
                {routed_transition_reason_select},
                {oi_price_state_select},
                {oi_price_build_long_select},
                {oi_price_short_covering_select},
                {oi_price_build_short_select},
                {oi_price_long_unwinding_select}
            FROM {self.config.market_state_live_table} AS m
            {where_clause}
            ORDER BY m.ts DESC, m.symbol ASC
            LIMIT %s
        """
        params.append(int(limit))
        with self._connect() as connection:
            with connection.cursor() as cursor:
                try:
                    cursor.execute(f"SHOW COLUMNS FROM {self.config.market_state_live_table}")
                    columns = cursor.fetchall() or []
                except Exception:
                    columns = []
                existing_columns = {
                    str(row.get("Field") or row.get("field") or "")
                    for row in columns
                    if isinstance(row, Mapping)
                }
                if "oi_price_state" in existing_columns:
                    query = query.replace(oi_price_state_select, "oi_price_state")
                if "oi_price_build_long" in existing_columns:
                    query = query.replace(oi_price_build_long_select, "oi_price_build_long")
                if "oi_price_short_covering" in existing_columns:
                    query = query.replace(oi_price_short_covering_select, "oi_price_short_covering")
                if "oi_price_build_short" in existing_columns:
                    query = query.replace(oi_price_build_short_select, "oi_price_build_short")
                if "oi_price_long_unwinding" in existing_columns:
                    query = query.replace(oi_price_long_unwinding_select, "oi_price_long_unwinding")
                if "routed_state" in existing_columns:
                    query = query.replace(routed_state_select, "routed_state")
                if "confidence" in existing_columns:
                    query = query.replace(confidence_select, "confidence")
                if "confidence_source" in existing_columns:
                    query = query.replace(confidence_source_select, "confidence_source")
                if "conflict_flags_json" in existing_columns:
                    query = query.replace(conflict_flags_select, "conflict_flags_json")
                if "instability_flags_json" in existing_columns:
                    query = query.replace(instability_flags_select, "instability_flags_json")
                if "decision" in existing_columns:
                    query = query.replace(decision_select, "decision")
                if "decision_reason" in existing_columns:
                    query = query.replace(decision_reason_select, "decision_reason")
                if "range_unclear_diagnosis" in existing_columns:
                    query = query.replace(range_unclear_diagnosis_select, "range_unclear_diagnosis")
                if "entry_allowed" in existing_columns:
                    query = query.replace(entry_allowed_select, "entry_allowed")
                if "transition_reason" in existing_columns:
                    query = query.replace(transition_reason_select, "transition_reason")
                if "routed_transition_reason" in existing_columns:
                    query = query.replace(routed_transition_reason_select, "routed_transition_reason")
                cursor.execute(query, params)
                rows = cursor.fetchall() or []
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            oi_price_state = str(row.get("oi_price_state") or "neutral")
            oi_flags = self._derive_oi_price_flags(oi_price_state)
            conflict_flags = self._safe_json_loads(row.get("conflict_flags_json"), {})
            instability_flags = self._safe_json_loads(row.get("instability_flags_json"), {})
            if not isinstance(conflict_flags, dict):
                conflict_flags = {}
            if not isinstance(instability_flags, dict):
                instability_flags = {}
            raw_transition_reason = row.get("transition_reason")
            if isinstance(raw_transition_reason, list):
                transition_reason = [str(item) for item in raw_transition_reason]
            else:
                transition_reason = [part for part in str(raw_transition_reason or "").split(";") if part]
            raw_routed_transition_reason = row.get("routed_transition_reason")
            if isinstance(raw_routed_transition_reason, list):
                routed_transition_reason = [str(item) for item in raw_routed_transition_reason]
            else:
                routed_transition_reason = [part for part in str(raw_routed_transition_reason or "").split(";") if part]
            routed_state = _sanitize_routed_state(row.get("routed_state") or row.get("state"))
            confidence, confidence_source = self._resolve_confidence(
                raw_confidence=row.get("confidence"),
                routed_state=routed_state,
                mid_state=row.get("mid_state"),
            )
            stored_confidence_source = str(row.get("confidence_source") or "").strip().lower()
            stored_decision = str(row.get("decision") or "").strip().upper() or None
            stored_range_unclear_diagnosis = (
                str(row.get("range_unclear_diagnosis") or "").strip() or None
            )
            normalized_rows.append(
                {
                    "symbol": str(row.get("symbol") or "").upper(),
                    "ts": self._safe_datetime(row.get("ts")),
                    "state": _sanitize_routed_state(row.get("state")),
                    "routed_state": routed_state,
                    "slow_state": str(row.get("slow_state") or "") or None,
                    "mid_state": str(row.get("mid_state") or "") or None,
                    "fast_state": str(row.get("fast_state") or "") or None,
                    "confidence": confidence,
                    "confidence_source": (
                        confidence_source
                        if stored_confidence_source in {"", "missing"} and row.get("confidence") is None
                        else (stored_confidence_source or confidence_source or "missing")
                    ),
                    "conflict_flags": {str(key): bool(value) for key, value in conflict_flags.items()},
                    "instability_flags": {str(key): bool(value) for key, value in instability_flags.items()},
                    "decision": stored_decision,
                    "decision_reason": str(row.get("decision_reason") or "") or None,
                    "range_unclear_diagnosis": stored_range_unclear_diagnosis,
                    "entry_allowed": bool(int(row.get("entry_allowed") or 0)),
                    "transition_reason": transition_reason,
                    "routed_transition_reason": routed_transition_reason,
                    "oi_price_state": oi_price_state,
                    "oi_price_build_long": bool(oi_flags["oi_price_build_long"]),
                    "oi_price_short_covering": bool(oi_flags["oi_price_short_covering"]),
                    "oi_price_build_short": bool(oi_flags["oi_price_build_short"]),
                    "oi_price_long_unwinding": bool(oi_flags["oi_price_long_unwinding"]),
                }
            )
        return normalized_rows

    def load_market_state_live_oi_performance_rows(
        self,
        *,
        symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        clean_symbols = [symbol.upper() for symbol in (symbols or []) if str(symbol).strip()]
        where_clause = ""
        params: list[Any] = []
        if clean_symbols:
            placeholders = ", ".join(["%s"] * len(clean_symbols))
            where_clause = f"WHERE m.symbol IN ({placeholders})"
            params.extend(clean_symbols)
        query = f"""
            SELECT
                m.symbol,
                m.ts,
                m.active_state AS state,
                COALESCE(NULLIF(m.oi_price_state, ''), 'neutral') AS oi_price_state,
                r5.price_change_5m AS future_return_5m,
                r15.price_change_15m AS future_return_15m
            FROM {self.config.market_state_live_table} AS m
            LEFT JOIN {self.config.raw_table} AS r5
                ON r5.symbol = m.symbol
               AND r5.timestamp = DATE_ADD(m.ts, INTERVAL 5 MINUTE)
            LEFT JOIN {self.config.raw_table} AS r15
                ON r15.symbol = m.symbol
               AND r15.timestamp = DATE_ADD(m.ts, INTERVAL 15 MINUTE)
            {where_clause}
            ORDER BY m.ts ASC, m.symbol ASC
        """
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall() or []

    def load_market_state_live_telemetry_rows(
        self,
        *,
        symbols: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clean_symbols = [symbol.upper() for symbol in (symbols or []) if str(symbol).strip()]
        where_clause = ""
        params: list[Any] = []
        if clean_symbols:
            placeholders = ", ".join(["%s"] * len(clean_symbols))
            where_clause = f"WHERE m.symbol IN ({placeholders})"
            params.extend(clean_symbols)
        active_state_select = "'range_unclear' AS active_state"
        routed_state_select = "active_state AS routed_state"
        slow_state_select = "NULL AS slow_state"
        mid_state_select = "NULL AS mid_state"
        fast_state_select = "NULL AS fast_state"
        confidence_select = "NULL AS confidence"
        confidence_source_select = "'missing' AS confidence_source"
        conflict_flags_select = "'{}' AS conflict_flags_json"
        instability_flags_select = "'{}' AS instability_flags_json"
        decision_select = "'WATCHLIST' AS decision"
        decision_reason_select = "'' AS decision_reason"
        range_unclear_diagnosis_select = "NULL AS range_unclear_diagnosis"
        entry_allowed_select = "0 AS entry_allowed"
        transition_reason_select = "'' AS transition_reason"
        routed_transition_reason_select = "'' AS routed_transition_reason"
        limit_clause = ""
        if limit is not None and int(limit) > 0:
            limit_clause = "LIMIT %s"
        query = f"""
            SELECT
                m.symbol,
                m.ts,
                {active_state_select},
                {routed_state_select},
                {slow_state_select},
                {mid_state_select},
                {fast_state_select},
                {confidence_select},
                {confidence_source_select},
                {conflict_flags_select},
                {instability_flags_select},
                {decision_select},
                {decision_reason_select},
                {range_unclear_diagnosis_select},
                {entry_allowed_select},
                {transition_reason_select},
                {routed_transition_reason_select}
            FROM {self.config.market_state_live_table} AS m
            {where_clause}
            ORDER BY m.ts DESC, m.symbol ASC
            {limit_clause}
        """
        if limit is not None and int(limit) > 0:
            params.append(int(limit))
        with self._connect() as connection:
            with connection.cursor() as cursor:
                try:
                    cursor.execute(f"SHOW COLUMNS FROM {self.config.market_state_live_table}")
                    columns = cursor.fetchall() or []
                except Exception:
                    columns = []
                existing_columns = {
                    str(row.get("Field") or row.get("field") or "")
                    for row in columns
                    if isinstance(row, Mapping)
                }
                if "active_state" in existing_columns:
                    query = query.replace(active_state_select, "active_state")
                if "routed_state" in existing_columns:
                    query = query.replace(routed_state_select, "routed_state")
                if "slow_state" in existing_columns:
                    query = query.replace(slow_state_select, "slow_state")
                if "mid_state" in existing_columns:
                    query = query.replace(mid_state_select, "mid_state")
                if "fast_state" in existing_columns:
                    query = query.replace(fast_state_select, "fast_state")
                if "confidence" in existing_columns:
                    query = query.replace(confidence_select, "confidence")
                if "confidence_source" in existing_columns:
                    query = query.replace(confidence_source_select, "confidence_source")
                if "conflict_flags_json" in existing_columns:
                    query = query.replace(conflict_flags_select, "conflict_flags_json")
                if "instability_flags_json" in existing_columns:
                    query = query.replace(instability_flags_select, "instability_flags_json")
                if "decision" in existing_columns:
                    query = query.replace(decision_select, "decision")
                if "decision_reason" in existing_columns:
                    query = query.replace(decision_reason_select, "decision_reason")
                if "range_unclear_diagnosis" in existing_columns:
                    query = query.replace(range_unclear_diagnosis_select, "range_unclear_diagnosis")
                if "entry_allowed" in existing_columns:
                    query = query.replace(entry_allowed_select, "entry_allowed")
                if "transition_reason" in existing_columns:
                    query = query.replace(transition_reason_select, "transition_reason")
                if "routed_transition_reason" in existing_columns:
                    query = query.replace(routed_transition_reason_select, "routed_transition_reason")
                cursor.execute(query, params)
                rows = cursor.fetchall() or []
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            conflict_flags = self._safe_json_loads(row.get("conflict_flags_json"), {})
            instability_flags = self._safe_json_loads(row.get("instability_flags_json"), {})
            if not isinstance(conflict_flags, dict):
                conflict_flags = {}
            if not isinstance(instability_flags, dict):
                instability_flags = {}
            raw_transition_reason = row.get("transition_reason")
            if isinstance(raw_transition_reason, list):
                transition_reason = [str(item) for item in raw_transition_reason if str(item)]
            else:
                transition_reason = [part for part in str(raw_transition_reason or "").split(";") if part]
            raw_routed_transition_reason = row.get("routed_transition_reason")
            if isinstance(raw_routed_transition_reason, list):
                routed_transition_reason = [str(item) for item in raw_routed_transition_reason if str(item)]
            else:
                routed_transition_reason = [part for part in str(raw_routed_transition_reason or "").split(";") if part]
            routed_state = _sanitize_routed_state(
                row.get("routed_state") or row.get("active_state")
            )
            confidence, confidence_source = self._resolve_confidence(
                raw_confidence=row.get("confidence"),
                routed_state=routed_state,
                mid_state=row.get("mid_state"),
            )
            stored_confidence_source = str(row.get("confidence_source") or "").strip().lower()
            range_unclear_diagnosis = str(row.get("range_unclear_diagnosis") or "").strip() or None
            normalized_rows.append(
                {
                    "symbol": str(row.get("symbol") or "").upper(),
                    "ts": self._safe_datetime(row.get("ts")),
                    "active_state": _sanitize_routed_state(row.get("active_state")),
                    "routed_state": routed_state,
                    "slow_state": str(row.get("slow_state") or "") or None,
                    "mid_state": str(row.get("mid_state") or "") or None,
                    "fast_state": str(row.get("fast_state") or "") or None,
                    "confidence": confidence,
                    "confidence_source": (
                        confidence_source
                        if stored_confidence_source in {"", "missing"} and row.get("confidence") is None
                        else (stored_confidence_source or confidence_source or "missing")
                    ),
                    "conflict_flags": {str(key): bool(value) for key, value in conflict_flags.items()},
                    "instability_flags": {str(key): bool(value) for key, value in instability_flags.items()},
                    "decision": str(row.get("decision") or "").strip().upper() or None,
                    "decision_reason": str(row.get("decision_reason") or "") or None,
                    "range_unclear_diagnosis": range_unclear_diagnosis,
                    "entry_allowed": bool(int(row.get("entry_allowed") or 0)),
                    "transition_reason": transition_reason,
                    "routed_transition_reason": routed_transition_reason,
                }
            )
        return normalized_rows

    def analyze_market_state_live_telemetry(
        self,
        *,
        symbols: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        rows = self.load_market_state_live_telemetry_rows(symbols=symbols, limit=limit)
        distributions = {
            "routed_state": defaultdict(int),
            "slow_state": defaultdict(int),
            "mid_state": defaultdict(int),
            "fast_state": defaultdict(int),
        }
        confidence_values: list[float] = []
        confidence_buckets = {
            "lt_0_33": 0,
            "between_0_33_and_0_66": 0,
            "gt_0_66": 0,
        }
        rows_with_conflict = 0
        rows_with_instability = 0
        confidence_source_counts: dict[str, int] = defaultdict(int)
        conflict_flag_counts: dict[str, int] = defaultdict(int)
        instability_flag_counts: dict[str, int] = defaultdict(int)
        conflict_flag_counts_by_routed_state: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        instability_flag_counts_by_routed_state: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        transition_reason_counts: dict[str, int] = defaultdict(int)
        routed_transition_reason_counts: dict[str, int] = defaultdict(int)

        for row in rows:
            routed_state = str(row.get("routed_state") or "none")
            slow_state = str(row.get("slow_state") or "none")
            mid_state = str(row.get("mid_state") or "none")
            fast_state = str(row.get("fast_state") or "none")
            distributions["routed_state"][routed_state] += 1
            distributions["slow_state"][slow_state] += 1
            distributions["mid_state"][mid_state] += 1
            distributions["fast_state"][fast_state] += 1

            confidence = self._safe_optional_float(row.get("confidence"))
            confidence_source_counts[str(row.get("confidence_source") or "missing")] += 1
            if confidence is not None:
                confidence_values.append(confidence)
                if confidence < 0.33:
                    confidence_buckets["lt_0_33"] += 1
                elif confidence <= 0.66:
                    confidence_buckets["between_0_33_and_0_66"] += 1
                else:
                    confidence_buckets["gt_0_66"] += 1

            conflict_flags = row.get("conflict_flags")
            if not isinstance(conflict_flags, Mapping):
                conflict_flags = {}
            active_conflicts = [str(key) for key, value in conflict_flags.items() if bool(value)]
            if active_conflicts:
                rows_with_conflict += 1
                for key in active_conflicts:
                    conflict_flag_counts[key] += 1
                    conflict_flag_counts_by_routed_state[routed_state][key] += 1

            instability_flags = row.get("instability_flags")
            if not isinstance(instability_flags, Mapping):
                instability_flags = {}
            active_instabilities = [str(key) for key, value in instability_flags.items() if bool(value)]
            if active_instabilities:
                rows_with_instability += 1
                for key in active_instabilities:
                    instability_flag_counts[key] += 1
                    instability_flag_counts_by_routed_state[routed_state][key] += 1

            for reason in row.get("transition_reason") or []:
                transition_reason_counts[str(reason)] += 1
            for reason in row.get("routed_transition_reason") or []:
                routed_transition_reason_counts[str(reason)] += 1

        rows_with_confidence = len(confidence_values)
        row_count = len(rows)
        return {
            "row_count": row_count,
            "selected_symbols": [symbol.upper() for symbol in (symbols or []) if str(symbol).strip()],
            "limit": int(limit) if limit is not None and int(limit) > 0 else None,
            "state_distributions": {
                key: dict(sorted(value.items()))
                for key, value in distributions.items()
            },
            "confidence_stats": {
                "rows_with_confidence": rows_with_confidence,
                "rows_without_confidence": row_count - rows_with_confidence,
                "min": min(confidence_values) if confidence_values else None,
                "max": max(confidence_values) if confidence_values else None,
                "avg": (sum(confidence_values) / rows_with_confidence) if confidence_values else None,
                "buckets": confidence_buckets,
                "source_counts": dict(sorted(confidence_source_counts.items())),
            },
            "conflict_flag_stats": {
                "rows_with_any_conflict": rows_with_conflict,
                "rows_without_conflict": row_count - rows_with_conflict,
                "flag_counts": dict(sorted(conflict_flag_counts.items())),
                "flag_counts_by_routed_state": {
                    state: dict(sorted(counts.items()))
                    for state, counts in sorted(conflict_flag_counts_by_routed_state.items())
                },
            },
            "instability_flag_stats": {
                "rows_with_any_instability": rows_with_instability,
                "rows_without_instability": row_count - rows_with_instability,
                "flag_counts": dict(sorted(instability_flag_counts.items())),
                "flag_counts_by_routed_state": {
                    state: dict(sorted(counts.items()))
                    for state, counts in sorted(instability_flag_counts_by_routed_state.items())
                },
            },
            "transition_stats": {
                "transition_reason_counts": dict(sorted(transition_reason_counts.items())),
                "routed_transition_reason_counts": dict(sorted(routed_transition_reason_counts.items())),
            },
        }

    @staticmethod
    def _derive_confidence_fallback(
        *,
        routed_state: str | None,
        mid_state: str | None,
    ) -> float | None:
        routed = _sanitize_routed_state(routed_state)
        mid = str(mid_state or "").strip()
        if routed == "emergency":
            return 0.95
        if mid and routed == _sanitize_routed_state(mid):
            return 0.85
        if routed in {
            "trend_continuation_long",
            "trend_continuation_short",
            "pullback_in_long_context",
            "pullback_in_short_context",
        }:
            return 0.75
        if routed == "range_unclear":
            return 0.40
        return None

    @classmethod
    def _resolve_confidence(
        cls,
        *,
        raw_confidence: Any,
        routed_state: str | None,
        mid_state: str | None,
    ) -> tuple[float | None, str]:
        stored = cls._safe_optional_float(raw_confidence)
        if stored is not None:
            return stored, "stored"
        derived = cls._derive_confidence_fallback(
            routed_state=routed_state,
            mid_state=mid_state,
        )
        if derived is not None:
            return derived, "derived_fallback"
        return None, "missing"

    @staticmethod
    def _classify_range_unclear_reason(
        *,
        transition_reason: Sequence[str],
        routed_transition_reason: Sequence[str],
    ) -> str:
        all_reasons = [str(item) for item in [*transition_reason, *routed_transition_reason] if str(item)]
        if any(reason.startswith("awaiting_confirmation:") for reason in all_reasons):
            return "waiting_for_confirmation"
        if any(
            reason.startswith("blocked_transition:")
            or "ambiguous" in reason
            or "conflict" in reason
            for reason in all_reasons
        ):
            return "conflicting_candidates"
        if any(reason in {"true_range_or_unclear_context", "range_or_unclear_context"} for reason in all_reasons):
            return "true_range_context"
        if any(reason in {"same_ts_guard", "cooldown_active"} for reason in all_reasons):
            return "guard_or_holdover"
        return "no_signal_confirmed"

    def load_market_state_live_return_rows(
        self,
        *,
        symbols: list[str] | None = None,
        limit: int | None = None,
        horizons: tuple[int, ...] = (5, 15, 30),
    ) -> list[dict[str, Any]]:
        clean_symbols = [symbol.upper() for symbol in (symbols or []) if str(symbol).strip()]
        where_clause = ""
        params: list[Any] = []
        if clean_symbols:
            placeholders = ", ".join(["%s"] * len(clean_symbols))
            where_clause = f"WHERE m.symbol IN ({placeholders})"
            params.extend(clean_symbols)
        active_state_select = "'range_unclear' AS active_state"
        routed_state_select = "active_state AS routed_state"
        slow_state_select = "NULL AS slow_state"
        mid_state_select = "NULL AS mid_state"
        fast_state_select = "NULL AS fast_state"
        confidence_select = "NULL AS confidence"
        conflict_flags_select = "'{}' AS conflict_flags_json"
        instability_flags_select = "'{}' AS instability_flags_json"
        transition_reason_select = "'' AS transition_reason"
        routed_transition_reason_select = "'' AS routed_transition_reason"
        future_joins: list[str] = []
        future_selects: list[str] = []
        join_params: list[int] = []
        for horizon in horizons:
            alias = f"future_{horizon}"
            future_joins.append(
                f"""
                LEFT JOIN {self.config.raw_table} AS {alias}
                    ON {alias}.symbol = m.symbol
                   AND {alias}.timestamp = DATE_ADD(m.ts, INTERVAL %s MINUTE)
                """
            )
            future_selects.append(f"{alias}.price AS future_price_{horizon}")
            join_params.append(horizon)
        limit_clause = ""
        if limit is not None and int(limit) > 0:
            limit_clause = "LIMIT %s"
        query = f"""
            SELECT
                m.symbol,
                m.ts,
                m.price,
                {active_state_select},
                {routed_state_select},
                {slow_state_select},
                {mid_state_select},
                {fast_state_select},
                {confidence_select},
                {conflict_flags_select},
                {instability_flags_select},
                {transition_reason_select},
                {routed_transition_reason_select},
                {", ".join(future_selects)}
            FROM {self.config.market_state_live_table} AS m
            {" ".join(future_joins)}
            {where_clause}
            ORDER BY m.ts DESC, m.symbol ASC
            {limit_clause}
        """
        params = join_params + params
        if limit is not None and int(limit) > 0:
            params.append(int(limit))
        with self._connect() as connection:
            with connection.cursor() as cursor:
                try:
                    cursor.execute(f"SHOW COLUMNS FROM {self.config.market_state_live_table}")
                    columns = cursor.fetchall() or []
                except Exception:
                    columns = []
                existing_columns = {
                    str(row.get("Field") or row.get("field") or "")
                    for row in columns
                    if isinstance(row, Mapping)
                }
                if "active_state" in existing_columns:
                    query = query.replace(active_state_select, "active_state")
                if "routed_state" in existing_columns:
                    query = query.replace(routed_state_select, "routed_state")
                if "slow_state" in existing_columns:
                    query = query.replace(slow_state_select, "slow_state")
                if "mid_state" in existing_columns:
                    query = query.replace(mid_state_select, "mid_state")
                if "fast_state" in existing_columns:
                    query = query.replace(fast_state_select, "fast_state")
                if "confidence" in existing_columns:
                    query = query.replace(confidence_select, "confidence")
                if "conflict_flags_json" in existing_columns:
                    query = query.replace(conflict_flags_select, "conflict_flags_json")
                if "instability_flags_json" in existing_columns:
                    query = query.replace(instability_flags_select, "instability_flags_json")
                if "transition_reason" in existing_columns:
                    query = query.replace(transition_reason_select, "transition_reason")
                if "routed_transition_reason" in existing_columns:
                    query = query.replace(routed_transition_reason_select, "routed_transition_reason")
                cursor.execute(query, params)
                rows = cursor.fetchall() or []
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            conflict_flags = self._safe_json_loads(row.get("conflict_flags_json"), {})
            instability_flags = self._safe_json_loads(row.get("instability_flags_json"), {})
            if not isinstance(conflict_flags, Mapping):
                conflict_flags = {}
            if not isinstance(instability_flags, Mapping):
                instability_flags = {}
            raw_transition_reason = row.get("transition_reason")
            if isinstance(raw_transition_reason, list):
                transition_reason = [str(item) for item in raw_transition_reason if str(item)]
            else:
                transition_reason = [part for part in str(raw_transition_reason or "").split(";") if part]
            raw_routed_transition_reason = row.get("routed_transition_reason")
            if isinstance(raw_routed_transition_reason, list):
                routed_transition_reason = [str(item) for item in raw_routed_transition_reason if str(item)]
            else:
                routed_transition_reason = [
                    part for part in str(raw_routed_transition_reason or "").split(";") if part
                ]
            routed_state = _sanitize_routed_state(
                row.get("routed_state") or row.get("active_state")
            )
            confidence, confidence_source = self._resolve_confidence(
                raw_confidence=row.get("confidence"),
                routed_state=routed_state,
                mid_state=row.get("mid_state"),
            )
            future_prices = {}
            for horizon in horizons:
                future_prices[horizon] = self._safe_optional_float(row.get(f"future_price_{horizon}"))
            normalized_rows.append(
                {
                    "symbol": str(row.get("symbol") or "").upper(),
                    "ts": self._safe_datetime(row.get("ts")),
                    "price": self._safe_optional_float(row.get("price")),
                    "routed_state": routed_state,
                    "slow_state": str(row.get("slow_state") or "") or None,
                    "mid_state": str(row.get("mid_state") or "") or None,
                    "fast_state": str(row.get("fast_state") or "") or None,
                    "confidence": confidence,
                    "confidence_source": confidence_source,
                    "conflict_flags": {str(key): bool(value) for key, value in conflict_flags.items()},
                    "instability_flags": {str(key): bool(value) for key, value in instability_flags.items()},
                    "transition_reason": transition_reason,
                    "routed_transition_reason": routed_transition_reason,
                    "future_prices": future_prices,
                }
            )
        return normalized_rows

    def analyze_market_state_live_quality(
        self,
        *,
        symbols: list[str] | None = None,
        limit: int | None = None,
        horizons: tuple[int, ...] = (5, 15, 30),
    ) -> dict[str, Any]:
        rows = self.load_market_state_live_return_rows(
            symbols=symbols,
            limit=limit,
            horizons=horizons,
        )
        row_count = len(rows)
        selected_symbols = [symbol.upper() for symbol in (symbols or []) if str(symbol).strip()]
        limit_value = int(limit) if limit is not None and int(limit) > 0 else None

        def _make_return_template():
            return {
                horizon: {"sum": 0.0, "count": 0, "positive": 0}
                for horizon in horizons
            }

        state_quality: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "row_count": 0,
                "confidence_sum": 0.0,
                "confidence_count": 0,
                "conflict_rows": 0,
                "instability_rows": 0,
                "return_stats": _make_return_template(),
            }
        )
        confidence_buckets: dict[str, dict[str, Any]] = {
            key: {
                "row_count": 0,
                "confidence_sum": 0.0,
                "confidence_source_counts": defaultdict(int),
                "return_stats": {
                    horizon: {"sum": 0.0, "count": 0, "positive": 0, "values": []}
                    for horizon in horizons
                },
            }
            for key in ("lt_0_33", "between_0_33_and_0_66", "gt_0_66", "none")
        }
        confidence_source_counts: dict[str, int] = defaultdict(int)
        conflict_flags: dict[str, dict[str, Any]] = {}
        instability_flags: dict[str, dict[str, Any]] = {}
        transition_stats: dict[str, dict[str, Any]] = {}
        routed_transition_stats: dict[str, dict[str, Any]] = {}
        range_unclear_diagnosis: dict[str, int] = defaultdict(int)

        for row in rows:
            routed_state = str(row.get("routed_state") or "none")
            bucket_key = "none"
            confidence = row.get("confidence")
            confidence_source = str(row.get("confidence_source") or "missing")
            confidence_source_counts[confidence_source] += 1
            if confidence is not None:
                if confidence < 0.33:
                    bucket_key = "lt_0_33"
                elif confidence <= 0.66:
                    bucket_key = "between_0_33_and_0_66"
                else:
                    bucket_key = "gt_0_66"
            bucket_info = confidence_buckets[bucket_key]
            bucket_info["row_count"] += 1
            bucket_info["confidence_source_counts"][confidence_source] += 1
            if confidence is not None:
                bucket_info["confidence_sum"] += confidence

            state_entry = state_quality[routed_state]
            state_entry["row_count"] += 1
            if confidence is not None:
                state_entry["confidence_sum"] += confidence
                state_entry["confidence_count"] += 1
            conflict_flags_data = row.get("conflict_flags") or {}
            active_conflicts = [key for key, value in conflict_flags_data.items() if bool(value)]
            if active_conflicts:
                state_entry["conflict_rows"] += 1
                for flag in active_conflicts:
                    bucket_stats = conflict_flags.setdefault(
                        flag,
                        {
                            "row_count": 0,
                            "routed_states": defaultdict(int),
                            "return_stats": _make_return_template(),
                        },
                    )
                    bucket_stats["row_count"] += 1
                    bucket_stats["routed_states"][routed_state] += 1
            instability_flags_data = row.get("instability_flags") or {}
            active_instabilities = [key for key, value in instability_flags_data.items() if bool(value)]
            if active_instabilities:
                state_entry["instability_rows"] += 1
                for flag in active_instabilities:
                    bucket_stats = instability_flags.setdefault(
                        flag,
                        {
                            "row_count": 0,
                            "routed_states": defaultdict(int),
                            "return_stats": _make_return_template(),
                        },
                    )
                    bucket_stats["row_count"] += 1
                    bucket_stats["routed_states"][routed_state] += 1
            future_prices = row.get("future_prices") or {}
            current_price = row.get("price")
            def _compute_return(horizon: int) -> float | None:
                future_value = future_prices.get(horizon)
                if (
                    current_price is None
                    or future_value is None
                    or current_price == 0.0
                ):
                    return None
                return float(future_value) / float(current_price) - 1.0

            returns = {h: _compute_return(h) for h in horizons}
            for horizon, value in returns.items():
                if value is None:
                    continue
                state_entry["return_stats"][horizon]["sum"] += value
                state_entry["return_stats"][horizon]["count"] += 1
                if value > 0:
                    state_entry["return_stats"][horizon]["positive"] += 1
                bucket_info["return_stats"][horizon]["sum"] += value
                bucket_info["return_stats"][horizon]["count"] += 1
                bucket_info["return_stats"][horizon]["positive"] += 1 if value > 0 else 0
                bucket_info["return_stats"][horizon]["values"].append(value)
                for flag in active_conflicts:
                    flag_entry = conflict_flags[flag]
                    flag_entry["return_stats"][horizon]["sum"] += value
                    flag_entry["return_stats"][horizon]["count"] += 1
                    if value > 0:
                        flag_entry["return_stats"][horizon]["positive"] += 1
                for flag in active_instabilities:
                    flag_entry = instability_flags[flag]
                    flag_entry["return_stats"][horizon]["sum"] += value
                    flag_entry["return_stats"][horizon]["count"] += 1
                    if value > 0:
                        flag_entry["return_stats"][horizon]["positive"] += 1
            def _update_reason_stats(reason_key: str, target_stats: dict[str, dict[str, Any]]) -> None:
                if not reason_key:
                    return
                entry = target_stats.setdefault(
                    reason_key,
                    {
                        "row_count": 0,
                        "routed_state_distribution": defaultdict(int),
                        "return_stats": _make_return_template(),
                    },
                )
                entry["row_count"] += 1
                entry["routed_state_distribution"][routed_state] += 1
                for horizon, value in returns.items():
                    if value is None:
                        continue
                    entry["return_stats"][horizon]["sum"] += value
                    entry["return_stats"][horizon]["count"] += 1
                    if value > 0:
                        entry["return_stats"][horizon]["positive"] += 1

            for reason in row.get("transition_reason") or []:
                _update_reason_stats(str(reason), transition_stats)
            for reason in row.get("routed_transition_reason") or []:
                _update_reason_stats(str(reason), routed_transition_stats)
            if routed_state == "range_unclear":
                diagnosis_key = self._classify_range_unclear_reason(
                    transition_reason=row.get("transition_reason") or [],
                    routed_transition_reason=row.get("routed_transition_reason") or [],
                )
                range_unclear_diagnosis[diagnosis_key] += 1

        def _finalize_state_stats():
            result: dict[str, Any] = {}
            for state, entry in state_quality.items():
                row_cnt = entry["row_count"]
                result[state] = {
                    "row_count": row_cnt,
                    "avg_confidence": (
                        entry["confidence_sum"] / entry["confidence_count"]
                        if entry["confidence_count"]
                        else None
                    ),
                    "conflict_rate": (entry["conflict_rows"] / row_cnt * 100.0) if row_cnt else 0.0,
                    "instability_rate": (entry["instability_rows"] / row_cnt * 100.0) if row_cnt else 0.0,
                    "return_stats": {
                        str(horizon): {
                            "count": entry["return_stats"][horizon]["count"],
                            "avg": (
                                entry["return_stats"][horizon]["sum"]
                                / entry["return_stats"][horizon]["count"]
                                if entry["return_stats"][horizon]["count"]
                                else None
                            ),
                            "positive_pct": (
                                entry["return_stats"][horizon]["positive"]
                                / entry["return_stats"][horizon]["count"]
                                * 100.0
                                if entry["return_stats"][horizon]["count"]
                                else None
                            ),
                        }
                        for horizon in horizons
                    },
                }
            return result

        def _finalize_bucket(bucket_entry: dict[str, Any]) -> dict[str, Any]:
            row_cnt = bucket_entry["row_count"]
            return {
                "row_count": row_cnt,
                "avg_confidence": (
                    bucket_entry["confidence_sum"] / row_cnt if row_cnt else None
                ),
                "confidence_source_counts": dict(
                    sorted(bucket_entry["confidence_source_counts"].items())
                ),
                "return_stats": {
                    str(horizon): {
                        "count": bucket_entry["return_stats"][horizon]["count"],
                        "avg": (
                            bucket_entry["return_stats"][horizon]["sum"]
                            / bucket_entry["return_stats"][horizon]["count"]
                            if bucket_entry["return_stats"][horizon]["count"]
                            else None
                        ),
                        "positive_pct": (
                            bucket_entry["return_stats"][horizon]["positive"]
                            / bucket_entry["return_stats"][horizon]["count"]
                            * 100.0
                            if bucket_entry["return_stats"][horizon]["count"]
                            else None
                        ),
                        "median": median(
                            bucket_entry["return_stats"][horizon]["values"]
                        )
                        if bucket_entry["return_stats"][horizon]["values"]
                        else None,
                    }
                    for horizon in horizons
                },
            }

        def _finalize_flag_stats(
            stats: dict[str, dict[str, Any]]
        ) -> dict[str, dict[str, Any]]:
            result: dict[str, dict[str, Any]] = {}
            for flag, entry in stats.items():
                result[flag] = {
                    "row_count": entry["row_count"],
                    "routed_states": dict(sorted(entry["routed_states"].items())),
                    "return_stats": {
                        str(horizon): {
                            "count": entry["return_stats"][horizon]["count"],
                            "avg": (
                                entry["return_stats"][horizon]["sum"]
                                / entry["return_stats"][horizon]["count"]
                                if entry["return_stats"][horizon]["count"]
                                else None
                            ),
                            "positive_pct": (
                                entry["return_stats"][horizon]["positive"]
                                / entry["return_stats"][horizon]["count"]
                                * 100.0
                                if entry["return_stats"][horizon]["count"]
                                else None
                            ),
                        }
                        for horizon in horizons
                    },
                }
            return result

        def _finalize_reason_stats(
            stats: dict[str, dict[str, Any]]
        ) -> dict[str, dict[str, Any]]:
            result: dict[str, dict[str, Any]] = {}
            for reason, entry in stats.items():
                result[reason] = {
                    "row_count": entry["row_count"],
                    "routed_state_distribution": dict(
                        sorted(entry["routed_state_distribution"].items())
                    ),
                    "return_stats": {
                        str(horizon): {
                            "count": entry["return_stats"][horizon]["count"],
                            "avg": (
                                entry["return_stats"][horizon]["sum"]
                                / entry["return_stats"][horizon]["count"]
                                if entry["return_stats"][horizon]["count"]
                                else None
                            ),
                            "positive_pct": (
                                entry["return_stats"][horizon]["positive"]
                                / entry["return_stats"][horizon]["count"]
                                * 100.0
                                if entry["return_stats"][horizon]["count"]
                                else None
                            ),
                        }
                        for horizon in horizons
                    },
                }
            return result

        bucket_results = {
            key: _finalize_bucket(entry) for key, entry in confidence_buckets.items()
        }
        return {
            "row_count": row_count,
            "selected_symbols": selected_symbols,
            "limit": limit_value,
            "horizons": horizons,
            "confidence_coverage": {
                "rows_with_confidence": sum(
                    int(bucket["row_count"])
                    for key, bucket in confidence_buckets.items()
                    if key != "none"
                ),
                "rows_without_confidence": int(confidence_buckets["none"]["row_count"]),
                "source_counts": dict(sorted(confidence_source_counts.items())),
            },
            "state_quality": _finalize_state_stats(),
            "confidence_buckets": bucket_results,
            "conflict_flags": _finalize_flag_stats(conflict_flags),
            "instability_flags": _finalize_flag_stats(instability_flags),
            "transition_reason_stats": _finalize_reason_stats(transition_stats),
            "routed_transition_reason_stats": _finalize_reason_stats(
                routed_transition_stats
            ),
            "range_unclear_diagnosis": dict(sorted(range_unclear_diagnosis.items())),
        }

    def analyze_market_state_live_quality_review(
        self,
        *,
        symbols: list[str] | None = None,
        limit: int | None = None,
        horizons: tuple[int, ...] = (5, 15, 30),
        min_rows_for_ranking: int = 3,
    ) -> dict[str, Any]:
        """
        Review helper that distills the quality analysis into a dashboard-friendly view.
        Ranking heuristics here are strictly for review/export and do not affect runtime logic.
        """
        payload = self.analyze_market_state_live_quality(
            symbols=symbols,
            limit=limit,
            horizons=horizons,
        )
        return self._build_quality_review(payload, min_rows_for_ranking)

    def _build_quality_review(self, payload: dict[str, Any], min_rows_for_ranking: int) -> dict[str, Any]:
        state_quality = payload.get("state_quality", {})
        horizons = tuple(payload.get("horizons") or ())

        horizon_return_counts: dict[str, int] = {str(h): 0 for h in horizons}
        for entry in state_quality.values():
            for horizon in horizons:
                stats = entry["return_stats"].get(str(horizon), {})
                horizon_return_counts[str(horizon)] += stats.get("count", 0)

        overview = {
            "row_count": payload.get("row_count", 0),
            "selected_symbols": payload.get("selected_symbols", []),
            "limit": payload.get("limit"),
            "horizons": horizons,
            "horizon_return_counts": horizon_return_counts,
            "confidence_coverage": payload.get("confidence_coverage", {}),
            "range_unclear_rows": int(state_quality.get("range_unclear", {}).get("row_count") or 0),
            "range_unclear_share_pct": (
                float(state_quality.get("range_unclear", {}).get("row_count") or 0)
                / float(payload.get("row_count") or 1)
                * 100.0
                if payload.get("row_count")
                else 0.0
            ),
        }

        def _state_score(entry: dict[str, Any]) -> float | None:
            scores = [
                value
                for horizon in horizons
                if (value := entry["return_stats"].get(str(horizon), {}).get("avg")) is not None
            ]
            return sum(scores) / len(scores) if scores else None

        def _build_return_summary(entry: dict[str, Any]) -> dict[str, Any]:
            return {
                str(horizon): {
                    "count": entry["return_stats"].get(str(horizon), {}).get("count", 0),
                    "avg": entry["return_stats"].get(str(horizon), {}).get("avg"),
                    "positive_pct": entry["return_stats"].get(str(horizon), {}).get("positive_pct"),
                }
                for horizon in horizons
            }

        context_entries: list[dict[str, Any]] = []
        for state_name, entry in state_quality.items():
            context_entries.append(
                {
                    "state": state_name,
                    "row_count": entry["row_count"],
                    "low_sample_warning": entry["row_count"] < min_rows_for_ranking,
                    "avg_confidence": entry.get("avg_confidence"),
                    "conflict_rate": entry.get("conflict_rate"),
                    "instability_rate": entry.get("instability_rate"),
                    "return_stats": _build_return_summary(entry),
                    "ranking_score": _state_score(entry),
                }
            )

        def _select_contexts(reverse: bool, allow_low_sample: bool) -> list[dict[str, Any]]:
            filtered = [
                entry
                for entry in context_entries
                if (allow_low_sample or entry["row_count"] >= min_rows_for_ranking)
                and entry["ranking_score"] is not None
            ]
            sorted_entries = sorted(
                filtered,
                key=lambda entry: entry["ranking_score"],
                reverse=not reverse,
            )
            return sorted_entries[:5]

        strongest = _select_contexts(reverse=False, allow_low_sample=False)
        weakest = sorted(
            _select_contexts(reverse=True, allow_low_sample=True),
            key=lambda entry: entry["ranking_score"]
            if entry["ranking_score"] is not None
            else float("inf"),
        )[:5]

        def _ingredient_review(
            data: dict[str, dict[str, Any]],
            *,
            distribution_key: str,
        ) -> list[dict[str, Any]]:
            review: list[dict[str, Any]] = []
            for name, entry in sorted(data.items(), key=lambda item: -item[1].get("row_count", 0)):
                routed_distribution = dict(sorted(entry.get(distribution_key, {}).items()))
                top_routed_state = next(iter(routed_distribution.items()), (None, 0))
                averages = [
                    stats.get("avg")
                    for stats in entry.get("return_stats", {}).values()
                    if stats.get("avg") is not None
                ]
                if averages and all(value > 0 for value in averages):
                    outcome_character = "consistently_positive"
                elif averages and all(value <= 0 for value in averages):
                    outcome_character = "weak_or_warning"
                elif averages:
                    outcome_character = "mixed"
                else:
                    outcome_character = "insufficient_returns"
                review.append(
                    {
                        "name": name,
                        "row_count": entry.get("row_count", 0),
                        "low_sample_warning": entry.get("row_count", 0) < min_rows_for_ranking,
                        "routed_state_distribution": routed_distribution,
                        "top_routed_state": {
                            "state": top_routed_state[0],
                            "rows": top_routed_state[1],
                        },
                        "outcome_character": outcome_character,
                        "return_stats": {
                            str(horizon): {
                                "count": entry["return_stats"].get(str(horizon), {}).get("count", 0),
                                "avg": entry["return_stats"].get(str(horizon), {}).get("avg"),
                                "positive_pct": entry["return_stats"].get(str(horizon), {}).get("positive_pct"),
                            }
                            for horizon in horizons
                        },
                    }
                )
            return review

        def _top_states_by_horizon(top_n: int, reverse: bool) -> dict[str, list[dict[str, Any]]]:
            results: dict[str, list[dict[str, Any]]] = {}
            for horizon in horizons:
                key = str(horizon)
                ranked = [
                    {
                        "state": entry["state"],
                        "row_count": entry["row_count"],
                        "low_sample_warning": entry["low_sample_warning"],
                        "avg_confidence": entry["avg_confidence"],
                        "conflict_rate": entry["conflict_rate"],
                        "instability_rate": entry["instability_rate"],
                        "avg_return": entry["return_stats"][key]["avg"],
                        "positive_pct": entry["return_stats"][key]["positive_pct"],
                    }
                    for entry in context_entries
                    if entry["return_stats"][key]["avg"] is not None
                ]
                ranked = sorted(
                    ranked,
                    key=lambda item: item["avg_return"],
                    reverse=reverse,
                )
                results[key] = ranked[:top_n]
            return results

        review_warnings: list[str] = []
        if int(overview["range_unclear_rows"]) > 0:
            review_warnings.append(
                f"range_unclear_share_pct={overview['range_unclear_share_pct']:.2f}"
            )
        rows_without_confidence = int(
            payload.get("confidence_coverage", {}).get("rows_without_confidence") or 0
        )
        if rows_without_confidence > 0:
            review_warnings.append(
                f"rows_without_confidence={rows_without_confidence}"
            )
        if not strongest:
            review_warnings.append("no_state_context_met_min_rows_for_ranking")

        review = {
            "overview": overview,
            "confidence_coverage": payload.get("confidence_coverage", {}),
            "strongest_state_contexts": strongest,
            "weakest_state_contexts": weakest,
            "top_positive_states": _top_states_by_horizon(top_n=3, reverse=True),
            "weakest_states": _top_states_by_horizon(top_n=3, reverse=False),
            "conflict_review": _ingredient_review(
                payload.get("conflict_flags", {}),
                distribution_key="routed_states",
            ),
            "instability_review": _ingredient_review(
                payload.get("instability_flags", {}),
                distribution_key="routed_states",
            ),
            "confidence_review": {
                "buckets": payload.get("confidence_buckets", {}),
                "coverage": payload.get("confidence_coverage", {}),
            },
            "transition_review": {
                "transition_reason": _ingredient_review(
                    payload.get("transition_reason_stats", {}),
                    distribution_key="routed_state_distribution",
                ),
                "routed_transition_reason": _ingredient_review(
                    payload.get("routed_transition_reason_stats", {}),
                    distribution_key="routed_state_distribution",
                ),
            },
            "range_unclear_diagnosis": payload.get("range_unclear_diagnosis", {}),
            "review_warnings": review_warnings,
        }
        return review

    def audit_data_coverage(
        self,
        *,
        symbols: list[str] | None = None,
    ) -> dict[str, Any]:
        clean_symbols = [symbol.upper() for symbol in (symbols or []) if str(symbol).strip()]
        table_specs = [
            {
                "table_name": self.config.raw_table,
                "role": "raw_market_data",
                "symbol_column": "symbol",
                "timestamp_candidates": ("timestamp", "ts", "minute_ts", "candle_start"),
                "relevant_fields": RAW_AUDIT_FIELDS,
            },
            {
                "table_name": self.config.market_state_live_table,
                "role": "live_state_history",
                "symbol_column": "symbol",
                "timestamp_candidates": ("ts", "timestamp", "minute_ts", "candle_start"),
                "relevant_fields": MARKET_STATE_AUDIT_FIELDS,
            },
            {
                "table_name": self.config.profiles_table,
                "role": "profiles_current",
                "symbol_column": "symbol",
                "timestamp_candidates": ("updated_at", "snapshot_time", "created_at"),
                "relevant_fields": PROFILES_CURRENT_AUDIT_FIELDS,
            },
            {
                "table_name": self.config.profiles_history_table,
                "role": "profiles_history",
                "symbol_column": "symbol",
                "timestamp_candidates": ("snapshot_time", "updated_at", "created_at"),
                "relevant_fields": PROFILES_HISTORY_AUDIT_FIELDS,
            },
        ]
        with self._connect() as connection:
            with connection.cursor() as cursor:
                discovered_tables = self._load_existing_table_names(cursor)
                table_reports = [
                    self._audit_table(
                        cursor,
                        table_name=str(spec["table_name"] or ""),
                        role=str(spec["role"]),
                        symbol_column=str(spec["symbol_column"]),
                        timestamp_candidates=tuple(spec["timestamp_candidates"]),
                        relevant_fields=tuple(spec["relevant_fields"]),
                        symbols=clean_symbols,
                    )
                    for spec in table_specs
                ]
                join_compatibility = self._audit_join_compatibility(cursor, symbols=clean_symbols)
                oi_backfill_readiness = self._audit_oi_backfill_readiness(cursor, symbols=clean_symbols)
                analysis_readiness = self._audit_analysis_readiness(cursor, symbols=clean_symbols)
        return {
            "database": self.config.database,
            "selected_symbols": clean_symbols,
            "discovered_tables": discovered_tables,
            "tables": table_reports,
            "join_compatibility": join_compatibility,
            "oi_backfill_readiness": oi_backfill_readiness,
            "analysis_readiness": analysis_readiness,
            "summary": self._build_data_audit_summary(
                table_reports=table_reports,
                join_compatibility=join_compatibility,
                oi_backfill_readiness=oi_backfill_readiness,
                analysis_readiness=analysis_readiness,
            ),
        }

    def _audit_table(
        self,
        cursor: Any,
        *,
        table_name: str,
        role: str,
        symbol_column: str,
        timestamp_candidates: Sequence[str],
        relevant_fields: Sequence[str],
        symbols: list[str],
    ) -> dict[str, Any]:
        if not table_name:
            return {
                "table_name": table_name,
                "role": role,
                "exists": False,
                "error": "missing_table_name",
            }
        if not self._table_exists(cursor, table_name):
            return {
                "table_name": table_name,
                "role": role,
                "exists": False,
                "error": "table_not_found",
            }

        columns = self._load_table_columns(cursor, table_name)
        timestamp_column = next((candidate for candidate in timestamp_candidates if candidate in columns), None)
        relevant_existing = [field for field in relevant_fields if field in columns]
        missing_expected = [field for field in relevant_fields if field not in columns]

        row_stats = self._load_table_row_stats(
            cursor,
            table_name=table_name,
            symbol_column=symbol_column if symbol_column in columns else None,
            timestamp_column=timestamp_column,
            symbols=symbols,
        )
        estimated_granularity_minutes = self._load_dominant_gap_minutes(
            cursor,
            table_name=table_name,
            symbol_column=symbol_column if symbol_column in columns else None,
            timestamp_column=timestamp_column,
            symbols=symbols,
        )
        per_symbol_coverage = self._load_per_symbol_coverage(
            cursor,
            table_name=table_name,
            symbol_column=symbol_column if symbol_column in columns else None,
            timestamp_column=timestamp_column,
            symbols=symbols,
            expected_gap_minutes=estimated_granularity_minutes,
        )
        field_quality = self._load_field_quality(
            cursor,
            table_name=table_name,
            symbols=symbols,
            symbol_column=symbol_column if symbol_column in columns else None,
            columns=columns,
            relevant_fields=relevant_existing,
        )
        duplicate_symbol_ts_rows = self._load_duplicate_key_count(
            cursor,
            table_name=table_name,
            symbol_column=symbol_column if symbol_column in columns else None,
            timestamp_column=timestamp_column,
            symbols=symbols,
        )
        return {
            "table_name": table_name,
            "role": role,
            "exists": True,
            "symbol_column": symbol_column if symbol_column in columns else None,
            "timestamp_column": timestamp_column,
            "row_count": row_stats["row_count"],
            "symbol_count": row_stats["symbol_count"],
            "first_ts": self._dt_to_iso(row_stats["first_ts"]),
            "last_ts": self._dt_to_iso(row_stats["last_ts"]),
            "estimated_granularity_minutes": estimated_granularity_minutes,
            "duplicate_symbol_ts_rows": duplicate_symbol_ts_rows,
            "unique_symbol_ts": duplicate_symbol_ts_rows == 0 if symbol_column in columns and timestamp_column else None,
            "existing_relevant_fields": relevant_existing,
            "missing_expected_fields": missing_expected,
            "field_quality": field_quality,
            "per_symbol_coverage": per_symbol_coverage,
        }

    def _audit_join_compatibility(
        self,
        cursor: Any,
        *,
        symbols: list[str],
    ) -> dict[str, Any]:
        if not self._table_exists(cursor, self.config.market_state_live_table) or not self._table_exists(cursor, self.config.raw_table):
            return {
                "ready": False,
                "error": "required_tables_missing",
            }
        state_to_raw_exact = self._load_exact_join_stats(
            cursor,
            left_table=self.config.market_state_live_table,
            left_symbol_column="symbol",
            left_timestamp_column="ts",
            right_table=self.config.raw_table,
            right_symbol_column="symbol",
            right_timestamp_column="timestamp",
            symbols=symbols,
            offset_minutes=0,
        )
        shift_candidates = []
        for offset_minutes in (-15, -5, -1, 0, 1, 5, 15):
            shift_stats = self._load_exact_join_stats(
                cursor,
                left_table=self.config.market_state_live_table,
                left_symbol_column="symbol",
                left_timestamp_column="ts",
                right_table=self.config.raw_table,
                right_symbol_column="symbol",
                right_timestamp_column="timestamp",
                symbols=symbols,
                offset_minutes=offset_minutes,
            )
            shift_candidates.append(
                {
                    "offset_minutes": offset_minutes,
                    "matched_rows": shift_stats["matched_rows"],
                    "unmatched_rows": shift_stats["unmatched_rows"],
                    "match_pct": shift_stats["match_pct"],
                    "aligned_rows": shift_stats["aligned_rows"],
                    "alignment_pct": shift_stats["alignment_pct"],
                }
            )
        best_shift = max(
            shift_candidates,
            key=lambda item: (float(item["alignment_pct"]), float(item["match_pct"]), -abs(int(item["offset_minutes"]))),
        )
        return {
            "ready": True,
            "join_pairs": [
                {
                    "left_table": self.config.market_state_live_table,
                    "right_table": self.config.raw_table,
                    "left_key": "(symbol, ts)",
                    "right_key": "(symbol, timestamp)",
                    **state_to_raw_exact,
                }
            ],
            "systematic_time_shift_check": {
                "candidate_offsets_minutes": shift_candidates,
                "best_offset_minutes": best_shift["offset_minutes"],
                "best_alignment_pct": best_shift["alignment_pct"],
                "best_match_pct": best_shift["match_pct"],
            },
            "state_rows_missing_required_raw_source": state_to_raw_exact["unmatched_rows"],
        }

    def _audit_oi_backfill_readiness(
        self,
        cursor: Any,
        *,
        symbols: list[str],
    ) -> dict[str, Any]:
        if not self._table_exists(cursor, self.config.raw_table):
            return {
                "ready": False,
                "error": "raw_table_missing",
            }
        raw_per_symbol = self._load_raw_oi_price_source_coverage(cursor, symbols=symbols)
        earliest_any = min(
            (item["first_valid_ts"] for item in raw_per_symbol if item["first_valid_ts"] is not None),
            default=None,
        )
        earliest_all_symbols = max(
            (item["first_valid_ts"] for item in raw_per_symbol if item["first_valid_ts"] is not None),
            default=None,
        )
        state_alignment = self._load_state_source_alignment_for_oi_backfill(cursor, symbols=symbols)
        symbols_ready = [
            item["symbol"]
            for item in state_alignment
            if int(item["rows_missing_source_data"]) == 0 and int(item["rows_with_source_data"]) > 0
        ]
        symbols_with_gaps = [
            item["symbol"]
            for item in state_alignment
            if int(item["rows_missing_source_data"]) > 0 or int(item["largest_gap_minutes"] or 0) > int(item["expected_gap_minutes"] or 1)
        ]
        return {
            "ready": True,
            "source_table": self.config.raw_table,
            "required_source_fields": ["price_change_1m", "oi_change"],
            "earliest_backfill_ready_ts_any_symbol": self._dt_to_iso(earliest_any),
            "earliest_backfill_ready_ts_all_symbols": self._dt_to_iso(earliest_all_symbols),
            "symbols_ready_for_backfill": symbols_ready,
            "symbols_with_gaps_or_missing_source": symbols_with_gaps,
            "raw_source_coverage_per_symbol": [
                {
                    **item,
                    "first_valid_ts": self._dt_to_iso(item["first_valid_ts"]),
                    "last_valid_ts": self._dt_to_iso(item["last_valid_ts"]),
                }
                for item in raw_per_symbol
            ],
            "market_state_alignment_per_symbol": state_alignment,
        }

    def _audit_analysis_readiness(
        self,
        cursor: Any,
        *,
        symbols: list[str],
    ) -> dict[str, Any]:
        if not self._table_exists(cursor, self.config.market_state_live_table) or not self._table_exists(cursor, self.config.raw_table):
            return {
                "ready": False,
                "error": "required_tables_missing",
            }
        per_symbol = self._load_future_return_readiness(cursor, symbols=symbols)
        total_state_rows = sum(int(item["state_rows"]) for item in per_symbol)
        total_rows_with_5m = sum(int(item["rows_with_future_5m"]) for item in per_symbol)
        total_rows_with_15m = sum(int(item["rows_with_future_15m"]) for item in per_symbol)
        return {
            "ready": True,
            "source_table": self.config.raw_table,
            "future_horizons_minutes": [5, 15],
            "total_state_rows": total_state_rows,
            "rows_with_future_5m": total_rows_with_5m,
            "rows_with_future_15m": total_rows_with_15m,
            "rows_missing_future_5m": total_state_rows - total_rows_with_5m,
            "rows_missing_future_15m": total_state_rows - total_rows_with_15m,
            "per_symbol": per_symbol,
        }

    def _build_data_audit_summary(
        self,
        *,
        table_reports: list[dict[str, Any]],
        join_compatibility: dict[str, Any],
        oi_backfill_readiness: dict[str, Any],
        analysis_readiness: dict[str, Any],
    ) -> dict[str, Any]:
        complete_tables = [
            report["table_name"]
            for report in table_reports
            if report.get("exists") and not report.get("missing_expected_fields")
        ]
        incomplete_tables = [
            {
                "table_name": report.get("table_name"),
                "missing_expected_fields": report.get("missing_expected_fields", []),
            }
            for report in table_reports
            if report.get("exists") and report.get("missing_expected_fields")
        ]
        problematic_tables = [
            {
                "table_name": report.get("table_name"),
                "duplicate_symbol_ts_rows": report.get("duplicate_symbol_ts_rows"),
                "missing_expected_fields": report.get("missing_expected_fields", []),
            }
            for report in table_reports
            if report.get("exists") and (
                int(report.get("duplicate_symbol_ts_rows") or 0) > 0
                or bool(report.get("missing_expected_fields"))
            )
        ]
        return {
            "tables_complete_enough": complete_tables,
            "tables_incomplete": incomplete_tables,
            "problematic_tables": problematic_tables,
            "state_raw_exact_join_match_pct": (
                join_compatibility.get("join_pairs", [{}])[0].get("match_pct")
                if join_compatibility.get("ready")
                else None
            ),
            "oi_backfill_reliable_from_any_symbol": oi_backfill_readiness.get("earliest_backfill_ready_ts_any_symbol"),
            "oi_backfill_reliable_from_all_symbols": oi_backfill_readiness.get("earliest_backfill_ready_ts_all_symbols"),
            "symbols_ready_for_oi_backfill": oi_backfill_readiness.get("symbols_ready_for_backfill", []),
            "symbols_with_oi_backfill_gaps": oi_backfill_readiness.get("symbols_with_gaps_or_missing_source", []),
            "analysis_rows_with_future_5m": analysis_readiness.get("rows_with_future_5m"),
            "analysis_rows_with_future_15m": analysis_readiness.get("rows_with_future_15m"),
            "analysis_rows_missing_future_5m": analysis_readiness.get("rows_missing_future_5m"),
            "analysis_rows_missing_future_15m": analysis_readiness.get("rows_missing_future_15m"),
        }

    @staticmethod
    def _signal_validation_direction(routed_state: str | None) -> str:
        normalized = _sanitize_routed_state(routed_state)
        if normalized in LONG_SIGNAL_STATES:
            return "LONG"
        if normalized in SHORT_SIGNAL_STATES:
            return "SHORT"
        return "NEUTRAL"

    @classmethod
    def _compute_directional_return_pct(
        cls,
        *,
        entry_price: float | None,
        future_price: float | None,
        routed_state: str | None,
    ) -> float | None:
        if entry_price is None or future_price is None or entry_price == 0.0:
            return None
        raw_return_pct = (float(future_price) - float(entry_price)) / float(entry_price) * 100.0
        direction = cls._signal_validation_direction(routed_state)
        if direction == "SHORT":
            return -raw_return_pct
        return raw_return_pct

    @classmethod
    def _build_signal_validation_result_row(
        cls,
        source_row: Mapping[str, Any],
        *,
        horizons: tuple[int, ...],
    ) -> dict[str, Any]:
        routed_state = _sanitize_routed_state(source_row.get("routed_state"))
        entry_price = cls._safe_optional_float(source_row.get("price_at_signal"))
        row: dict[str, Any] = {
            "signal_history_id": int(source_row.get("signal_history_id") or 0),
            "symbol": str(source_row.get("symbol") or "").upper(),
            "ts": cls._safe_datetime(source_row.get("ts")),
            "routed_state": routed_state,
            "decision": str(source_row.get("decision") or "").strip().upper() or None,
            "entry_allowed": int(bool(source_row.get("entry_allowed"))),
            "confidence": cls._safe_optional_float(source_row.get("confidence")),
            "confidence_source": str(source_row.get("confidence_source") or "").strip() or None,
            "signal_direction": cls._signal_validation_direction(routed_state),
            "price_at_signal": entry_price,
        }
        normalized_returns: dict[int, float | None] = {}
        for horizon in horizons:
            future_price = cls._safe_optional_float(source_row.get(f"price_{horizon}m"))
            row[f"price_{horizon}m"] = future_price
            normalized_returns[horizon] = cls._compute_directional_return_pct(
                entry_price=entry_price,
                future_price=future_price,
                routed_state=routed_state,
            )
            row[f"return_{horizon}m_pct"] = normalized_returns[horizon]

        evaluation_return = None
        for horizon in sorted(horizons, reverse=True):
            if normalized_returns[horizon] is not None:
                evaluation_return = normalized_returns[horizon]
                break
        row["evaluation_return_pct"] = evaluation_return

        max_price = cls._safe_optional_float(source_row.get("max_price_in_window"))
        min_price = cls._safe_optional_float(source_row.get("min_price_in_window"))
        direction = row["signal_direction"]
        if entry_price is None or entry_price == 0.0 or max_price is None or min_price is None:
            row["max_favorable_move_pct"] = None
            row["max_adverse_move_pct"] = None
        elif direction == "SHORT":
            row["max_favorable_move_pct"] = (entry_price - min_price) / entry_price * 100.0
            row["max_adverse_move_pct"] = (entry_price - max_price) / entry_price * 100.0
        else:
            row["max_favorable_move_pct"] = (max_price - entry_price) / entry_price * 100.0
            row["max_adverse_move_pct"] = (min_price - entry_price) / entry_price * 100.0
        return row

    def materialize_market_state_history_signal_validation(
        self,
        *,
        symbols: list[str] | None = None,
        limit: int | None = None,
        horizons: tuple[int, ...] = (5, 15, 30, 60),
    ) -> dict[str, Any]:
        clean_symbols = [symbol.upper() for symbol in (symbols or []) if str(symbol).strip()]
        sorted_horizons = tuple(sorted(set(int(h) for h in horizons if int(h) > 0)))
        if not sorted_horizons:
            raise ValueError("horizons must contain at least one positive minute value")

        future_joins: list[str] = []
        future_selects: list[str] = []
        params: list[Any] = []
        for horizon in sorted_horizons:
            alias = f"future_{horizon}"
            future_joins.append(
                f"""
                LEFT JOIN {self.config.raw_table} AS {alias}
                    ON {alias}.symbol = h.symbol
                   AND {alias}.timestamp = DATE_ADD(h.ts, INTERVAL %s MINUTE)
                """
            )
            future_selects.append(f"MAX({alias}.price) AS price_{horizon}m")
            params.append(horizon)

        where_clauses = [
            "results.signal_history_id IS NULL",
            "h.entry_allowed = 1",
            "h.decision IS NOT NULL",
            "h.confidence IS NOT NULL",
        ]
        if clean_symbols:
            placeholders = ", ".join(["%s"] * len(clean_symbols))
            where_clauses.append(f"h.symbol IN ({placeholders})")
            params.extend(clean_symbols)

        limit_clause = ""
        if limit is not None and int(limit) > 0:
            limit_clause = "LIMIT %s"

        query = f"""
            SELECT
                h.id AS signal_history_id,
                h.symbol,
                h.ts,
                h.routed_state,
                h.decision,
                h.entry_allowed,
                h.confidence,
                h.confidence_source,
                entry_price.price AS price_at_signal,
                {", ".join(future_selects)},
                MAX(window_prices.price) AS max_price_in_window,
                MIN(window_prices.price) AS min_price_in_window
            FROM {self.config.market_state_history_table} AS h
            INNER JOIN {self.config.raw_table} AS entry_price
                ON entry_price.symbol = h.symbol
               AND entry_price.timestamp = h.ts
            {" ".join(future_joins)}
            LEFT JOIN {self.config.raw_table} AS window_prices
                ON window_prices.symbol = h.symbol
               AND window_prices.timestamp BETWEEN h.ts AND DATE_ADD(h.ts, INTERVAL {max(sorted_horizons)} MINUTE)
            LEFT JOIN {self.config.signal_validation_results_table} AS results
                ON results.signal_history_id = h.id
            WHERE {" AND ".join(where_clauses)}
            GROUP BY
                h.id,
                h.symbol,
                h.ts,
                h.routed_state,
                h.decision,
                h.entry_allowed,
                h.confidence,
                h.confidence_source,
                entry_price.price
            ORDER BY h.ts ASC, h.symbol ASC
            {limit_clause}
        """
        if limit is not None and int(limit) > 0:
            params.append(int(limit))

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                source_rows = cursor.fetchall() or []

        result_rows = [
            self._build_signal_validation_result_row(row, horizons=sorted_horizons)
            for row in source_rows
        ]
        if result_rows:
            columns = list(result_rows[0].keys())
            insert_query = self._build_upsert_query(
                self.config.signal_validation_results_table,
                columns,
                conflict_skip_columns={"processed_at"},
            )
            values = [[row.get(column) for column in columns] for row in result_rows]
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.executemany(insert_query, values)
                    inserted_rows = int(getattr(cursor, "rowcount", len(result_rows)) or 0)
        else:
            inserted_rows = 0

        summary = self.refresh_signal_validation_summary(symbols=clean_symbols or None)
        return {
            "selected_symbols": clean_symbols,
            "limit": int(limit) if limit is not None and int(limit) > 0 else None,
            "horizons": sorted_horizons,
            "candidate_rows": len(source_rows),
            "inserted_rows": inserted_rows,
            "summary_rows": summary["summary_rows"],
        }

    def refresh_signal_validation_summary(
        self,
        *,
        symbols: list[str] | None = None,
    ) -> dict[str, int]:
        clean_symbols = [symbol.upper() for symbol in (symbols or []) if str(symbol).strip()]
        where_clause = ""
        params: list[Any] = []
        if clean_symbols:
            placeholders = ", ".join(["%s"] * len(clean_symbols))
            where_clause = f"WHERE symbol IN ({placeholders})"
            params.extend(clean_symbols)

        delete_query = f"DELETE FROM {self.config.signal_validation_summary_table} {where_clause}"
        insert_query = f"""
            INSERT INTO {self.config.signal_validation_summary_table} (
                routed_state,
                decision,
                symbol,
                signal_count,
                hit_rate_pct,
                avg_return_5m_pct,
                avg_return_15m_pct,
                avg_return_30m_pct,
                avg_return_60m_pct,
                avg_mfe_pct,
                avg_mae_pct,
                expectancy_pct,
                updated_at
            )
            SELECT
                routed_state,
                decision,
                symbol,
                COUNT(*) AS signal_count,
                (
                    SUM(CASE WHEN evaluation_return_pct > 0 THEN 1 ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN evaluation_return_pct IS NOT NULL THEN 1 ELSE 0 END), 0)
                    * 100.0
                ) AS hit_rate_pct,
                AVG(return_5m_pct) AS avg_return_5m_pct,
                AVG(return_15m_pct) AS avg_return_15m_pct,
                AVG(return_30m_pct) AS avg_return_30m_pct,
                AVG(return_60m_pct) AS avg_return_60m_pct,
                AVG(max_favorable_move_pct) AS avg_mfe_pct,
                AVG(max_adverse_move_pct) AS avg_mae_pct,
                AVG(evaluation_return_pct) AS expectancy_pct,
                CURRENT_TIMESTAMP
            FROM {self.config.signal_validation_results_table}
            {where_clause}
            GROUP BY routed_state, decision, symbol
        """
        count_query = f"""
            SELECT COUNT(*) AS summary_rows
            FROM {self.config.signal_validation_summary_table}
            {where_clause}
        """
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(delete_query, params)
                cursor.execute(insert_query, params)
                cursor.execute(count_query, params)
                row = cursor.fetchone() or {}
        return {"summary_rows": int(row.get("summary_rows") or 0)}

    def load_signal_validation_summary_rows(
        self,
        *,
        symbols: list[str] | None = None,
        min_count: int = 1,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clean_symbols = [symbol.upper() for symbol in (symbols or []) if str(symbol).strip()]
        where_clauses = ["signal_count >= %s"]
        params: list[Any] = [max(int(min_count), 1)]
        if clean_symbols:
            placeholders = ", ".join(["%s"] * len(clean_symbols))
            where_clauses.append(f"symbol IN ({placeholders})")
            params.extend(clean_symbols)
        limit_clause = ""
        if limit is not None and int(limit) > 0:
            limit_clause = "LIMIT %s"
            params.append(int(limit))
        query = f"""
            SELECT
                routed_state,
                decision,
                symbol,
                signal_count,
                hit_rate_pct,
                avg_return_5m_pct,
                avg_return_15m_pct,
                avg_return_30m_pct,
                avg_return_60m_pct,
                avg_mfe_pct,
                avg_mae_pct,
                expectancy_pct,
                updated_at
            FROM {self.config.signal_validation_summary_table}
            WHERE {" AND ".join(where_clauses)}
            ORDER BY expectancy_pct DESC, signal_count DESC, symbol ASC
            {limit_clause}
        """
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall() or []

    def _load_existing_table_names(self, cursor: Any) -> list[str]:
        cursor.execute("SHOW TABLES")
        rows = cursor.fetchall() or []
        names: list[str] = []
        for row in rows:
            if isinstance(row, Mapping):
                for value in row.values():
                    if value is not None:
                        names.append(str(value))
                        break
            elif row is not None:
                names.append(str(row))
        return sorted(names)

    def _table_exists(self, cursor: Any, table_name: str) -> bool:
        return table_name in self._load_existing_table_names(cursor)

    def _load_table_columns(self, cursor: Any, table_name: str) -> dict[str, dict[str, Any]]:
        cursor.execute(f"SHOW COLUMNS FROM {table_name}")
        rows = cursor.fetchall() or []
        columns: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            name = str(row.get("Field") or row.get("field") or "")
            if not name:
                continue
            columns[name] = dict(row)
        return columns

    def _load_table_row_stats(
        self,
        cursor: Any,
        *,
        table_name: str,
        symbol_column: str | None,
        timestamp_column: str | None,
        symbols: list[str],
    ) -> dict[str, Any]:
        where_clause, params = self._build_symbol_filter(symbol_column=symbol_column, symbols=symbols)
        timestamp_select = "NULL AS first_ts, NULL AS last_ts"
        if timestamp_column:
            timestamp_select = (
                f"MIN({timestamp_column}) AS first_ts, "
                f"MAX({timestamp_column}) AS last_ts"
            )
        symbol_count_select = "NULL AS symbol_count"
        if symbol_column:
            symbol_count_select = f"COUNT(DISTINCT {symbol_column}) AS symbol_count"
        query = f"""
            SELECT
                COUNT(*) AS row_count,
                {symbol_count_select},
                {timestamp_select}
            FROM {table_name}
            {where_clause}
        """
        cursor.execute(query, params)
        row = cursor.fetchone() or {}
        return {
            "row_count": int(row.get("row_count") or 0),
            "symbol_count": None if row.get("symbol_count") is None else int(row.get("symbol_count") or 0),
            "first_ts": self._safe_datetime(row.get("first_ts")),
            "last_ts": self._safe_datetime(row.get("last_ts")),
        }

    def _load_field_quality(
        self,
        cursor: Any,
        *,
        table_name: str,
        symbols: list[str],
        symbol_column: str | None,
        columns: Mapping[str, Mapping[str, Any]],
        relevant_fields: Sequence[str],
    ) -> list[dict[str, Any]]:
        if not relevant_fields:
            return []
        where_clause, params = self._build_symbol_filter(symbol_column=symbol_column, symbols=symbols)
        expressions: list[str] = []
        for field in relevant_fields:
            column_type = str(columns[field].get("Type") or "").lower()
            if any(token in column_type for token in ("double", "float", "decimal")):
                expressions.append(
                    f"SUM(CASE WHEN {field} IS NULL OR {field} != {field} THEN 1 ELSE 0 END) AS {field}__missing"
                )
            else:
                expressions.append(f"SUM(CASE WHEN {field} IS NULL THEN 1 ELSE 0 END) AS {field}__missing")
        query = f"""
            SELECT
                COUNT(*) AS row_count,
                {", ".join(expressions)}
            FROM {table_name}
            {where_clause}
        """
        cursor.execute(query, params)
        row = cursor.fetchone() or {}
        row_count = int(row.get("row_count") or 0)
        quality_rows: list[dict[str, Any]] = []
        for field in relevant_fields:
            missing_count = int(row.get(f"{field}__missing") or 0)
            quality_rows.append(
                {
                    "field": field,
                    "missing_count": missing_count,
                    "missing_pct": (missing_count / row_count * 100.0) if row_count else 0.0,
                }
            )
        return quality_rows

    def _load_duplicate_key_count(
        self,
        cursor: Any,
        *,
        table_name: str,
        symbol_column: str | None,
        timestamp_column: str | None,
        symbols: list[str],
    ) -> int:
        if not symbol_column or not timestamp_column:
            return 0
        where_clause, params = self._build_symbol_filter(symbol_column=symbol_column, symbols=symbols)
        query = f"""
            SELECT COALESCE(SUM(duplicate_rows), 0) AS duplicate_symbol_ts_rows
            FROM (
                SELECT
                    COUNT(*) - 1 AS duplicate_rows
                FROM {table_name}
                {where_clause}
                GROUP BY {symbol_column}, {timestamp_column}
                HAVING COUNT(*) > 1
            ) AS duplicates
        """
        cursor.execute(query, params)
        row = cursor.fetchone() or {}
        return int(row.get("duplicate_symbol_ts_rows") or 0)

    def _load_dominant_gap_minutes(
        self,
        cursor: Any,
        *,
        table_name: str,
        symbol_column: str | None,
        timestamp_column: str | None,
        symbols: list[str],
    ) -> int | None:
        if not symbol_column or not timestamp_column:
            return None
        where_clause, params = self._build_symbol_filter(symbol_column=symbol_column, symbols=symbols)
        query = f"""
            SELECT gap_minutes, COUNT(*) AS occurrence_count
            FROM (
                SELECT
                    TIMESTAMPDIFF(
                        MINUTE,
                        LAG({timestamp_column}) OVER (PARTITION BY {symbol_column} ORDER BY {timestamp_column}),
                        {timestamp_column}
                    ) AS gap_minutes
                FROM {table_name}
                {where_clause}
            ) AS gaps
            WHERE gap_minutes IS NOT NULL AND gap_minutes > 0
            GROUP BY gap_minutes
            ORDER BY occurrence_count DESC, gap_minutes ASC
            LIMIT 1
        """
        cursor.execute(query, params)
        row = cursor.fetchone() or {}
        return None if row.get("gap_minutes") is None else int(row.get("gap_minutes") or 0)

    def _load_per_symbol_coverage(
        self,
        cursor: Any,
        *,
        table_name: str,
        symbol_column: str | None,
        timestamp_column: str | None,
        symbols: list[str],
        expected_gap_minutes: int | None,
    ) -> list[dict[str, Any]]:
        if not symbol_column:
            return []
        where_clause, params = self._build_symbol_filter(symbol_column=symbol_column, symbols=symbols)
        timestamp_select = "NULL AS first_ts, NULL AS last_ts"
        if timestamp_column:
            timestamp_select = f"MIN({timestamp_column}) AS first_ts, MAX({timestamp_column}) AS last_ts"
        duplicate_select = "0 AS duplicate_symbol_ts_rows"
        if timestamp_column:
            duplicate_select = f"COUNT(*) - COUNT(DISTINCT {timestamp_column}) AS duplicate_symbol_ts_rows"
        base_query = f"""
            SELECT
                {symbol_column} AS symbol,
                COUNT(*) AS row_count,
                {timestamp_select},
                {duplicate_select}
            FROM {table_name}
            {where_clause}
            GROUP BY {symbol_column}
            ORDER BY {symbol_column}
        """
        cursor.execute(base_query, params)
        base_rows = cursor.fetchall() or []
        gap_rows_by_symbol: dict[str, dict[str, Any]] = {}
        if timestamp_column and expected_gap_minutes:
            gap_query = f"""
                SELECT
                    symbol,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN gap_minutes > %s THEN GREATEST(CEIL(gap_minutes / %s) - 1, 0)
                                ELSE 0
                            END
                        ),
                        0
                    ) AS missing_intervals,
                    MAX(gap_minutes) AS largest_gap_minutes
                FROM (
                    SELECT
                        {symbol_column} AS symbol,
                        TIMESTAMPDIFF(
                            MINUTE,
                            LAG({timestamp_column}) OVER (PARTITION BY {symbol_column} ORDER BY {timestamp_column}),
                            {timestamp_column}
                        ) AS gap_minutes
                    FROM {table_name}
                    {where_clause}
                ) AS symbol_gaps
                WHERE gap_minutes IS NOT NULL
                GROUP BY symbol
            """
            cursor.execute(gap_query, [int(expected_gap_minutes), int(expected_gap_minutes), *params])
            gap_rows = cursor.fetchall() or []
            gap_rows_by_symbol = {
                str(row.get("symbol") or "").upper(): dict(row)
                for row in gap_rows
                if row.get("symbol") is not None
            }
        coverage_rows: list[dict[str, Any]] = []
        for row in base_rows:
            symbol = str(row.get("symbol") or "").upper()
            gap_row = gap_rows_by_symbol.get(symbol, {})
            coverage_rows.append(
                {
                    "symbol": symbol,
                    "row_count": int(row.get("row_count") or 0),
                    "first_ts": self._dt_to_iso(self._safe_datetime(row.get("first_ts"))),
                    "last_ts": self._dt_to_iso(self._safe_datetime(row.get("last_ts"))),
                    "expected_gap_minutes": expected_gap_minutes,
                    "missing_intervals": int(gap_row.get("missing_intervals") or 0),
                    "largest_gap_minutes": None
                    if gap_row.get("largest_gap_minutes") is None
                    else int(gap_row.get("largest_gap_minutes") or 0),
                    "duplicate_symbol_ts_rows": int(row.get("duplicate_symbol_ts_rows") or 0),
                }
            )
        return coverage_rows

    def _load_exact_join_stats(
        self,
        cursor: Any,
        *,
        left_table: str,
        left_symbol_column: str,
        left_timestamp_column: str,
        right_table: str,
        right_symbol_column: str,
        right_timestamp_column: str,
        symbols: list[str],
        offset_minutes: int,
    ) -> dict[str, Any]:
        where_clause, params = self._build_symbol_filter(symbol_column=f"l.{left_symbol_column}", symbols=symbols)
        query = f"""
            SELECT
                COUNT(*) AS total_left_rows,
                SUM(
                    CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM {right_table} AS r
                            WHERE r.{right_symbol_column} = l.{left_symbol_column}
                              AND r.{right_timestamp_column} = TIMESTAMPADD(MINUTE, %s, l.{left_timestamp_column})
                        )
                        THEN 1
                        ELSE 0
                    END
                ) AS matched_rows,
                SUM(
                    CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM {right_table} AS r
                            WHERE r.{right_symbol_column} = l.{left_symbol_column}
                              AND r.{right_timestamp_column} = TIMESTAMPADD(MINUTE, %s, l.{left_timestamp_column})
                              AND ABS(COALESCE(r.price, 0) - COALESCE(l.price, 0)) <= 1e-12
                        )
                        THEN 1
                        ELSE 0
                    END
                ) AS aligned_rows
            FROM {left_table} AS l
            {where_clause}
        """
        cursor.execute(query, [int(offset_minutes), int(offset_minutes), *params])
        row = cursor.fetchone() or {}
        total_left_rows = int(row.get("total_left_rows") or 0)
        matched_rows = int(row.get("matched_rows") or 0)
        aligned_rows = int(row.get("aligned_rows") or 0)
        unmatched_rows = max(total_left_rows - matched_rows, 0)
        return {
            "total_left_rows": total_left_rows,
            "matched_rows": matched_rows,
            "unmatched_rows": unmatched_rows,
            "match_pct": (matched_rows / total_left_rows * 100.0) if total_left_rows else 0.0,
            "aligned_rows": aligned_rows,
            "alignment_pct": (aligned_rows / total_left_rows * 100.0) if total_left_rows else 0.0,
        }

    def _load_raw_oi_price_source_coverage(
        self,
        cursor: Any,
        *,
        symbols: list[str],
    ) -> list[dict[str, Any]]:
        where_clause, params = self._build_symbol_filter(symbol_column="symbol", symbols=symbols)
        query = f"""
            SELECT
                symbol,
                COUNT(*) AS total_rows,
                SUM(CASE WHEN price_change_1m IS NOT NULL AND oi_change IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_oi_price_source,
                MIN(CASE WHEN price_change_1m IS NOT NULL AND oi_change IS NOT NULL THEN timestamp END) AS first_valid_ts,
                MAX(CASE WHEN price_change_1m IS NOT NULL AND oi_change IS NOT NULL THEN timestamp END) AS last_valid_ts
            FROM {self.config.raw_table}
            {where_clause}
            GROUP BY symbol
            ORDER BY symbol
        """
        cursor.execute(query, params)
        rows = cursor.fetchall() or []
        gap_lookup = {
            item["symbol"]: item
            for item in self._load_per_symbol_coverage(
                cursor,
                table_name=self.config.raw_table,
                symbol_column="symbol",
                timestamp_column="timestamp",
                symbols=symbols,
                expected_gap_minutes=self._load_dominant_gap_minutes(
                    cursor,
                    table_name=self.config.raw_table,
                    symbol_column="symbol",
                    timestamp_column="timestamp",
                    symbols=symbols,
                ),
            )
        }
        result: list[dict[str, Any]] = []
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            gap_row = gap_lookup.get(symbol, {})
            result.append(
                {
                    "symbol": symbol,
                    "total_rows": int(row.get("total_rows") or 0),
                    "rows_with_oi_price_source": int(row.get("rows_with_oi_price_source") or 0),
                    "rows_missing_oi_price_source": max(
                        int(row.get("total_rows") or 0) - int(row.get("rows_with_oi_price_source") or 0),
                        0,
                    ),
                    "first_valid_ts": self._safe_datetime(row.get("first_valid_ts")),
                    "last_valid_ts": self._safe_datetime(row.get("last_valid_ts")),
                    "largest_gap_minutes": gap_row.get("largest_gap_minutes"),
                    "missing_intervals": int(gap_row.get("missing_intervals") or 0),
                }
            )
        return result

    def _load_state_source_alignment_for_oi_backfill(
        self,
        cursor: Any,
        *,
        symbols: list[str],
    ) -> list[dict[str, Any]]:
        expected_gap_minutes = self._load_dominant_gap_minutes(
            cursor,
            table_name=self.config.raw_table,
            symbol_column="symbol",
            timestamp_column="timestamp",
            symbols=symbols,
        )
        where_clause, params = self._build_symbol_filter(symbol_column="m.symbol", symbols=symbols)
        query = f"""
            SELECT
                m.symbol AS symbol,
                COUNT(*) AS state_rows,
                SUM(
                    CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM {self.config.raw_table} AS r
                            WHERE r.symbol = m.symbol
                              AND r.timestamp = m.ts
                              AND r.price_change_1m IS NOT NULL
                              AND r.oi_change IS NOT NULL
                        ) THEN 1 ELSE 0
                    END
                ) AS rows_with_source_data
            FROM {self.config.market_state_live_table} AS m
            {where_clause}
            GROUP BY m.symbol
            ORDER BY m.symbol
        """
        cursor.execute(query, params)
        rows = cursor.fetchall() or []
        raw_gap_lookup = {
            item["symbol"]: item
            for item in self._load_per_symbol_coverage(
                cursor,
                table_name=self.config.raw_table,
                symbol_column="symbol",
                timestamp_column="timestamp",
                symbols=symbols,
                expected_gap_minutes=expected_gap_minutes,
            )
        }
        result: list[dict[str, Any]] = []
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            state_rows = int(row.get("state_rows") or 0)
            rows_with_source_data = int(row.get("rows_with_source_data") or 0)
            raw_gap = raw_gap_lookup.get(symbol, {})
            result.append(
                {
                    "symbol": symbol,
                    "state_rows": state_rows,
                    "rows_with_source_data": rows_with_source_data,
                    "rows_missing_source_data": max(state_rows - rows_with_source_data, 0),
                    "expected_gap_minutes": expected_gap_minutes,
                    "largest_gap_minutes": raw_gap.get("largest_gap_minutes"),
                    "missing_intervals": int(raw_gap.get("missing_intervals") or 0),
                }
            )
        return result

    def _load_future_return_readiness(
        self,
        cursor: Any,
        *,
        symbols: list[str],
    ) -> list[dict[str, Any]]:
        where_clause, params = self._build_symbol_filter(symbol_column="m.symbol", symbols=symbols)
        query = f"""
            SELECT
                m.symbol AS symbol,
                COUNT(*) AS state_rows,
                SUM(
                    CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM {self.config.raw_table} AS r5
                            WHERE r5.symbol = m.symbol
                              AND r5.timestamp = TIMESTAMPADD(MINUTE, 5, m.ts)
                              AND r5.price_change_5m IS NOT NULL
                        ) THEN 1 ELSE 0
                    END
                ) AS rows_with_future_5m,
                SUM(
                    CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM {self.config.raw_table} AS r15
                            WHERE r15.symbol = m.symbol
                              AND r15.timestamp = TIMESTAMPADD(MINUTE, 15, m.ts)
                              AND r15.price_change_15m IS NOT NULL
                        ) THEN 1 ELSE 0
                    END
                ) AS rows_with_future_15m
            FROM {self.config.market_state_live_table} AS m
            {where_clause}
            GROUP BY m.symbol
            ORDER BY m.symbol
        """
        cursor.execute(query, params)
        rows = cursor.fetchall() or []
        return [
            {
                "symbol": str(row.get("symbol") or "").upper(),
                "state_rows": int(row.get("state_rows") or 0),
                "rows_with_future_5m": int(row.get("rows_with_future_5m") or 0),
                "rows_missing_future_5m": max(
                    int(row.get("state_rows") or 0) - int(row.get("rows_with_future_5m") or 0),
                    0,
                ),
                "rows_with_future_15m": int(row.get("rows_with_future_15m") or 0),
                "rows_missing_future_15m": max(
                    int(row.get("state_rows") or 0) - int(row.get("rows_with_future_15m") or 0),
                    0,
                ),
            }
            for row in rows
        ]

    @staticmethod
    def _build_symbol_filter(*, symbol_column: str | None, symbols: list[str]) -> tuple[str, list[Any]]:
        if not symbol_column or not symbols:
            return "", []
        placeholders = ", ".join(["%s"] * len(symbols))
        return f"WHERE {symbol_column} IN ({placeholders})", list(symbols)

    @staticmethod
    def _dt_to_iso(value: datetime | None) -> str | None:
        return value.isoformat() if isinstance(value, datetime) else None

    def backfill_market_state_live_oi_price_state(
        self,
        dry_run: bool = True,
        *,
        batch_size: int = 1000,
    ) -> dict[str, Any]:
        total_rows_scanned = 0
        rows_with_source_data = 0
        rows_missing_source_data = 0
        rows_needing_update = 0
        rows_already_correct = 0
        rows_updated = 0
        target_state_counts: dict[str, int] = defaultdict(int)
        written_state_counts: dict[str, int] = defaultdict(int)

        select_query = f"""
            SELECT
                m.id,
                m.oi_price_state,
                m.oi_price_build_long,
                m.oi_price_short_covering,
                m.oi_price_build_short,
                m.oi_price_long_unwinding,
                r.price_change_1m AS source_price_change_1m,
                r.oi_change AS source_oi_change
            FROM {self.config.market_state_live_table} AS m
            LEFT JOIN {self.config.raw_table} AS r
                ON r.symbol = m.symbol
               AND r.timestamp = m.ts
            WHERE m.id > %s
            ORDER BY m.id ASC
            LIMIT %s
        """
        update_query = f"""
            UPDATE {self.config.market_state_live_table}
            SET
                oi_price_state = %s,
                oi_price_build_long = %s,
                oi_price_short_covering = %s,
                oi_price_build_short = %s,
                oi_price_long_unwinding = %s
            WHERE id = %s
        """

        last_id = 0
        with self._connect() as connection:
            with connection.cursor() as cursor:
                while True:
                    cursor.execute(select_query, [last_id, int(batch_size)])
                    rows = cursor.fetchall() or []
                    if not rows:
                        break

                    updates: list[list[Any]] = []
                    for row in rows:
                        row_id = int(row.get("id") or 0)
                        last_id = max(last_id, row_id)
                        total_rows_scanned += 1

                        source_price_change_1m = row.get("source_price_change_1m")
                        source_oi_change = row.get("source_oi_change")
                        has_source_data = source_price_change_1m is not None and source_oi_change is not None
                        if has_source_data:
                            rows_with_source_data += 1
                        else:
                            rows_missing_source_data += 1

                        target_state = compute_oi_price_state(
                            price_change_1m=source_price_change_1m,
                            oi_change=source_oi_change,
                        )
                        target_state_counts[target_state] += 1
                        target_flags = self._derive_oi_price_flags(target_state)

                        current_state_raw = row.get("oi_price_state")
                        current_state = str(current_state_raw or "neutral")
                        current_flags = {
                            "oi_price_build_long": int(row.get("oi_price_build_long") or 0),
                            "oi_price_short_covering": int(row.get("oi_price_short_covering") or 0),
                            "oi_price_build_short": int(row.get("oi_price_build_short") or 0),
                            "oi_price_long_unwinding": int(row.get("oi_price_long_unwinding") or 0),
                        }

                        is_correct = (
                            current_state_raw is not None
                            and current_state == target_state
                            and current_flags == target_flags
                        )
                        if is_correct:
                            rows_already_correct += 1
                            continue

                        rows_needing_update += 1
                        if dry_run:
                            continue

                        updates.append(
                            [
                                target_state,
                                target_flags["oi_price_build_long"],
                                target_flags["oi_price_short_covering"],
                                target_flags["oi_price_build_short"],
                                target_flags["oi_price_long_unwinding"],
                                row_id,
                            ]
                        )
                        rows_updated += 1
                        written_state_counts[target_state] += 1

                    if updates:
                        cursor.executemany(update_query, updates)

        return {
            "dry_run": bool(dry_run),
            "total_rows_scanned": total_rows_scanned,
            "rows_with_source_data": rows_with_source_data,
            "rows_missing_source_data": rows_missing_source_data,
            "rows_needing_update": rows_needing_update,
            "rows_already_correct": rows_already_correct,
            "rows_updated": rows_updated,
            "target_state_counts": dict(sorted(target_state_counts.items())),
            "written_state_counts": dict(sorted(written_state_counts.items())),
        }

    def backfill_market_state_live_oi_price_flags(self, *, dry_run: bool = True) -> dict[str, Any]:
        summary_query = f"""
            SELECT
                COUNT(*) AS total_rows_scanned,
                SUM(CASE WHEN oi_price_state = 'price_up_oi_up' THEN 1 ELSE 0 END) AS count_price_up_oi_up,
                SUM(CASE WHEN oi_price_state = 'price_up_oi_down' THEN 1 ELSE 0 END) AS count_price_up_oi_down,
                SUM(CASE WHEN oi_price_state = 'price_down_oi_up' THEN 1 ELSE 0 END) AS count_price_down_oi_up,
                SUM(CASE WHEN oi_price_state = 'price_down_oi_down' THEN 1 ELSE 0 END) AS count_price_down_oi_down,
                SUM(
                    CASE
                        WHEN COALESCE(oi_price_build_long, 0) <> CASE WHEN oi_price_state = 'price_up_oi_up' THEN 1 ELSE 0 END
                          OR COALESCE(oi_price_short_covering, 0) <> CASE WHEN oi_price_state = 'price_up_oi_down' THEN 1 ELSE 0 END
                          OR COALESCE(oi_price_build_short, 0) <> CASE WHEN oi_price_state = 'price_down_oi_up' THEN 1 ELSE 0 END
                          OR COALESCE(oi_price_long_unwinding, 0) <> CASE WHEN oi_price_state = 'price_down_oi_down' THEN 1 ELSE 0 END
                        THEN 1
                        ELSE 0
                    END
                ) AS rows_needing_update
            FROM {self.config.market_state_live_table}
        """
        update_query = f"""
            UPDATE {self.config.market_state_live_table}
            SET
                oi_price_build_long = CASE WHEN oi_price_state = 'price_up_oi_up' THEN 1 ELSE 0 END,
                oi_price_short_covering = CASE WHEN oi_price_state = 'price_up_oi_down' THEN 1 ELSE 0 END,
                oi_price_build_short = CASE WHEN oi_price_state = 'price_down_oi_up' THEN 1 ELSE 0 END,
                oi_price_long_unwinding = CASE WHEN oi_price_state = 'price_down_oi_down' THEN 1 ELSE 0 END
            WHERE
                COALESCE(oi_price_build_long, 0) <> CASE WHEN oi_price_state = 'price_up_oi_up' THEN 1 ELSE 0 END
                OR COALESCE(oi_price_short_covering, 0) <> CASE WHEN oi_price_state = 'price_up_oi_down' THEN 1 ELSE 0 END
                OR COALESCE(oi_price_build_short, 0) <> CASE WHEN oi_price_state = 'price_down_oi_up' THEN 1 ELSE 0 END
                OR COALESCE(oi_price_long_unwinding, 0) <> CASE WHEN oi_price_state = 'price_down_oi_down' THEN 1 ELSE 0 END
        """

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(summary_query)
                before = cursor.fetchone() or {}
                rows_needing_update = int(before.get("rows_needing_update") or 0)
                rows_updated = 0
                if not dry_run and rows_needing_update > 0:
                    cursor.execute(update_query)
                    rows_updated = rows_needing_update
                cursor.execute(summary_query)
                after = cursor.fetchone() or {}

        return {
            "dry_run": bool(dry_run),
            "total_rows_scanned": int(before.get("total_rows_scanned") or 0),
            "rows_needing_update": rows_needing_update,
            "rows_updated": rows_updated,
            "quadrant_counts": {
                "price_up_oi_up": int(after.get("count_price_up_oi_up") or 0),
                "price_up_oi_down": int(after.get("count_price_up_oi_down") or 0),
                "price_down_oi_up": int(after.get("count_price_down_oi_up") or 0),
                "price_down_oi_down": int(after.get("count_price_down_oi_down") or 0),
            },
        }

    def upsert_coin_profiles(self, profiles: Sequence[CoinProfile]) -> None:
        rows = [self._build_coin_profile_row(profile) for profile in profiles if profile.symbol]
        if not rows:
            return
        columns = list(rows[0].keys())
        placeholders = ", ".join(["%s"] * len(columns))
        update_clause = ", ".join(
            f"{column}=VALUES({column})" for column in columns if column != "symbol"
        )
        query = (
            f"INSERT INTO {self.config.profiles_table} "
            f"({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {update_clause}"
        )
        values = [[row.get(column) for column in columns] for row in rows]
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(query, values)

    def insert_coin_profile_history(self, profiles: Sequence[CoinProfile], snapshot_time: datetime | None = None) -> None:
        if not profiles:
            return
        rows = []
        for profile in profiles:
            rows.append(
                {
                    "symbol": profile.symbol.upper(),
                    "snapshot_time": snapshot_time or profile.updated_at or datetime.utcnow(),
                    "sample_size": profile.sample_size,
                    "profile_version": profile.profile_version,
                    "window_start": profile.window_start,
                    "window_end": profile.window_end,
                    "profile_json": self._json_dumps(self._build_coin_profile_row(profile)),
                }
            )
        columns = list(rows[0].keys())
        placeholders = ", ".join(["%s"] * len(columns))
        query = (
            f"INSERT INTO {self.config.profiles_history_table} "
            f"({', '.join(columns)}) VALUES ({placeholders})"
        )
        values = [[row.get(column) for column in columns] for row in rows]
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(query, values)

    def insert_market_state_live(
        self,
        *,
        symbol: str,
        ts: datetime,
        raw_snapshot: RawMarketSnapshot,
        normalized_snapshot: NormalizedSnapshot,
        events: PrimitiveEvents,
        scores: ScoreSnapshot,
        slow_regime: SlowRegimeSnapshot,
        mid_regime: MidRegimeSnapshot,
        fast_trigger: FastTriggerSnapshot,
        routed_regime: RoutedRegimeSnapshot,
        regime: RegimeSnapshot,
        state_machine: StateMachineSnapshot,
        decision: DecisionPolicyResult,
        engine_version: int = 1,
    ) -> None:
        row = self._build_market_state_live_row(
            symbol=symbol,
            ts=ts,
            raw_snapshot=raw_snapshot,
            normalized_snapshot=normalized_snapshot,
            events=events,
            scores=scores,
            slow_regime=slow_regime,
            mid_regime=mid_regime,
            fast_trigger=fast_trigger,
            routed_regime=routed_regime,
            regime=regime,
            state_machine=state_machine,
            decision=decision,
            engine_version=engine_version,
        )
        columns = list(row.keys())
        query = self._build_upsert_query(
            self.config.market_state_live_table,
            columns,
            conflict_skip_columns={"created_at"},
        )
        values = [row[column] for column in columns]
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, values)

    def insert_market_state_live_batch(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        columns = list(rows[0].keys())
        query = self._build_upsert_query(
            self.config.market_state_live_table,
            columns,
            conflict_skip_columns={"created_at"},
        )
        values = [[row.get(column) for column in columns] for row in rows]
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(query, values)

    @staticmethod
    def _build_upsert_query(
        table_name: str,
        columns: Sequence[str],
        *,
        conflict_skip_columns: set[str] | None = None,
    ) -> str:
        placeholders = ", ".join(["%s"] * len(columns))
        skip_columns = set(conflict_skip_columns or set())
        update_columns = [column for column in columns if column not in skip_columns]
        update_clause = ", ".join(f"{column}=VALUES({column})" for column in update_columns)
        return (
            f"INSERT INTO {table_name} "
            f"({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {update_clause}"
        )

    def _row_to_coin_profile(self, row: Mapping[str, Any]) -> CoinProfile:
        symbol = str(row.get("symbol") or "").upper()
        features: dict[str, FeatureProfileStats] = {}
        for feature_name in PROFILED_FEATURES:
            features[feature_name] = FeatureProfileStats(
                mean=self._safe_float(row.get(f"{feature_name}_mean")),
                std=self._safe_float(row.get(f"{feature_name}_std")),
                p50=self._safe_float(row.get(f"{feature_name}_p50")),
                p75=self._safe_float(row.get(f"{feature_name}_p75")),
                p90=self._safe_float(row.get(f"{feature_name}_p90")),
                p95=self._safe_float(row.get(f"{feature_name}_p95")),
                p99=self._safe_float(row.get(f"{feature_name}_p99")),
                n95=self._safe_optional_float(row.get(f"{feature_name}_n95")),
                n99=self._safe_optional_float(row.get(f"{feature_name}_n99")),
                median=self._safe_optional_float(row.get(f"{feature_name}_median")),
                mad=self._safe_optional_float(row.get(f"{feature_name}_mad")),
            )

        return CoinProfile(
            symbol=symbol,
            updated_at=self._safe_datetime(row.get("updated_at")),
            sample_size=int(row.get("sample_size") or 0),
            profile_version=int(row.get("profile_version") or 1),
            window_start=self._safe_datetime(row.get("window_start")),
            window_end=self._safe_datetime(row.get("window_end")),
            features=features,
            threshold_orderflow_long=self._safe_float(row.get("threshold_orderflow_long"), 0.15),
            threshold_orderflow_short=self._safe_float(row.get("threshold_orderflow_short"), -0.15),
            threshold_buy_sell_imbalance_long=self._safe_float(
                row.get("threshold_buy_sell_imbalance_long"), 0.20
            ),
            threshold_buy_sell_imbalance_short=self._safe_float(
                row.get("threshold_buy_sell_imbalance_short"), -0.20
            ),
            decay_alpha=self._safe_optional_float(row.get("decay_alpha")),
            profile_mode=str(row.get("profile_mode") or "rolling"),
            notes=str(row.get("notes")) if row.get("notes") is not None else None,
        )

    def _row_to_raw_market_snapshot(self, row: Mapping[str, Any]) -> RawMarketSnapshot:
        return RawMarketSnapshot(
            symbol=str(row.get("symbol") or "").upper(),
            ts=self._safe_datetime(row.get("timestamp")) or datetime.utcnow(),
            price=self._safe_float(row.get("price")),
            price_change_1m=self._safe_float(row.get("price_change_1m")),
            price_change_5m=self._safe_float(row.get("price_change_5m")),
            price_change_15m=self._safe_float(row.get("price_change_15m")),
            oi_change=self._safe_float(row.get("oi_change")),
            oi_change_ratio=self._safe_float(row.get("oi_change_ratio")),
            trade_volume_1m=self._safe_float(row.get("trade_volume_1m")),
            volume_spike_ratio=self._safe_float(row.get("volume_spike_ratio")),
            buy_volume=self._safe_float(row.get("buy_volume")),
            sell_volume=self._safe_float(row.get("sell_volume")),
            delta=self._safe_float(row.get("delta")),
            orderflow_ratio=self._safe_float(row.get("orderflow_ratio")),
            liquidation_density_5m=self._safe_float(row.get("liquidation_density_5m")),
            liquidation_cluster_score=self._safe_float(row.get("liquidation_cluster_score")),
            microburst_score=self._safe_float(row.get("microburst_score")),
            spread=self._safe_float(row.get("spread")),
            trade_count_1m=self._safe_float(row.get("trade_count_1m")),
            avg_trade_size=self._safe_optional_float(row.get("avg_trade_size")),
            atr_1m=self._safe_optional_float(row.get("atr_1m")),
            raw=dict(row),
        )

    def _build_market_state_live_row(
        self,
        *,
        symbol: str,
        ts: datetime,
        raw_snapshot: RawMarketSnapshot,
        normalized_snapshot: NormalizedSnapshot,
        events: PrimitiveEvents,
        scores: ScoreSnapshot,
        slow_regime: SlowRegimeSnapshot,
        mid_regime: MidRegimeSnapshot,
        fast_trigger: FastTriggerSnapshot,
        routed_regime: RoutedRegimeSnapshot,
        regime: RegimeSnapshot,
        state_machine: StateMachineSnapshot,
        decision: DecisionPolicyResult,
        engine_version: int,
    ) -> dict[str, Any]:
        oi_price_state = routed_regime.oi_price_state or fast_trigger.oi_price_state
        oi_flags = self._derive_oi_price_flags(oi_price_state)
        no_trade_signal = raw_snapshot.trade_count_1m <= 0 or raw_snapshot.trade_volume_1m <= 0
        return {
            "symbol": symbol.upper(),
            "ts": ts,
            "price": raw_snapshot.price,
            "price_change_1m": raw_snapshot.price_change_1m,
            "price_change_5m": raw_snapshot.price_change_5m,
            "price_change_15m": raw_snapshot.price_change_15m,
            "oi_change_ratio": raw_snapshot.oi_change_ratio,
            "trade_volume_1m": raw_snapshot.trade_volume_1m,
            "volume_spike_ratio": raw_snapshot.volume_spike_ratio,
            "orderflow_ratio": raw_snapshot.orderflow_ratio,
            "delta_ratio": normalized_snapshot.value("delta_ratio"),
            "microburst_score": None if no_trade_signal else raw_snapshot.microburst_score,
            "liquidation_density_5m": raw_snapshot.liquidation_density_5m,
            "liquidation_cluster_score": raw_snapshot.liquidation_cluster_score,
            "spread_ratio": normalized_snapshot.value("spread_ratio"),
            "trade_count_1m": raw_snapshot.trade_count_1m,
            "avg_trade_size": None if no_trade_signal else raw_snapshot.avg_trade_size,
            "velocity_1m": normalized_snapshot.value("velocity_1m"),
            "velocity_5m": normalized_snapshot.value("velocity_5m"),
            "velocity_15m": normalized_snapshot.value("velocity_15m"),
            "acceleration_1m": normalized_snapshot.value("acceleration_1m"),
            "acceleration_5m": normalized_snapshot.value("acceleration_5m"),
            "buy_sell_imbalance": normalized_snapshot.value("buy_sell_imbalance"),
            "oi_slope_short": normalized_snapshot.value("oi_slope_short"),
            "volume_slope_short": normalized_snapshot.value("volume_slope_short"),
            "atr_move_ratio": normalized_snapshot.value("atr_move_ratio"),
            "price_change_1m_z": normalized_snapshot.z("price_change_1m"),
            "price_change_5m_z": normalized_snapshot.z("price_change_5m"),
            "price_change_15m_z": normalized_snapshot.z("price_change_15m"),
            "oi_change_ratio_z": normalized_snapshot.z("oi_change_ratio"),
            "trade_volume_1m_z": normalized_snapshot.z("trade_volume_1m"),
            "volume_spike_ratio_z": normalized_snapshot.z("volume_spike_ratio"),
            "orderflow_ratio_z": normalized_snapshot.z("orderflow_ratio"),
            "delta_ratio_z": normalized_snapshot.z("delta_ratio"),
            "microburst_score_z": normalized_snapshot.z("microburst_score"),
            "liquidation_density_5m_z": normalized_snapshot.z("liquidation_density_5m"),
            "liquidation_cluster_score_z": normalized_snapshot.z("liquidation_cluster_score"),
            "spread_ratio_z": normalized_snapshot.z("spread_ratio"),
            "trade_count_1m_z": normalized_snapshot.z("trade_count_1m"),
            "avg_trade_size_z": normalized_snapshot.z("avg_trade_size"),
            "velocity_1m_z": normalized_snapshot.z("velocity_1m"),
            "pressure_score": scores.pressure_score,
            "participation_score": scores.participation_score,
            "instability_score": scores.instability_score,
            "exhaustion_score": scores.exhaustion_score,
            "slow_pressure_score": slow_regime.pressure_score_slow,
            "slow_participation_score": slow_regime.participation_score_slow,
            "slow_exhaustion_score": slow_regime.exhaustion_score_slow,
            "fast_pressure_score": fast_trigger.pressure_score_fast,
            "fast_participation_score": fast_trigger.participation_score_fast,
            "fast_instability_score": fast_trigger.instability_score_fast,
            "fast_exhaustion_score": fast_trigger.exhaustion_score_fast,
            "primitive_events_json": self._json_dumps(events.to_dict()),
            "regime_candidates_json": self._json_dumps(
                {
                    "candidate_states": regime.candidate_states,
                    "candidate_flags": regime.candidate_flags,
                    "transition_reason": regime.transition_reason,
                }
            ),
            "slow_state": slow_regime.state,
            "slow_state_memory": slow_regime.state_memory,
            "slow_transition_counter": slow_regime.transition_counter,
            "slow_bias": slow_regime.bias,
            "mid_state": mid_regime.state,
            "fast_state": fast_trigger.state,
            "routed_state": state_machine.routed_state or routed_regime.routed_state,
            "confidence": routed_regime.confidence,
            "confidence_source": decision.confidence_source,
            "conflict_flags_json": self._json_dumps(routed_regime.conflict_flags),
            "instability_flags_json": self._json_dumps(routed_regime.instability_flags),
            "decision": decision.decision,
            "decision_reason": decision.decision_reason,
            "range_unclear_diagnosis": decision.range_unclear_diagnosis,
            "entry_allowed": int(decision.entry_allowed),
            "oi_price_state": oi_price_state,
            "oi_price_build_long": oi_flags["oi_price_build_long"],
            "oi_price_short_covering": oi_flags["oi_price_short_covering"],
            "oi_price_build_short": oi_flags["oi_price_build_short"],
            "oi_price_long_unwinding": oi_flags["oi_price_long_unwinding"],
            "slow_transition_reason": ";".join(slow_regime.transition_reason),
            "mid_transition_reason": ";".join(mid_regime.transition_reason),
            "fast_transition_reason": ";".join(fast_trigger.transition_reason),
            "routed_transition_reason": ";".join(routed_regime.transition_reason),
            "active_state": state_machine.current_state,
            "emergency_trigger": regime.emergency_trigger,
            "hf_rebound_participation_flag": regime.hf_rebound_participation_flag,
            "confirmation_counters_json": self._json_dumps(state_machine.confirmation_counters),
            "cooldown_remaining_fast_updates": state_machine.cooldown_remaining_fast_updates,
            "transition_reason": ";".join(state_machine.transition_reason),
            "last_processed_ts": state_machine.current_ts or ts,
            "last_confirmed_ts": state_machine.last_confirmed_ts,
            "engine_version": engine_version,
        }

    def _build_coin_profile_row(self, profile: CoinProfile) -> dict[str, Any]:
        row: dict[str, Any] = {
            "symbol": profile.symbol.upper(),
            "updated_at": profile.updated_at,
            "sample_size": profile.sample_size,
            "profile_version": profile.profile_version,
            "window_start": profile.window_start,
            "window_end": profile.window_end,
            "threshold_orderflow_long": profile.threshold_orderflow_long,
            "threshold_orderflow_short": profile.threshold_orderflow_short,
            "threshold_buy_sell_imbalance_long": profile.threshold_buy_sell_imbalance_long,
            "threshold_buy_sell_imbalance_short": profile.threshold_buy_sell_imbalance_short,
            "decay_alpha": profile.decay_alpha,
            "profile_mode": profile.profile_mode,
            "notes": profile.notes,
        }
        for feature_name in PROFILED_FEATURES:
            stats = profile.get_stats(feature_name)
            row[f"{feature_name}_mean"] = stats.mean
            row[f"{feature_name}_std"] = stats.std
            row[f"{feature_name}_p50"] = stats.p50
            row[f"{feature_name}_p75"] = stats.p75
            row[f"{feature_name}_p90"] = stats.p90
            row[f"{feature_name}_p95"] = stats.p95
            row[f"{feature_name}_p99"] = stats.p99
            row[f"{feature_name}_n95"] = stats.n95
            row[f"{feature_name}_n99"] = stats.n99
            row[f"{feature_name}_median"] = stats.median
            row[f"{feature_name}_mad"] = stats.mad
        return row

    def _build_create_coin_profiles_current_sql(self) -> str:
        feature_columns: list[str] = []
        for feature_name in PROFILED_FEATURES:
            feature_columns.extend(
                [
                    f"{feature_name}_mean DOUBLE NULL",
                    f"{feature_name}_std DOUBLE NULL",
                    f"{feature_name}_p50 DOUBLE NULL",
                    f"{feature_name}_p75 DOUBLE NULL",
                    f"{feature_name}_p90 DOUBLE NULL",
                    f"{feature_name}_p95 DOUBLE NULL",
                    f"{feature_name}_p99 DOUBLE NULL",
                    f"{feature_name}_n95 DOUBLE NULL",
                    f"{feature_name}_n99 DOUBLE NULL",
                    f"{feature_name}_median DOUBLE NULL",
                    f"{feature_name}_mad DOUBLE NULL",
                ]
            )
        feature_sql = ",\n            ".join(feature_columns)
        return f"""
        CREATE TABLE IF NOT EXISTS {self.config.profiles_table} (
            symbol VARCHAR(32) PRIMARY KEY,
            updated_at DATETIME NOT NULL,
            sample_size BIGINT NOT NULL,
            profile_version INT NOT NULL DEFAULT 1,
            window_start DATETIME NULL,
            window_end DATETIME NULL,
            {feature_sql},
            threshold_orderflow_long DOUBLE NOT NULL DEFAULT 0.15,
            threshold_orderflow_short DOUBLE NOT NULL DEFAULT -0.15,
            threshold_buy_sell_imbalance_long DOUBLE NOT NULL DEFAULT 0.20,
            threshold_buy_sell_imbalance_short DOUBLE NOT NULL DEFAULT -0.20,
            decay_alpha DOUBLE NULL,
            profile_mode VARCHAR(32) NOT NULL DEFAULT 'rolling',
            notes TEXT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """

    def _build_create_coin_profiles_history_sql(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS {self.config.profiles_history_table} (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            symbol VARCHAR(32) NOT NULL,
            snapshot_time DATETIME NOT NULL,
            sample_size BIGINT NOT NULL,
            profile_version INT NOT NULL DEFAULT 1,
            window_start DATETIME NULL,
            window_end DATETIME NULL,
            profile_json JSON NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_symbol_snapshot (symbol, snapshot_time)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """

    def _build_create_market_state_live_sql(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS {self.config.market_state_live_table} (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            symbol VARCHAR(32) NOT NULL,
            ts DATETIME NOT NULL,
            price DOUBLE NOT NULL,
            price_change_1m DOUBLE NULL,
            price_change_5m DOUBLE NULL,
            price_change_15m DOUBLE NULL,
            oi_change_ratio DOUBLE NULL,
            trade_volume_1m DOUBLE NULL,
            volume_spike_ratio DOUBLE NULL,
            orderflow_ratio DOUBLE NULL,
            delta_ratio DOUBLE NULL,
            microburst_score DOUBLE NULL,
            liquidation_density_5m DOUBLE NULL,
            liquidation_cluster_score DOUBLE NULL,
            spread_ratio DOUBLE NULL,
            trade_count_1m DOUBLE NULL,
            avg_trade_size DOUBLE NULL,
            velocity_1m DOUBLE NULL,
            velocity_5m DOUBLE NULL,
            velocity_15m DOUBLE NULL,
            acceleration_1m DOUBLE NULL,
            acceleration_5m DOUBLE NULL,
            buy_sell_imbalance DOUBLE NULL,
            oi_slope_short DOUBLE NULL,
            volume_slope_short DOUBLE NULL,
            atr_move_ratio DOUBLE NULL,
            price_change_1m_z DOUBLE NULL,
            price_change_5m_z DOUBLE NULL,
            price_change_15m_z DOUBLE NULL,
            oi_change_ratio_z DOUBLE NULL,
            trade_volume_1m_z DOUBLE NULL,
            volume_spike_ratio_z DOUBLE NULL,
            orderflow_ratio_z DOUBLE NULL,
            delta_ratio_z DOUBLE NULL,
            microburst_score_z DOUBLE NULL,
            liquidation_density_5m_z DOUBLE NULL,
            liquidation_cluster_score_z DOUBLE NULL,
            spread_ratio_z DOUBLE NULL,
            trade_count_1m_z DOUBLE NULL,
            avg_trade_size_z DOUBLE NULL,
            velocity_1m_z DOUBLE NULL,
            pressure_score DOUBLE NOT NULL,
            participation_score DOUBLE NOT NULL,
            instability_score DOUBLE NOT NULL,
            exhaustion_score DOUBLE NOT NULL,
            slow_pressure_score DOUBLE NULL,
            slow_participation_score DOUBLE NULL,
            slow_exhaustion_score DOUBLE NULL,
            fast_pressure_score DOUBLE NULL,
            fast_participation_score DOUBLE NULL,
            fast_instability_score DOUBLE NULL,
            fast_exhaustion_score DOUBLE NULL,
            primitive_events_json JSON NOT NULL,
            regime_candidates_json JSON NULL,
            slow_state VARCHAR(64) NULL,
            slow_state_memory VARCHAR(64) NULL,
            slow_transition_counter INT NOT NULL DEFAULT 0,
            slow_bias INT NOT NULL DEFAULT 0,
            mid_state VARCHAR(64) NULL,
            fast_state VARCHAR(64) NULL,
            routed_state VARCHAR(64) NULL,
            confidence DOUBLE NULL,
            confidence_source VARCHAR(32) NULL,
            conflict_flags_json JSON NULL,
            instability_flags_json JSON NULL,
            decision VARCHAR(16) NULL,
            decision_reason VARCHAR(128) NULL,
            range_unclear_diagnosis VARCHAR(64) NULL,
            entry_allowed TINYINT(1) NOT NULL DEFAULT 0,
            oi_price_state VARCHAR(64) NULL,
            oi_price_build_long TINYINT(1) NOT NULL DEFAULT 0,
            oi_price_short_covering TINYINT(1) NOT NULL DEFAULT 0,
            oi_price_build_short TINYINT(1) NOT NULL DEFAULT 0,
            oi_price_long_unwinding TINYINT(1) NOT NULL DEFAULT 0,
            slow_transition_reason TEXT NULL,
            mid_transition_reason TEXT NULL,
            fast_transition_reason TEXT NULL,
            routed_transition_reason TEXT NULL,
            active_state VARCHAR(64) NOT NULL,
            emergency_trigger TINYINT(1) NOT NULL DEFAULT 0,
            hf_rebound_participation_flag TINYINT(1) NOT NULL DEFAULT 0,
            confirmation_counters_json JSON NULL,
            cooldown_remaining_fast_updates INT NOT NULL DEFAULT 0,
            transition_reason TEXT NULL,
            last_processed_ts DATETIME NULL,
            last_confirmed_ts DATETIME NULL,
            engine_version INT NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY ux_symbol_ts (symbol, ts),
            INDEX idx_symbol_state_ts (symbol, active_state, ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """

    def _build_create_signal_validation_results_sql(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS {self.config.signal_validation_results_table} (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            signal_history_id BIGINT NOT NULL,
            symbol VARCHAR(32) NOT NULL,
            ts DATETIME(6) NOT NULL,
            routed_state VARCHAR(64) NOT NULL,
            decision VARCHAR(32) NOT NULL,
            entry_allowed TINYINT(1) NOT NULL DEFAULT 0,
            confidence DOUBLE NULL,
            confidence_source VARCHAR(64) NULL,
            signal_direction VARCHAR(16) NOT NULL,
            price_at_signal DOUBLE NULL,
            price_5m DOUBLE NULL,
            price_15m DOUBLE NULL,
            price_30m DOUBLE NULL,
            price_60m DOUBLE NULL,
            return_5m_pct DOUBLE NULL,
            return_15m_pct DOUBLE NULL,
            return_30m_pct DOUBLE NULL,
            return_60m_pct DOUBLE NULL,
            evaluation_return_pct DOUBLE NULL,
            max_favorable_move_pct DOUBLE NULL,
            max_adverse_move_pct DOUBLE NULL,
            processed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY ux_signal_history_id (signal_history_id),
            INDEX idx_symbol_ts (symbol, ts),
            INDEX idx_routed_decision_symbol (routed_state, decision, symbol)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """

    def _build_create_signal_validation_summary_sql(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS {self.config.signal_validation_summary_table} (
            routed_state VARCHAR(64) NOT NULL,
            decision VARCHAR(32) NOT NULL,
            symbol VARCHAR(32) NOT NULL,
            signal_count BIGINT NOT NULL,
            hit_rate_pct DOUBLE NULL,
            avg_return_5m_pct DOUBLE NULL,
            avg_return_15m_pct DOUBLE NULL,
            avg_return_30m_pct DOUBLE NULL,
            avg_return_60m_pct DOUBLE NULL,
            avg_mfe_pct DOUBLE NULL,
            avg_mae_pct DOUBLE NULL,
            expectancy_pct DOUBLE NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (routed_state, decision, symbol),
            INDEX idx_symbol_expectancy (symbol, expectancy_pct)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """

    def _ensure_market_state_live_v2_columns(self, cursor: Any) -> None:
        required_columns = {
            "slow_pressure_score": "ALTER TABLE {table} ADD COLUMN slow_pressure_score DOUBLE NULL",
            "slow_participation_score": "ALTER TABLE {table} ADD COLUMN slow_participation_score DOUBLE NULL",
            "slow_exhaustion_score": "ALTER TABLE {table} ADD COLUMN slow_exhaustion_score DOUBLE NULL",
            "fast_pressure_score": "ALTER TABLE {table} ADD COLUMN fast_pressure_score DOUBLE NULL",
            "fast_participation_score": "ALTER TABLE {table} ADD COLUMN fast_participation_score DOUBLE NULL",
            "fast_instability_score": "ALTER TABLE {table} ADD COLUMN fast_instability_score DOUBLE NULL",
            "fast_exhaustion_score": "ALTER TABLE {table} ADD COLUMN fast_exhaustion_score DOUBLE NULL",
            "slow_state": "ALTER TABLE {table} ADD COLUMN slow_state VARCHAR(64) NULL",
            "slow_state_memory": "ALTER TABLE {table} ADD COLUMN slow_state_memory VARCHAR(64) NULL",
            "slow_transition_counter": "ALTER TABLE {table} ADD COLUMN slow_transition_counter INT NOT NULL DEFAULT 0",
            "slow_bias": "ALTER TABLE {table} ADD COLUMN slow_bias INT NOT NULL DEFAULT 0",
            "mid_state": "ALTER TABLE {table} ADD COLUMN mid_state VARCHAR(64) NULL",
            "fast_state": "ALTER TABLE {table} ADD COLUMN fast_state VARCHAR(64) NULL",
            "routed_state": "ALTER TABLE {table} ADD COLUMN routed_state VARCHAR(64) NULL",
            "confidence": "ALTER TABLE {table} ADD COLUMN confidence DOUBLE NULL",
            "confidence_source": "ALTER TABLE {table} ADD COLUMN confidence_source VARCHAR(32) NULL",
            "conflict_flags_json": "ALTER TABLE {table} ADD COLUMN conflict_flags_json JSON NULL",
            "instability_flags_json": "ALTER TABLE {table} ADD COLUMN instability_flags_json JSON NULL",
            "decision": "ALTER TABLE {table} ADD COLUMN decision VARCHAR(16) NULL",
            "decision_reason": "ALTER TABLE {table} ADD COLUMN decision_reason VARCHAR(128) NULL",
            "range_unclear_diagnosis": "ALTER TABLE {table} ADD COLUMN range_unclear_diagnosis VARCHAR(64) NULL",
            "entry_allowed": "ALTER TABLE {table} ADD COLUMN entry_allowed TINYINT(1) NOT NULL DEFAULT 0",
            "oi_price_state": "ALTER TABLE {table} ADD COLUMN oi_price_state VARCHAR(64) NULL",
            "oi_price_build_long": "ALTER TABLE {table} ADD COLUMN oi_price_build_long TINYINT(1) NOT NULL DEFAULT 0",
            "oi_price_short_covering": "ALTER TABLE {table} ADD COLUMN oi_price_short_covering TINYINT(1) NOT NULL DEFAULT 0",
            "oi_price_build_short": "ALTER TABLE {table} ADD COLUMN oi_price_build_short TINYINT(1) NOT NULL DEFAULT 0",
            "oi_price_long_unwinding": "ALTER TABLE {table} ADD COLUMN oi_price_long_unwinding TINYINT(1) NOT NULL DEFAULT 0",
            "slow_transition_reason": "ALTER TABLE {table} ADD COLUMN slow_transition_reason TEXT NULL",
            "mid_transition_reason": "ALTER TABLE {table} ADD COLUMN mid_transition_reason TEXT NULL",
            "fast_transition_reason": "ALTER TABLE {table} ADD COLUMN fast_transition_reason TEXT NULL",
            "routed_transition_reason": "ALTER TABLE {table} ADD COLUMN routed_transition_reason TEXT NULL",
            "last_processed_ts": "ALTER TABLE {table} ADD COLUMN last_processed_ts DATETIME NULL",
            "last_confirmed_ts": "ALTER TABLE {table} ADD COLUMN last_confirmed_ts DATETIME NULL",
        }
        try:
            cursor.execute(f"SHOW COLUMNS FROM {self.config.market_state_live_table}")
            rows = cursor.fetchall() or []
        except Exception:
            return
        existing_columns = {str(row.get('Field') or row.get('field') or "") for row in rows if isinstance(row, Mapping)}
        for column_name, statement in required_columns.items():
            if column_name in existing_columns:
                continue
            cursor.execute(statement.format(table=self.config.market_state_live_table))

    def _ensure_market_state_live_uniqueness(self, cursor: Any) -> None:
        if not self._table_exists(cursor, self.config.market_state_live_table):
            return
        indexes = self._load_table_indexes(cursor, self.config.market_state_live_table)
        if self._has_unique_symbol_ts_index(indexes):
            return
        cursor.execute(
            f"""
            DELETE older
            FROM {self.config.market_state_live_table} AS older
            INNER JOIN {self.config.market_state_live_table} AS newer
                ON older.symbol = newer.symbol
               AND older.ts = newer.ts
               AND older.id < newer.id
            """
        )
        cursor.execute(
            f"ALTER TABLE {self.config.market_state_live_table} ADD CONSTRAINT ux_symbol_ts UNIQUE (symbol, ts)"
        )

    def _load_table_indexes(self, cursor: Any, table_name: str) -> dict[str, list[dict[str, Any]]]:
        cursor.execute(f"SHOW INDEX FROM {table_name}")
        rows = cursor.fetchall() or []
        indexes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            key_name = str(row.get("Key_name") or row.get("key_name") or "")
            if not key_name:
                continue
            indexes[key_name].append(dict(row))
        return indexes

    @staticmethod
    def _has_unique_symbol_ts_index(indexes: Mapping[str, Sequence[Mapping[str, Any]]]) -> bool:
        for index_rows in indexes.values():
            ordered_rows = sorted(
                index_rows,
                key=lambda row: int(row.get("Seq_in_index") or row.get("seq_in_index") or 0),
            )
            if not ordered_rows:
                continue
            non_unique = int(ordered_rows[0].get("Non_unique") or ordered_rows[0].get("non_unique") or 0)
            columns = [
                str(row.get("Column_name") or row.get("column_name") or "")
                for row in ordered_rows
            ]
            if non_unique == 0 and columns == ["symbol", "ts"]:
                return True
        return False

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_optional_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_json_loads(value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default

    @staticmethod
    def _safe_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return None

    @staticmethod
    def _json_dumps(payload: Mapping[str, Any] | Iterable[Any]) -> str:
        return json.dumps(payload, sort_keys=True, default=str)

    @staticmethod
    def _derive_oi_price_flags(oi_price_state: str | None) -> dict[str, int]:
        state = str(oi_price_state or "neutral")
        return {
            "oi_price_build_long": int(state == "price_up_oi_up"),
            "oi_price_short_covering": int(state == "price_up_oi_down"),
            "oi_price_build_short": int(state == "price_down_oi_up"),
            "oi_price_long_unwinding": int(state == "price_down_oi_down"),
        }

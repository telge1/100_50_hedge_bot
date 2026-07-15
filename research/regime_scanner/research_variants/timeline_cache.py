"""Timeline reuse + window evaluation cache (compute once, slice/rescore many).

Core principle (Phase 8/11-18): treat a completed ``research_runs`` row as a
timeline. Windows are evaluated by slicing stored trend states / structure
events and scoring them — never by re-running the scanner. Score/metric changes
bump ``score_version`` / ``metric_version`` and reuse the same timeline.

All functions here are deterministic and side-effect free except the explicit
store class.
"""

from __future__ import annotations

import sys
from typing import Any

from research.regime_scanner.research_runs.hashing import json_hash
from research.regime_scanner.research_runs.parameters import (
    ResearchParameterSet,
    _config_to_dict,
)
from research.regime_scanner.research_variants.scoring import (
    METRIC_VERSION,
    SCORE_VERSION,
    evaluate_window,
)
from research.regime_scanner.timeframes import ensure_utc_timestamp

# Decision-time semantics are fixed for this research system (decision = bar close + 5m).
DECISION_TIME_SEMANTICS = "close_plus_5m"


def log_cache(msg: str) -> None:
    print(f"[cache] {msg}", file=sys.stderr, flush=True)


def _iso(ts: Any) -> str:
    return ensure_utc_timestamp(ts).isoformat()


def feature_config_hash(params: ResearchParameterSet) -> str:
    """Variant-INDEPENDENT config hash (shared prepared-context inputs).

    Variants only override ``trend_state``; everything below is shared and can be
    prepared once.
    """
    payload = {
        "timeframes": list(params.timeframes),
        "history_candles": int(params.history_candles),
        "regime_scanner": _config_to_dict(params.regime_scanner),
        "price_action": _config_to_dict(params.price_action),
        "momentum": _config_to_dict(params.momentum),
    }
    return json_hash(payload)


def prepared_context_hash(
    *,
    exchange: str,
    symbol: str,
    data_source: str,
    warmup_start: Any,
    timeline_end: Any,
    candle_hash_5m: str,
    candle_hash_15m: str,
    candle_hash_30m: str,
    feature_config_hash: str,
    scanner_code_version: str,
) -> str:
    payload = {
        "exchange": exchange,
        "symbol": symbol,
        "data_source": data_source,
        "warmup_start": _iso(warmup_start),
        "timeline_end": _iso(timeline_end),
        "candle_input_hashes": {
            "5m": candle_hash_5m,
            "15m": candle_hash_15m,
            "30m": candle_hash_30m,
        },
        "feature_config_hash": feature_config_hash,
        "scanner_code_version": scanner_code_version,
        "decision_time_semantics": DECISION_TIME_SEMANTICS,
    }
    return json_hash(payload)


def timeline_fingerprint(
    *,
    prepared_context_hash: str,
    parameter_hash: str,
    scanner_version: str,
    warmup_start: Any,
    timeline_start: Any,
    timeline_end: Any,
) -> str:
    payload = {
        "prepared_context_hash": prepared_context_hash,
        "parameter_hash": parameter_hash,
        "scanner_version": scanner_version,
        "warmup_start": _iso(warmup_start),
        "timeline_start": _iso(timeline_start),
        "timeline_end": _iso(timeline_end),
        "decision_time_semantics": DECISION_TIME_SEMANTICS,
    }
    return json_hash(payload)


def evaluation_hash(
    *,
    timeline_id: str,
    window_hash: str,
    metric_version: int,
    score_version: int,
) -> str:
    return json_hash(
        {
            "timeline_id": timeline_id,
            "window_hash": window_hash,
            "metric_version": int(metric_version),
            "score_version": int(score_version),
        }
    )


def slice_timeline_for_window(
    trend_states: list[dict[str, Any]],
    structure_events: list[dict[str, Any]],
    *,
    start: Any,
    end: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cut stored rows to a window using the SAME boundary semantics as a run.

    * trend states: decision-time in ``[start, end]`` (inclusive), matching
      ``run_trend_state_timeline``.
    * structure events: event-time in ``[start, end)`` (end-exclusive), matching
      ``normalize_structure_events``.
    """
    s = ensure_utc_timestamp(start)
    e = ensure_utc_timestamp(end)
    trend = [r for r in trend_states if s <= ensure_utc_timestamp(r["timestamp"]) <= e]
    struct = [r for r in structure_events if s <= ensure_utc_timestamp(r["timestamp"]) < e]
    return trend, struct


def timeline_covers_window(run_row: dict[str, Any], *, start: Any, end: Any) -> bool:
    if run_row.get("status") != "completed":
        return False
    r_start = ensure_utc_timestamp(run_row["start_time"])
    r_end = ensure_utc_timestamp(run_row["end_time"])
    return r_start <= ensure_utc_timestamp(start) and r_end >= ensure_utc_timestamp(end)


def find_covering_completed_timeline(
    research_store: Any,
    *,
    parameter_hash: str,
    symbol: str,
    data_source: str,
    window_start: Any,
    window_end: Any,
) -> dict[str, Any] | None:
    """Return the smallest completed run that fully covers [start, end] for this
    parameter set / symbol / data source, or None.

    Reuse requires exact parameter_hash, symbol and data_source; only completed
    runs are considered (never running/failed/interrupted).
    """
    candidates = research_store.list_runs(
        symbol=symbol, status="completed", parameter_hash=parameter_hash, limit=500
    )
    covering = [
        r
        for r in candidates
        if r.get("data_source") == data_source
        and timeline_covers_window(r, start=window_start, end=window_end)
    ]
    if not covering:
        return None
    # Prefer the tightest covering timeline (least extra replay) deterministically.
    def _span(r: dict[str, Any]) -> tuple[float, str]:
        span = (
            ensure_utc_timestamp(r["end_time"]) - ensure_utc_timestamp(r["start_time"])
        ).total_seconds()
        return (span, str(r["run_id"]))

    covering.sort(key=_span)
    return covering[0]


def evaluate_window_from_timeline(
    research_store: Any,
    *,
    timeline_run_id: str,
    window_start: Any,
    window_end: Any,
    expected_character: str | None = None,
) -> dict[str, Any]:
    """Slice a stored timeline for a window and score it (no scanner)."""
    trend = research_store.load_trend_states(timeline_run_id)
    struct = research_store.load_structure_events(timeline_run_id)
    sl_trend, sl_struct = slice_timeline_for_window(
        trend, struct, start=window_start, end=window_end
    )
    ev = evaluate_window(
        trend_states=sl_trend,
        structure_events=sl_struct,
        expected_character=expected_character,
    )
    ev["timeline_id"] = timeline_run_id
    ev["sliced_trend_count"] = len(sl_trend)
    ev["sliced_structure_count"] = len(sl_struct)
    return ev


class MySQLCacheStore:
    """Prepared-context registry + window-evaluation cache with build locking."""

    def __init__(self, variant_store: Any) -> None:
        self._engine = variant_store._engine
        self._text = variant_store._text

    # --- prepared contexts (build lock via atomic status) ---
    def try_begin_prepared_context(self, **kwargs: Any) -> str:
        """Return 'reuse' if a completed context exists, 'building' if this call
        claimed the build, or 'in_progress' if another build is active."""
        import json

        h = kwargs["prepared_context_hash"]
        with self._engine.begin() as conn:
            row = conn.execute(
                self._text(
                    "SELECT status FROM research_prepared_contexts WHERE prepared_context_hash=:h"
                ),
                {"h": h},
            ).first()
            if row is not None:
                return "reuse" if row[0] == "completed" else "in_progress"
            try:
                conn.execute(
                    self._text(
                        """
                        INSERT INTO research_prepared_contexts (
                          prepared_context_hash, exchange, symbol, data_source,
                          warmup_start, timeline_end, candle_hashes_json,
                          feature_config_hash, scanner_code_version, status, metadata_json
                        ) VALUES (
                          :h, :exchange, :symbol, :data_source, :warmup_start, :timeline_end,
                          CAST(:candle_hashes_json AS JSON), :feature_config_hash,
                          :scanner_code_version, 'building', CAST(:metadata_json AS JSON)
                        )
                        """
                    ),
                    {
                        "h": h,
                        "exchange": kwargs["exchange"],
                        "symbol": kwargs["symbol"],
                        "data_source": kwargs["data_source"],
                        "warmup_start": ensure_utc_timestamp(kwargs["warmup_start"]).to_pydatetime().replace(tzinfo=None),
                        "timeline_end": ensure_utc_timestamp(kwargs["timeline_end"]).to_pydatetime().replace(tzinfo=None),
                        "candle_hashes_json": json.dumps(kwargs["candle_hashes"], sort_keys=True),
                        "feature_config_hash": kwargs["feature_config_hash"],
                        "scanner_code_version": kwargs["scanner_code_version"],
                        "metadata_json": json.dumps(kwargs.get("metadata") or {}, sort_keys=True),
                    },
                )
                return "building"
            except Exception:
                # Lost the race: another process inserted the same hash.
                return "in_progress"

    def complete_prepared_context(self, prepared_context_hash: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                self._text(
                    """
                    UPDATE research_prepared_contexts
                    SET status='completed', completed_at=UTC_TIMESTAMP(6)
                    WHERE prepared_context_hash=:h
                    """
                ),
                {"h": prepared_context_hash},
            )

    def get_prepared_context(self, prepared_context_hash: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text(
                    "SELECT * FROM research_prepared_contexts WHERE prepared_context_hash=:h"
                ),
                {"h": prepared_context_hash},
            ).mappings().first()
            return dict(row) if row else None

    # --- window evaluations ---
    def get_window_evaluation(
        self, *, timeline_id: str, window_hash: str, metric_version: int, score_version: int
    ) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text(
                    """
                    SELECT * FROM research_window_evaluations
                    WHERE timeline_id=:t AND window_hash=:w
                      AND metric_version=:m AND score_version=:s
                    LIMIT 1
                    """
                ),
                {"t": timeline_id, "w": window_hash, "m": int(metric_version), "s": int(score_version)},
            ).mappings().first()
            return dict(row) if row else None

    def upsert_window_evaluation(self, **kwargs: Any) -> None:
        import json

        with self._engine.begin() as conn:
            conn.execute(
                self._text(
                    """
                    INSERT INTO research_window_evaluations (
                      timeline_id, window_hash, window_name, metric_version, score_version,
                      metrics_json, score, degenerate, degenerate_reason, rankable,
                      character_fit, evaluation_hash
                    ) VALUES (
                      :timeline_id, :window_hash, :window_name, :metric_version, :score_version,
                      CAST(:metrics_json AS JSON), :score, :degenerate, :degenerate_reason,
                      :rankable, :character_fit, :evaluation_hash
                    )
                    ON DUPLICATE KEY UPDATE
                      window_name=VALUES(window_name),
                      metrics_json=VALUES(metrics_json),
                      score=VALUES(score),
                      degenerate=VALUES(degenerate),
                      degenerate_reason=VALUES(degenerate_reason),
                      rankable=VALUES(rankable),
                      character_fit=VALUES(character_fit),
                      evaluation_hash=VALUES(evaluation_hash)
                    """
                ),
                {
                    "timeline_id": kwargs["timeline_id"],
                    "window_hash": kwargs["window_hash"],
                    "window_name": kwargs.get("window_name"),
                    "metric_version": int(kwargs.get("metric_version", METRIC_VERSION)),
                    "score_version": int(kwargs.get("score_version", SCORE_VERSION)),
                    "metrics_json": json.dumps(kwargs.get("metrics") or {}, sort_keys=True, default=str),
                    "score": kwargs.get("score"),
                    "degenerate": 1 if kwargs.get("degenerate") else 0,
                    "degenerate_reason": kwargs.get("degenerate_reason"),
                    "rankable": 1 if kwargs.get("rankable") else 0,
                    "character_fit": kwargs.get("character_fit"),
                    "evaluation_hash": kwargs["evaluation_hash"],
                },
            )

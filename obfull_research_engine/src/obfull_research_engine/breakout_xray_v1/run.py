"""Orchestration: strategy-edge, manual-window, and shared xray core.

Full analysis path always loads data through AnalysisDependencies ports.
Loose empty-list kwargs are not a productive path.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from . import FORMULA_ID, IMPLEMENTATION_SOURCE, PACKAGE_VERSION
from .adapters import market_profile as mp_adapter
from .adapters.live_data import assert_price_band_width
from .baseline_hash import (
    UNRESOLVED_BASELINE_HASH_MISMATCH,
    UNRESOLVED_BASELINE_MISSING_SILVER_HASH,
    assert_start_aligned_to_100ms,
    merge_chunk_keys,
    predecessor_bucket_start_ns,
    resolve_lc_price_band,
)
from .errors import AnalysisFailure, expected_failure, internal_failure
from .execution_hold import DEFAULT_SILVER_LOCK, ExecutionSentinel, assert_lock_untouched
from .matching import match_trades_to_wall
from .models import (
    AnalysisWindow,
    BaselineBookState,
    ManualWindowConfig,
    ReferenceLevel,
    RunMode,
    StrategyEdgeConfig,
    WallThresholdSpec,
    XRayResult,
)
from .outcomes import evaluate_outcomes
from .phases import detect_important_levels, detect_neutral_phases
from .ports import AnalysisDependencies, BookLevel, MidState
from .reference import (
    resolve_manual_reference,
    resolve_strategy_reference,
    wall_threshold_for_manual,
    wall_threshold_for_strategy,
)
from .resource_preflight import ResourcePreflightResult
from .states import run_breakout_state_machine
from .time_windows import (
    assert_half_open,
    dt_to_ns,
    ns_to_dt,
    rank_rolling_by_abs_net,
    strategy_analysis_bounds,
)
from .trades import dedup_trades_by_id, flow_summary, price_move_per_delta_million
from .walls import (
    accumulate_level_sizes,
    classify_wall_lifecycle,
    level_events_for_wall,
    select_walls_by_notional,
)
from .writers import RunOutputSession, write_xray_artifacts


def _window_from_bounds(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    chain_version: str,
    chain_hash: str,
) -> AnalysisWindow:
    assert_half_open(start, end)
    return AnalysisWindow(
        symbol=symbol,
        start_utc=start,
        end_utc=end,
        start_ns=dt_to_ns(start),
        end_ns=dt_to_ns(end),
        chain_version=chain_version,
        chain_hash=chain_hash,
    )


def _mid_to_sm_series(mids: list[MidState]) -> list[tuple[datetime, float]]:
    return [(ns_to_dt(m.bucket_start_ns), m.mid) for m in mids]


def _build_walls(
    *,
    baseline: BaselineBookState,
    level_changes,
    trades,
    mid_series: list[MidState],
    reference: ReferenceLevel,
    wall_threshold: WallThresholdSpec,
    q95_start_ns: int,
    q95_end_ns: int,
) -> tuple[float, list[dict[str, Any]], dict[str, Any]]:
    ref_px = reference.price
    if ref_px is None:
        return 0.0, [], {"reason": "NO_REFERENCE"}

    baseline_levels = [
        BookLevel(side=str(x["side"]), price=float(x["price"]), size=float(x["size"]))
        for x in (baseline.levels_in_band or ())
    ]
    sizes = accumulate_level_sizes(
        baseline_levels=baseline_levels,
        events=level_changes,
        window_start_ns=q95_start_ns,
        window_end_ns=q95_end_ns,
    )
    thr, ranked = select_walls_by_notional(
        sizes,
        ref_price=float(ref_px),
        local_band_usd=wall_threshold.local_band_usd,
        q=wall_threshold.quantile,
        max_rank=wall_threshold.max_rank,
    )
    by_side: dict[str, list[float]] = {"bid": [], "ask": []}
    for c in ranked:
        by_side.setdefault(c["side"], []).append(float(c["price"]))

    wall_lives: list[dict[str, Any]] = []
    for c in ranked:
        side, price = c["side"], float(c["price"])
        baseline_size = next(
            (float(b.size) for b in baseline_levels if b.side == side and b.price == price),
            float(c["size_base"]),
        )
        evs = level_events_for_wall(level_changes, side=side, price=price)
        match = match_trades_to_wall(
            side=side,
            price=price,
            baseline_size=baseline_size,
            events=[e for e in level_changes if e.side == side and abs(e.price - price) < 1e-9],
            trades=trades,
            mid_series=mid_series,
        )
        first_touch = (
            None
            if match.first_aggressive_touch_ns is None
            else ns_to_dt(match.first_aggressive_touch_ns)
        )
        neighbor_growth = False
        if match.minimum_size_after_touch == 0 or (
            evs and evs[-1].new_size <= 1e-9
        ):
            neighbors = [
                p for p in by_side.get(side, []) if abs(p - price) > 1e-9 and abs(p - price) <= 50.0
            ]
            for npx in neighbors:
                n_evs = level_events_for_wall(level_changes, side=side, price=npx)
                if n_evs and n_evs[-1].new_size > baseline_size * 0.5:
                    neighbor_growth = True
                    break

        life = classify_wall_lifecycle(
            baseline_size=baseline_size,
            events=evs,
            first_aggressive_touch=first_touch,
            hit_notional=match.hit_notional_usdt,
            baseline_complete=baseline.complete,
            neighbor_growth=neighbor_growth,
            side=side,
            price=price,
            matching_confidence=match.matching_confidence,
        )
        if match.matching_confidence == "LOW" and life.classification.value not in {
            "UNRESOLVED_BASELINE",
            "PULL",
        }:
            if match.hit_trade_count == 0 and not evs:
                from .models import WallClass

                life.classification = WallClass.UNRESOLVED
                life.evidence = {"reason": "LOW_MATCHING_CONFIDENCE"}
        life.local_rank = c["local_rank"]
        life.distance_to_ref = c["distance_to_ref"]
        life.size_base = float(c["size_base"])
        life.notional_usdt = float(c["notional_usdt"])
        life.match = match.to_dict()
        d = life.to_dict()
        d["q95_threshold_notional"] = thr
        wall_lives.append(d)

    meta = {
        "q95_threshold_notional": thr,
        "wall_threshold_method": wall_threshold.method,
        "wall_threshold_causal": wall_threshold.causal,
        "selected_count": len(wall_lives),
    }
    return thr, wall_lives, meta


def _shell_result(
    *,
    mode: RunMode,
    window: AnalysisWindow,
    reference: ReferenceLevel,
    wall_threshold: WallThresholdSpec,
    baseline: BaselineBookState,
    sections: dict[str, Any],
    data_quality: dict[str, Any],
) -> XRayResult:
    return XRayResult(
        mode=mode,
        window=window,
        reference=reference,
        wall_threshold=wall_threshold,
        baseline=baseline,
        sections=sections,
        data_quality=data_quality,
    )


def run_xray_core(
    *,
    mode: RunMode,
    window: AnalysisWindow,
    reference: ReferenceLevel,
    wall_threshold: WallThresholdSpec,
    output_dir: Path,
    check_only: bool,
    deps: AnalysisDependencies | None = None,
    decision_time_utc: datetime | None = None,
    lock_path: Path = DEFAULT_SILVER_LOCK,
    skip_execution_hold: bool = False,
    execute_live: bool = False,
    sentinel: ExecutionSentinel | None = None,
    resource_preflight: ResourcePreflightResult | None = None,
) -> XRayResult:
    log: list[str] = [f"{PACKAGE_VERSION} mode={mode.value}"]
    lock_info = assert_lock_untouched(lock_path)
    log.append(f"lock_probe={lock_info}")
    output_dir = Path(output_dir)
    manifest_extra: dict[str, Any] = {}
    if resource_preflight is not None:
        manifest_extra["resource_preflight"] = resource_preflight.to_dict()
    if sentinel is not None:
        manifest_extra["execution_sentinel"] = sentinel.to_dict()

    if check_only:
        baseline = BaselineBookState(
            source="check_only_skipped",
            timestamp=None,
            age_ms=None,
            complete=False,
            hash=None,
            unresolved_reason="CHECK_ONLY",
        )
        result = _shell_result(
            mode=mode,
            window=window,
            reference=reference,
            wall_threshold=wall_threshold,
            baseline=baseline,
            sections={
                "readiness": {"status": "CHECK_ONLY"},
                "coverage": {"check_only": True},
                "mp_manifest": {
                    "formula_id": FORMULA_ID,
                    "implementation_source": IMPLEMENTATION_SOURCE,
                    "temporary_cross_repo_dependency": True,
                },
                "window_summary": window.to_dict(),
                "price_phases": [],
                "trade_flow": {},
                "efficiency": {},
                "important_levels": [],
                "wall_lifecycles": [],
                "reference_interaction": None
                if reference.price is None
                else {"causal_reference": reference.causal_reference},
                "avr": {"status": "NOT_RUN_CHECK_ONLY"},
                "open_interest": {"status": "NOT_RUN_CHECK_ONLY"},
                "outcomes": [],
                "minute_metrics": [],
                "trade_sequence": [],
                "trade_dedup": {},
                "breakout_states": None,
            },
            data_quality={"check_only": True, "lock": lock_info},
        )
        write_xray_artifacts(output_dir, result, log_lines=log + ["CHECK_ONLY_COMPLETE"])
        return result

    session = RunOutputSession(output_dir)
    session.write_running_marker()

    def _fail(result: XRayResult | None, failure: AnalysisFailure) -> XRayResult:
        if result is None:
            result = _shell_result(
                mode=mode,
                window=window,
                reference=reference,
                wall_threshold=wall_threshold,
                baseline=BaselineBookState(
                    source="failed",
                    timestamp=None,
                    age_ms=None,
                    complete=False,
                    hash=None,
                    unresolved_reason=failure.failure_reason,
                ),
                sections={
                    "readiness": {},
                    "coverage": {"blocked": True},
                    "mp_manifest": {
                        "formula_id": FORMULA_ID,
                        "implementation_source": IMPLEMENTATION_SOURCE,
                        "temporary_cross_repo_dependency": True,
                    },
                    "window_summary": window.to_dict(),
                    "price_phases": [],
                    "trade_flow": {},
                    "efficiency": {},
                    "important_levels": [],
                    "wall_lifecycles": [],
                    "reference_interaction": None,
                    "avr": {"status": "NOT_RUN_FAILED"},
                    "open_interest": {"status": "NOT_RUN_FAILED"},
                    "outcomes": [],
                    "minute_metrics": [],
                    "trade_sequence": [],
                    "trade_dedup": {},
                    "breakout_states": None,
                },
                data_quality={"failure": failure.to_dict(), "lock": lock_info},
            )
        result.sections["failure"] = failure.to_dict()
        result.data_quality["failure"] = failure.to_dict()
        session.abort_failed(
            result,
            log_lines=log + [f"FAILED:{failure.failure_reason}"],
            failure=failure,
            manifest_extra=manifest_extra,
        )
        return result

    try:
        if deps is None:
            raise RuntimeError(
                "STOP_XRAY_DEPS_REQUIRED: full analysis requires AnalysisDependencies ports "
                "(use build_live_analysis_dependencies after --execute-live, or test fakes)"
            )

        if not skip_execution_hold:
            if execute_live:
                from .execution_hold import assert_live_execution_allowed

                assert_live_execution_allowed(
                    execute_live=True, lock_path=lock_path
                )
            else:
                from .execution_hold import EXPLICIT_EXECUTION_REQUIRED

                raise RuntimeError(EXPLICIT_EXECUTION_REQUIRED)

        if sentinel is not None:
            sentinel.check("core_start")

        # Start must land on a 100ms bucket boundary
        try:
            assert_start_aligned_to_100ms(window.start_ns)
        except RuntimeError as exc:
            return _fail(
                None,
                expected_failure("start_alignment", str(exc).split(":", 1)[0]),
            )

        # ---- 1. Readiness (fail-closed) ----
        readiness = deps.readiness.assess_window(
            symbol=window.symbol, start_ns=window.start_ns, end_ns=window.end_ns
        )
        log.append(f"readiness={readiness.status}:{readiness.reason}")
        if not readiness.ready:
            baseline = BaselineBookState(
                source="not_loaded_not_ready",
                timestamp=None,
                age_ms=None,
                complete=False,
                hash=None,
                unresolved_reason="NOT_READY",
            )
            result = _shell_result(
                mode=mode,
                window=window,
                reference=reference,
                wall_threshold=wall_threshold,
                baseline=baseline,
                sections={
                    "readiness": readiness.to_dict(),
                    "coverage": {"blocked": True},
                    "mp_manifest": {
                        "formula_id": FORMULA_ID,
                        "implementation_source": IMPLEMENTATION_SOURCE,
                        "temporary_cross_repo_dependency": True,
                    },
                    "window_summary": window.to_dict(),
                    "price_phases": [],
                    "trade_flow": {},
                    "efficiency": {},
                    "important_levels": [],
                    "wall_lifecycles": [],
                    "reference_interaction": None,
                    "avr": {"status": "NOT_RUN_NOT_READY"},
                    "open_interest": {"status": "NOT_RUN_NOT_READY"},
                    "outcomes": [],
                    "minute_metrics": [],
                    "trade_sequence": [],
                    "trade_dedup": {},
                    "breakout_states": None,
                },
                data_quality={
                    "readiness": readiness.to_dict(),
                    "loaders_invoked": False,
                    "lock": lock_info,
                },
            )
            return _fail(
                result,
                expected_failure("readiness", readiness.reason or "NOT_READY"),
            )

        # Predecessor Silver bucket coverage (chunk boundary safe)
        pred_bucket = predecessor_bucket_start_ns(window.start_ns)
        pred_ready = deps.readiness.assess_window(
            symbol=window.symbol,
            start_ns=pred_bucket,
            end_ns=window.start_ns,
        )
        if not pred_ready.ready:
            return _fail(
                None,
                expected_failure(
                    "baseline_predecessor_coverage",
                    UNRESOLVED_BASELINE_MISSING_SILVER_HASH,
                ),
            )
        chunk_keys = merge_chunk_keys(readiness.chunk_keys, pred_ready.chunk_keys)
        coverage_meta = {
            "analysis_chunk_keys": list(readiness.chunk_keys),
            "predecessor_chunk_keys": list(pred_ready.chunk_keys),
            "merged_chunk_keys": list(chunk_keys),
            "predecessor_bucket_start_ns": pred_bucket,
            "baseline_bucket_end_ns": window.start_ns,
        }

        if sentinel is not None:
            sentinel.check("before_silver_metrics")

        mid_series = deps.metrics.load_mid_series(
            symbol=window.symbol,
            start_ns=window.start_ns,
            end_ns=window.end_ns,
            chunk_keys=chunk_keys,
        )
        minute_loader = getattr(deps.metrics, "load_minute_rows", None)
        if callable(minute_loader):
            minute_rows = minute_loader(
                symbol=window.symbol,
                start_ns=window.start_ns,
                end_ns=window.end_ns,
                chunk_keys=chunk_keys,
            )
        else:
            minute_rows = []

        if sentinel is not None:
            sentinel.check("before_public_trades")
        trades_raw = deps.trades.load_trades(
            symbol=window.symbol, start_ns=window.start_ns, end_ns=window.end_ns
        )
        trades_dedup, dedup_stats = dedup_trades_by_id(trades_raw)

        price_min, price_max = resolve_lc_price_band(
            reference_price=reference.price,
            local_band_usd=wall_threshold.local_band_usd,
            mid_series=mid_series,
        )
        assert_price_band_width(price_min, price_max)

        if sentinel is not None:
            sentinel.check("before_level_changes")
        level_changes = deps.level_changes.load_level_changes(
            symbol=window.symbol,
            start_ns=window.start_ns,
            end_ns=window.end_ns,
            chunk_keys=chunk_keys,
            price_min=price_min,
            price_max=price_max,
        )

        data_quality: dict[str, Any] = {
            "lock": lock_info,
            "readiness": readiness.to_dict(),
            "predecessor_readiness": pred_ready.to_dict(),
            "coverage": coverage_meta,
            "loaders_invoked": True,
            "mid_empty": len(mid_series) == 0,
            "trades_empty": len(trades_dedup) == 0,
            "level_changes_empty": len(level_changes) == 0,
            "dedup": dedup_stats.to_dict(),
            "lc_price_band": {"price_min": price_min, "price_max": price_max},
        }
        if data_quality["mid_empty"]:
            data_quality["mid_empty_reason"] = "SOURCE_EMPTY_OR_NO_COVERAGE"
        if data_quality["trades_empty"]:
            data_quality["trades_empty_reason"] = "SOURCE_EMPTY_OR_NO_COVERAGE"

        # ---- Baseline hash from Silver predecessor bucket ----
        if sentinel is not None:
            sentinel.check("before_silver_hash")
        silver_hash = deps.metrics.load_book_hash_at_bucket(
            symbol=window.symbol,
            bucket_start_ns=pred_bucket,
            chunk_keys=chunk_keys,
        )
        if silver_hash is None:
            return _fail(
                _shell_result(
                    mode=mode,
                    window=window,
                    reference=reference,
                    wall_threshold=wall_threshold,
                    baseline=BaselineBookState(
                        source="silver_hash_missing",
                        timestamp=None,
                        age_ms=None,
                        complete=False,
                        hash=None,
                        unresolved_reason=UNRESOLVED_BASELINE_MISSING_SILVER_HASH,
                    ),
                    sections={
                        "readiness": readiness.to_dict(),
                        "coverage": coverage_meta,
                        "mp_manifest": {
                            "formula_id": FORMULA_ID,
                            "implementation_source": IMPLEMENTATION_SOURCE,
                            "temporary_cross_repo_dependency": True,
                        },
                        "window_summary": window.to_dict(),
                        "price_phases": [],
                        "trade_flow": {},
                        "efficiency": {},
                        "important_levels": [],
                        "wall_lifecycles": [],
                        "reference_interaction": None,
                        "avr": {"status": "NOT_RUN_BASELINE_FAIL"},
                        "open_interest": {"status": "NOT_RUN_BASELINE_FAIL"},
                        "outcomes": [],
                        "minute_metrics": [],
                        "trade_sequence": [],
                        "trade_dedup": {},
                        "breakout_states": None,
                    },
                    data_quality={**data_quality, "baseline_complete": False},
                ),
                expected_failure(
                    "baseline_silver_hash", UNRESOLVED_BASELINE_MISSING_SILVER_HASH
                ),
            )

        if sentinel is not None:
            sentinel.check("before_bronze_replay")
        baseline = deps.baseline.load_baseline(
            symbol=window.symbol,
            start_ns=window.start_ns,
            epoch_id=readiness.epoch_id,
            expected_book_hash=silver_hash,
        )
        data_quality["baseline_complete"] = baseline.complete
        data_quality["silver_baseline_hash"] = silver_hash
        data_quality["bronze_baseline_hash"] = baseline.hash
        if not baseline.complete:
            reason = baseline.unresolved_reason or UNRESOLVED_BASELINE_HASH_MISMATCH
            return _fail(
                _shell_result(
                    mode=mode,
                    window=window,
                    reference=reference,
                    wall_threshold=wall_threshold,
                    baseline=baseline,
                    sections={
                        "readiness": readiness.to_dict(),
                        "coverage": coverage_meta,
                        "mp_manifest": {
                            "formula_id": FORMULA_ID,
                            "implementation_source": IMPLEMENTATION_SOURCE,
                            "temporary_cross_repo_dependency": True,
                        },
                        "window_summary": window.to_dict(),
                        "price_phases": [],
                        "trade_flow": {},
                        "efficiency": {},
                        "important_levels": [],
                        "wall_lifecycles": [],
                        "reference_interaction": None,
                        "avr": {"status": "NOT_RUN_BASELINE_FAIL"},
                        "open_interest": {"status": "NOT_RUN_BASELINE_FAIL"},
                        "outcomes": [],
                        "minute_metrics": minute_rows,
                        "trade_sequence": [t.to_dict() for t in trades_dedup],
                        "trade_dedup": dedup_stats.to_dict(),
                        "breakout_states": None,
                    },
                    data_quality=data_quality,
                ),
                expected_failure("baseline_replay", reason),
            )

        # ---- Walls (only after successful baseline) ----
        q95_start_ns = dt_to_ns(wall_threshold.window_start)
        q95_end_ns = dt_to_ns(wall_threshold.window_end)
        _, wall_lives, wall_meta = _build_walls(
            baseline=baseline,
            level_changes=level_changes,
            trades=trades_dedup,
            mid_series=mid_series,
            reference=reference,
            wall_threshold=wall_threshold,
            q95_start_ns=q95_start_ns,
            q95_end_ns=q95_end_ns,
        )

        breakout_states = None
        reference_interaction: dict[str, Any] | None = None
        sm_series = _mid_to_sm_series(mid_series)
        if reference.price is not None and reference.side is not None and sm_series:
            if reference.causal_reference or mode == RunMode.MANUAL_WINDOW:
                sm = run_breakout_state_machine(
                    side=reference.side,
                    reference_price=float(reference.price),
                    mid_series=sm_series,
                )
                breakout_states = sm.to_dict()
                if mode == RunMode.STRATEGY_EDGE and not reference.causal_reference:
                    breakout_states = {
                        "blocked": True,
                        "reason": "NON_CAUSAL_REFERENCE_NO_STRATEGY_VERDICT",
                        "forensic_states": sm.to_dict(),
                    }
                reference_interaction = {
                    "causal_reference": reference.causal_reference,
                    "reference": reference.to_dict(),
                    "state_machine": breakout_states,
                }

        event_time = window.start_utc
        if decision_time_utc is not None:
            event_time = decision_time_utc
        if breakout_states and isinstance(breakout_states, dict):
            trans = breakout_states.get("transitions") or []
            if trans:
                event_time = datetime.fromisoformat(
                    str(trans[-1]["at_utc"]).replace("Z", "+00:00")
                )
        side_str = None if reference.side is None else reference.side.value
        outcomes = [
            o.to_dict()
            for o in evaluate_outcomes(
                event_time=event_time,
                window_end=window.end_utc,
                mid_series=mid_series,
                reference_price=reference.price,
                side=side_str,
            )
        ]

        flow = flow_summary(trades_dedup)
        ranked = rank_rolling_by_abs_net(minute_rows)
        move = None
        if minute_rows:
            move = float(minute_rows[-1]["c"]) - float(minute_rows[0]["o"])
        elif mid_series:
            move = float(mid_series[-1].mid) - float(mid_series[0].mid)
        efficiency = {
            "price_move": move,
            "delta": flow.get("delta"),
            "price_move_per_delta_million": price_move_per_delta_million(
                move or 0.0, float(flow.get("delta") or 0.0)
            ),
        }

        avr = deps.avr.load_avr(
            symbol=window.symbol, start_ns=window.start_ns, end_ns=window.end_ns
        )
        oi = deps.open_interest.load_oi(
            symbol=window.symbol, start_ns=window.start_ns, end_ns=window.end_ns
        )

        sections = {
            "readiness": readiness.to_dict(),
            "coverage": {
                "outcomes": outcomes,
                "wall_meta": wall_meta,
                **coverage_meta,
            },
            "mp_manifest": {
                "formula_id": FORMULA_ID,
                "implementation_source": IMPLEMENTATION_SOURCE,
                "temporary_cross_repo_dependency": True,
            },
            "window_summary": {
                **window.to_dict(),
                "minute_count": len(minute_rows),
                "mid_count": len(mid_series),
                "top_300s_impulses": ranked[:5],
            },
            "price_phases": detect_neutral_phases(minute_rows),
            "trade_flow": flow,
            "efficiency": efficiency,
            "important_levels": detect_important_levels(minute_rows),
            "wall_lifecycles": wall_lives,
            "reference_interaction": reference_interaction,
            "avr": avr,
            "open_interest": oi,
            "outcomes": outcomes,
            "minute_metrics": minute_rows,
            "trade_sequence": [t.to_dict() for t in trades_dedup],
            "trade_dedup": dedup_stats.to_dict(),
            "breakout_states": breakout_states,
            "mid_series": [m.to_dict() for m in mid_series],
        }
        result = _shell_result(
            mode=mode,
            window=window,
            reference=reference,
            wall_threshold=wall_threshold,
            baseline=baseline,
            sections=sections,
            data_quality=data_quality,
        )

        def _pre_rename() -> None:
            if sentinel is not None:
                sentinel.check("before_final_rename")

        session.commit_complete(
            result,
            log_lines=log + ["CORE_COMPLETE"],
            pre_rename_check=_pre_rename,
            manifest_extra=manifest_extra,
        )
        return result
    except RuntimeError as exc:
        code = str(exc).split(":", 1)[0]
        if code.startswith("STOP_") or code.startswith("UNRESOLVED_") or code in {
            "OUTPUT_DIR_ALREADY_EXISTS",
            "FULL_RUN_BLOCKED_EXPLICIT_EXECUTION_REQUIRED",
            "FULL_RUN_BLOCKED_ACTIVE_SILVER_BUILDER",
        }:
            return _fail(None, expected_failure("runtime", code))
        return _fail(None, internal_failure("runtime", exc))
    except Exception as exc:  # noqa: BLE001 — outer fail-closed
        return _fail(None, internal_failure("runtime", exc))


def run_strategy_edge_analysis(
    config: StrategyEdgeConfig,
    *,
    deps: AnalysisDependencies | None = None,
    mp_profile: dict[str, Any] | None = None,
    lock_path: Path = DEFAULT_SILVER_LOCK,
    skip_execution_hold: bool = False,
    execute_live: bool = False,
    sentinel: ExecutionSentinel | None = None,
    resource_preflight: ResourcePreflightResult | None = None,
) -> XRayResult:
    start, end = strategy_analysis_bounds(
        config.decision_time_utc,
        pre_minutes=config.pre_window_minutes,
        post_minutes=config.post_window_minutes,
    )
    window = _window_from_bounds(
        symbol=config.symbol,
        start=start,
        end=end,
        chain_version=config.chain_version,
        chain_hash=config.chain_hash,
    )
    if config.reference_price is None:
        if mp_profile is None and deps is not None and not config.check_only:
            mp_profile = deps.market_profile.load_previous_closed_30m_tpo(
                symbol=config.symbol, decision_time=config.decision_time_utc
            )
        elif mp_profile is None and not config.check_only:
            from .execution_hold import assert_live_execution_allowed

            assert_live_execution_allowed(
                execute_live=execute_live, lock_path=lock_path
            )
            mp_profile = mp_adapter.load_strategy_tpo_edge_profile(
                symbol=config.symbol, decision_time=config.decision_time_utc
            )
        elif mp_profile is None and config.check_only:
            prev_start, prev_end = mp_adapter.previous_closed_30m_bounds(
                config.decision_time_utc
            )
            mp_profile = mp_adapter.profile_from_fixture(
                window_start=prev_start,
                window_end=prev_end,
                vah=0.0,
                val=0.0,
            )
    reference = resolve_strategy_reference(
        decision_time_utc=config.decision_time_utc,
        edge_side=config.edge_side,
        reference_price=config.reference_price,
        reference_known_as_of_utc=config.reference_known_as_of_utc,
        mp_profile=mp_profile,
    )
    thr = wall_threshold_for_strategy(
        decision_time=config.decision_time_utc,
        pre_window_minutes=config.pre_window_minutes,
        local_band_usd=config.local_band_usd,
        quantile=config.wall_quantile,
        max_rank=config.wall_max_rank,
    )
    wall_spec = WallThresholdSpec(**thr)
    return run_xray_core(
        mode=RunMode.STRATEGY_EDGE,
        window=window,
        reference=reference,
        wall_threshold=wall_spec,
        output_dir=Path(config.output_dir),
        check_only=config.check_only,
        deps=deps,
        decision_time_utc=config.decision_time_utc,
        lock_path=lock_path,
        skip_execution_hold=skip_execution_hold,
        execute_live=execute_live,
        sentinel=sentinel,
        resource_preflight=resource_preflight,
    )


def run_manual_window_analysis(
    config: ManualWindowConfig,
    *,
    deps: AnalysisDependencies | None = None,
    lock_path: Path = DEFAULT_SILVER_LOCK,
    skip_execution_hold: bool = False,
    execute_live: bool = False,
    sentinel: ExecutionSentinel | None = None,
    resource_preflight: ResourcePreflightResult | None = None,
) -> XRayResult:
    window = _window_from_bounds(
        symbol=config.symbol,
        start=config.start_utc,
        end=config.end_utc,
        chain_version=config.chain_version,
        chain_hash=config.chain_hash,
    )
    reference = resolve_manual_reference(
        analysis_start=config.start_utc,
        reference_price=config.reference_price,
        reference_side=config.reference_side,
        reference_known_as_of_utc=config.reference_known_as_of_utc,
    )
    thr = wall_threshold_for_manual(
        start=config.start_utc,
        end=config.end_utc,
        local_band_usd=config.local_band_usd,
        quantile=config.wall_quantile,
        max_rank=config.wall_max_rank,
    )
    wall_spec = WallThresholdSpec(**thr)
    return run_xray_core(
        mode=RunMode.MANUAL_WINDOW,
        window=window,
        reference=reference,
        wall_threshold=wall_spec,
        output_dir=Path(config.output_dir),
        check_only=config.check_only,
        deps=deps,
        lock_path=lock_path,
        skip_execution_hold=skip_execution_hold,
        execute_live=execute_live,
        sentinel=sentinel,
        resource_preflight=resource_preflight,
    )

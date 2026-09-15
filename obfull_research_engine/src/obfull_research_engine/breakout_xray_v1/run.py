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
from .execution_hold import DEFAULT_SILVER_LOCK, assert_full_run_allowed, assert_lock_untouched
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
from .states import run_breakout_state_machine
from .time_windows import (
    assert_half_open,
    dt_to_ns,
    ns_to_dt,
    rank_rolling_by_abs_net,
    strategy_analysis_bounds,
)
from .trades import flow_summary, price_move_per_delta_million
from .walls import (
    accumulate_level_sizes,
    classify_wall_lifecycle,
    level_events_for_wall,
    select_walls_by_notional,
)
from .writers import write_xray_artifacts


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
    # Neighbor map for relocation
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
        # Relocation: neighbor grew while this vanished
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
            # Insufficient temporal association → UNRESOLVED
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
) -> XRayResult:
    log: list[str] = [f"{PACKAGE_VERSION} mode={mode.value}"]
    lock_info = assert_lock_untouched(lock_path)
    log.append(f"lock_probe={lock_info}")

    if check_only:
        baseline = BaselineBookState(
            source="check_only_skipped",
            timestamp=None,
            age_ms=None,
            complete=False,
            hash=None,
            unresolved_reason="CHECK_ONLY",
        )
        result = XRayResult(
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

    if deps is None:
        raise RuntimeError(
            "STOP_XRAY_DEPS_REQUIRED: full analysis requires AnalysisDependencies ports"
        )

    if not skip_execution_hold:
        # Live adapters call assert again; fixture tests pass inactive lock.
        assert_full_run_allowed(lock_path)

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
        result = XRayResult(
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
        write_xray_artifacts(output_dir, result, log_lines=log + ["NOT_READY_ABORT"])
        return result

    chunk_keys = readiness.chunk_keys

    # ---- 2–4 Mid / Trades / Level changes ----
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

    trades_dedup, dedup_stats = deps.trades.load_trades(
        symbol=window.symbol, start_ns=window.start_ns, end_ns=window.end_ns
    )
    level_changes = deps.level_changes.load_level_changes(
        symbol=window.symbol,
        start_ns=window.start_ns,
        end_ns=window.end_ns,
        chunk_keys=chunk_keys,
    )

    data_quality: dict[str, Any] = {
        "lock": lock_info,
        "readiness": readiness.to_dict(),
        "loaders_invoked": True,
        "mid_empty": len(mid_series) == 0,
        "trades_empty": len(trades_dedup) == 0,
        "level_changes_empty": len(level_changes) == 0,
        "dedup": dedup_stats.to_dict(),
    }
    if data_quality["mid_empty"]:
        data_quality["mid_empty_reason"] = "SOURCE_EMPTY_OR_NO_COVERAGE"
    if data_quality["trades_empty"]:
        data_quality["trades_empty_reason"] = "SOURCE_EMPTY_OR_NO_COVERAGE"

    # ---- 5 Baseline ----
    expected_hash = None
    if mid_series:
        # Prefer first in-window mid book hash as silver parity check when present
        expected_hash = mid_series[0].book_hash or None
        if expected_hash == "":
            expected_hash = None
    baseline = deps.baseline.load_baseline(
        symbol=window.symbol,
        start_ns=window.start_ns,
        epoch_id=readiness.epoch_id,
        expected_book_hash=expected_hash,
    )
    data_quality["baseline_complete"] = baseline.complete

    # ---- 6–8 Walls ----
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

    # ---- Reference SM ----
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

    # ---- Outcomes (no reload outside ready window) ----
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
        "coverage": {"outcomes": outcomes, "wall_meta": wall_meta},
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
    result = XRayResult(
        mode=mode,
        window=window,
        reference=reference,
        wall_threshold=wall_threshold,
        baseline=baseline,
        sections=sections,
        data_quality=data_quality,
    )
    write_xray_artifacts(output_dir, result, log_lines=log + ["CORE_COMPLETE"])
    return result


def run_strategy_edge_analysis(
    config: StrategyEdgeConfig,
    *,
    deps: AnalysisDependencies | None = None,
    mp_profile: dict[str, Any] | None = None,
    lock_path: Path = DEFAULT_SILVER_LOCK,
    skip_execution_hold: bool = False,
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
            assert_full_run_allowed(lock_path)
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
    )


def run_manual_window_analysis(
    config: ManualWindowConfig,
    *,
    deps: AnalysisDependencies | None = None,
    lock_path: Path = DEFAULT_SILVER_LOCK,
    skip_execution_hold: bool = False,
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
    )

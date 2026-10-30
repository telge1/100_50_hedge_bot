"""Touch / recovery with configurable selector and same-bar recovery (Phase 2D/2J)."""

from __future__ import annotations

from typing import Any, Callable

from long_lineage_lifecycle_v2j_v1 import (
    materialize_support_v2e_for_region,
    record_structure_change,
)
from long_research_config_v2d_v1 import LongResearchConfigV2DV1
from long_support_selection_v1 import SelectedSupportV1, region_known_in_snapshot
from long_support_selection_v2d_v1 import SelectedSupportV2DV1, v2d_as_v1_compat
from long_support_selection_v2e_v1 import region_pool_height_pct
from long_touch_recovery_v1 import TouchRecoveryTrackerV1, _append


SelectorFn = Callable[..., tuple[SelectedSupportV2DV1 | None, str | None]]


def _clear_touch(tracker: TouchRecoveryTrackerV1) -> None:
    tracker.phase = "IDLE"
    tracker.frozen_support = None
    tracker.touch_bar = None
    tracker.frozen_v2d = None
    tracker.frozen_lineage_id = None
    tracker.frozen_origin_ids = None


def _origin_ids(sel_v2: SelectedSupportV2DV1) -> str:
    return "|".join(sel_v2.region.distinct_origin_ids)


def _lineage_active_at_touch_close(
    *,
    config: LongResearchConfigV2DV1,
    session: Any,
    bar: str,
    lineage_id: str | None,
    frozen_origin_ids: str,
    lineage_tracker: Any | None,
) -> tuple[bool, Any | None, str | None]:
    if config.touch_lifecycle != "LINEAGE_AWARE_V2J" or not lineage_id:
        return False, None, "NOT_LINEAGE_MODE"
    from long_lineage_lifecycle_v2j_v1 import lineage_region_active_at_close

    if config.v2e_yaml is None:
        return False, None, "NO_YAML"
    ok, reason, region = lineage_region_active_at_close(
        session,
        decision_close_utc=bar,
        lineage_id=lineage_id,
        frozen_origin_ids=frozen_origin_ids,
        lineage_tracker=lineage_tracker,
        yaml=config.v2e_yaml,
    )
    return ok, region, reason


def _validate_frozen(
    validate_fn: Callable[..., tuple[bool, str | None]],
    *,
    tracker: TouchRecoveryTrackerV1,
    config: LongResearchConfigV2DV1,
    snap_now: dict[str, Any],
    bar: str,
    close: float,
    persistence_streak: dict[str, int] | None,
    session: Any | None,
    prices: dict[str, dict[str, float]] | None,
    streak_cache: dict | None,
    bar_index: int | None,
    closes: list[str] | None,
) -> tuple[bool, str | None]:
    fs = tracker.frozen_support
    assert fs is not None
    return validate_fn(
        snap_now,
        region_lower=fs.region.lower_edge,
        region_upper=fs.region.upper_edge,
        config=config,
        persistence_bars_by_pool_key=persistence_streak,
        session=session,
        decision_close_utc=bar,
        market_close=close,
        prices=prices,
        streak_cache=streak_cache,
        bar_index=bar_index,
        closes=closes,
        frozen_lineage_id=tracker.frozen_lineage_id,
        touch_lifecycle=config.touch_lifecycle,
    )


def _refresh_v2d_after_recovery(
    *,
    tracker: TouchRecoveryTrackerV1,
    config: LongResearchConfigV2DV1,
    session: Any,
    bar: str,
    close: float,
    lineage_tracker: Any | None,
    prices: dict[str, dict[str, float]] | None,
    streak_cache: dict | None,
    bar_index: int | None,
    closes: list[str] | None,
    structure_audits: list[dict[str, Any]] | None,
) -> SelectedSupportV2DV1 | None:
    if config.touch_lifecycle != "LINEAGE_AWARE_V2J" or not tracker.frozen_lineage_id:
        return tracker.frozen_v2d if isinstance(tracker.frozen_v2d, SelectedSupportV2DV1) else None
    ok, region, _reason = _lineage_active_at_touch_close(
        config=config,
        session=session,
        bar=bar,
        lineage_id=tracker.frozen_lineage_id,
        frozen_origin_ids=tracker.frozen_origin_ids or "",
        lineage_tracker=lineage_tracker,
    )
    if not ok or region is None or config.v2e_yaml is None or prices is None:
        return None
    old_v2 = tracker.frozen_v2d
    old_key = old_v2.region_key if isinstance(old_v2, SelectedSupportV2DV1) else None
    new_key = f"{region.lower_edge}|{region.upper_edge}"
    if old_key and old_key != new_key:
        record_structure_change(
            structure_audits,
            bar=bar,
            phase="RECOVERY",
            old_key=old_key,
            new_key=new_key,
            lineage_id=tracker.frozen_lineage_id,
            old_height_pct=region_pool_height_pct(old_v2.region) if isinstance(old_v2, SelectedSupportV2DV1) else None,
            new_height_pct=region_pool_height_pct(region),
            touch_phase=tracker.phase,
            pending_order_id=None,
            decision="CONTINUE",
            reason="LINEAGE_UNION_GROWTH_REQUALIFY",
        )
    sel, err = materialize_support_v2e_for_region(
        session,
        region=region,
        decision_close_utc=bar,
        market_close=close,
        prices=prices,
        yaml=config.v2e_yaml,
        sl_mode=config.sl_mode,
        live_consecutive_streak=streak_cache,
        bar_index=bar_index,
        closes=closes,
        pool_age_semantics=config.pool_age_semantics,
        lineage_tracker=lineage_tracker,
        formation_semantics=config.formation_semantics,
    )
    if sel is None:
        _append(tracker, "RECOVERY_MATERIALIZE_FAILED", bar=bar, reason=err)
        return None
    tracker.frozen_v2d = sel
    return sel


def process_touch_recovery_v2d_at_close(
    *,
    bar: str,
    prev_bar: str | None,
    snap_prev: dict[str, Any] | None,
    snap_now: dict[str, Any],
    close_prev: float | None,
    ohlc: dict[str, float],
    ema_authorized: bool,
    pool_history_ok: bool,
    tracker: TouchRecoveryTrackerV1,
    config: LongResearchConfigV2DV1,
    select_fn: SelectorFn,
    validate_fn: Callable[..., tuple[bool, str | None]],
    persistence_streak: dict[str, int] | None = None,
    session: Any | None = None,
    prices: dict[str, dict[str, float]] | None = None,
    streak_cache: dict | None = None,
    bar_index: int | None = None,
    closes: list[str] | None = None,
    lineage_tracker: Any | None = None,
    structure_audits: list[dict[str, Any]] | None = None,
) -> tuple[TouchRecoveryTrackerV1, SelectedSupportV1 | None]:
    if not pool_history_ok or not ema_authorized:
        if tracker.phase != "IDLE":
            _append(tracker, "RESET_CONTEXT_LOST", bar=bar)
            _clear_touch(tracker)
        return tracker, None

    close = float(ohlc["c"])
    low = float(ohlc["l"])

    def _confirm_recovery(fs: SelectedSupportV1) -> SelectedSupportV1 | None:
        ok, reason = _validate_frozen(
            validate_fn,
            tracker=tracker,
            config=config,
            snap_now=snap_now,
            bar=bar,
            close=close,
            persistence_streak=persistence_streak,
            session=session,
            prices=prices,
            streak_cache=streak_cache,
            bar_index=bar_index,
            closes=closes,
        )
        if close <= fs.region.upper_edge + 1e-12:
            return None
        if not ok:
            _append(tracker, "TOUCH_ABORT_INVALID", bar=bar, reason=reason)
            _clear_touch(tracker)
            return None
        refreshed = _refresh_v2d_after_recovery(
            tracker=tracker,
            config=config,
            session=session,
            bar=bar,
            close=close,
            lineage_tracker=lineage_tracker,
            prices=prices,
            streak_cache=streak_cache,
            bar_index=bar_index,
            closes=closes,
            structure_audits=structure_audits,
        )
        if refreshed is None and config.touch_lifecycle == "LINEAGE_AWARE_V2J":
            _append(tracker, "TOUCH_ABORT_INVALID", bar=bar, reason="RECOVERY_REQUALIFY_FAILED")
            _clear_touch(tracker)
            return None
        _append(tracker, "RECOVERY_CONFIRMED", bar=bar, close=close, recovery_mode=config.recovery_mode)
        tracker.phase = "RECOVERED"
        return fs

    if tracker.phase == "TOUCHED" and tracker.frozen_support is not None:
        fs = tracker.frozen_support
        if close < fs.region.lower_edge - 1e-12:
            _append(tracker, "TOUCH_ABORT_BREAK", bar=bar, close=close)
            _clear_touch(tracker)
            return tracker, None
        if config.recovery_mode == "NEXT_BAR":
            ok, reason = _validate_frozen(
                validate_fn,
                tracker=tracker,
                config=config,
                snap_now=snap_now,
                bar=bar,
                close=close,
                persistence_streak=persistence_streak,
                session=session,
                prices=prices,
                streak_cache=streak_cache,
                bar_index=bar_index,
                closes=closes,
            )
            if not ok:
                _append(tracker, "TOUCH_ABORT_INVALID", bar=bar, reason=reason)
                _clear_touch(tracker)
                return tracker, None
            if close > fs.region.upper_edge + 1e-12:
                refreshed = _refresh_v2d_after_recovery(
                    tracker=tracker,
                    config=config,
                    session=session,
                    bar=bar,
                    close=close,
                    lineage_tracker=lineage_tracker,
                    prices=prices,
                    streak_cache=streak_cache,
                    bar_index=bar_index,
                    closes=closes,
                    structure_audits=structure_audits,
                )
                if refreshed is None and config.touch_lifecycle == "LINEAGE_AWARE_V2J":
                    _append(tracker, "TOUCH_ABORT_INVALID", bar=bar, reason="RECOVERY_REQUALIFY_FAILED")
                    _clear_touch(tracker)
                    return tracker, None
                _append(tracker, "RECOVERY_CONFIRMED", bar=bar, close=close, recovery_mode=config.recovery_mode)
                tracker.phase = "RECOVERED"
                return tracker, fs
            return tracker, None

    if tracker.phase == "RECOVERED":
        return tracker, None

    if prev_bar is None or snap_prev is None or close_prev is None:
        return tracker, None

    sel_v2, reason = select_fn(
        snap_prev,
        market_price=close_prev,
        config=config,
        persistence_bars_by_pool_key=persistence_streak,
        session=session,
        decision_close_utc=prev_bar,
        prices=prices,
        streak_cache=streak_cache,
        bar_index=(bar_index - 1) if bar_index is not None else None,
        closes=closes,
    )
    if sel_v2 is None:
        return tracker, None
    sel = v2d_as_v1_compat(sel_v2)
    if not region_known_in_snapshot(sel.region, snap_prev):
        _append(tracker, "TOUCH_REJECTED_POOL_NOT_KNOWN_PREV", bar=bar, reason=reason)
        return tracker, None

    if low <= sel.region.upper_edge + 1e-12:
        active_ok = region_known_in_snapshot(sel.region, snap_now)
        lineage_id = getattr(sel_v2, "lineage_id", None)
        if config.touch_lifecycle == "LINEAGE_AWARE_V2J" and lineage_id:
            active_ok, _, lin_reason = _lineage_active_at_touch_close(
                config=config,
                session=session,
                bar=bar,
                lineage_id=lineage_id,
                frozen_origin_ids=_origin_ids(sel_v2),
                lineage_tracker=lineage_tracker,
            )
            if not active_ok:
                _append(tracker, "TOUCH_REJECTED_NOT_ACTIVE_AT_CLOSE", bar=bar, reason=lin_reason)
                return tracker, None
            if not region_known_in_snapshot(sel.region, snap_now):
                record_structure_change(
                    structure_audits,
                    bar=bar,
                    phase="TOUCH",
                    old_key=sel_v2.region_key,
                    new_key=None,
                    lineage_id=lineage_id,
                    old_height_pct=region_pool_height_pct(sel_v2.region),
                    new_height_pct=None,
                    touch_phase="TOUCH_BEGIN",
                    pending_order_id=None,
                    decision="CONTINUE",
                    reason="UNION_KEY_CHANGED_LINEAGE_OK",
                )
        elif not active_ok:
            _append(tracker, "TOUCH_REJECTED_NOT_ACTIVE_AT_CLOSE", bar=bar)
            return tracker, None

        tracker.phase = "TOUCHED"
        tracker.touch_bar = bar
        tracker.frozen_support = sel
        tracker.frozen_v2d = sel_v2
        tracker.frozen_lineage_id = lineage_id
        tracker.frozen_origin_ids = _origin_ids(sel_v2)
        _append(
            tracker,
            "TOUCH_BEGIN",
            bar=bar,
            pool_upper=sel.region.upper_edge,
            pool_lower=sel.region.lower_edge,
            lineage_id=lineage_id,
            sl_pool_upper=sel_v2.sl_region.upper_edge if isinstance(sel_v2, SelectedSupportV2DV1) else None,
            as_of_prev=prev_bar,
        )
        if config.recovery_mode == "SAME_BAR_TOUCH_CLOSE":
            recovered = _confirm_recovery(sel)
            if recovered is not None:
                return tracker, recovered

    return tracker, None

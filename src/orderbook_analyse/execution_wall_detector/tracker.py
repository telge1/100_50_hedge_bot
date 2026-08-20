"""Execution-wall sequence tracking and lifecycle transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from orderbook_analyse.execution_wall_detector.types import (
    ExecutionState,
    ExecutionWallParams,
    ExecutionWallSequence,
    LocalLevelMetrics,
    WallScope,
    WallType,
)


@dataclass
class _Active:
    seq: ExecutionWallSequence
    last_bucket_price: float
    last_qty: float
    last_distance_bps: float
    last_ts: datetime
    touched: bool = False
    qty_at_touch: float | None = None
    aggressive_trade_qty: float = 0.0
    visible_consumed: float = 0.0
    progress_bps_during_attack: float = 0.0
    rejection_bps_after_attack: float = 0.0
    attack_start: datetime | None = None
    attack_mid_at_start: float | None = None


class ExecutionWallTracker:
    def __init__(self, *, symbol: str, params: ExecutionWallParams, tick: float) -> None:
        self.symbol = symbol
        self.params = params
        self.tick = tick
        self.active: dict[str, _Active] = {}
        self.completed: list[ExecutionWallSequence] = []
        self.candidate_rows: list[dict[str, Any]] = []
        self.transition_rows: list[dict[str, Any]] = []
        self.trade_interaction_rows: list[dict[str, Any]] = []
        self.candidate_writer: Any = None
        self.transition_writer: Any = None
        self.candidate_count: int = 0
        self._counters = {"ask": 0, "bid": 0}
        self._segment = "S0001"

    def attach_writers(self, *, candidate_writer: Any = None, transition_writer: Any = None) -> None:
        self.candidate_writer = candidate_writer
        self.transition_writer = transition_writer

    def set_segment(self, segment_id: str) -> None:
        self._segment = segment_id

    def _new_id(self, side: str) -> str:
        self._counters[side] = self._counters.get(side, 0) + 1
        return f"{self.symbol}:EX:{self._segment}:{side.upper()}:EW{self._counters[side]:06d}"

    def _key(self, side: str, bucket_price: float) -> str:
        return f"{side}|{round(bucket_price, 10)}"

    def _emit(
        self,
        seq: ExecutionWallSequence,
        *,
        ts: datetime,
        state: str,
        details: str = "",
        force: bool = False,
    ) -> None:
        if (
            not force
            and seq.transitions
            and seq.transitions[-1].get("transition_type") == state
            and state
            in {
                ExecutionState.PERSISTED.value,
                ExecutionState.MOVED_TOWARD_MARKET.value,
                ExecutionState.MOVED_AWAY_FROM_MARKET.value,
            }
        ):
            return
        seq.terminal_state = state
        row = {
            "wall_sequence_id": seq.wall_sequence_id,
            "transition_ts": ts.isoformat(),
            "side": seq.side,
            "transition_type": state,
            "price": seq.representative_price,
            "qty": seq.last_qty,
            "details": details,
        }
        seq.transitions.append(row)
        if len(seq.transitions) > 40:
            seq.transitions = seq.transitions[-40:]
        if self.transition_writer is not None:
            try:
                self.transition_writer(row)
            except ValueError:
                # Stream already closed (finalize after partial failure) — keep in memory.
                if len(self.transition_rows) < 50_000:
                    self.transition_rows.append(row)
        elif len(self.transition_rows) < 500_000:
            self.transition_rows.append(row)

    def on_sample(
        self,
        *,
        ts: datetime,
        mid: float,
        metrics: Sequence[LocalLevelMetrics],
        sample_interval_ms: float,
    ) -> None:
        seen_keys: set[str] = set()
        for m in metrics:
            if not m.is_candidate:
                continue
            row = {
                    "sample_ts": ts.isoformat(),
                    "symbol": self.symbol,
                    "side": m.side,
                    "price": m.price,
                    "bucket_price": m.bucket_price,
                    "level_qty": m.level_qty,
                    "level_notional": m.level_notional,
                    "distance_bps": m.distance_bps,
                    "same_side_near_depth": m.same_side_near_depth,
                    "same_side_local_median_qty": m.same_side_local_median_qty,
                    "same_side_local_mean_qty": m.same_side_local_mean_qty,
                    "same_side_local_percentile": m.same_side_local_percentile,
                    "opposite_side_near_depth": m.opposite_side_near_depth,
                    "local_depth_share": m.local_depth_share,
                    "local_multiple": m.local_multiple,
                    "book_imbalance_near": m.book_imbalance_near,
                    "level_rank_within_near_band": m.level_rank_within_near_band,
                    "band_label": m.band_label,
                    "wall_type": WallType.EXECUTION_WALL.value,
                    "wall_scope": WallScope.EXECUTION.value,
                }
            self.candidate_count += 1
            if self.candidate_writer is not None:
                self.candidate_writer(row)
            elif len(self.candidate_rows) < 200_000:
                self.candidate_rows.append(row)
            key = self._key(m.side, m.bucket_price)
            seen_keys.add(key)
            act = self.active.get(key)
            if act is None:
                act = self._try_rematch(m)
            if act is None:
                sid = self._new_id(m.side)
                seq = ExecutionWallSequence(
                    wall_sequence_id=sid,
                    symbol=self.symbol,
                    side=m.side,
                    representative_price=m.bucket_price,
                    price_min=m.price,
                    price_max=m.price,
                    first_seen=ts,
                    last_active=ts,
                    initial_qty=m.level_qty,
                    peak_qty=m.level_qty,
                    last_qty=m.level_qty,
                    min_distance_bps=m.distance_bps,
                    max_distance_bps=m.distance_bps,
                    local_multiple_peak=m.local_multiple,
                    local_percentile_peak=m.same_side_local_percentile,
                    sample_count=1,
                )
                act = _Active(
                    seq=seq,
                    last_bucket_price=m.bucket_price,
                    last_qty=m.level_qty,
                    last_distance_bps=m.distance_bps,
                    last_ts=ts,
                )
                self.active[key] = act
                self._emit(seq, ts=ts, state=ExecutionState.APPEARED.value, force=True)
                continue

            seq = act.seq
            # Rematch may have moved the active entry under a different key.
            old_key = self._key(seq.side, act.last_bucket_price)
            seq.last_active = ts
            seq.sample_count += 1
            seq.price_min = min(seq.price_min, m.price)
            seq.price_max = max(seq.price_max, m.price)
            seq.representative_price = m.bucket_price
            seq.min_distance_bps = (
                m.distance_bps
                if seq.min_distance_bps is None
                else min(seq.min_distance_bps, m.distance_bps)
            )
            seq.max_distance_bps = (
                m.distance_bps
                if seq.max_distance_bps is None
                else max(seq.max_distance_bps, m.distance_bps)
            )
            seq.local_multiple_peak = max(seq.local_multiple_peak, m.local_multiple)
            seq.local_percentile_peak = max(
                seq.local_percentile_peak, m.same_side_local_percentile
            )
            if m.distance_bps <= self.params.max_distance_bps:
                seq.time_near_market_ms += sample_interval_ms

            # Book-distance touch (mid/BBO proximity), independent of trades.
            if m.distance_bps <= self.params.touch_bps and not act.touched:
                act.touched = True
                act.qty_at_touch = act.last_qty
                seq.touch_time = ts
                seq.touch_status = "TOUCHED"
                self._emit(seq, ts=ts, state=ExecutionState.TOUCHED.value, force=True)

            if m.level_qty > act.last_qty * 1.05:
                grew = m.level_qty - act.last_qty
                seq.refilled_qty += grew
                seq.refill_count += 1
                self._emit(seq, ts=ts, state=ExecutionState.GREW.value, force=True)
                if act.touched:
                    self._emit(seq, ts=ts, state=ExecutionState.REFILLED.value, force=True)
            elif m.level_qty < act.last_qty * 0.95:
                removed = act.last_qty - m.level_qty
                seq.unexplained_removed_qty += removed
                self._emit(seq, ts=ts, state=ExecutionState.SHRANK.value, force=True)
            else:
                self._emit(seq, ts=ts, state=ExecutionState.PERSISTED.value)

            if m.distance_bps + 1e-9 < act.last_distance_bps:
                self._emit(seq, ts=ts, state=ExecutionState.MOVED_TOWARD_MARKET.value)
            elif m.distance_bps > act.last_distance_bps + 1e-9:
                self._emit(seq, ts=ts, state=ExecutionState.MOVED_AWAY_FROM_MARKET.value)

            seq.peak_qty = max(seq.peak_qty, m.level_qty)
            seq.last_qty = m.level_qty
            act.last_qty = m.level_qty
            act.last_distance_bps = m.distance_bps
            act.last_bucket_price = m.bucket_price
            act.last_ts = ts

            new_key = self._key(m.side, m.bucket_price)
            if old_key in self.active and old_key != new_key:
                self.active.pop(old_key, None)
            self.active[new_key] = act
            seen_keys.add(new_key)

        for key in list(self.active.keys()):
            if key in seen_keys:
                continue
            act = self.active.pop(key)
            self._close_sequence(act, ts=ts)

    def _try_rematch(self, m: LocalLevelMetrics) -> _Active | None:
        tol = self.params.match_price_ticks * self.tick
        qty_tol = self.params.match_qty_tolerance_pct / 100.0
        best: _Active | None = None
        best_d: float | None = None
        for act in self.active.values():
            if act.seq.side != m.side:
                continue
            d = abs(act.last_bucket_price - m.bucket_price)
            if d > tol:
                continue
            # Reject rematch when qty jumped wildly (likely different wall).
            base = max(act.last_qty, 1e-9)
            if abs(m.level_qty - act.last_qty) / base > max(qty_tol, 0.5) and d > 0:
                continue
            if best_d is None or d < best_d:
                best_d = d
                best = act
        return best

    def mark_touch(self, *, side: str, bucket_price: float, ts: datetime) -> _Active | None:
        act = self.active.get(self._key(side, bucket_price)) or self._find_near(
            side, bucket_price
        )
        if act is None:
            return None
        if not act.touched:
            act.touched = True
            act.qty_at_touch = act.last_qty
            act.seq.touch_time = ts
            act.seq.touch_status = "TOUCHED"
            self._emit(act.seq, ts=ts, state=ExecutionState.TOUCHED.value, force=True)
        return act

    def _find_near(self, side: str, bucket_price: float) -> _Active | None:
        tol = self.params.match_price_ticks * self.tick
        for act in self.active.values():
            if act.seq.side == side and abs(act.last_bucket_price - bucket_price) <= tol:
                return act
        return None

    def apply_trade_hit(
        self,
        *,
        side: str,
        price: float,
        qty: float,
        ts: datetime,
        mid: float | None = None,
        alignment_status: str = "OK",
    ) -> None:
        """Aggressive trade hitting wall side (Buy vs ask / Sell vs bid)."""
        tick_tol = max(self.tick * 2, abs(price) * self.params.touch_bps / 10_000.0)
        for act in list(self.active.values()):
            if act.seq.side != side:
                continue
            lo = act.seq.price_min - tick_tol
            hi = act.seq.price_max + tick_tol
            # Ask: aggressive buys at/above wall band; Bid: sells at/below.
            if side == "ask":
                hit = price + 1e-15 >= lo and price <= hi + abs(price) * self.params.break_bps / 10_000.0
            else:
                hit = price - 1e-15 <= hi and price >= lo - abs(price) * self.params.break_bps / 10_000.0
            if not hit and abs(price - act.seq.representative_price) > tick_tol:
                continue
            self.mark_touch(side=side, bucket_price=act.last_bucket_price, ts=ts)
            act.aggressive_trade_qty += qty
            act.seq.executed_qty_estimate += qty
            act.seq.unexplained_removed_qty = max(
                0.0, act.seq.unexplained_removed_qty - qty
            )
            act.visible_consumed += qty
            if act.attack_start is None:
                act.attack_start = ts
                act.attack_mid_at_start = mid
            if alignment_status == "AMBIGUOUS":
                act.seq.execution_alignment_status = "AMBIGUOUS"
            self.trade_interaction_rows.append(
                {
                    "wall_sequence_id": act.seq.wall_sequence_id,
                    "trade_ts": ts.isoformat(),
                    "side": side,
                    "trade_price": price,
                    "trade_qty": qty,
                    "wall_price": act.seq.representative_price,
                    "execution_alignment_status": act.seq.execution_alignment_status,
                    "note": "aggressive_trade_vs_wall_estimate; no order identity claimed",
                }
            )
            self._emit(
                act.seq, ts=ts, state=ExecutionState.PARTIALLY_EXECUTED.value, force=True
            )
            if act.seq.executed_qty_estimate >= 0.8 * max(act.seq.peak_qty, 1e-9):
                self._emit(act.seq, ts=ts, state=ExecutionState.CONSUMED.value, force=True)

    def update_attack_progress(self, *, mid: float) -> None:
        for act in self.active.values():
            if not act.touched or act.attack_mid_at_start is None or act.attack_mid_at_start <= 0:
                continue
            if act.seq.side == "ask":
                progress = (mid - act.attack_mid_at_start) / act.attack_mid_at_start * 10_000.0
            else:
                progress = (act.attack_mid_at_start - mid) / act.attack_mid_at_start * 10_000.0
            act.progress_bps_during_attack = max(act.progress_bps_during_attack, progress)
            if progress < 0:
                act.rejection_bps_after_attack = max(
                    act.rejection_bps_after_attack, -progress
                )

    def finalize_open(self, ts: datetime) -> None:
        for key in list(self.active.keys()):
            act = self.active.pop(key)
            self._close_sequence(act, ts=ts, evaluate=True)

    def _close_sequence(self, act: _Active, *, ts: datetime, evaluate: bool = True) -> None:
        seq = act.seq
        if seq.first_seen is None:
            return
        life = (ts - seq.first_seen).total_seconds() * 1000.0
        seq.lifetime_ms = life
        seq.disappeared_at = ts
        if life < self.params.min_lifetime_ms and not act.touched:
            return
        if not act.touched and seq.unexplained_removed_qty > 0:
            seq.pulled_before_touch = True
            seq.cancelled_or_pulled_qty_estimate = seq.unexplained_removed_qty
            self._emit(
                seq, ts=ts, state=ExecutionState.PULLED_BEFORE_TOUCH.value, force=True
            )
        self._emit(seq, ts=ts, state=ExecutionState.DISAPPEARED.value, force=True)
        seq.touch_status = "TOUCHED" if act.touched else "UNTOUCHED"
        if evaluate:
            self._evaluate_absorption(act)
        self.completed.append(seq)

    def _evaluate_absorption(self, act: _Active) -> None:
        seq = act.seq
        if not act.touched:
            return
        peak = max(seq.peak_qty, 1e-9)
        exec_ratio = seq.executed_qty_estimate / peak
        refill_ratio = seq.refilled_qty / peak
        if (
            exec_ratio >= self.params.absorption_exec_to_peak_min
            and refill_ratio >= self.params.absorption_min_refill_ratio
            and act.progress_bps_during_attack <= self.params.absorption_max_progress_bps
            and not seq.breakout_accepted
            and act.rejection_bps_after_attack >= 0.0
        ):
            seq.absorption_candidate = True
            ref_ts = seq.last_active or seq.first_seen
            if ref_ts is not None:
                self._emit(
                    seq, ts=ref_ts, state=ExecutionState.ABSORBING.value, force=True
                )


def apply_price_break_checks(
    sequences: Sequence[ExecutionWallSequence],
    *,
    price_series: Sequence[tuple[datetime, float]],
    params: ExecutionWallParams,
    tick: float,
) -> list[dict[str, Any]]:
    """Post-pass break/acceptance using shared outcome-style rules."""
    from orderbook_analyse.wall_toxicity_audit.outcomes import evaluate_wall_role
    from orderbook_analyse.wall_toxicity_audit.types import OutcomeParams

    events: list[dict[str, Any]] = []
    for seq in sequences:
        if seq.first_seen is None:
            continue
        touch_bps = params.touch_bps
        break_bps = params.break_bps
        if params.touch_ticks is not None and seq.representative_price > 0:
            touch_bps = min(
                touch_bps,
                params.touch_ticks * tick / seq.representative_price * 10_000.0,
            )
        if params.break_ticks is not None and seq.representative_price > 0:
            break_bps = min(
                break_bps,
                params.break_ticks * tick / seq.representative_price * 10_000.0,
            )
        op_local = OutcomeParams(
            touch_bps=touch_bps,
            break_bps=break_bps,
            acceptance_seconds=params.acceptance_seconds,
            failed_break_return_seconds=params.failed_break_return_seconds,
            forward_seconds=params.forward_seconds,
        )
        band_low = seq.price_min
        band_high = seq.price_max
        if abs(band_high - band_low) < tick:
            band_low = seq.representative_price - tick
            band_high = seq.representative_price + tick
        for horizon in params.forward_seconds:
            role = evaluate_wall_role(
                price_series,
                reference_ts=seq.first_seen,
                horizon_seconds=float(horizon),
                side=seq.side,
                band_low=band_low,
                band_high=band_high,
                params=op_local,
            )
            if role.time_to_first_touch_seconds is not None and seq.touch_time is None:
                seq.touch_time = seq.first_seen + timedelta(
                    seconds=role.time_to_first_touch_seconds
                )
                seq.touch_status = "TOUCHED"
            if role.broken:
                seq.breakout_attempted = True
                if seq.break_time is None and role.time_to_break_seconds is not None:
                    seq.break_time = seq.first_seen + timedelta(
                        seconds=role.time_to_break_seconds
                    )
                if role.acceptance:
                    seq.breakout_accepted = True
                    seq.terminal_state = ExecutionState.ACCEPTED_BEYOND_LEVEL.value
                if role.failed_break:
                    seq.breakout_failed = True
                    if not seq.breakout_accepted:
                        seq.terminal_state = ExecutionState.FAILED_BREAK.value
                elif role.broken and not role.acceptance:
                    seq.terminal_state = ExecutionState.BROKEN.value
            events.append(
                {
                    "wall_sequence_id": seq.wall_sequence_id,
                    "side": seq.side,
                    "horizon_seconds": horizon,
                    "held": role.held,
                    "broken": role.broken,
                    "acceptance": role.acceptance,
                    "failed_break": role.failed_break,
                    "time_to_first_touch_seconds": role.time_to_first_touch_seconds,
                    "time_to_break_seconds": role.time_to_break_seconds,
                    "event_label": _break_label(seq.side, role),
                    "wall_type": WallType.EXECUTION_WALL.value,
                }
            )
    return events


def _break_label(side: str, role: Any) -> str:
    if str(side).lower() in {"ask", "sell"}:
        if role.acceptance:
            return "ASK_BREAKOUT_ACCEPTED"
        if role.failed_break:
            return "ASK_BREAKOUT_FAILED"
        if role.broken:
            return "ASK_BREAKOUT_ATTEMPT"
        if role.held:
            return "ASK_EXECUTION_WALL_HELD"
        return "ASK_NO_BREAK"
    if role.acceptance:
        return "BID_BREAKDOWN_ACCEPTED"
    if role.failed_break:
        return "BID_BREAKDOWN_FAILED"
    if role.broken:
        return "BID_BREAKDOWN_ATTEMPT"
    if role.held:
        return "BID_EXECUTION_WALL_HELD"
    return "BID_NO_BREAK"

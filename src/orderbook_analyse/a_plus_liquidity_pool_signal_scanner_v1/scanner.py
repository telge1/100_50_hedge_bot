"""A+ pool signal scanner engine V2 (research-only, no execution)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from orderbook_analyse.liquidity_location_pool_lifecycle.ema_context import attach_context

from .config import CANDIDATE_EXPIRY_MINUTES, TF_CONFIRM, TF_ENTRY_POOL, TF_LIQUIDITY, TF_MACRO, TF_STRUCTURE, VERIFIED_TICK_SYMBOLS
from .gates import apply_gates, evaluate_gates, estimated_net_rr, gross_rr
from .models import CandidateState, PoolRecord, ScannerCandidate, _utc_naive
from .pools import (
    PoolLifecycle,
    load_pools_at,
    pool_present_in_snapshot,
    pools_known_before_approach,
    resolve_pool_lifecycle,
)
from .setups import (
    _liquidity_asymmetry_long,
    _liquidity_asymmetry_short,
    _select_target_above,
    _select_target_below,
    atr_available,
    detect_pullback_long_candidates,
    detect_pullback_short_candidates,
    detect_terminal_long_context,
    detect_terminal_short_context,
    finalize_levels,
    freeze_target_context,
    classify_pool_below_terminal,
    is_terminal_ask_pool,
    is_terminal_bid_pool,
    pool_valid_at,
)
from .terminal_ladder import LadderEvent, TerminalLadderTracker, pools_swept_on_bar, try_terminal_reclaim


def _as_dt(ts: Any) -> datetime:
    return _utc_naive(ts)


def _naive(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def _bar_close_dt(bar_close: Any) -> datetime:
    return _as_dt(bar_close)


class PoolSignalScanner:
    """Deterministic research scanner driven by closed 1m bars."""

    def __init__(self, *, symbol: str) -> None:
        self.symbol = symbol.upper()
        self._candles_by_tf: dict[str, pd.DataFrame] = {}
        self.active: dict[str, ScannerCandidate] = {}
        self.confirmed: list[ScannerCandidate] = []
        self.invalidated: list[ScannerCandidate] = []
        self.superseded: list[ScannerCandidate] = []
        self.candidates_log: list[ScannerCandidate] = []
        self._seen_episodes: set[str] = set()
        self._filled_episodes: set[str] = set()
        self.terminal_long_tracker = TerminalLadderTracker(direction="LONG")
        self.terminal_short_tracker = TerminalLadderTracker(direction="SHORT")
        self.pullback_limit_events: list[dict[str, Any]] = []
        self.signal_intents: list[dict[str, Any]] = []
        self.lifecycle_events: list[dict[str, Any]] = []
        self.pool_selection_rows: list[dict[str, Any]] = []

    def scan(
        self,
        candles_by_tf: dict[str, pd.DataFrame],
        *,
        enable_pullback: bool = True,
        enable_terminal: bool = True,
    ) -> dict[str, Any]:
        df1 = candles_by_tf.get(TF_CONFIRM)
        if df1 is None or df1.empty:
            return self._result(empty_reason="missing_1m")
        for tf in (TF_STRUCTURE, TF_ENTRY_POOL, TF_LIQUIDITY, TF_MACRO):
            if tf in candles_by_tf and candles_by_tf[tf] is not None:
                candles_by_tf[tf] = attach_context(candles_by_tf[tf].sort_values("open_time").reset_index(drop=True))

        df1 = attach_context(df1.sort_values("open_time").reset_index(drop=True))
        candles_by_tf[TF_CONFIRM] = df1
        self._candles_by_tf = candles_by_tf

        for i in range(len(df1)):
            row = df1.iloc[i]
            bar_open = _naive(row["open_time"])
            bar_close = bar_open + pd.Timedelta(minutes=1)
            price = float(row["close"])
            high = float(row["high"])
            low = float(row["low"])
            open_px = float(row["open"])
            atr = float(row.get("atr_14") or float("nan"))

            pools = load_pools_at(
                candles_by_tf,
                symbol=self.symbol,
                as_of=_bar_close_dt(bar_close),
            )
            row_5m = self._last_closed_row(candles_by_tf.get(TF_STRUCTURE), bar_close)
            prev_row_5m = self._prev_closed_row(candles_by_tf.get(TF_STRUCTURE), bar_close)

            self._update_active(row, bar_close, open_px, price, high, low, atr, row_5m, pools)
            self._spawn_candidates(
                bar_close,
                price,
                high,
                low,
                atr,
                pools,
                row_5m,
                prev_row_5m,
                enable_pullback=enable_pullback,
                enable_terminal=enable_terminal,
            )

        return self._result()

    def _last_closed_row(self, df: pd.DataFrame | None, bar_close: pd.Timestamp) -> pd.Series:
        if df is None or df.empty:
            return pd.Series(dtype=float)
        mins = self._tf_minutes(df)
        closes = pd.to_datetime(df["open_time"]) + pd.Timedelta(minutes=mins)
        sl = df[closes <= bar_close]
        return sl.iloc[-1] if len(sl) else pd.Series(dtype=float)

    def _prev_closed_row(self, df: pd.DataFrame | None, bar_close: pd.Timestamp) -> pd.Series | None:
        if df is None or df.empty:
            return None
        mins = self._tf_minutes(df)
        closes = pd.to_datetime(df["open_time"]) + pd.Timedelta(minutes=mins)
        sl = df[closes <= bar_close]
        if len(sl) < 2:
            return None
        return sl.iloc[-2]

    def _tf_minutes(self, df: pd.DataFrame) -> int:
        if len(df) < 2:
            return 5
        d = (pd.to_datetime(df.iloc[1]["open_time"]) - pd.to_datetime(df.iloc[0]["open_time"])).total_seconds() / 60
        return max(1, int(d))

    def _spawn_candidates(
        self,
        bar_close: pd.Timestamp,
        price: float,
        high: float,
        low: float,
        atr: float,
        pools: dict[str, list[PoolRecord]],
        row_5m: pd.Series,
        prev_row_5m: pd.Series | None,
        *,
        enable_pullback: bool,
        enable_terminal: bool,
    ) -> None:
        approach_at = _bar_close_dt(bar_close)
        if not atr_available(atr):
            return

        if enable_pullback:
            for det in detect_pullback_short_candidates(
                symbol=self.symbol,
                price=price,
                approach_at=approach_at,
                pools_15m=pools.get(TF_ENTRY_POOL, []),
                pools_30m=pools.get(TF_LIQUIDITY, []),
                row_5m=row_5m,
                prev_row_5m=prev_row_5m,
                atr=atr,
            ):
                self._register_pullback(det, atr=atr)
            for det in detect_pullback_long_candidates(
                symbol=self.symbol,
                price=price,
                approach_at=approach_at,
                pools_15m=pools.get(TF_ENTRY_POOL, []),
                pools_30m=pools.get(TF_LIQUIDITY, []),
                row_5m=row_5m,
                prev_row_5m=prev_row_5m,
                atr=atr,
            ):
                self._register_pullback(det, atr=atr)

        if enable_terminal:
            self._update_terminal_ladder(
                direction="LONG",
                approach_at=approach_at,
                price=price,
                high=high,
                low=low,
                atr=atr,
                pools_1h=pools.get(TF_MACRO, []),
                pools_15m=pools.get(TF_ENTRY_POOL, []),
                pools_30m=pools.get(TF_LIQUIDITY, []),
                tracker=self.terminal_long_tracker,
            )
            self._update_terminal_ladder(
                direction="SHORT",
                approach_at=approach_at,
                price=price,
                high=high,
                low=low,
                atr=atr,
                pools_1h=pools.get(TF_MACRO, []),
                pools_15m=pools.get(TF_ENTRY_POOL, []),
                pools_30m=pools.get(TF_LIQUIDITY, []),
                tracker=self.terminal_short_tracker,
            )

    def _register_pullback(self, cand: ScannerCandidate, *, atr: float) -> None:
        ep = cand.episode_id or f"{cand.setup_type}:{cand.entry_pool.pool_id}"
        if ep in self._seen_episodes or ep in self._filled_episodes:
            return
        self._seen_episodes.add(ep)
        cand.state = CandidateState.LIMIT_INTENT_ARMED
        cand.signal_id = cand.setup_id
        cand.decision_at = cand.armed_at
        cand.max_feature_timestamp = cand.armed_at
        cand.plan_frozen_at = cand.armed_at
        cand.entry_policy = "pool_depth_60_percent"
        cand.entry_order_type = "hypothetical_limit"
        cand.research_only = True
        cand.entry_price = cand.limit_entry_price
        if cand.target_pool is None or not pool_valid_at(cand.target_pool, cand.armed_at):
            cand.state = CandidateState.NO_TRADE
            cand.reason_codes.append("NO_CAUSAL_TARGET_POOL")
            return
        finalize_levels(cand, symbol=self.symbol, atr=atr)
        if cand.stop_price is None or cand.target_price is None:
            return
        g = gross_rr(cand.direction, cand.entry_price, cand.stop_price, cand.target_price)
        cand.data_quality["gross_rr"] = g
        cand.data_quality["estimated_net_rr"] = estimated_net_rr(g)
        freeze_target_context(cand, armed_at=cand.armed_at)
        self.active[cand.setup_id] = cand
        self.candidates_log.append(cand)
        intent = cand.to_intent_dict()
        self.signal_intents.append(intent)
        self.pool_selection_rows.append(
            {
                "setup_id": cand.setup_id,
                "pool_id": cand.entry_pool.pool_id,
                "selection_reason": cand.pool_selection_reason,
                "limit_entry_price": cand.limit_entry_price,
                "armed_at": cand.armed_at.isoformat() if cand.armed_at else None,
            }
        )
        self.pullback_limit_events.append(
            {
                "event": "LIMIT_INTENT_ARMED",
                "setup_id": cand.setup_id,
                "signal_id": cand.signal_id,
                "pool_id": cand.entry_pool.pool_id,
                "limit_entry_price": cand.limit_entry_price,
                "stop_loss": cand.stop_price,
                "take_profit": cand.target_price,
                "at": cand.armed_at.isoformat() if cand.armed_at else None,
            }
        )
        self.lifecycle_events.append(
            self._lifecycle_event(cand, event_type="LIMIT_INTENT_ARMED", event_at=cand.armed_at)
        )

    def _update_terminal_ladder(
        self,
        *,
        direction: str,
        approach_at: datetime,
        price: float,
        high: float,
        low: float,
        atr: float,
        pools_1h: list[PoolRecord],
        pools_15m: list[PoolRecord],
        pools_30m: list[PoolRecord],
        tracker: TerminalLadderTracker,
    ) -> None:
        ladder_pools = pools_1h + pools_15m
        swept = pools_swept_on_bar(ladder_pools, direction=direction, low=low, high=high, approach_at=approach_at)
        if not swept:
            return

        if direction == "LONG":
            bid_swept = [p for p in swept if p.side == "BID"]
            if not bid_swept:
                return
            deepest = min(bid_swept, key=lambda p: p.lower_edge)
            individuals = [p for p in bid_swept if p.component_count == 1 and str(p.pool_id).startswith("lld:")]
            if individuals:
                deepest = min(individuals, key=lambda p: p.lower_edge)
            sweep_val = low
        else:
            ask_swept = [p for p in swept if p.side == "ASK"]
            if not ask_swept:
                return
            deepest = max(ask_swept, key=lambda p: p.upper_edge)
            sweep_val = high

        active_terminal = next(
            (c for c in self.active.values() if c.setup_type == f"A_PLUS_TERMINAL_POOL_{direction}"),
            None,
        )
        if active_terminal is not None:
            prev_sweep = active_terminal.sweep_low if direction == "LONG" else active_terminal.sweep_high
            if prev_sweep is not None:
                supersede = (direction == "LONG" and sweep_val < prev_sweep - 1e-12) or (
                    direction == "SHORT" and sweep_val > prev_sweep + 1e-12
                )
                if supersede:
                    active_terminal.state = CandidateState.TERMINAL_CANDIDATE_SUPERSEDED
                    active_terminal.invalidation_reason = "TERMINAL_CANDIDATE_SUPERSEDED_BY_LOWER_SWEEP"
                    self.superseded.append(active_terminal)
                    tracker.record_reset(
                        at=approach_at,
                        sweep_low=sweep_val if direction == "LONG" else None,
                        sweep_high=sweep_val if direction == "SHORT" else None,
                        detail=f"superseded {active_terminal.entry_pool.pool_id}",
                    )
                    self.active.pop(active_terminal.setup_id, None)

        if direction == "LONG":
            below_class = classify_pool_below_terminal(deepest, ladder_pools, price=price, atr=atr)
            if below_class == "nearby_comparable_pool_below":
                return
            terminal = deepest
            tclass = below_class
            det = detect_terminal_long_context(
                symbol=self.symbol,
                price=price,
                approach_at=approach_at,
                pools_1h=pools_1h,
                pools_15m=pools_15m,
                pools_30m=pools_30m,
                atr=atr,
                wick_low=low,
            )
            if det is not None:
                det.entry_pool = terminal
        else:
            terminal, tclass = is_terminal_ask_pool(ladder_pools, price, atr, approach_at=approach_at)
            if terminal is None or deepest.pool_id != terminal.pool_id:
                return
            det = detect_terminal_short_context(
                symbol=self.symbol,
                price=price,
                approach_at=approach_at,
                pools_1h=pools_1h,
                pools_15m=pools_15m,
                pools_30m=pools_30m,
                atr=atr,
                wick_high=high,
            )
        if det is None:
            return
        ep = f"{det.setup_type}:{det.entry_pool.pool_id}"
        if ep in self._filled_episodes:
            return
        existing = next(
            (
                c
                for c in self.active.values()
                if c.setup_type == det.setup_type and c.entry_pool.pool_id == det.entry_pool.pool_id
            ),
            None,
        )
        if existing is not None:
            existing.sweep_low = low if direction == "LONG" else existing.sweep_low
            existing.sweep_high = high if direction == "SHORT" else existing.sweep_high
            existing.approach_at = approach_at
            return
        if ep in self._seen_episodes:
            return
        self._seen_episodes.add(ep)
        det.episode_id = ep
        det.sweep_low = low if direction == "LONG" else det.sweep_low
        det.sweep_high = high if direction == "SHORT" else det.sweep_high
        det.reaction_high = None
        det.reaction_low = None
        self.active[det.setup_id] = det
        self.candidates_log.append(det)
        tracker.record(
            LadderEvent(
                event="LAST_RELEVANT_POOL_SWEPT",
                at=approach_at,
                pool_id=terminal.pool_id,
                sweep_low=low if direction == "LONG" else None,
                sweep_high=high if direction == "SHORT" else None,
                detail=tclass,
            )
        )

    def _update_active(
        self,
        row: pd.Series,
        bar_close: pd.Timestamp,
        open_px: float,
        close: float,
        high: float,
        low: float,
        atr: float,
        row_5m: pd.Series,
        pools: dict[str, list[PoolRecord]],
    ) -> None:
        expired: list[str] = []
        approach_ts = _bar_close_dt(bar_close)

        for sid, cand in list(self.active.items()):
            if cand.filled_once:
                continue

            if cand.approach_at and approach_ts > cand.approach_at + timedelta(minutes=CANDIDATE_EXPIRY_MINUTES):
                cand.state = CandidateState.EXPIRED_UNFILLED if "PULLBACK" in cand.setup_type else CandidateState.EXPIRED
                if "PULLBACK" in cand.setup_type:
                    cand.invalidation_reason = "setup_expired_unfilled"
                    cand.expired_at = approach_ts
                    self.lifecycle_events.append(
                        self._lifecycle_event(cand, event_type="EXPIRED_UNFILLED", event_at=approach_ts)
                    )
                expired.append(sid)
                continue

            if "PULLBACK" in cand.setup_type:
                self._update_pullback(
                    cand,
                    close,
                    high,
                    low,
                    bar_close,
                    atr,
                    row_5m,
                    pools,
                    expired,
                    sid,
                    getattr(self, "_candles_by_tf", {}),
                )
            else:
                self._update_terminal(cand, open_px, close, high, low, bar_close, atr, pools, expired, sid)

        for sid in expired:
            c = self.active.pop(sid, None)
            if c and c.state in (
                CandidateState.INVALIDATED,
                CandidateState.INVALIDATED_UNFILLED,
                CandidateState.EXPIRED_UNFILLED,
                CandidateState.AMBIGUOUS_INTRABAR,
            ):
                if c not in self.invalidated:
                    self.invalidated.append(c)

    def _lifecycle_event(
        self,
        cand: ScannerCandidate,
        *,
        event_type: str,
        event_at: datetime | None,
    ) -> dict[str, Any]:
        return {
            "event_id": f"{cand.setup_id}:{event_type}:{event_at.isoformat() if event_at else 'na'}",
            "signal_id": cand.signal_id or cand.setup_id,
            "episode_id": cand.episode_id,
            "symbol": cand.symbol,
            "event_type": event_type,
            "event_at": None if event_at is None else event_at.isoformat(),
            "decision_at": None if cand.decision_at is None else cand.decision_at.isoformat(),
            "max_feature_timestamp": None
            if cand.max_feature_timestamp is None
            else cand.max_feature_timestamp.isoformat(),
            "research_only": True,
            "entry_price": cand.entry_price,
            "stop_loss": cand.stop_price,
            "take_profit": cand.target_price,
        }

    def _update_pullback(
        self,
        cand: ScannerCandidate,
        close: float,
        high: float,
        low: float,
        bar_close: pd.Timestamp,
        atr: float,
        row_5m: pd.Series,
        pools: dict[str, list[PoolRecord]],
        expired: list[str],
        sid: str,
        candles_by_tf: dict[str, pd.DataFrame],
    ) -> None:
        """Pending LIMIT_INTENT lifecycle on each closed 1m bar.

        Evaluation order (no invented intraminute timestamps):
        1. Skip bars at/before armed_at
        2. Detect geometric/pool events on this closed bar
        3. Same-bar ambiguity → AMBIGUOUS_INTRABAR (no WIN/fill)
        4. Pure pool/structural invalidation → INVALIDATED_UNFILLED
        5. Expiry (handled upstream) / asymmetry lost
        6. Clean limit touch → fill with frozen plan levels
        """
        pool = cand.entry_pool
        limit_px = cand.limit_entry_price
        if limit_px is None or cand.armed_at is None:
            return
        bar_ts = _bar_close_dt(bar_close)
        if bar_ts <= cand.armed_at:
            return

        frozen_entry = cand.entry_price
        frozen_sl = cand.stop_price
        frozen_tp = cand.target_price
        frozen_pool_id = pool.pool_id

        entry_status, _ = resolve_pool_lifecycle(
            pool.pool_id,
            candles_by_tf,
            symbol=self.symbol,
            as_of=bar_ts,
            inject_records=getattr(self, "_inject_lifecycle_records", None),
        )
        target_status = PoolLifecycle.ACTIVE.value
        if cand.target_pool is not None:
            target_status, _ = resolve_pool_lifecycle(
                cand.target_pool.pool_id,
                candles_by_tf,
                symbol=self.symbol,
                as_of=bar_ts,
                inject_records=getattr(self, "_inject_lifecycle_records", None),
            )

        entry_invalidated = entry_status == PoolLifecycle.INVALIDATED.value
        target_invalidated = cand.target_pool is not None and target_status == PoolLifecycle.INVALIDATED.value
        entry_data_quality = entry_status in {
            PoolLifecycle.DATA_QUALITY_ERROR.value,
            PoolLifecycle.SNAPSHOT_INCONSISTENT.value,
        }
        target_data_quality = target_status in {
            PoolLifecycle.DATA_QUALITY_ERROR.value,
            PoolLifecycle.SNAPSHOT_INCONSISTENT.value,
        }
        if entry_data_quality or target_data_quality:
            cand.htf_context["pool_data_quality_error"] = {
                "entry_status": entry_status,
                "target_status": target_status,
                "at": bar_ts.isoformat(),
            }

        structural_inv = False
        if cand.direction == "SHORT":
            if not row_5m.empty and float(row_5m["close"]) > pool.upper_edge:
                structural_inv = True
        elif not row_5m.empty and float(row_5m["close"]) < pool.lower_edge:
            structural_inv = True

        limit_touched = (cand.direction == "SHORT" and high >= limit_px) or (
            cand.direction == "LONG" and low <= limit_px
        )
        sl_touched = False
        tp_touched = False
        if frozen_sl is not None:
            sl_touched = (cand.direction == "SHORT" and high >= frozen_sl) or (
                cand.direction == "LONG" and low <= frozen_sl
            )
        if frozen_tp is not None:
            tp_touched = (cand.direction == "SHORT" and low <= frozen_tp) or (
                cand.direction == "LONG" and high >= frozen_tp
            )

        inv_reason: str | None = None
        if entry_invalidated:
            inv_reason = "ENTRY_POOL_INVALIDATED_BEFORE_FILL"
        elif target_invalidated:
            inv_reason = "TARGET_POOL_INVALIDATED_BEFORE_FILL"
        elif structural_inv:
            inv_reason = (
                "5m_accepted_above_entry_pool"
                if cand.direction == "SHORT"
                else "5m_accepted_below_entry_pool"
            )

        # Same-bar: limit + invalidation or limit + SL/TP — order not provable from OHLC.
        if limit_touched and (inv_reason is not None or sl_touched or tp_touched):
            amb_bits = []
            if inv_reason:
                amb_bits.append(inv_reason)
            if sl_touched:
                amb_bits.append("ENTRY_AND_SL_SAME_BAR")
            if tp_touched:
                amb_bits.append("ENTRY_AND_TP_SAME_BAR")
            cand.state = CandidateState.AMBIGUOUS_INTRABAR
            cand.invalidation_reason = "AMBIGUOUS_INTRABAR:" + "+".join(amb_bits)
            cand.reason_codes.append("AMBIGUOUS_INTRABAR")
            cand.invalidated_at = bar_ts
            cand.htf_context["same_bar_ambiguity"] = True
            cand.htf_context["same_bar_events"] = amb_bits
            # Keep frozen levels for audit only — never FILLED/CONFIRMED.
            cand.entry_price = frozen_entry
            cand.stop_price = frozen_sl
            cand.target_price = frozen_tp
            self.lifecycle_events.append(
                self._lifecycle_event(cand, event_type="AMBIGUOUS_INTRABAR", event_at=bar_ts)
            )
            self.invalidated.append(cand)
            expired.append(sid)
            return

        if inv_reason is not None:
            cand.state = CandidateState.INVALIDATED_UNFILLED
            cand.invalidation_reason = inv_reason
            cand.reason_codes.append(inv_reason)
            cand.invalidated_at = bar_ts
            if target_invalidated and cand.target_pool is not None:
                cand.htf_context["target_invalidated_before_fill_at"] = bar_ts.isoformat()
            if entry_invalidated:
                cand.htf_context["entry_invalidated_before_fill_at"] = bar_ts.isoformat()
            self.lifecycle_events.append(
                self._lifecycle_event(cand, event_type="INVALIDATED_UNFILLED", event_at=bar_ts)
            )
            expired.append(sid)
            return

        asym_ok = (
            _liquidity_asymmetry_short(close, pools.get(TF_LIQUIDITY, []), atr)
            if cand.direction == "SHORT"
            else _liquidity_asymmetry_long(close, pools.get(TF_LIQUIDITY, []), atr)
        )
        if not asym_ok:
            if limit_touched:
                cand.state = CandidateState.AMBIGUOUS_INTRABAR
                cand.invalidation_reason = "AMBIGUOUS_INTRABAR:asymmetry_lost+limit_touch"
                cand.reason_codes.append("AMBIGUOUS_INTRABAR")
                cand.invalidated_at = bar_ts
                cand.htf_context["same_bar_ambiguity"] = True
                self.lifecycle_events.append(
                    self._lifecycle_event(cand, event_type="AMBIGUOUS_INTRABAR", event_at=bar_ts)
                )
                self.invalidated.append(cand)
                expired.append(sid)
                return
            cand.state = CandidateState.INVALIDATED_UNFILLED
            cand.invalidation_reason = "asymmetry_lost"
            cand.invalidated_at = bar_ts
            self.lifecycle_events.append(
                self._lifecycle_event(cand, event_type="INVALIDATED_UNFILLED", event_at=bar_ts)
            )
            expired.append(sid)
            return

        if limit_touched and cand.first_tradeable_touch_at_entry is None:
            cand.first_tradeable_touch_at_entry = bar_ts
        if not limit_touched:
            return

        cand.hypothetical_filled_at = bar_ts
        cand.filled_at = bar_ts
        cand.state = CandidateState.HYPOTHETICAL_FILLED
        cand.filled_once = True
        ep = cand.episode_id or f"{cand.setup_type}:{pool.pool_id}"
        self._filled_episodes.add(ep)

        cand.entry_price = frozen_entry
        cand.stop_price = frozen_sl
        cand.target_price = frozen_tp
        assert cand.entry_pool.pool_id == frozen_pool_id

        self.pullback_limit_events.append(
            {
                "event": "HYPOTHETICAL_FILLED",
                "setup_id": cand.setup_id,
                "signal_id": cand.signal_id,
                "pool_id": pool.pool_id,
                "entry_price": frozen_entry,
                "at": cand.hypothetical_filled_at.isoformat(),
            }
        )
        self.lifecycle_events.append(
            self._lifecycle_event(cand, event_type="HYPOTHETICAL_FILLED", event_at=bar_ts)
        )

        gates = evaluate_gates(
            cand,
            symbol=self.symbol,
            approach_at_known=pools_known_before_approach(cand.entry_pool, cand.approach_at or cand.armed_at),
            closed_bar_safe=True,
            context_complete=cand.target_pool is not None,
            intermediate_block=False,
            confirmed_1m=True,
            limit_filled=True,
            candle_coverage_ok=True,
            no_data_gap=True,
            unique_episode=True,
            target_reached_before_entry=False,
            tick_verified=self.symbol in VERIFIED_TICK_SYMBOLS,
        )
        apply_gates(cand, gates)

        if cand.all_gates_pass():
            cand.state = CandidateState.CONFIRMED
            cand.confirmed_at = bar_ts
            cand.signal_at = cand.armed_at
            cand.confirmation_at = bar_ts
            self.confirmed.append(cand)
            self.lifecycle_events.append(
                self._lifecycle_event(cand, event_type="CONFIRMED", event_at=bar_ts)
            )
        else:
            cand.state = CandidateState.NO_TRADE
            if any(g.gate == "minimum_net_reward_distance_after_costs" and not g.passed for g in cand.gates):
                cand.reason_codes.append("RECLAIM_VALID_BUT_RR_BLOCKED")
            cand.reason_codes.append("NO_TRADE")
        expired.append(sid)

    def _update_terminal(
        self,
        cand: ScannerCandidate,
        open_px: float,
        close: float,
        high: float,
        low: float,
        bar_close: pd.Timestamp,
        atr: float,
        pools: dict[str, list[PoolRecord]],
        expired: list[str],
        sid: str,
    ) -> None:
        if cand.direction == "LONG" and cand.sweep_low is not None and low < cand.sweep_low - 1e-12:
            if cand.state != CandidateState.CONFIRMED:
                self.terminal_long_tracker.record_reset(
                    at=_bar_close_dt(bar_close),
                    sweep_low=low,
                    sweep_high=None,
                    detail="new_low_before_reclaim",
                )
                cand.sweep_low = low
                cand.reaction_high = None
                cand.reaction_low = None
                cand.terminal_ladder_state = "WAIT_FOR_REACTION"
                return

        if try_terminal_reclaim(cand, open_px=open_px, close=close, high=high, low=low):
            bar_ts = _bar_close_dt(bar_close)
            cand.decision_at = bar_ts
            cand.confirmation_at = bar_ts
            cand.armed_at = bar_ts
            cand.confirmed_at = bar_ts
            cand.signal_at = bar_ts
            cand.entry_price = close
            cand.filled_once = True
            cand.signal_id = cand.setup_id
            cand.max_feature_timestamp = bar_ts
            cand.plan_frozen_at = bar_ts
            cand.entry_order_type = "market_on_reclaim_close"
            pools_15m = pools.get(TF_ENTRY_POOL, [])
            pools_30m = pools.get(TF_LIQUIDITY, [])
            pools_1h = pools.get(TF_MACRO, [])
            if cand.direction == "LONG":
                target = _select_target_above(
                    close,
                    pools_15m + pools_30m + [p for p in pools_1h if p.side == "ASK"],
                    atr,
                    as_of=bar_ts,
                )
            else:
                target = _select_target_below(
                    close,
                    pools_15m + pools_30m + [p for p in pools_1h if p.side == "BID"],
                    atr,
                    as_of=bar_ts,
                )
            if target is None or not pool_valid_at(target, bar_ts):
                cand.state = CandidateState.NO_TRADE
                cand.reason_codes.append("NO_CAUSAL_TARGET_POOL")
                expired.append(sid)
                return
            cand.target_pool = target
            finalize_levels(cand, symbol=self.symbol, atr=atr)
            g = gross_rr(cand.direction, cand.entry_price, cand.stop_price, cand.target_price)
            cand.data_quality["gross_rr"] = g
            cand.data_quality["estimated_net_rr"] = estimated_net_rr(g)
            freeze_target_context(cand, armed_at=bar_ts)

            gates = evaluate_gates(
                cand,
                symbol=self.symbol,
                approach_at_known=pools_known_before_approach(cand.entry_pool, cand.approach_at or cand.confirmation_at),
                closed_bar_safe=True,
                context_complete=cand.target_pool is not None,
                intermediate_block=False,
                confirmed_1m=True,
                limit_filled=False,
                candle_coverage_ok=True,
                no_data_gap=True,
                unique_episode=True,
                target_reached_before_entry=False,
                tick_verified=self.symbol in VERIFIED_TICK_SYMBOLS,
            )
            apply_gates(cand, gates)

            if cand.all_gates_pass():
                cand.state = CandidateState.CONFIRMED
                self.confirmed.append(cand)
                self.signal_intents.append(
                    {
                        **cand.to_intent_dict(),
                        "state": "CONFIRMED",
                        "armed_at": bar_ts.isoformat(),
                        "entry_order_type": "market_on_reclaim_close",
                    }
                )
                self.lifecycle_events.append(
                    self._lifecycle_event(cand, event_type="TERMINAL_RECLAIM_CONFIRMED", event_at=bar_ts)
                )
            else:
                cand.state = CandidateState.NO_TRADE
                if any(g.gate == "minimum_net_reward_distance_after_costs" and not g.passed for g in cand.gates):
                    cand.reason_codes.append("RECLAIM_VALID_BUT_RR_BLOCKED")
                cand.reason_codes.append("NO_TRADE")
            expired.append(sid)

    def _result(self, empty_reason: str | None = None) -> dict[str, Any]:
        ladder_audit = {
            "long": self.terminal_long_tracker.audit_summary(),
            "short": self.terminal_short_tracker.audit_summary(),
        }
        return {
            "symbol": self.symbol,
            "confirmed": [c.to_dict() for c in self.confirmed],
            "invalidated": [c.to_dict() for c in self.invalidated],
            "superseded": [c.to_dict() for c in self.superseded],
            "candidates": [c.to_dict() for c in self.candidates_log],
            "signal_intents": list(self.signal_intents),
            "lifecycle_events": list(self.lifecycle_events),
            "n_confirmed": len(self.confirmed),
            "n_invalidated": len(self.invalidated),
            "n_superseded": len(self.superseded),
            "pullback_limit_events": self.pullback_limit_events,
            "pool_selection_audit": self.pool_selection_rows,
            "terminal_ladder_events": [
                e.to_dict() for e in self.terminal_long_tracker.events + self.terminal_short_tracker.events
            ],
            "reaction_state_resets": [
                e.to_dict() for e in self.terminal_long_tracker.reset_events + self.terminal_short_tracker.reset_events
            ],
            "ladder_audit": ladder_audit,
            "empty_reason": empty_reason,
        }

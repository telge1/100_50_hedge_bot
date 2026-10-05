"""Per-symbol load, warmup replay, live refresh, and incremental bar steps."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from bot.e1r_live_scanner.baseline import enrich_signal_row, process_bar_baseline
from bot.e1r_live_scanner.config import ScannerConfig, context_entry15_module, ensure_runtime_paths
from bot.e1r_live_scanner.e1r_v1.engine import E1REngine
from bot.e1r_live_scanner.live_refresh import (
    SymbolPollResult,
    missed_15m_closes,
    new_bars_after,
    scan_new_bar_metrics,
    utc,
)
from bot.e1r_live_scanner.state import Readiness, SymbolState


class SymbolProcessor:
    def __init__(self, config: ScannerConfig | None = None, registry: Any | None = None) -> None:
        self.config = config or ScannerConfig()
        self.registry = registry

    def _load_end(self, now: datetime | None = None) -> datetime | None:
        if not self.config.live:
            return self.config.research_load_end()
        return self.config.effective_data_end(now)

    def _fetch_market_and_pane(
        self, symbol: str, now: datetime | None = None
    ) -> tuple[dict, Any, list, dict, bool]:
        ensure_runtime_paths()
        from dashboard.research_charts.lld_research_kernel import load_pane_candles, ui_lld_config

        entry15 = context_entry15_module()
        end = self._load_end(now)
        end_unix = int(utc(end if end is not None else self.config.pane_to).timestamp())
        if self.config.live:
            market = entry15.load_market(symbol, load_end=utc(end))
        else:
            market = entry15.load_market(symbol)
        packed, candles15 = load_pane_candles(
            symbol,
            "15m",
            from_unix=int(self.config.pane_from.timestamp()),
            to_unix=end_unix,
            history_weeks=entry15.HISTORY_WEEKS,
        )
        strict = bool(packed.get("strict_complete_buckets"))
        bars15 = entry15.build_15m_bars(candles15) if candles15 else []
        return market, candles15, bars15, packed, strict

    def load_state(self, symbol: str, now: datetime | None = None) -> SymbolState:
        ensure_runtime_paths()
        from short_block_4h_guard import H4FloorGuard

        st = SymbolState(symbol=symbol)
        try:
            market, candles15, bars15, packed, strict = self._fetch_market_and_pane(symbol, now)
            if not strict:
                st.readiness = Readiness.WAITING_FOR_DATA if self.config.live else Readiness.DATA_MISSING
                st.data_missing_reason = "strict_complete_buckets=False for 15m pane"
                return st
            if not bars15:
                st.readiness = Readiness.DATA_MISSING
                st.data_missing_reason = "no 15m bars"
                return st
            end = self._load_end(now) or self.config.pane_to
            st.market = market
            st.candles15 = candles15
            st.bars15 = bars15
            st.by_open = {b["open_time"]: b for b in bars15}
            from dashboard.research_charts.lld_research_kernel import ui_lld_config

            st.lld_cfg = ui_lld_config("15m")
            st.floor_guard = H4FloorGuard(symbol, self.config.pane_from, end)
            st.e1r_engine = E1REngine(self.config.e1r_sim_from, utc(end))
            self._assess_readiness(st, now)
        except Exception as exc:  # noqa: BLE001
            st.readiness = Readiness.DATA_MISSING
            st.data_missing_reason = str(exc)
        return st

    def refresh_candles(self, st: SymbolState, now: datetime | None = None) -> None:
        """Reload CH candles; preserve logical state (E1R, dedup, guard episode fields)."""
        if st.readiness == Readiness.DATA_MISSING:
            return
        now = utc(now or datetime.now(timezone.utc))
        old_len = len(st.bars15 or [])
        market, candles15, bars15, packed, strict = self._fetch_market_and_pane(st.symbol, now)
        if not strict:
            st.readiness = Readiness.WAITING_FOR_DATA
            st.data_missing_reason = "strict_complete_buckets=False for 15m pane"
            return
        if not bars15:
            st.readiness = Readiness.WAITING_FOR_DATA
            st.data_missing_reason = "no 15m bars after refresh"
            return

        end = self._load_end(now) or self.config.pane_to
        if len(bars15) > old_len:
            st.lld_cache.pop("_ui_selected", None)
        st.market = market
        st.candles15 = candles15
        st.bars15 = bars15
        st.by_open = {b["open_time"]: b for b in bars15}
        if st.floor_guard is not None:
            st.floor_guard.pane_to = utc(end)
            st.floor_guard._candles_4h = None
            st.floor_guard._snap_cache.clear()
        if st.e1r_engine is not None:
            st.e1r_engine.sim_to = utc(end)
        self._assess_readiness(st, now)

    def _assess_readiness(self, st: SymbolState, now: datetime | None) -> None:
        if not st.bars15:
            st.readiness = Readiness.DATA_MISSING
            return
        now = utc(now or datetime.now(timezone.utc))
        latest = st.bars15[-1]["close_time"]
        lag_min = (now - utc(latest)).total_seconds() / 60.0
        if self.config.live and lag_min > self.config.max_closed_15m_lag_minutes:
            st.readiness = Readiness.WAITING_FOR_DATA
            st.data_missing_reason = f"15m_tail_lag_minutes={lag_min:.1f}"
            return
        if st.readiness in (Readiness.WARMING, Readiness.DATA_MISSING, Readiness.WAITING_FOR_DATA):
            if st.last_processed_15m_close is None:
                st.readiness = Readiness.WARMING
            else:
                st.readiness = Readiness.LIVE_READY

    def warmup(self, st: SymbolState, through_close: datetime | None = None) -> None:
        if st.readiness in (Readiness.DATA_MISSING, Readiness.WAITING_FOR_DATA):
            return
        last = through_close
        if last is None:
            last = st.bars15[-1]["close_time"]
        for bar in st.bars15:
            ct = bar["close_time"]
            if ct > last:
                break
            self._process_one_bar(st, bar, emit_signals=False)
        st.last_processed_15m_close = last
        st.readiness = Readiness.LIVE_READY
        st.note_ts("live_ready", last)

    def replay_through(
        self, symbol: str, through_close: datetime, *, emit_signals: bool = True
    ) -> tuple[SymbolState, list[dict]]:
        """Fresh load + chronological replay (parity harness)."""
        st = self.load_state(symbol)
        if st.readiness == Readiness.DATA_MISSING:
            return st, []
        logs: list[dict] = []
        for bar in st.bars15:
            ct = bar["close_time"]
            if ct > through_close:
                break
            rows = self._process_one_bar(st, bar, emit_signals=emit_signals)
            logs.extend(rows)
            st.last_processed_15m_close = ct
        st.readiness = Readiness.LIVE_READY
        return st, logs

    def catch_up(self, st: SymbolState, emit: Callable[[dict], None] | None = None) -> list[dict]:
        if st.readiness != Readiness.LIVE_READY:
            return []
        logs: list[dict] = []
        last = st.last_processed_15m_close
        for bar in st.bars15:
            ct = bar["close_time"]
            if last is not None and ct <= last:
                continue
            rows = self._process_one_bar(st, bar, emit_signals=True)
            logs.extend(rows)
            if emit:
                for row in rows:
                    emit(row)
            st.last_processed_15m_close = ct
        return logs

    def poll_symbol_live(self, st: SymbolState, now: datetime | None = None) -> SymbolPollResult:
        now = utc(now or datetime.now(timezone.utc))
        res = SymbolPollResult(symbol=st.symbol, status=st.readiness.value)
        if st.readiness == Readiness.DATA_MISSING:
            res.status = Readiness.DATA_MISSING.value
            res.errors.append(st.data_missing_reason or "data_missing")
            return res

        t0 = time.perf_counter()
        res.processing_started_at = now.isoformat()
        self.refresh_candles(st, now)
        if st.bars15:
            res.first_seen_in_ch = st.bars15[-1]["close_time"].isoformat()

        if st.readiness == Readiness.WAITING_FOR_DATA:
            res.status = Readiness.WAITING_FOR_DATA.value
            res.processing_finished_at = datetime.now(timezone.utc).isoformat()
            return res

        latest = st.bars15[-1]["close_time"]
        last = st.last_processed_15m_close
        if last is not None and utc(latest) <= utc(last):
            res.status = Readiness.LIVE_READY.value
            res.processing_finished_at = datetime.now(timezone.utc).isoformat()
            return res

        new_bars = new_bars_after(st.bars15, last)
        if last is not None:
            res.missed_bars = missed_15m_closes(last, latest, new_bars)
        res.duplicate_bars, res.out_of_order = scan_new_bar_metrics(new_bars, last)

        prev = utc(last) if last is not None else None
        for bar in new_bars:
            ct = utc(bar["close_time"])
            if prev is not None and ct <= prev:
                continue
            rows = self._process_one_bar(st, bar, emit_signals=True)
            res.catchup_bars += 1
            res.bars_processed += 1
            res.bar_close = ct.isoformat()
            for row in rows:
                res.signal_rows.append(row)
                if row.get("variant") == "with_guard":
                    if row.get("signal_status") == "ALLOWED":
                        res.allowed += 1
                    elif row.get("signal_status") == "BLOCKED":
                        res.blocked += 1
                elif row.get("variant") == "floor_blocked":
                    res.floor_blocked += 1
            st.last_processed_15m_close = ct
            prev = ct

        res.status = Readiness.LIVE_READY.value
        res.processing_finished_at = datetime.now(timezone.utc).isoformat()
        _ = (time.perf_counter() - t0) * 1000
        return res

    def _process_one_bar(self, st: SymbolState, bar: dict, *, emit_signals: bool) -> list[dict]:
        moment = bar["close_time"]
        i = bar["candle_index"]
        st.e1r_engine.process_bar(i, bar, st.bars15, st.candles15, st.lld_cfg, st.lld_cache)

        baseline_rows = process_bar_baseline(
            st.symbol,
            bar,
            moment=moment,
            candles15=st.candles15,
            market=st.market,
            lld_cfg=st.lld_cfg,
            lld_cache=st.lld_cache,
            guard=st.floor_guard,
            by_open=st.by_open,
            seen_no_guard=st.seen_no_guard,
            seen_with_guard=st.seen_with_guard,
            report_from=self.config.report_from,
            report_to=self.config.effective_report_to(),
        )

        if not emit_signals:
            return []

        out: list[dict] = []
        for row in baseline_rows:
            decision = moment
            e1r_eval = {"e1r_state": st.e1r_engine.e1r_state, "ignore": False, "status": "N/A"}
            if row.get("variant") == "with_guard":
                entry_open = row["entry_open"]
                ot = datetime.fromisoformat(entry_open)
                entry_bar = st.by_open.get(ot, bar)
                decision = entry_bar["close_time"]
                e1r_eval = st.e1r_engine.evaluate_signal(
                    decision,
                    float(row["close"]),
                    st.bars15,
                    st.candles15,
                    st.lld_cfg,
                    st.lld_cache,
                )
            enriched = enrich_signal_row(row, e1r_eval=e1r_eval, decision_time=decision, state_ts=st.state_ts)
            out.append(enriched)
        if emit_signals and self.registry and out:
            from bot.shadow_signal_registry.short_product import consolidate_short_product

            by_pool: dict[str, list[dict]] = defaultdict(list)
            for row in out:
                if row.get("pool_id"):
                    by_pool[row["pool_id"]].append(row)
            for group in by_pool.values():
                product = consolidate_short_product(group)
                if product:
                    self.registry.register_short_product(product)
        st.note_ts("last_bar", moment)
        return out

    def benchmark_one_bar(self, st: SymbolState) -> dict[str, float]:
        if st.readiness != Readiness.LIVE_READY or not st.bars15:
            return {}
        now = datetime.now(timezone.utc)
        t_refresh = time.perf_counter()
        self.refresh_candles(st, now)
        refresh_ms = (time.perf_counter() - t_refresh) * 1000
        bar = st.bars15[-1]
        t0 = time.perf_counter()
        ensure_runtime_paths()
        from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index

        idx = bar["candle_index"]
        scanner_pools_for_index(st.candles15, "15m", idx, st.lld_cfg, cache=st.lld_cache)
        lld_ms = (time.perf_counter() - t0) * 1000

        t_g = time.perf_counter()
        st.floor_guard.advance(bar, bar["close_time"])
        floor_ms = (time.perf_counter() - t_g) * 1000

        t_e = time.perf_counter()
        st.e1r_engine.process_bar(idx, bar, st.bars15, st.candles15, st.lld_cfg, st.lld_cache)
        e1r_ms = (time.perf_counter() - t_e) * 1000

        t_b = time.perf_counter()
        process_bar_baseline(
            st.symbol,
            bar,
            moment=bar["close_time"],
            candles15=st.candles15,
            market=st.market,
            lld_cfg=st.lld_cfg,
            lld_cache=st.lld_cache,
            guard=st.floor_guard,
            by_open=st.by_open,
            seen_no_guard=set(st.seen_no_guard),
            seen_with_guard=set(st.seen_with_guard),
            report_from=self.config.report_from,
            report_to=self.config.effective_report_to(),
        )
        baseline_ms = (time.perf_counter() - t_b) * 1000
        total_ms = (time.perf_counter() - t0) * 1000 + refresh_ms
        return {
            "total_ms": total_ms,
            "refresh_ms": refresh_ms,
            "candle_update_ms": refresh_ms,
            "lld_ms": lld_ms,
            "floor_guard_ms": floor_ms,
            "e1r_ms": e1r_ms,
            "baseline_ms": baseline_ms,
        }


def data_readiness_row(st: SymbolState) -> dict[str, Any]:
    missing: list[str] = []
    warmup_ok = False
    if st.readiness == Readiness.DATA_MISSING:
        missing.append(st.data_missing_reason or "unknown")
    elif st.readiness == Readiness.WAITING_FOR_DATA:
        missing.append(st.data_missing_reason or "waiting_for_data")
    elif st.market and st.bars15:
        for tf in ("15m", "30m", "1h", "4h"):
            bars = st.market.get(tf, {}).get("bars") or []
            if not bars:
                missing.append(tf)
        ema_ok = any(b.get("ema200") is not None for b in st.bars15[-250:])
        if not ema_ok:
            missing.append("ema200_warmup")
        warmup_ok = st.readiness == Readiness.LIVE_READY and not missing
    return {
        "symbol": st.symbol,
        "status": st.readiness.value,
        "missing": missing,
        "warmup_ok": warmup_ok,
    }

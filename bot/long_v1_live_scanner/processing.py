"""Long V1 live symbol processing (geometry, ladder, shadow management)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.long_v1_live_scanner.config import ScannerConfig, ensure_runtime_paths
from bot.long_v1_live_scanner.data_1m import load_1m_bars
from bot.long_v1_live_scanner.engine import evaluate_geometry_bar, signal_id_for
from bot.long_v1_live_scanner.live_refresh import (
    SymbolPollResult,
    missed_15m_closes,
    new_bars_after,
    scan_new_bar_metrics,
    utc,
)
from bot.long_v1_live_scanner.management import ShadowTrade, log_row_from_trade, process_one_1m_bar
from bot.long_v1_live_scanner.state import Readiness, SymbolState


class SymbolProcessor:
    def __init__(self, config: ScannerConfig | None = None, registry: Any | None = None) -> None:
        self.config = config or ScannerConfig()
        self.registry = registry

    def _load_end(self, now: datetime | None = None) -> datetime:
        return self.config.effective_data_end(now)

    def _fetch_pane(self, symbol: str, now: datetime | None = None) -> tuple[Any, list, list[dict], dict, bool]:
        ensure_runtime_paths()
        from bot.long_v1_live_scanner.config import context_entry15_module
        from dashboard.research_charts.lld_research_kernel import load_pane_candles, ui_lld_config

        entry15 = context_entry15_module()
        end = self._load_end(now)
        end_unix = int(utc(end).timestamp())
        packed, candles15 = load_pane_candles(
            symbol,
            "15m",
            from_unix=int(self.config.pane_from.timestamp()),
            to_unix=end_unix,
            history_weeks=entry15.HISTORY_WEEKS,
        )
        strict = bool(packed.get("strict_complete_buckets"))
        bars15 = entry15.build_15m_bars(candles15) if candles15 else []
        lld_cfg = ui_lld_config("15m")
        return lld_cfg, candles15, bars15, packed, strict

    def load_state(self, symbol: str, now: datetime | None = None) -> SymbolState:
        st = SymbolState(symbol=symbol)
        try:
            lld_cfg, candles15, bars15, _packed, strict = self._fetch_pane(symbol, now)
            if not strict:
                st.readiness = Readiness.WAITING_FOR_DATA if self.config.live else Readiness.DATA_MISSING
                st.data_missing_reason = "strict_complete_buckets=False for 15m pane"
                return st
            if not bars15:
                st.readiness = Readiness.DATA_MISSING
                st.data_missing_reason = "no 15m bars"
                return st
            st.lld_cfg = lld_cfg
            st.candles15 = candles15
            st.bars15 = bars15
            st.by_open = {b["open_time"]: b for b in bars15}
            self._assess_readiness(st, now)
        except Exception as exc:  # noqa: BLE001
            st.readiness = Readiness.DATA_MISSING
            st.data_missing_reason = str(exc)
        return st

    def refresh_candles(self, st: SymbolState, now: datetime | None = None) -> None:
        if st.readiness == Readiness.DATA_MISSING:
            return
        now = utc(now or datetime.now(timezone.utc))
        old_len = len(st.bars15 or [])
        _lld, candles15, bars15, _packed, strict = self._fetch_pane(st.symbol, now)
        if not strict:
            st.readiness = Readiness.WAITING_FOR_DATA
            st.data_missing_reason = "strict_complete_buckets=False for 15m pane"
            return
        if not bars15:
            st.readiness = Readiness.WAITING_FOR_DATA
            st.data_missing_reason = "no 15m bars after refresh"
            return
        if len(bars15) > old_len:
            st.lld_cache.pop("_ui_selected", None)
        st.candles15 = candles15
        st.bars15 = bars15
        st.by_open = {b["open_time"]: b for b in bars15}
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
        if st.last_processed_15m_close is None:
            st.readiness = Readiness.WARMING
        else:
            st.readiness = Readiness.LIVE_READY

    def warmup(self, st: SymbolState, through_close: datetime | None = None) -> None:
        if st.readiness in (Readiness.DATA_MISSING, Readiness.WAITING_FOR_DATA):
            return
        last = through_close or st.bars15[-1]["close_time"]
        for bar in st.bars15:
            if bar["close_time"] > last:
                break
            self._process_one_15m(st, bar, emit_signals=False, log_ts=None)
        st.last_processed_15m_close = last
        st.readiness = Readiness.LIVE_READY

    def replay_through(
        self,
        symbol: str,
        through_close: datetime,
        *,
        emit_signals: bool = True,
        report_from: datetime | None = None,
        report_to: datetime | None = None,
    ) -> tuple[SymbolState, list[dict]]:
        st = self.load_state(symbol)
        if st.readiness == Readiness.DATA_MISSING:
            return st, []
        rf = report_from or self.config.report_from
        rt = report_to or self.config.report_to
        logs: list[dict] = []
        for bar in st.bars15:
            ct = bar["close_time"]
            if ct > through_close:
                break
            rows = self._process_one_15m(
                st,
                bar,
                emit_signals=emit_signals,
                log_ts=ct,
                report_from=rf,
                report_to=rt,
            )
            logs.extend(rows)
            st.last_processed_15m_close = ct
            mgmt = self._process_management(st, utc(ct), emit_signals=emit_signals, log_ts=ct)
            logs.extend(mgmt)
        st.readiness = Readiness.LIVE_READY
        return st, logs

    def poll_symbol_live(self, st: SymbolState, now: datetime | None = None) -> SymbolPollResult:
        now = utc(now or datetime.now(timezone.utc))
        res = SymbolPollResult(symbol=st.symbol, status=st.readiness.value)
        if st.readiness == Readiness.DATA_MISSING:
            res.errors.append(st.data_missing_reason or "data_missing")
            return res

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
            mgmt_rows = self._process_management(st, now, emit_signals=True, log_ts=now)
            res.signal_rows.extend(mgmt_rows)
            for row in mgmt_rows:
                self._tally_close(res, row)
            res.open_shadow = sum(1 for t in st.open_trades if t.status == "OPEN")
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
            rows = self._process_one_15m(st, bar, emit_signals=True, log_ts=now)
            res.catchup_bars += 1
            res.bars_processed += 1
            res.bar_close = ct.isoformat()
            for row in rows:
                res.signal_rows.append(row)
                stat = row.get("signal_status")
                if stat == "GEOMETRY":
                    res.geometry_candidates += 1
                elif stat == "LADDER_BLOCKED":
                    res.ladder_blocked += 1
                elif stat == "ALLOWED":
                    res.allowed += 1
                self._tally_close(res, row)
            st.last_processed_15m_close = ct
            prev = ct
            mgmt_rows = self._process_management(st, now, emit_signals=True, log_ts=now)
            res.signal_rows.extend(mgmt_rows)
            for row in mgmt_rows:
                self._tally_close(res, row)
            res.new_1m_bars += sum(1 for r in mgmt_rows if r.get("event") == "management_step")

        res.open_shadow = sum(1 for t in st.open_trades if t.status == "OPEN")
        res.status = Readiness.LIVE_READY.value
        res.processing_finished_at = datetime.now(timezone.utc).isoformat()
        return res

    @staticmethod
    def _tally_close(res: SymbolPollResult, row: dict) -> None:
        s = row.get("signal_status")
        if s == "TP":
            res.closed_tp += 1
        elif s == "SL":
            res.closed_sl += 1
        elif s in ("BE", "INTRABAR_AMBIGUOUS"):
            res.closed_be += 1

    def _process_one_15m(
        self,
        st: SymbolState,
        bar: dict,
        *,
        emit_signals: bool,
        log_ts: datetime | None,
        report_from: datetime | None = None,
        report_to: datetime | None = None,
    ) -> list[dict]:
        ensure_runtime_paths()
        from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index

        moment = bar["close_time"]
        idx = bar["candle_index"]
        pools, _ui, _proof = scanner_pools_for_index(st.candles15, "15m", idx, st.lld_cfg, cache=st.lld_cache)
        sig = evaluate_geometry_bar(st.symbol, pools, bar, moment, st.by_open)
        if sig is None:
            return []

        pool_id = sig["pool_id"]
        if pool_id in st.seen_pool_ids:
            return []
        st.seen_pool_ids.add(pool_id)

        if not emit_signals:
            return []

        rf = report_from or self.config.report_from
        rt = report_to or self.config.report_to
        if moment < rf or moment > rt:
            return []

        ts = log_ts or moment
        rows: list[dict] = []
        base = {
            "event": "signal",
            "timestamp": ts.isoformat(),
            "side": "LONG",
            "symbol": st.symbol,
            "decision_time": sig["decision_time"],
            "entry_time": sig["entry_time"],
            "entry_price": sig["entry_price"],
            "stop": sig["stop"],
            "tp": sig["tp"],
            "pool_id": pool_id,
            "m15_lower_2_age_h": sig["m15_lower_2_age_h"],
            "be_trigger_pct": sig["be_trigger_pct"],
            "gap_pct": sig.get("gap_pct"),
            "tp_gap_pct": sig.get("tp_gap_pct"),
        }

        rows.append({**base, "signal_status": "GEOMETRY"})
        if self.registry:
            self.registry.register_long_geometry(sig, detected_at=ts.isoformat())

        if not sig["ladder_pass"]:
            rows.append({**base, "signal_status": "LADDER_BLOCKED"})
            trade = self._open_shadow(st, sig, hypothetical=True)
            rows.append(log_row_from_trade(trade, signal_status="OPEN", timestamp=ts))
            return rows

        row = {**base, "signal_status": "ALLOWED"}
        rows.append(row)
        trade = self._open_shadow(st, sig, hypothetical=False)
        rows.append(log_row_from_trade(trade, signal_status="OPEN", timestamp=ts))
        return rows

    def _open_shadow(self, st: SymbolState, sig: dict, *, hypothetical: bool) -> ShadowTrade:
        sid = signal_id_for(sig)
        trade = ShadowTrade(
            signal_id=sid,
            symbol=st.symbol,
            pool_id=sig["pool_id"],
            decision_time=sig["decision_time"],
            entry_time=sig["entry_time"],
            entry_price=float(sig["entry_price"]),
            original_stop=float(sig["stop"]),
            active_stop=float(sig["stop"]),
            tp=float(sig["tp"]),
            ladder_age_h=sig.get("m15_lower_2_age_h"),
            hypothetical=hypothetical,
        )
        st.open_trades.append(trade)
        return trade

    def _process_management(
        self,
        st: SymbolState,
        now: datetime,
        *,
        emit_signals: bool,
        log_ts: datetime,
    ) -> list[dict]:
        open_trades = [t for t in st.open_trades if t.status == "OPEN"]
        if not open_trades:
            return []
        rows: list[dict] = []
        end = self._load_end(now)
        for trade in open_trades:
            entry_close = datetime.fromisoformat(trade.decision_time)
            if trade.last_processed_1m_close:
                start = datetime.fromisoformat(trade.last_processed_1m_close) + timedelta(minutes=1)
            else:
                start = entry_close
            bars1m = load_1m_bars(st.symbol, start, end)
            for bar in bars1m:
                closed, be_trig = process_one_1m_bar(trade, bar)
                if not emit_signals:
                    continue
                if be_trig and self.registry:
                    sid = trade.signal_id
                    if sid in self.registry.snapshots:
                        self.registry._append(sid, "BE_TRIGGERED", trade.symbol, {})
                if closed:
                    st.open_trades = [t for t in st.open_trades if t.status == "OPEN"]
                    st.closed_trades.append(trade)
                    if self.registry:
                        self.registry.sync_close_from_scanner(trade)
                    rows.append(
                        log_row_from_trade(trade, signal_status=trade.status, timestamp=log_ts)
                    )
        return rows


def data_readiness_row(st: SymbolState) -> dict[str, Any]:
    missing: list[str] = []
    if st.readiness == Readiness.DATA_MISSING:
        missing.append(st.data_missing_reason or "unknown")
    elif st.readiness == Readiness.WAITING_FOR_DATA:
        missing.append(st.data_missing_reason or "waiting")
    warmup_ok = st.readiness == Readiness.LIVE_READY and not missing
    return {
        "symbol": st.symbol,
        "status": st.readiness.value,
        "missing": missing,
        "warmup_ok": warmup_ok,
    }

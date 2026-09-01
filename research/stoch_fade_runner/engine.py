"""In-memory evaluation using canonical causal Wave-Fade functions. No ClickHouse writes."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .candles import CandleSource
from .first_valid import attach_confirmation_times, first_valid_from_indicators
from .htf import audit_htf_buckets
from .config import (
    CONFIRMATION_SOURCE,
    EVAL_ERROR,
    EVAL_INCOMPLETE,
    EVAL_NO_CANDLE,
    EVAL_NO_SIGNAL,
    EVAL_WITH_SIGNALS,
    EXIT_POLICY,
    GENERATOR_VERSION,
    INTRABAR_POLICY,
    OUTCOME_ENGINE,
    SIDE_EFFECT_FLAGS,
    STRATEGY_ID,
    WARMUP_DAYS,
    assert_frozen_pin,
    candle_load_start,
    ensure_sg_on_path,
)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_maybe(ts: Any) -> str | None:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.isoformat().replace("+00:00", "Z")


def _signal_record(sig: Any, row: Any, *, entry_valid: bool, rejection: str | None) -> dict[str, Any]:
    meta = getattr(sig, "metadata", None)
    try:
        meta_obj = {} if not meta else __import__("json").loads(meta)
    except Exception:
        meta_obj = {}
    return {
        "setup_id": str(getattr(sig, "setup_id", None) or meta_obj.get("setup_id") or ""),
        "signal_id": str(sig.signal_id),
        "symbol": sig.symbol,
        "timeframe": sig.timeframe,
        "direction": sig.direction,
        "signal_type": sig.signal_type,
        "start_ts": _iso_maybe(row.get("start_ts")) or meta_obj.get("start_ts"),
        "end_ts": _iso_maybe(row.get("end_ts")) or meta_obj.get("end_ts"),
        "start_available_at": _iso_maybe(row.get("start_available_at")) or meta_obj.get("start_available_at"),
        "end_available_at": _iso_maybe(row.get("end_available_at")) or meta_obj.get("end_available_at"),
        "recognition_ts": _iso_maybe(row.get("recognition_ts")) or meta_obj.get("recognition_ts"),
        "recognition_available_at": _iso_maybe(row.get("recognition_available_at"))
        or meta_obj.get("recognition_available_at"),
        "candle_open_time": sig.candle_open_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "candle_close_time": sig.candle_close_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "generated_at": sig.generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "confirmation_available_at": _iso_maybe(row.get("confirmation_available_at")),
        "confirmation_policy": str(row.get("confirmation_source") or CONFIRMATION_SOURCE),
        "confirmation_source": str(row.get("confirmation_source") or CONFIRMATION_SOURCE),
        "tier_a": bool(sig.tier_a),
        "is_q4": bool(row.get("is_q4", False)),
        "trend_bucket": str(row.get("trend_bucket") or ""),
        "eff_quantile": str(row.get("eff_quantile") or ""),
        "entry_valid": bool(entry_valid),
        "entry_time": _iso_maybe(row.get("entry_time")) if entry_valid else _iso_maybe(row.get("entry_time")),
        "entry_price": None if not entry_valid else float(row["entry_price"]),
        "tp_price": None if row.get("tp_price") is None or pd.isna(row.get("tp_price")) else float(row["tp_price"]),
        "sl_price": None if row.get("sl_price") is None or pd.isna(row.get("sl_price")) else float(row["sl_price"]),
        "strategy_version": sig.strategy_version,
        "generator_version": sig.generator_version,
        "exit_policy": EXIT_POLICY,
        "intrabar_policy": INTRABAR_POLICY,
        "outcome_engine": OUTCOME_ENGINE,
        "uses_be50_exit": False,
        "max_hold": "disabled",
        "rejection_reason": rejection,
        "selection_status": "NOT_APPLIED_AMBIGUOUS",
        "metadata": sig.metadata,
    }


def parameter_hash() -> str:
    ensure_sg_on_path()
    from signal_generator.pipeline.versions import EDGES_VERSION
    from signal_generator.strategy.wave_fade.parameters import SIGNAL_TFS, SOURCE_COMMIT

    raw = f"{STRATEGY_ID}|{SOURCE_COMMIT}|{EDGES_VERSION}|{','.join(SIGNAL_TFS)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _records_from_frame(raw: pd.DataFrame) -> list[dict[str, Any]]:
    df = raw.copy()
    ot = pd.to_datetime(df["open_time"], utc=True)
    df["open_time"] = ot
    if "close_time" not in df.columns:
        df["close_time"] = ot + pd.Timedelta(minutes=1)
    else:
        ct = pd.to_datetime(df["close_time"], utc=True)
        df["close_time"] = ct.fillna(ot + pd.Timedelta(minutes=1))
    return df.to_dict("records")


def evaluate_symbol(
    *,
    symbol: str,
    candle_source: CandleSource,
    signal_start: datetime,
    signal_end_exclusive: datetime,
    recorder: Any | None = None,
    call_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    from contextlib import nullcontext

    from .htf import aggregate_1m_to_timeframe
    from .stages import StageRecorder

    rec = recorder if recorder is not None else StageRecorder(None)
    counts = call_counts if call_counts is not None else {}

    def _count(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1

    with rec.stage("identity_validation") if hasattr(rec, "stage") else nullcontext():
        assert_frozen_pin()
    ensure_sg_on_path()
    from signal_generator.pipeline.mapper import wave_event_to_signal
    from signal_generator.pipeline.trade_plan import attach_resolved_entries
    from signal_generator.pipeline.versions import EDGES_VERSION
    from signal_generator.strategy.wave_fade.adapter import bars_to_ohlcv_df, one_minute_books
    from signal_generator.strategy.wave_fade.edges import load_frozen_eff_edges
    from signal_generator.strategy.wave_fade.parameters import CONFIRMATION_CROSS_RECOGNITION, SIGNAL_TFS
    from signal_generator.strategy.wave_fade.indicators import attach_indicators
    from signal_generator.strategy.wave_fade.signals import (
        build_symbol_signals,
        build_waves_from_ohlcv,
    )
    from signal_generator.timeframes import bars_from_mappings, ensure_utc

    load_start = candle_load_start(signal_start)
    try:
        with rec.stage("clickhouse_load") as st:
            raw = candle_source.get_candles(symbol, load_start, signal_end_exclusive)
            st["output_rows"] = 0 if raw is None or raw.empty else int(len(raw))
    except Exception as exc:
        rec.mark("FAILED")
        return {
            "symbol": symbol,
            "status": EVAL_ERROR,
            "error": str(exc),
            "signals": [],
            "warmup_complete": False,
            "side_effect_flags": dict(SIDE_EFFECT_FLAGS),
            "call_counts": counts,
        }
    if raw is None or raw.empty:
        rec.mark("COMPLETED")
        return {
            "symbol": symbol,
            "status": EVAL_NO_CANDLE,
            "signals": [],
            "warmup_complete": False,
            "bars_1m": 0,
            "side_effect_flags": dict(SIDE_EFFECT_FLAGS),
            "call_counts": counts,
        }

    in_window = pd.to_datetime(raw["open_time"], utc=True)
    window_bars = int(((in_window >= signal_start) & (in_window < signal_end_exclusive)).sum())
    if window_bars <= 0:
        rec.mark("COMPLETED")
        return {
            "symbol": symbol,
            "status": EVAL_INCOMPLETE,
            "signals": [],
            "warmup_complete": False,
            "bars_1m": int(len(raw)),
            "side_effect_flags": dict(SIDE_EFFECT_FLAGS),
            "call_counts": counts,
        }

    with rec.stage("candle_normalization", input_rows=int(len(raw))) as st:
        records = _records_from_frame(raw)
        actual_load_start = pd.to_datetime(raw["open_time"], utc=True).min().to_pydatetime()
        bars_1m = bars_from_mappings(records)
        as_of = max(ensure_utc(b.close_time) for b in bars_1m)
        end = ensure_utc(signal_end_exclusive)
        if as_of > end:
            as_of = end
        ohlcv_1m = bars_to_ohlcv_df(bars_1m)
        open_times, opens = one_minute_books(ohlcv_1m)
        edges = load_frozen_eff_edges()
        st["output_rows"] = len(bars_1m)

    waves_by_tf: dict[str, pd.DataFrame] = {}
    htf_counts: dict[str, int] = {}
    first_valid_meta: dict[str, dict[str, Any]] = {}
    htf_audits: dict[str, dict[str, Any]] = {}
    adapter_parity: dict[str, Any] = {}
    tf_key = {"15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h"}
    for tf in SIGNAL_TFS:
        label = tf_key[tf]
        with rec.stage(f"aggregate_{label}", input_rows=len(bars_1m)) as st:
            _count("aggregate_1m_to_timeframe")
            htf_bars = aggregate_1m_to_timeframe(
                bars_1m, tf, as_of=as_of, require_complete=True
            )
            ohlcv = bars_to_ohlcv_df(htf_bars)
            st["output_rows"] = int(len(ohlcv))
        htf_counts[tf] = int(len(ohlcv))
        htf_audits[tf] = audit_htf_buckets(bars_1m, tf, as_of=as_of)
        with rec.stage(f"indicators_waves_{label}", input_rows=int(len(ohlcv))) as st:
            _count("build_waves_from_ohlcv")
            ind = attach_indicators(ohlcv) if not ohlcv.empty else ohlcv
            first_valid_meta[tf] = first_valid_from_indicators(ind, signal_start=signal_start)
            waves_by_tf[tf] = build_waves_from_ohlcv(ohlcv, symbol=symbol, timeframe=tf)
            st["output_rows"] = int(len(waves_by_tf[tf]))
    with rec.stage("htf_adapter_parity_slices") as st:
        from signal_generator.timeframes import aggregate_1m_to_timeframe as sg_agg

        n = len(bars_1m)
        slices = {
            "head_10080": bars_1m[: min(n, 10080)],
            "tail_10080": bars_1m[max(0, n - 10080) :],
        }
        for name, sl in slices.items():
            adapter_parity[name] = {}
            for tf in SIGNAL_TFS:
                slow = sg_agg(sl, tf, as_of=as_of, require_complete=True)
                fast = aggregate_1m_to_timeframe(sl, tf, as_of=as_of, require_complete=True)
                adapter_parity[name][tf] = {
                    "sg_count": len(slow),
                    "runner_count": len(fast),
                    "open_times_equal": [b.open_time for b in slow] == [b.open_time for b in fast],
                }
        st["output_rows"] = n

    with rec.stage("signals_batch") as st:
        _count("build_symbol_signals")
        all_sig = build_symbol_signals(
            symbol, edges, waves_by_tf, confirmation_source=CONFIRMATION_CROSS_RECOGNITION
        )
        st["output_rows"] = 0 if all_sig is None or all_sig.empty else int(len(all_sig))

    raw_candidates: list[dict[str, Any]] = []
    collected: list[dict[str, Any]] = []
    technical_dups: list[dict[str, Any]] = []
    if all_sig is not None and not all_sig.empty:
        for tf in SIGNAL_TFS:
            tf_all = all_sig[all_sig["signal_tf"].astype(str) == tf]
            first_valid_meta[tf] = attach_confirmation_times(first_valid_meta.get(tf, {}), tf_all)
        conf = pd.to_datetime(all_sig["confirmation_available_at"], utc=True)
        events = all_sig.loc[(conf >= signal_start) & (conf < signal_end_exclusive)].copy()
        for tf in SIGNAL_TFS:
            label = tf_key[tf]
            tf_events = events[events["signal_tf"].astype(str) == tf].copy() if not events.empty else events
            with rec.stage(f"signals_{label}", input_rows=int(len(tf_events))) as st:
                st["output_rows"] = int(len(tf_events))
            if tf_events is None or tf_events.empty:
                continue
            with rec.stage(f"entries_{label}", input_rows=int(len(tf_events))) as st:
                _count("attach_resolved_entries")
                tf_events = attach_resolved_entries(tf_events, open_times, opens)
                st["output_rows"] = int(len(tf_events))
            for _, row in tf_events.iterrows():
                entry_valid = bool(row.get("entry_valid", False))
                rejection = None
                entry_ts = row.get("entry_time")
                if entry_valid and entry_ts is not None and not pd.isna(entry_ts):
                    et = pd.Timestamp(entry_ts)
                    if et.tzinfo is None:
                        et = et.tz_localize("UTC")
                    else:
                        et = et.tz_convert("UTC")
                    if et.to_pydatetime() >= end:
                        entry_valid = False
                        rejection = "ENTRY_OUTSIDE_EXCLUSIVE_END"
                        row = row.copy()
                        row["entry_valid"] = False
                sig = wave_event_to_signal(
                    row,
                    symbol=symbol,
                    timeframe=tf,
                    generator_version=GENERATOR_VERSION,
                    strategy_version=STRATEGY_ID,
                    edges_version=EDGES_VERSION,
                    selected=False,
                    selection_reason="NOT_APPLIED_AMBIGUOUS",
                )
                rec_row = _signal_record(sig, row, entry_valid=entry_valid, rejection=rejection)
                raw_candidates.append(rec_row)

    with rec.stage("technical_dedup", input_rows=len(raw_candidates)) as st:
        seen_ids: set[str] = set()
        for rec_row in raw_candidates:
            sid = str(rec_row["signal_id"])
            if sid in seen_ids:
                technical_dups.append(rec_row)
                continue
            seen_ids.add(sid)
            collected.append(rec_row)
        st["output_rows"] = len(collected)

    if all_sig is None or (hasattr(all_sig, "empty") and all_sig.empty):
        for tf in SIGNAL_TFS:
            first_valid_meta[tf] = attach_confirmation_times(first_valid_meta.get(tf, {}), None)
    per_tf: dict[str, dict[str, int]] = {}
    for tf in SIGNAL_TFS:
        rows = [s for s in raw_candidates if s["timeframe"] == tf]
        per_tf[tf] = {
            "raw_candidates": len(rows),
            "tier_a": sum(1 for s in rows if s["tier_a"]),
            "non_tier_a": sum(1 for s in rows if not s["tier_a"]),
            "entry_resolved": sum(1 for s in rows if s["entry_valid"]),
            "entry_unresolved": sum(1 for s in rows if not s["entry_valid"]),
        }
    warmup_complete = all(bool(first_valid_meta.get(tf, {}).get("warmup_complete")) for tf in SIGNAL_TFS)
    candle_stats = dict(getattr(candle_source, "last_stats", {}) or {})
    ot_series = pd.to_datetime(raw["open_time"], utc=True)
    candle_stats.update(
        {
            "first_loaded_1m": _iso(ot_series.min().to_pydatetime()),
            "last_loaded_1m": _iso(ot_series.max().to_pydatetime()),
            "loaded_count": int(len(raw)),
            "uniq_timestamp_count": int(ot_series.nunique()),
            "signal_window_count": window_bars,
        }
    )
    tier_a_raw = [s for s in raw_candidates if s["tier_a"]]
    tier_a_dedup = [s for s in collected if s["tier_a"]]
    status = EVAL_WITH_SIGNALS if tier_a_dedup else EVAL_NO_SIGNAL
    rec.mark("COMPLETED")
    return {
        "symbol": symbol,
        "status": status,
        "signals": collected,
        "raw_candidates": raw_candidates,
        "technical_duplicates": technical_dups,
        "tier_a_count": len(tier_a_dedup),
        "tier_a_raw_count": len(tier_a_raw),
        "non_tier_a_count": sum(1 for s in raw_candidates if not s["tier_a"]),
        "counts_by_timeframe": per_tf,
        "raw_count": len(raw_candidates),
        "technical_duplicate_count": len(technical_dups),
        "entry_resolved_count": sum(1 for s in collected if s["entry_valid"]),
        "entry_unresolved_count": sum(1 for s in collected if not s["entry_valid"]),
        "warmup_complete": warmup_complete,
        "warmup_days": WARMUP_DAYS,
        "htf_bar_counts": htf_counts,
        "first_valid_indicator_time": {
            tf: first_valid_meta.get(tf, {}).get("first_indicator_valid_at") for tf in SIGNAL_TFS
        },
        "first_valid_by_timeframe": first_valid_meta,
        "htf_bucket_audit": htf_audits,
        "htf_adapter_parity_slices": adapter_parity,
        "candle_parity": candle_stats,
        "bars_1m": int(len(raw)),
        "window_bars_1m": window_bars,
        "signal_start": _iso(signal_start),
        "signal_end_exclusive": _iso(signal_end_exclusive),
        "requested_candle_load_start": _iso(load_start),
        "actual_candle_load_start": _iso(actual_load_start),
        "candle_load_start": _iso(load_start),
        "strategy_version": STRATEGY_ID,
        "confirmation_policy": CONFIRMATION_SOURCE,
        "confirmation_source": CONFIRMATION_SOURCE,
        "exit_policy": EXIT_POLICY,
        "intrabar_policy": INTRABAR_POLICY,
        "outcome_engine": OUTCOME_ENGINE,
        "side_effect_flags": dict(SIDE_EFFECT_FLAGS),
        "execution_dedup_policy": "none_phase_2a",
        "selection_status": "NOT_APPLIED_AMBIGUOUS",
        "as_of": _iso(as_of),
        "call_counts": counts,
        "stages": list(getattr(rec, "stages", [])),
    }


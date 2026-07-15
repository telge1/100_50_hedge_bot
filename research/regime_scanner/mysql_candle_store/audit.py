"""Reproducibility and Direct-vs-Aggregated audits for the candle store."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from research.regime_scanner.htf_freqtrade_equality_audit import (
    ABS_TOL,
    REL_TOL,
    values_equal_exact,
    values_within_tol,
)
from research.regime_scanner.mysql_candle_store.aggregator import aggregate_htf_from_store
from research.regime_scanner.mysql_candle_store.hashing import (
    HTF_EQUALITY_AUDIT_HASH,
    candles_export_hash,
    json_hash,
)
from research.regime_scanner.mysql_candle_store.repository import load_candles, summarize_timeframe
from research.regime_scanner.mysql_candle_store.schema import SOURCE_FREQTRADE_DIRECT
from research.regime_scanner.mysql_candle_store.store_memory import CandleStore
from research.regime_scanner.timeframes import (
    aggregate_candles,
    ensure_utc_timestamp,
    timeframe_timedelta,
)


@dataclass
class StoreAuditReport:
    exchange: str
    symbol: str
    timeframes: dict[str, dict[str, Any]] = field(default_factory=dict)
    equality: dict[str, dict[str, Any]] = field(default_factory=dict)
    direct_vs_agg: dict[str, dict[str, Any]] = field(default_factory=dict)
    deterministic_hash: str | None = None
    htf_equality_audit_hash_reference: str = HTF_EQUALITY_AUDIT_HASH
    htf_equality_audit_hash_note: str = (
        "Reference hash belongs to results_htf_freqtrade_equality_audit serialization; "
        "DB export uses candles_export_hash / store audit hash instead."
    )
    errors: list[str] = field(default_factory=list)
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compare_shared(
    direct: pd.DataFrame,
    aggregated: pd.DataFrame,
    *,
    timeframe: str,
) -> dict[str, Any]:
    d = direct.copy()
    a = aggregated.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True)
    a["timestamp"] = pd.to_datetime(a["timestamp"], utc=True)
    shared = sorted(set(d["timestamp"]) & set(a["timestamp"]))
    only_direct = sorted(set(d["timestamp"]) - set(a["timestamp"]))
    only_agg = sorted(set(a["timestamp"]) - set(d["timestamp"]))
    di = d.set_index("timestamp")
    ai = a.set_index("timestamp")
    ohlc_exact = 0
    vol_exact = 0
    vol_within = 0
    vol_outside = 0
    max_diffs = {c: 0.0 for c in ("open", "high", "low", "close", "volume")}
    for ts in shared:
        left = di.loc[ts]
        right = ai.loc[ts]
        if isinstance(left, pd.DataFrame):
            left = left.iloc[0]
        if isinstance(right, pd.DataFrame):
            right = right.iloc[0]
        if all(values_equal_exact(float(left[c]), float(right[c])) for c in ("open", "high", "low", "close")):
            ohlc_exact += 1
        for c in ("open", "high", "low", "close", "volume"):
            diff = abs(float(left[c]) - float(right[c]))
            if diff > max_diffs[c]:
                max_diffs[c] = diff
        if values_equal_exact(float(left["volume"]), float(right["volume"])):
            vol_exact += 1
            vol_within += 1
        elif values_within_tol(
            float(left["volume"]), float(right["volume"]), abs_tol=ABS_TOL, rel_tol=REL_TOL
        ):
            vol_within += 1
        else:
            vol_outside += 1
    n = len(shared)
    return {
        "timeframe": timeframe,
        "shared": n,
        "only_in_direct": len(only_direct),
        "only_in_aggregated": len(only_agg),
        "ohlc_exact": ohlc_exact,
        "ohlc_exact_rate": (ohlc_exact / n) if n else None,
        "volume_exact": vol_exact,
        "volume_within_tolerance": vol_within,
        "volume_within_tolerance_rate": (vol_within / n) if n else None,
        "volume_outside_tolerance": vol_outside,
        "max_diffs": max_diffs,
        "ok": ohlc_exact == n and vol_outside == 0 and len(only_agg) == 0,
        # only_in_direct after 5m end is expected and not a failure by itself
    }


def compare_direct_htf_with_5m_aggregation(
    store: CandleStore,
    *,
    exchange: str,
    symbol: str,
) -> dict[str, Any]:
    """Compare stored Direct HTF vs temporary in-memory aggregation from stored 5m."""
    five = load_candles(store, exchange, symbol, "5m", closed_only=True)
    out: dict[str, Any] = {"five_m_rows": int(len(five)), "timeframes": {}}
    if five.empty:
        out["error"] = "no 5m candles"
        return out
    five_last = ensure_utc_timestamp(five["timestamp"].iloc[-1])
    five_last_close = five_last + timeframe_timedelta("5m")
    base = five.loc[:, ["timestamp", "open", "high", "low", "close", "volume"]].copy()
    decision = five_last_close
    for tf in ("15m", "30m"):
        direct = load_candles(
            store, exchange, symbol, tf, closed_only=True, source=SOURCE_FREQTRADE_DIRECT
        )
        agg = aggregate_candles(base, tf, decision)
        if agg.empty and direct.empty:
            out["timeframes"][tf] = {"shared": 0, "ok": True, "direct_only_after_5m_end": 0}
            continue
        cmp = _compare_shared(direct, agg, timeframe=tf)
        if not direct.empty:
            if not agg.empty:
                last_agg_open = ensure_utc_timestamp(agg["timestamp"].iloc[-1])
                direct_after = direct.loc[
                    pd.to_datetime(direct["timestamp"], utc=True) > last_agg_open
                ]
            else:
                direct_after = direct
            cmp["direct_only_after_5m_end"] = int(len(direct_after))
            cmp["direct_total"] = int(len(direct))
            cmp["aggregated_temp_total"] = int(len(agg))
            cmp["ok"] = (
                cmp["ohlc_exact"] == cmp["shared"]
                and cmp["volume_outside_tolerance"] == 0
                and cmp["only_in_aggregated"] == 0
            )
        else:
            cmp["direct_only_after_5m_end"] = 0
        out["timeframes"][tf] = cmp
    return out


def audit_candle_store(
    store: CandleStore,
    *,
    exchange: str,
    symbol: str,
    persist_validation_row: bool = True,
    compare_direct_htf_with_5m: bool = True,
) -> StoreAuditReport:
    report = StoreAuditReport(exchange=exchange, symbol=symbol)
    for tf in ("5m", "15m", "30m"):
        summary = summarize_timeframe(store, exchange=exchange, symbol=symbol, timeframe=tf)
        frame = load_candles(store, exchange, symbol, timeframe=tf, closed_only=True)
        if not frame.empty:
            summary["export_hash"] = candles_export_hash(frame)
            if frame["timestamp"].duplicated().any():
                report.errors.append(f"{tf}: duplicate timestamps")
        report.timeframes[tf] = summary

    five = load_candles(store, exchange, symbol, "5m", closed_only=True)
    if five.empty:
        report.errors.append("no 5m candles for audit")
        report.ok = False
        report.deterministic_hash = json_hash(report.to_dict())
        return report

    # Dual aggregation dry-run determinism (fill-missing / validate-only does not overwrite).
    a1 = aggregate_htf_from_store(
        store,
        exchange=exchange,
        symbol=symbol,
        timeframes=["15m", "30m"],
        mode="validate-only",
        dry_run=True,
    )
    a2 = aggregate_htf_from_store(
        store,
        exchange=exchange,
        symbol=symbol,
        timeframes=["15m", "30m"],
        mode="validate-only",
        dry_run=True,
    )
    h1 = {tf: a1.results.get(tf, {}).get("export_hash") for tf in ("15m", "30m")}
    h2 = {tf: a2.results.get(tf, {}).get("export_hash") for tf in ("15m", "30m")}
    report.equality["aggregation_validate_only_hashes"] = {
        "pass1": h1,
        "pass2": h2,
        "match": h1 == h2,
    }
    if h1 != h2:
        report.errors.append("aggregation validate-only hashes are not deterministic")

    if compare_direct_htf_with_5m:
        cmp = compare_direct_htf_with_5m_aggregation(
            store, exchange=exchange, symbol=symbol
        )
        report.direct_vs_agg = cmp
        for tf, payload in (cmp.get("timeframes") or {}).items():
            if payload.get("ok") is False:
                report.errors.append(f"{tf}: direct vs 5m-aggregation mismatch in common window")

    body = {
        "exchange": exchange,
        "symbol": symbol,
        "timeframes": report.timeframes,
        "equality": report.equality,
        "direct_vs_agg": report.direct_vs_agg,
        "errors": report.errors,
    }
    report.deterministic_hash = json_hash(body)
    report.ok = not report.errors

    if persist_validation_row:
        store.insert_validation_run(
            {
                "validation_type": "candle_store_audit",
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": None,
                "canonical_source": "market_candles mixed direct+optional aggregated",
                "comparison_source": "timeframes.aggregate_candles(temp)",
                "common_start": report.timeframes.get("5m", {}).get("min_open_time"),
                "common_end": report.timeframes.get("5m", {}).get("max_open_time"),
                "row_count": report.timeframes.get("5m", {}).get("rows"),
                "shared_buckets": (report.direct_vs_agg.get("timeframes") or {})
                .get("15m", {})
                .get("shared"),
                "ohlc_mismatches": None,
                "volume_within_tolerance": (report.direct_vs_agg.get("timeframes") or {})
                .get("15m", {})
                .get("volume_within_tolerance"),
                "deterministic_output_hash": report.deterministic_hash,
                "metadata_json": {
                    "htf_equality_audit_hash_reference": HTF_EQUALITY_AUDIT_HASH,
                    "direct_vs_agg": report.direct_vs_agg,
                    "timeframes": report.timeframes,
                },
            }
        )
    return report


def record_direct_htf_validation_metadata(
    store: CandleStore,
    *,
    exchange: str,
    symbol: str,
    fifteen_path: str | None,
    thirty_path: str | None,
    fifteen_sha256: str | None,
    thirty_sha256: str | None,
    equality_audit_hash: str = HTF_EQUALITY_AUDIT_HASH,
) -> list[int]:
    """Persist Direct-HTF file hashes as validation metadata (in addition to operational import)."""
    ids: list[int] = []
    for tf, path, digest in (
        ("15m", fifteen_path, fifteen_sha256),
        ("30m", thirty_path, thirty_sha256),
    ):
        if not path and not digest:
            continue
        rid = store.insert_validation_run(
            {
                "validation_type": "direct_htf_file_reference",
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": tf,
                "canonical_source": SOURCE_FREQTRADE_DIRECT,
                "comparison_source": "htf_freqtrade_equality_audit",
                "input_path": path,
                "input_sha256": digest,
                "deterministic_output_hash": equality_audit_hash,
                "metadata_json": {
                    "role": "operational_bootstrap_source_plus_audit_reference",
                    "imported_into_market_candles": True,
                    "htf_equality_audit_hash": equality_audit_hash,
                    "bootstrap_policy": "historical_direct_feather_import",
                    "ongoing_policy": "5m_canonical_with_optional_aggregated_fill_missing",
                },
            }
        )
        ids.append(rid)
    return ids

"""Causal discovery for OI/liquidation flush, impact compression and L2 recovery.

This module produces descriptive observations, not trading signals. It contains
no profitability rule, no threshold search and no Strategy Lab adapter.
Future outcomes are written to a separate label sidecar and never merged back
into predictor rows.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.contracts import (
    AGGRESSIVE_NOTIONAL_COLUMN_BY_DIRECTION,
    AGGRESSOR_SIDE_BY_DIRECTION,
    L2_ADDED_COLUMN_BY_DIRECTION,
    L2_DEPTH_COLUMN_BY_DIRECTION,
    L2_OPPOSING_DEPTH_COLUMN_BY_DIRECTION,
    L2_RECOVERY_RELATION,
    L2_REMOVED_COLUMN_BY_DIRECTION,
    L2_SIDE_BY_DIRECTION,
    LIQUIDATION_COUNT_COLUMN_BY_DIRECTION,
    LIQUIDATION_NOTIONAL_COLUMN_BY_DIRECTION,
    LIQUIDATION_SIDE_BY_DIRECTION,
    OPPOSITE_NOTIONAL_COLUMN_BY_DIRECTION,
    ORDERBOOK_CARRIED_FORWARD_POLICY,
    ORDERBOOK_DEPTH,
    ORDERBOOK_EXPECTED_SECONDS_PER_MINUTE,
    ORDERBOOK_GENUINE_DESCRIPTION,
    ORDERBOOK_PARSER_VERSION,
    ORDERBOOK_TABLE,
)

FORMAT_VERSION = "oi_liq_impact_l2_discovery/v2"
DIRECTIONS = ("LONG", "SHORT")

MINUTE_FEATURE_COLUMNS = (
    "symbol",
    "minute",
    "decision_at",
    "direction",
    "technical_gap",
    "quality_reason",
    "candle_present",
    "trades_present",
    "oi_present",
    "oi_state_valid",
    "orderbook_present",
    "ob_seconds",
    "ob_invalid_seconds",
    "ob_carried_forward_seconds",
    "ob_genuine_seconds",
    "ob_carried_forward_rate",
    "ob_genuine_rate",
    "open",
    "high",
    "low",
    "close",
    "previous_close",
    "close_vs_previous_close_pct",
    "price_displacement_pct",
    "directional_adverse_displacement_pct",
    "oi_last",
    "oi_delta_abs_1m",
    "oi_delta_pct_1m",
    "aggressive_notional",
    "opposite_notional",
    "trade_count",
    "liquidation_count",
    "liquidation_notional",
    "liquidation_to_aggressive_notional",
    "impact_per_aggressive_notional",
    "previous_impact_per_aggressive_notional",
    "aggressive_notional_change",
    "impact_compression_observed",
    "genuine_spread_bps_mean",
    "genuine_imbalance_l50_mean",
    "directional_imbalance",
    "genuine_support_depth_l50_mean",
    "genuine_opposing_depth_l50_mean",
    "directional_depth_change",
    "directional_imbalance_change",
    "directional_ofi",
    "directional_net_add",
    "directional_net_add_change",
    "l2_recovery_observed",
    "directional_flush_observed",
    "stage_reached",
)

CANDIDATE_COLUMNS = (
    "candidate_id",
    "symbol",
    "minute",
    "decision_at",
    "direction",
    "stage_reached",
    "quality_reason",
    "price_displacement_pct",
    "oi_delta_pct_1m",
    "liquidation_count",
    "liquidation_notional",
    "aggressive_notional",
    "impact_per_aggressive_notional",
    "previous_impact_per_aggressive_notional",
    "impact_compression_observed",
    "l2_recovery_observed",
)

LABEL_COLUMNS = (
    "candidate_id",
    "symbol",
    "direction",
    "decision_at",
    "entry_at",
    "entry_price",
    "label_horizon_minutes",
    "mfe_pct",
    "mae_pct",
    "forward_return_pct",
    "label_status",
)

_DISTRIBUTION_FEATURES = (
    "price_displacement_pct",
    "close_vs_previous_close_pct",
    "directional_adverse_displacement_pct",
    "oi_delta_pct_1m",
    "aggressive_notional",
    "liquidation_count",
    "liquidation_notional",
    "liquidation_to_aggressive_notional",
    "impact_per_aggressive_notional",
    "ob_carried_forward_rate",
    "ob_genuine_rate",
    "directional_imbalance",
    "genuine_support_depth_l50_mean",
    "directional_depth_change",
    "directional_imbalance_change",
    "directional_ofi",
    "directional_net_add",
    "directional_net_add_change",
)


class DiscoveryError(ValueError):
    """Invalid discovery input or violated deterministic data contract."""


class DiscoveryLoader(Protocol):
    def __call__(
        self,
        client: Any,
        *,
        symbol: str,
        start: datetime,
        end: datetime,
        label_end: datetime,
    ) -> dict[str, pd.DataFrame]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscoveryInputs:
    symbol: str
    start: datetime
    end: datetime
    label_horizon_minutes: int
    candles: pd.DataFrame
    trades: pd.DataFrame
    open_interest: pd.DataFrame
    liquidations: pd.DataFrame
    orderbook: pd.DataFrame


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolDiscoveryResult:
    symbol: str
    minute_features: tuple[dict[str, object], ...]
    candidates: tuple[dict[str, object], ...]
    labels: tuple[dict[str, object], ...]
    quality: dict[str, object]


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscoveryRunResult:
    symbol_count: int
    minute_feature_count: int
    candidate_count: int
    output_dir: Path


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise DiscoveryError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _minute_text(value: pd.Timestamp | datetime) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat().replace("+00:00", "Z")


def _number(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _indexed(frame: pd.DataFrame, time_col: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    if time_col not in frame.columns:
        raise DiscoveryError(f"missing time column {time_col!r}")
    out = frame.copy()
    out[time_col] = pd.to_datetime(out[time_col], utc=True)
    if out[time_col].duplicated().any():
        raise DiscoveryError(f"duplicate {time_col} rows")
    return out.sort_values(time_col).set_index(time_col, drop=False)


def _row_at(frame: pd.DataFrame, ts: pd.Timestamp) -> Mapping[str, object] | None:
    if frame.empty or ts not in frame.index:
        return None
    row = frame.loc[ts]
    if isinstance(row, pd.DataFrame):
        raise DiscoveryError(f"duplicate rows at {ts}")
    return row


def _candidate_id(symbol: str, minute: str, direction: str) -> str:
    raw = f"{symbol}|{minute}|{direction}".encode("utf-8")
    return "oildisc:" + hashlib.sha1(raw).hexdigest()[:20]


def build_symbol_discovery(inputs: DiscoveryInputs) -> SymbolDiscoveryResult:
    """Build causal minute features and descriptive event observations."""
    start = _as_utc(inputs.start)
    end = _as_utc(inputs.end)
    if start >= end:
        raise DiscoveryError("start must be before end")
    if inputs.label_horizon_minutes <= 0:
        raise DiscoveryError("label_horizon_minutes must be positive")

    candles = _indexed(inputs.candles, "open_time")
    trades = _indexed(inputs.trades, "minute")
    oi = _indexed(inputs.open_interest, "minute")
    liquidations = _indexed(inputs.liquidations, "minute")
    orderbook = _indexed(inputs.orderbook, "minute")
    expected = pd.date_range(start, end, freq="1min", inclusive="left")

    features: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []
    labels: list[dict[str, object]] = []
    prior_impact_by_direction: dict[str, dict[str, object] | None] = {
        "LONG": None,
        "SHORT": None,
    }
    prior_l2_by_direction: dict[str, dict[str, object] | None] = {
        "LONG": None,
        "SHORT": None,
    }
    gap_minutes = 0

    previous_close: float | None = None
    previous_oi: float | None = None
    previous_oi_valid = False
    for minute in expected:
        candle = _row_at(candles, minute)
        trade = _row_at(trades, minute)
        oi_row = _row_at(oi, minute)
        liq = _row_at(liquidations, minute)
        ob = _row_at(orderbook, minute)

        candle_present = candle is not None
        trades_present = trade is not None
        oi_present = oi_row is not None
        ob_present = ob is not None
        oi_state_valid = bool(
            oi_row is not None and int(_number(oi_row.get("state_valid")) or 0) == 1
        )
        ob_seconds = int(_number(ob.get("seconds")) or 0) if ob is not None else 0
        ob_invalid = (
            int(_number(ob.get("invalid_seconds")) or 0) if ob is not None else 0
        )
        ob_cf = (
            int(_number(ob.get("carried_forward_seconds")) or 0)
            if ob is not None
            else 0
        )
        ob_genuine = (
            int(_number(ob.get("genuine_seconds")) or 0) if ob is not None else 0
        )

        reasons: list[str] = []
        if not candle_present:
            reasons.append("MISSING_CANDLE")
        if not oi_present:
            reasons.append("MISSING_OI")
        elif not oi_state_valid:
            reasons.append("INVALID_OI")
        if not ob_present:
            reasons.append("MISSING_ORDERBOOK")
        elif ob_seconds != ORDERBOOK_EXPECTED_SECONDS_PER_MINUTE:
            reasons.append("INCOMPLETE_ORDERBOOK_MINUTE")
        elif ob_invalid:
            reasons.append("INVALID_ORDERBOOK_SECONDS")
        technical_gap = bool(reasons)
        if technical_gap:
            gap_minutes += 1
            prior_impact_by_direction = {"LONG": None, "SHORT": None}
            prior_l2_by_direction = {"LONG": None, "SHORT": None}

        open_px = _number(candle.get("open")) if candle is not None else None
        high_px = _number(candle.get("high")) if candle is not None else None
        low_px = _number(candle.get("low")) if candle is not None else None
        close_px = _number(candle.get("close")) if candle is not None else None
        price_displacement = (
            (close_px - open_px) / open_px
            if open_px is not None and close_px is not None and open_px > 0
            else None
        )
        close_vs_previous = (
            (close_px - previous_close) / previous_close
            if close_px is not None
            and previous_close is not None
            and previous_close > 0
            else None
        )
        oi_last = (
            _number(oi_row.get("open_interest")) if oi_row is not None else None
        )
        oi_delta = (
            oi_last - previous_oi
            if oi_state_valid
            and previous_oi_valid
            and oi_last is not None
            and previous_oi is not None
            else None
        )
        oi_delta_pct = _safe_ratio(oi_delta, previous_oi)

        for direction in DIRECTIONS:
            is_long = direction == "LONG"
            adverse = (
                max(0.0, -price_displacement)
                if is_long and price_displacement is not None
                else (
                    max(0.0, price_displacement)
                    if price_displacement is not None
                    else None
                )
            )
            aggressive = (
                _number(
                    trade.get(AGGRESSIVE_NOTIONAL_COLUMN_BY_DIRECTION[direction])
                )
                if trade is not None
                else None
            )
            opposite = (
                _number(
                    trade.get(OPPOSITE_NOTIONAL_COLUMN_BY_DIRECTION[direction])
                )
                if trade is not None
                else None
            )
            liq_count = (
                int(
                    _number(
                        liq.get(LIQUIDATION_COUNT_COLUMN_BY_DIRECTION[direction])
                    )
                    or 0
                )
                if liq is not None
                else 0
            )
            liq_notional = (
                _number(
                    liq.get(LIQUIDATION_NOTIONAL_COLUMN_BY_DIRECTION[direction])
                )
                if liq is not None
                else 0.0
            )
            if liq_notional is None:
                liq_notional = 0.0
            impact = _safe_ratio(adverse, aggressive)
            previous = prior_impact_by_direction[direction]
            prev_impact = (
                _number(previous.get("impact_per_aggressive_notional"))
                if previous
                else None
            )
            prev_aggressive = (
                _number(previous.get("aggressive_notional")) if previous else None
            )
            aggressive_change = (
                aggressive - prev_aggressive
                if aggressive is not None and prev_aggressive is not None
                else None
            )
            compression = bool(
                not technical_gap
                and trades_present
                and impact is not None
                and prev_impact is not None
                and aggressive is not None
                and prev_aggressive is not None
                and aggressive >= prev_aggressive
                and impact < prev_impact
            )

            imbalance = (
                _number(ob.get("genuine_imbalance_l50_mean"))
                if ob is not None
                else None
            )
            support_depth = (
                _number(ob.get(L2_DEPTH_COLUMN_BY_DIRECTION[direction]))
                if ob is not None
                else None
            )
            opposing_depth = (
                _number(ob.get(L2_OPPOSING_DEPTH_COLUMN_BY_DIRECTION[direction]))
                if ob is not None
                else None
            )
            directional_imbalance = (
                None
                if imbalance is None
                else imbalance
                if is_long
                else -imbalance
            )
            previous_l2 = prior_l2_by_direction[direction]
            prior_support = (
                _number(previous_l2.get("genuine_support_depth_l50_mean"))
                if previous_l2
                else None
            )
            prior_imbalance = (
                _number(previous_l2.get("directional_imbalance"))
                if previous_l2
                else None
            )
            directional_depth_change = (
                support_depth - prior_support
                if support_depth is not None and prior_support is not None
                else None
            )
            imbalance_change = (
                directional_imbalance - prior_imbalance
                if directional_imbalance is not None and prior_imbalance is not None
                else None
            )
            ofi = _number(ob.get("genuine_ofi_sum")) if ob is not None else None
            directional_ofi = ofi if is_long else -ofi if ofi is not None else None
            added = (
                _number(ob.get(L2_ADDED_COLUMN_BY_DIRECTION[direction]))
                if ob is not None
                else None
            )
            removed = (
                _number(ob.get(L2_REMOVED_COLUMN_BY_DIRECTION[direction]))
                if ob is not None
                else None
            )
            directional_net_add = (
                added - removed
                if added is not None and removed is not None
                else None
            )
            previous_net_add = (
                _number(previous_l2.get("directional_net_add"))
                if previous_l2
                else None
            )
            directional_net_add_change = (
                directional_net_add - previous_net_add
                if directional_net_add is not None
                and previous_net_add is not None
                else None
            )
            l2_recovery = bool(
                not technical_gap
                and ob_genuine > 0
                and previous_l2 is not None
                and (
                    (
                        directional_depth_change is not None
                        and directional_depth_change > 0
                    )
                    or (imbalance_change is not None and imbalance_change > 0)
                    or (
                        directional_net_add_change is not None
                        and directional_net_add_change > 0
                    )
                )
            )
            flush = bool(
                not technical_gap
                and trades_present
                and adverse is not None
                and adverse > 0
                and oi_delta is not None
                and oi_delta < 0
                and liq_count > 0
                and aggressive is not None
                and aggressive > 0
            )
            if not flush:
                stage = "NONE"
            elif not compression:
                stage = "DIRECTIONAL_FLUSH_OBSERVED"
            elif not l2_recovery:
                stage = "IMPACT_COMPRESSION_OBSERVED"
            else:
                stage = "L2_RECOVERY_OBSERVED"

            minute_text = _minute_text(minute)
            row: dict[str, object] = {
                "symbol": inputs.symbol,
                "minute": minute_text,
                "decision_at": _minute_text(minute + pd.Timedelta(minutes=1)),
                "direction": direction,
                "technical_gap": technical_gap,
                "quality_reason": "|".join(reasons),
                "candle_present": candle_present,
                "trades_present": trades_present,
                "oi_present": oi_present,
                "oi_state_valid": oi_state_valid,
                "orderbook_present": ob_present,
                "ob_seconds": ob_seconds,
                "ob_invalid_seconds": ob_invalid,
                "ob_carried_forward_seconds": ob_cf,
                "ob_genuine_seconds": ob_genuine,
                "ob_carried_forward_rate": _safe_ratio(float(ob_cf), float(ob_seconds)),
                "ob_genuine_rate": _safe_ratio(float(ob_genuine), float(ob_seconds)),
                "open": open_px,
                "high": high_px,
                "low": low_px,
                "close": close_px,
                "previous_close": previous_close,
                "close_vs_previous_close_pct": close_vs_previous,
                "price_displacement_pct": price_displacement,
                "directional_adverse_displacement_pct": adverse,
                "oi_last": oi_last,
                "oi_delta_abs_1m": oi_delta,
                "oi_delta_pct_1m": oi_delta_pct,
                "aggressive_notional": aggressive,
                "opposite_notional": opposite,
                "trade_count": int(_number(trade.get("trade_count")) or 0)
                if trade is not None
                else 0,
                "liquidation_count": liq_count,
                "liquidation_notional": liq_notional,
                "liquidation_to_aggressive_notional": _safe_ratio(
                    liq_notional, aggressive
                ),
                "impact_per_aggressive_notional": impact,
                "previous_impact_per_aggressive_notional": prev_impact,
                "aggressive_notional_change": aggressive_change,
                "impact_compression_observed": compression,
                "genuine_spread_bps_mean": _number(
                    ob.get("genuine_spread_bps_mean")
                )
                if ob is not None
                else None,
                "genuine_imbalance_l50_mean": imbalance,
                "directional_imbalance": directional_imbalance,
                "genuine_support_depth_l50_mean": support_depth,
                "genuine_opposing_depth_l50_mean": opposing_depth,
                "directional_depth_change": directional_depth_change,
                "directional_imbalance_change": imbalance_change,
                "directional_ofi": directional_ofi,
                "directional_net_add": directional_net_add,
                "directional_net_add_change": directional_net_add_change,
                "l2_recovery_observed": l2_recovery,
                "directional_flush_observed": flush,
                "stage_reached": stage,
            }
            features.append(row)
            if flush:
                candidate = {
                    "candidate_id": _candidate_id(
                        inputs.symbol, minute_text, direction
                    ),
                    **{name: row.get(name) for name in CANDIDATE_COLUMNS if name != "candidate_id"},
                }
                candidates.append(candidate)
                labels.append(
                    _label_candidate(
                        candidate,
                        candles,
                        horizon_minutes=inputs.label_horizon_minutes,
                    )
                )
            prior_impact_by_direction[direction] = None if technical_gap else row
            prior_l2_by_direction[direction] = (
                row if not technical_gap and ob_genuine > 0 else None
            )

        previous_close = close_px if candle_present else None
        previous_oi = oi_last if oi_state_valid else None
        previous_oi_valid = oi_state_valid and oi_last is not None

    quality = {
        "symbol": inputs.symbol,
        "expected_minutes": len(expected),
        "technical_gap_minutes": gap_minutes,
        "strict_common_minutes": len(expected) - gap_minutes,
        "minute_feature_rows": len(features),
        "candidate_rows": len(candidates),
        "carried_forward_seconds": sum(
            int(row["ob_carried_forward_seconds"])
            for row in features[::2]
        ),
        "orderbook_seconds": sum(int(row["ob_seconds"]) for row in features[::2]),
    }
    quality["carried_forward_rate"] = _safe_ratio(
        float(quality["carried_forward_seconds"]),
        float(quality["orderbook_seconds"]),
    )
    return SymbolDiscoveryResult(
        symbol=inputs.symbol,
        minute_features=tuple(features),
        candidates=tuple(candidates),
        labels=tuple(labels),
        quality=quality,
    )


def _label_candidate(
    candidate: Mapping[str, object],
    candles: pd.DataFrame,
    *,
    horizon_minutes: int,
) -> dict[str, object]:
    decision = pd.Timestamp(str(candidate["decision_at"]))
    entry_rows = candles.loc[candles.index >= decision]
    if entry_rows.empty:
        return _empty_label(candidate, horizon_minutes, "ENTRY_MISSING")
    entry = entry_rows.iloc[0]
    entry_at = pd.Timestamp(entry["open_time"])
    entry_price = _number(entry["open"])
    if entry_price is None or entry_price <= 0:
        return _empty_label(candidate, horizon_minutes, "ENTRY_PRICE_INVALID")
    end = entry_at + pd.Timedelta(minutes=horizon_minutes)
    path = candles.loc[(candles.index >= entry_at) & (candles.index < end)]
    if len(path) != horizon_minutes:
        return _empty_label(candidate, horizon_minutes, "INCOMPLETE_HORIZON")
    highs = pd.to_numeric(path["high"], errors="coerce")
    lows = pd.to_numeric(path["low"], errors="coerce")
    close = _number(path.iloc[-1]["close"])
    if highs.isna().any() or lows.isna().any() or close is None:
        return _empty_label(candidate, horizon_minutes, "INVALID_PATH")
    if candidate["direction"] == "LONG":
        mfe = float(highs.max()) / entry_price - 1.0
        mae = float(lows.min()) / entry_price - 1.0
        forward = close / entry_price - 1.0
    else:
        mfe = entry_price / float(lows.min()) - 1.0
        mae = entry_price / float(highs.max()) - 1.0
        forward = entry_price / close - 1.0
    return {
        "candidate_id": candidate["candidate_id"],
        "symbol": candidate["symbol"],
        "direction": candidate["direction"],
        "decision_at": candidate["decision_at"],
        "entry_at": _minute_text(entry_at),
        "entry_price": entry_price,
        "label_horizon_minutes": horizon_minutes,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "forward_return_pct": forward,
        "label_status": "COMPLETE",
    }


def _empty_label(
    candidate: Mapping[str, object], horizon: int, status: str
) -> dict[str, object]:
    return {
        "candidate_id": candidate["candidate_id"],
        "symbol": candidate["symbol"],
        "direction": candidate["direction"],
        "decision_at": candidate["decision_at"],
        "entry_at": None,
        "entry_price": None,
        "label_horizon_minutes": horizon,
        "mfe_pct": None,
        "mae_pct": None,
        "forward_return_pct": None,
        "label_status": status,
    }


def run_discovery(
    *,
    client: Any,
    loader: DiscoveryLoader,
    universe_path: Path,
    start: datetime,
    end: datetime,
    label_horizon_minutes: int,
    output_dir: Path,
    symbols: tuple[str, ...] | None = None,
) -> DiscoveryRunResult:
    """Sequentially load symbols and write deterministic discovery artifacts."""
    universe_bytes = universe_path.read_bytes()
    universe = json.loads(universe_bytes)
    all_symbols = tuple(str(s) for s in universe["symbols"])
    selected = symbols or all_symbols
    unknown = sorted(set(selected) - set(all_symbols))
    if unknown:
        raise DiscoveryError(f"symbols outside frozen universe: {unknown}")
    start_u, end_u = _as_utc(start), _as_utc(end)
    label_end = end_u + timedelta(minutes=label_horizon_minutes)

    feature_rows: list[dict[str, object]] = []
    candidate_rows: list[dict[str, object]] = []
    label_rows: list[dict[str, object]] = []
    quality_rows: list[dict[str, object]] = []
    for symbol in selected:
        raw = loader(
            client,
            symbol=symbol,
            start=start_u,
            end=end_u,
            label_end=label_end,
        )
        result = build_symbol_discovery(
            DiscoveryInputs(
                symbol=symbol,
                start=start_u,
                end=end_u,
                label_horizon_minutes=label_horizon_minutes,
                candles=raw["candles"],
                trades=raw["trades"],
                open_interest=raw["open_interest"],
                liquidations=raw["liquidations"],
                orderbook=raw["orderbook"],
            )
        )
        feature_rows.extend(result.minute_features)
        candidate_rows.extend(result.candidates)
        label_rows.extend(result.labels)
        quality_rows.append(result.quality)

    feature_rows.sort(key=lambda r: (str(r["symbol"]), str(r["minute"]), str(r["direction"])))
    candidate_rows.sort(key=lambda r: (str(r["symbol"]), str(r["minute"]), str(r["direction"])))
    label_rows.sort(key=lambda r: str(r["candidate_id"]))
    quality_rows.sort(key=lambda r: str(r["symbol"]))
    distributions = _distribution_summary(feature_rows)
    manifest = {
        "format_version": FORMAT_VERSION,
        "window": {
            "start": start_u.isoformat().replace("+00:00", "Z"),
            "end": end_u.isoformat().replace("+00:00", "Z"),
            "semantics": "[start,end)",
            "minutes": int((end_u - start_u).total_seconds() // 60),
        },
        "universe_path": str(universe_path),
        "universe_sha256": hashlib.sha256(universe_bytes).hexdigest(),
        "symbols": list(selected),
        "symbol_count": len(selected),
        "label_horizon_minutes": label_horizon_minutes,
        "predictor_policy": "causal minute-close features only",
        "labels_policy": "future outcomes isolated in labels_sidecar.csv",
        "threshold_search": False,
        "profitability_claim": False,
        "liquidation_feed_health": "assumed valid from external frozen-window audit",
        "source_tables": {
            "candles": "signal_generator.candles_1m",
            "public_trades": "orderbook_analysis.public_trades_canonical",
            "open_interest": "orderbook_analysis.open_interest_5s",
            "liquidations": "orderbook_analysis.all_liquidations",
            "orderbook": ORDERBOOK_TABLE,
        },
        "orderbook_contract": {
            "parser_version": ORDERBOOK_PARSER_VERSION,
            "depth": ORDERBOOK_DEPTH,
            "genuine_seconds_condition": ORDERBOOK_GENUINE_DESCRIPTION,
            "carried_forward_policy": ORDERBOOK_CARRIED_FORWARD_POLICY,
            "complete_minute_condition": (
                f"seconds={ORDERBOOK_EXPECTED_SECONDS_PER_MINUTE} "
                "and invalid_seconds=0"
            ),
            "comparison_minimum": (
                "current and immediately previous minute each have at least "
                "one genuine second"
            ),
            "l2_side_by_direction": L2_SIDE_BY_DIRECTION,
            "l2_recovery_observed": L2_RECOVERY_RELATION,
        },
        "direction_contract": {
            "liquidation_side_by_direction": LIQUIDATION_SIDE_BY_DIRECTION,
            "aggressor_side_by_direction": AGGRESSOR_SIDE_BY_DIRECTION,
        },
        "counts": {
            "minute_feature_rows": len(feature_rows),
            "candidate_rows": len(candidate_rows),
            "label_rows": len(label_rows),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "quality_by_symbol.csv", quality_rows)
    _write_csv(
        output_dir / "minute_features.csv",
        feature_rows,
        fieldnames=MINUTE_FEATURE_COLUMNS,
    )
    _write_csv(
        output_dir / "flush_candidates.csv",
        candidate_rows,
        fieldnames=CANDIDATE_COLUMNS,
    )
    _write_csv(
        output_dir / "labels_sidecar.csv",
        label_rows,
        fieldnames=LABEL_COLUMNS,
    )
    _write_json(output_dir / "distribution_summary.json", distributions)
    _write_json(output_dir / "discovery_manifest.json", manifest)
    return DiscoveryRunResult(
        symbol_count=len(selected),
        minute_feature_count=len(feature_rows),
        candidate_count=len(candidate_rows),
        output_dir=output_dir,
    )


def _distribution_summary(
    rows: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    output: dict[str, list[dict[str, object]]] = {"pooled": [], "by_symbol": []}
    for scope, scoped in (
        ("pooled", [(None, rows)]),
        (
            "by_symbol",
            [
                (symbol, [r for r in rows if r["symbol"] == symbol])
                for symbol in sorted({str(r["symbol"]) for r in rows})
            ],
        ),
    ):
        for symbol, subset in scoped:
            for direction in DIRECTIONS:
                directed = [r for r in subset if r["direction"] == direction]
                for feature in _DISTRIBUTION_FEATURES:
                    values = [
                        float(r[feature])
                        for r in directed
                        if r.get(feature) is not None
                        and math.isfinite(float(r[feature]))
                    ]
                    record: dict[str, object] = {
                        "direction": direction,
                        "feature": feature,
                        "n": len(values),
                        "missing": len(directed) - len(values),
                    }
                    if symbol is not None:
                        record["symbol"] = symbol
                    if values:
                        series = pd.Series(values)
                        record.update(
                            {
                                "q25": float(series.quantile(0.25)),
                                "median": float(series.quantile(0.50)),
                                "q75": float(series.quantile(0.75)),
                                "min": float(series.min()),
                                "max": float(series.max()),
                            }
                        )
                    output[scope].append(record)
    return output


def _write_csv(
    path: Path,
    rows: list[Mapping[str, object]],
    *,
    fieldnames: tuple[str, ...] | None = None,
) -> None:
    names = fieldnames or tuple(rows[0].keys()) if rows else fieldnames or ()
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(names), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in names})
    _atomic_write(path, buffer.getvalue())


def _write_json(path: Path, value: object) -> None:
    _atomic_write(
        path,
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

"""Causal orderbook features from orderbook_features_1s_v2 schema mapping."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from . import constants as C
from .causality import as_utc, mirror_for_direction, rows_at_or_before, window_slice
from .feature_value import FeatureValue, missing, ok
from .schema_mapping import SOURCE_SCHEMA_AUDIT

SRC = "orderbook_analysis.orderbook_features_1s_v2"


def _na(name: str, reason: str, *, asof: datetime) -> FeatureValue:
    return missing(name, reason=reason, status="NOT_AVAILABLE", source=SRC, asof=asof, causal=True)


def compute_orderbook_features(
    ob_1s: pd.DataFrame | None,
    decision_at: datetime | str,
    direction: str,
) -> dict[str, FeatureValue]:
    """Compute OB features from 1s buckets with bucket_start/last_source_ts <= decision_at.

    Expected columns when available: bucket_start, last_source_ts, is_valid,
    imbalance_l10, imbalance_l50, spread_bps. imbalance_l20 / impact_* → NOT_AVAILABLE.
    """
    dec = as_utc(decision_at)
    out: dict[str, FeatureValue] = {}

    # Hard NOT_AVAILABLE from schema
    for name, reason in SOURCE_SCHEMA_AUDIT["orderbook"]["not_available_columns"].items():
        feat_name = {
            "imbalance_l20": "ob_imbalance_l20_last",
            "impact_proxy_buy": "impact_proxy_buy",
            "impact_proxy_sell": "impact_proxy_sell",
            "directional_impact_proxy": "directional_impact_proxy",
        }.get(name, name)
        out[feat_name] = _na(feat_name, reason, asof=dec)

    if ob_1s is None or ob_1s.empty:
        for n in (
            "ob_imbalance_l10_last",
            "ob_imbalance_l50_last",
            "ob_imbalance_l50_mean_1m",
            "ob_imbalance_l50_mean_5m",
            "ob_imbalance_l50_std_5m",
            "ob_imbalance_directional",
            "spread_bps_last",
            "spread_bps_mean_1m",
            "spread_bps_mean_5m",
            "ob_sample_count_1m",
            "ob_sample_count_5m",
            "ob_freshness_seconds",
        ):
            if n not in out:
                out[n] = missing(n, reason="NO_OB_ROWS", status="MISSING", source=SRC, asof=dec)
        return out

    tcol = "bucket_start" if "bucket_start" in ob_1s.columns else ("minute" if "minute" in ob_1s.columns else None)
    if tcol is None:
        for n in list(out.keys()):
            pass
        for n in (
            "ob_imbalance_l10_last",
            "ob_imbalance_l50_last",
            "ob_imbalance_l50_mean_1m",
            "ob_imbalance_l50_mean_5m",
            "ob_imbalance_l50_std_5m",
            "ob_imbalance_directional",
            "spread_bps_last",
            "spread_bps_mean_1m",
            "spread_bps_mean_5m",
            "ob_sample_count_1m",
            "ob_sample_count_5m",
            "ob_freshness_seconds",
        ):
            out[n] = missing(n, reason="NO_TIME_COLUMN", status="MISSING", source=SRC, asof=dec)
        return out

    causal = rows_at_or_before(ob_1s, dec, time_col=tcol)
    if "is_valid" in causal.columns:
        causal = causal.loc[causal["is_valid"].fillna(1).astype(int) == 1]

    if causal.empty:
        for n in (
            "ob_imbalance_l10_last",
            "ob_imbalance_l50_last",
            "ob_imbalance_l50_mean_1m",
            "ob_imbalance_l50_mean_5m",
            "ob_imbalance_l50_std_5m",
            "ob_imbalance_directional",
            "spread_bps_last",
            "spread_bps_mean_1m",
            "spread_bps_mean_5m",
            "ob_sample_count_1m",
            "ob_sample_count_5m",
            "ob_freshness_seconds",
        ):
            out[n] = missing(n, reason="NO_CAUSAL_OB", status="MISSING", source=SRC, asof=dec)
        return out

    last = causal.iloc[-1]
    last_ts = as_utc(pd.Timestamp(last[tcol]).to_pydatetime())
    freshness = (dec - last_ts).total_seconds()
    out["ob_freshness_seconds"] = ok(
        "ob_freshness_seconds", float(freshness), asof=dec, window_start=last_ts, window_end=last_ts, source=SRC
    )

    stale = freshness > C.OB_STALE_SECONDS

    def last_col(col: str, name: str) -> FeatureValue:
        if col not in causal.columns:
            return _na(name, f"Column {col} absent in frame", asof=dec)
        if stale:
            return missing(name, reason="STALE_OB", status="STALE", source=SRC, asof=dec, window_end=last_ts)
        v = last.get(col)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return missing(name, reason="NULL_VALUE", status="MISSING", source=SRC, asof=dec)
        return ok(name, float(v), asof=dec, window_start=last_ts, window_end=last_ts, source=SRC)

    out["ob_imbalance_l10_last"] = last_col("imbalance_l10", "ob_imbalance_l10_last")
    out["ob_imbalance_l50_last"] = last_col("imbalance_l50", "ob_imbalance_l50_last")
    out["spread_bps_last"] = last_col("spread_bps", "spread_bps_last")

    if out["ob_imbalance_l50_last"].value is None:
        out["ob_imbalance_directional"] = missing(
            "ob_imbalance_directional",
            reason=out["ob_imbalance_l50_last"].missing_reason or "MISSING",
            status=out["ob_imbalance_l50_last"].coverage_status,
            source=SRC,
            asof=dec,
        )
    else:
        mirrored = mirror_for_direction(float(out["ob_imbalance_l50_last"].value), direction)
        out["ob_imbalance_directional"] = ok(
            "ob_imbalance_directional", mirrored, asof=dec, window_start=last_ts, window_end=last_ts, source=SRC
        )

    def window_stats(col: str, minutes: int, *, mean_name: str, std_name: str | None = None, count_name: str | None = None):
        if stale:
            if mean_name:
                out[mean_name] = missing(mean_name, reason="STALE_OB", status="STALE", source=SRC, asof=dec)
            if std_name:
                out[std_name] = missing(std_name, reason="STALE_OB", status="STALE", source=SRC, asof=dec)
            if count_name:
                out[count_name] = missing(count_name, reason="STALE_OB", status="STALE", source=SRC, asof=dec)
            return
        if col not in causal.columns:
            if mean_name:
                out[mean_name] = _na(mean_name, f"Column {col} absent", asof=dec)
            if std_name:
                out[std_name] = _na(std_name, f"Column {col} absent", asof=dec)
            if count_name:
                out[count_name] = missing(count_name, reason="NO_COL", status="MISSING", source=SRC, asof=dec)
            return
        sl = window_slice(causal, time_col=tcol, end=dec, lookback=timedelta(minutes=minutes))
        if sl.empty:
            if mean_name:
                out[mean_name] = missing(mean_name, reason="EMPTY_WINDOW", status="MISSING", source=SRC, asof=dec)
            if std_name:
                out[std_name] = missing(std_name, reason="EMPTY_WINDOW", status="MISSING", source=SRC, asof=dec)
            if count_name:
                out[count_name] = ok(count_name, 0, asof=dec, window_start=dec - timedelta(minutes=minutes), window_end=dec, source=SRC)
                # count 0 is factual sample count, not a fake feature fill for imbalance
            return
        vals = pd.to_numeric(sl[col], errors="coerce").dropna()
        w0 = sl.iloc[0][tcol]
        if mean_name:
            if vals.empty:
                out[mean_name] = missing(mean_name, reason="NO_VALID_SAMPLES", status="MISSING", source=SRC, asof=dec)
            else:
                out[mean_name] = ok(mean_name, float(vals.mean()), asof=dec, window_start=w0, window_end=dec, source=SRC)
        if std_name:
            if len(vals) < 2:
                out[std_name] = missing(std_name, reason="INSUFFICIENT_SAMPLES", status="INSUFFICIENT", source=SRC, asof=dec)
            else:
                out[std_name] = ok(std_name, float(vals.std(ddof=0)), asof=dec, window_start=w0, window_end=dec, source=SRC)
        if count_name:
            out[count_name] = ok(count_name, int(len(sl)), asof=dec, window_start=w0, window_end=dec, source=SRC)

    window_stats("imbalance_l50", 1, mean_name="ob_imbalance_l50_mean_1m", count_name="ob_sample_count_1m")
    window_stats(
        "imbalance_l50",
        5,
        mean_name="ob_imbalance_l50_mean_5m",
        std_name="ob_imbalance_l50_std_5m",
        count_name="ob_sample_count_5m",
    )
    window_stats("spread_bps", 1, mean_name="spread_bps_mean_1m")
    window_stats("spread_bps", 5, mean_name="spread_bps_mean_5m")
    return out

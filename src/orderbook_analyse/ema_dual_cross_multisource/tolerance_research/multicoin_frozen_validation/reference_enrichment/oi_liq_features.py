"""Optional OI / liquidation context at decision_at (causal; null if absent)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pandas as pd

from .causality import as_utc, rows_at_or_before, window_slice
from .feature_value import FeatureValue, missing, ok


def compute_oi_liq_features(
    oi_1m: pd.DataFrame | None,
    liq: pd.DataFrame | None,
    decision_at,
) -> dict[str, FeatureValue]:
    dec = as_utc(decision_at)
    feats: dict[str, FeatureValue] = {}

    if oi_1m is None or oi_1m.empty:
        feats["oi_last"] = missing("oi_last", reason="SOURCE_MISSING", status="MISSING", source="oi_1m", asof=dec)
        feats["oi_change_1h_pct"] = missing(
            "oi_change_1h_pct", reason="SOURCE_MISSING", status="MISSING", source="oi_1m", asof=dec
        )
    else:
        if "open_time" in oi_1m.columns:
            time_col = "open_time"
        elif "minute" in oi_1m.columns:
            time_col = "minute"
        elif "ts" in oi_1m.columns:
            time_col = "ts"
        else:
            time_col = None
        oi_col = "open_interest" if "open_interest" in oi_1m.columns else ("oi" if "oi" in oi_1m.columns else None)
        if time_col is None or oi_col is None:
            feats["oi_last"] = missing("oi_last", reason="SCHEMA_UNKNOWN", status="MISSING", source="oi_1m", asof=dec)
            feats["oi_change_1h_pct"] = missing(
                "oi_change_1h_pct", reason="SCHEMA_UNKNOWN", status="MISSING", source="oi_1m", asof=dec
            )
        else:
            hist = rows_at_or_before(oi_1m, dec, time_col=time_col)
            if hist.empty:
                feats["oi_last"] = missing("oi_last", reason="NO_CAUSAL_ROW", status="MISSING", source="oi_1m", asof=dec)
                feats["oi_change_1h_pct"] = missing(
                    "oi_change_1h_pct", reason="NO_CAUSAL_ROW", status="MISSING", source="oi_1m", asof=dec
                )
            else:
                last = float(hist.iloc[-1][oi_col])
                feats["oi_last"] = ok("oi_last", last, asof=dec, window_start=None, window_end=dec, source="oi_1m")
                win = window_slice(hist, time_col=time_col, end=dec, lookback=timedelta(hours=1))
                if len(win) < 2 or last == 0:
                    feats["oi_change_1h_pct"] = missing(
                        "oi_change_1h_pct", reason="INSUFFICIENT_HISTORY", status="MISSING", source="oi_1m", asof=dec
                    )
                else:
                    first = float(win.iloc[0][oi_col])
                    chg = None if first == 0 else (last - first) / abs(first) * 100.0
                    feats["oi_change_1h_pct"] = ok(
                        "oi_change_1h_pct",
                        chg,
                        asof=dec,
                        window_start=dec - timedelta(hours=1),
                        window_end=dec,
                        source="oi_1m",
                    )

    if liq is None or liq.empty:
        feats["liq_long_notional_1h"] = missing(
            "liq_long_notional_1h", reason="SOURCE_MISSING", status="MISSING", source="liquidations", asof=dec
        )
        feats["liq_short_notional_1h"] = missing(
            "liq_short_notional_1h", reason="SOURCE_MISSING", status="MISSING", source="liquidations", asof=dec
        )
    else:
        # Best-effort: sum notional in lookback with ts <= decision_at
        tcol = "event_time" if "event_time" in liq.columns else ("ts" if "ts" in liq.columns else None)
        if tcol is None:
            feats["liq_long_notional_1h"] = missing(
                "liq_long_notional_1h", reason="SCHEMA_UNKNOWN", status="MISSING", source="liquidations", asof=dec
            )
            feats["liq_short_notional_1h"] = missing(
                "liq_short_notional_1h", reason="SCHEMA_UNKNOWN", status="MISSING", source="liquidations", asof=dec
            )
        else:
            hist = rows_at_or_before(liq, dec, time_col=tcol)
            win = window_slice(hist, time_col=tcol, end=dec, lookback=timedelta(hours=1))
            side_col = "side" if "side" in win.columns else None
            notional_col = "notional" if "notional" in win.columns else ("qty" if "qty" in win.columns else None)
            if win.empty or side_col is None or notional_col is None:
                feats["liq_long_notional_1h"] = missing(
                    "liq_long_notional_1h", reason="NO_CAUSAL_ROW", status="MISSING", source="liquidations", asof=dec
                )
                feats["liq_short_notional_1h"] = missing(
                    "liq_short_notional_1h", reason="NO_CAUSAL_ROW", status="MISSING", source="liquidations", asof=dec
                )
            else:
                sides = win[side_col].astype(str).str.upper()
                long_mask = sides.isin(("LONG", "BUY", "BULLISH"))
                short_mask = sides.isin(("SHORT", "SELL", "BEARISH"))
                feats["liq_long_notional_1h"] = ok(
                    "liq_long_notional_1h",
                    float(pd.to_numeric(win.loc[long_mask, notional_col], errors="coerce").fillna(0).sum()),
                    asof=dec,
                    window_start=dec - timedelta(hours=1),
                    window_end=dec,
                    source="liquidations",
                )
                feats["liq_short_notional_1h"] = ok(
                    "liq_short_notional_1h",
                    float(pd.to_numeric(win.loc[short_mask, notional_col], errors="coerce").fillna(0).sum()),
                    asof=dec,
                    window_start=dec - timedelta(hours=1),
                    window_end=dec,
                    source="liquidations",
                )
    return feats

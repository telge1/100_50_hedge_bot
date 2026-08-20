"""Discover case candidates from per-symbol panels (pure / in-memory)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.market_event_report.metrics import mfe_mae_for_side

from .select import (
    COOLDOWN_M,
    THR_075,
    THR_100,
    CaseCandidate,
    cooldown_per_symbol,
    prefer_score_with_bonus,
)

STRONG_Q_LO = 0.02
STRONG_Q_HI = 0.98
FAILED_MFE_MAX = 0.0025  # "no meaningful MFE"
FAILED_MAE_MIN = 0.0075


def _naive(ts: Any) -> datetime:
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        return t.tz_convert("UTC").to_pydatetime().replace(tzinfo=None)
    return t.to_pydatetime()


def trailing_top(s: pd.Series, qv: float) -> pd.Series:
    return s >= s.shift(1).rolling(1440, min_periods=180).quantile(qv)


def trailing_bot(s: pd.Series, qv: float) -> pd.Series:
    return s <= s.shift(1).rolling(1440, min_periods=180).quantile(qv)


def build_discovery_panel(
    candles: pd.DataFrame,
    trades: pd.DataFrame,
    ob: pd.DataFrame,
) -> pd.DataFrame:
    """Join 1m frames and add causal flow / rare-confluence features."""
    df = candles.sort_values("open_time").reset_index(drop=True).copy()
    df["open_time"] = pd.to_datetime(df["open_time"])

    tr = trades.copy()
    if not tr.empty:
        tr["minute"] = pd.to_datetime(tr["minute"])
        # Normalize column names from market_event_report loaders
        if "trade_delta" in tr.columns and "delta" not in tr.columns:
            tr["delta"] = tr["trade_delta"]
        if "aggressive_buy_volume" in tr.columns and "buy_vol" not in tr.columns:
            tr["buy_vol"] = tr["aggressive_buy_volume"]
        if "aggressive_sell_volume" in tr.columns and "sell_vol" not in tr.columns:
            tr["sell_vol"] = tr["aggressive_sell_volume"]
        keep = [c for c in ["minute", "trade_count", "buy_vol", "sell_vol", "delta", "delta_ratio", "total_volume"] if c in tr.columns]
        tr = tr[keep]
    else:
        tr = pd.DataFrame(columns=["minute", "trade_count", "buy_vol", "sell_vol", "delta", "delta_ratio"])

    obx = ob.copy()
    if not obx.empty:
        obx["minute"] = pd.to_datetime(obx["minute"])
        # Align names used by rare confluence
        if "ofi" in obx.columns and "ofi_sum" not in obx.columns:
            obx["ofi_sum"] = obx["ofi"]
        if "imbalance_l50" in obx.columns and "imb_l50" not in obx.columns:
            obx["imb_l50"] = obx["imbalance_l50"]
    else:
        obx = pd.DataFrame(columns=["minute", "spread_bps", "imb_l50", "ofi_sum", "seconds", "valid_seconds"])

    df = df.merge(tr, left_on="open_time", right_on="minute", how="left")
    df = df.merge(obx, left_on="open_time", right_on="minute", how="left", suffixes=("", "_ob"))
    df = df.loc[:, ~df.columns.duplicated(keep="first")]
    for c in [x for x in df.columns if str(x).startswith("minute")]:
        df = df.drop(columns=[c])

    for c in ["trade_count", "buy_vol", "sell_vol", "delta", "total_volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        else:
            df[c] = 0.0
    if "delta_ratio" not in df.columns:
        denom = df["buy_vol"] + df["sell_vol"]
        df["delta_ratio"] = np.where(denom > 0, df["delta"] / denom, 0.0)
    else:
        df["delta_ratio"] = pd.to_numeric(df["delta_ratio"], errors="coerce").fillna(0.0)

    for c in ["spread_bps", "imb_l50", "ofi_sum", "seconds", "valid_seconds"]:
        if c not in df.columns:
            df[c] = np.nan
        else:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    tc = df["trade_count"]
    df["tps_ratio"] = tc / tc.shift(1).rolling(24 * 60, min_periods=60).median()
    df["delta_5m"] = df["delta"].rolling(5, min_periods=3).sum()
    df["ofi_5m"] = df["ofi_sum"].rolling(5, min_periods=3).sum()
    df["imb50_5m"] = df["imb_l50"].rolling(5, min_periods=3).mean()
    df["spread_5m"] = df["spread_bps"].rolling(5, min_periods=3).mean()
    df["ret_1m"] = df["close"] / df["close"].shift(1) - 1.0

    df["strong_buy"] = trailing_top(df["delta_ratio"], STRONG_Q_HI)
    df["strong_sell"] = trailing_bot(df["delta_ratio"], STRONG_Q_LO)

    # Rare confluence frozen flags
    df["tps_top1"] = trailing_top(df["tps_ratio"], 0.99)
    df["delta_lo_top2"] = trailing_bot(df["delta_5m"], 0.02)
    df["delta_hi_top2"] = trailing_top(df["delta_5m"], 0.98)
    df["imb50_lo_top1"] = trailing_bot(df["imb50_5m"], 0.01)
    df["imb50_hi_top1"] = trailing_top(df["imb50_5m"], 0.99)
    df["ofi_hi_top2"] = trailing_top(df["ofi_5m"], 0.98)
    df["ob_ok"] = (df["seconds"].fillna(0) >= 30) & ((df["valid_seconds"] / df["seconds"]).fillna(0) >= 0.95)
    df["spread_ok"] = trailing_bot(df["spread_5m"], 0.50) | df["spread_5m"].isna()
    gates = df["ob_ok"].fillna(False) & df["spread_ok"].fillna(False)
    df["sig_short_rare"] = (
        gates
        & df["imb50_lo_top1"].fillna(False)
        & df["delta_lo_top2"].fillna(False)
        & df["tps_top1"].fillna(False)
    )
    df["sig_long_rare"] = (
        gates
        & df["imb50_hi_top1"].fillna(False)
        & df["ofi_hi_top2"].fillna(False)
        & df["delta_hi_top2"].fillna(False)
    )
    return df


def discover_symbol_cases(
    panel: pd.DataFrame,
    *,
    symbol: str,
    start: datetime,
    end_exclusive: datetime,
) -> dict[str, list[CaseCandidate]]:
    """Return raw (pre top-N) candidates by case_type for one symbol."""
    df = panel
    n = len(df)
    times = [_naive(t) for t in df["open_time"]]
    opens = df["open"].to_numpy(dtype="float64")
    highs = df["high"].to_numpy(dtype="float64")
    lows = df["low"].to_numpy(dtype="float64")
    closes = df["close"].to_numpy(dtype="float64")
    delta_ratio_a = df["delta_ratio"].to_numpy(dtype="float64")
    delta_a = df["delta"].to_numpy(dtype="float64")
    ret1 = df["ret_1m"].to_numpy(dtype="float64") if "ret_1m" in df.columns else np.full(n, np.nan)
    strong_buy_a = df["strong_buy"].fillna(False).to_numpy(dtype=bool)
    strong_sell_a = df["strong_sell"].fillna(False).to_numpy(dtype=bool)
    sig_short_a = df["sig_short_rare"].fillna(False).to_numpy(dtype=bool)
    sig_long_a = df["sig_long_rare"].fillna(False).to_numpy(dtype=bool)

    out: dict[str, list[CaseCandidate]] = {
        "long_big_move": [],
        "short_big_move": [],
        "flow_opposed_reversal": [],
        "flow_aligned_move": [],
        "failed_directional": [],
        "rare_confluence": [],
    }

    def path(i: int, horizon: int) -> dict[str, Any] | None:
        entry_i = i + 1
        end = entry_i + horizon
        if end > n:
            return None
        entry = float(opens[entry_i])
        if not np.isfinite(entry) or entry <= 0:
            return None
        hs = highs[entry_i:end]
        ls = lows[entry_i:end]
        cs = closes[entry_i:end]
        long_m = mfe_mae_for_side(entry, hs, ls, cs, "LONG")
        short_m = mfe_mae_for_side(entry, hs, ls, cs, "SHORT")
        return {"entry": entry, "ret": long_m["ret"], "long": long_m, "short": short_m}

    for i, t in enumerate(times):
        if not (start <= t < end_exclusive):
            continue
        p60 = path(i, 60)
        if p60 is None:
            continue

        delta_ratio = float(delta_ratio_a[i]) if np.isfinite(delta_ratio_a[i]) else 0.0
        trade_delta = float(delta_a[i]) if np.isfinite(delta_a[i]) else 0.0
        ret60 = p60["ret"]
        long_mfe = p60["long"]["mfe"]
        long_mae = p60["long"]["mae"]
        short_mfe = p60["short"]["mfe"]
        short_mae = p60["short"]["mae"]

        is_rare = bool(sig_short_a[i] or sig_long_a[i])
        p240 = path(i, 240) if is_rare else None
        base_meta = {
            "delta_ratio": delta_ratio,
            "trade_delta": trade_delta,
            "ret_60m": ret60,
            "long_mfe_60m": long_mfe,
            "long_mae_60m": long_mae,
            "short_mfe_60m": short_mfe,
            "short_mae_60m": short_mae,
            "ret_240m": None if p240 is None else p240["ret"],
            "long_mfe_240m": None if p240 is None else p240["long"]["mfe"],
            "long_mae_240m": None if p240 is None else p240["long"]["mae"],
            "short_mfe_240m": None if p240 is None else p240["short"]["mfe"],
            "short_mae_240m": None if p240 is None else p240["short"]["mae"],
            "event_minute_return": float(ret1[i]) if np.isfinite(ret1[i]) else None,
        }

        if long_mfe is not None and long_mfe >= THR_075:
            out["long_big_move"].append(
                CaseCandidate(
                    case_type="long_big_move",
                    symbol=symbol,
                    event_time=t,
                    score=prefer_score_with_bonus(long_mfe, hit_100=long_mfe >= THR_100),
                    meta={**base_meta, "subtype": "long_big_move"},
                )
            )

        if short_mfe is not None and short_mfe >= THR_075:
            out["short_big_move"].append(
                CaseCandidate(
                    case_type="short_big_move",
                    symbol=symbol,
                    event_time=t,
                    score=prefer_score_with_bonus(short_mfe, hit_100=short_mfe >= THR_100),
                    meta={**base_meta, "subtype": "short_big_move"},
                )
            )

        strong_buy = bool(strong_buy_a[i])
        strong_sell = bool(strong_sell_a[i])

        if ret60 is not None and (strong_buy or strong_sell):
            score = abs(ret60) * max(abs(delta_ratio), 0.01)
            if strong_sell and ret60 > 0:
                out["flow_opposed_reversal"].append(
                    CaseCandidate(
                        case_type="flow_opposed_reversal",
                        symbol=symbol,
                        event_time=t,
                        score=score,
                        meta={**base_meta, "subtype": "flow_opposed_sell_then_up", "flow": "sell"},
                    )
                )
            elif strong_buy and ret60 < 0:
                out["flow_opposed_reversal"].append(
                    CaseCandidate(
                        case_type="flow_opposed_reversal",
                        symbol=symbol,
                        event_time=t,
                        score=score,
                        meta={**base_meta, "subtype": "flow_opposed_buy_then_down", "flow": "buy"},
                    )
                )
            elif strong_sell and ret60 < 0:
                out["flow_aligned_move"].append(
                    CaseCandidate(
                        case_type="flow_aligned_move",
                        symbol=symbol,
                        event_time=t,
                        score=score,
                        meta={**base_meta, "subtype": "flow_aligned_sell_down", "flow": "sell"},
                    )
                )
            elif strong_buy and ret60 > 0:
                out["flow_aligned_move"].append(
                    CaseCandidate(
                        case_type="flow_aligned_move",
                        symbol=symbol,
                        event_time=t,
                        score=score,
                        meta={**base_meta, "subtype": "flow_aligned_buy_up", "flow": "buy"},
                    )
                )

        if strong_sell and short_mfe is not None and short_mae is not None:
            failed = (short_mfe < FAILED_MFE_MAX) or (
                short_mae >= FAILED_MAE_MIN and short_mae > short_mfe
            )
            if failed:
                out["failed_directional"].append(
                    CaseCandidate(
                        case_type="failed_directional",
                        symbol=symbol,
                        event_time=t,
                        score=float(short_mae) - float(short_mfe),
                        meta={**base_meta, "subtype": "failed_short_after_sell", "flow": "sell"},
                    )
                )
        if strong_buy and long_mfe is not None and long_mae is not None:
            failed = (long_mfe < FAILED_MFE_MAX) or (
                long_mae >= FAILED_MAE_MIN and long_mae > long_mfe
            )
            if failed:
                out["failed_directional"].append(
                    CaseCandidate(
                        case_type="failed_directional",
                        symbol=symbol,
                        event_time=t,
                        score=float(long_mae) - float(long_mfe),
                        meta={**base_meta, "subtype": "failed_long_after_buy", "flow": "buy"},
                    )
                )

        if sig_short_a[i]:
            win_1h = bool(
                short_mfe is not None
                and short_mfe >= THR_075
                and (short_mae or 0) <= (short_mfe or 0)
            )
            win_4h = False
            if p240 is not None and p240["short"]["mfe"] is not None:
                win_4h = p240["short"]["mfe"] >= THR_075 and (p240["short"]["mae"] or 0) <= (
                    p240["short"]["mfe"] or 0
                )
            out["rare_confluence"].append(
                CaseCandidate(
                    case_type="rare_confluence",
                    symbol=symbol,
                    event_time=t,
                    score=float(short_mfe or 0) - float(short_mae or 0),
                    meta={
                        **base_meta,
                        "subtype": "SHORT_RARE_IMB_DELTA_TPS_V1",
                        "candidate": "SHORT_RARE_IMB_DELTA_TPS_V1",
                        "side": "SHORT",
                        "winner_1h": win_1h,
                        "winner_4h": win_4h,
                        "outcome_1h": "winner" if win_1h else "loser",
                        "outcome_4h": "winner" if win_4h else "loser",
                    },
                )
            )
        if sig_long_a[i]:
            win_1h = bool(
                long_mfe is not None and long_mfe >= THR_075 and (long_mae or 0) <= (long_mfe or 0)
            )
            win_4h = False
            if p240 is not None and p240["long"]["mfe"] is not None:
                win_4h = p240["long"]["mfe"] >= THR_075 and (p240["long"]["mae"] or 0) <= (
                    p240["long"]["mfe"] or 0
                )
            out["rare_confluence"].append(
                CaseCandidate(
                    case_type="rare_confluence",
                    symbol=symbol,
                    event_time=t,
                    score=float(long_mfe or 0) - float(long_mae or 0),
                    meta={
                        **base_meta,
                        "subtype": "LONG_RARE_IMB_OFI_DELTA_V1",
                        "candidate": "LONG_RARE_IMB_OFI_DELTA_V1",
                        "side": "LONG",
                        "winner_1h": win_1h,
                        "winner_4h": win_4h,
                        "outcome_1h": "winner" if win_1h else "loser",
                        "outcome_4h": "winner" if win_4h else "loser",
                    },
                )
            )

    for k in list(out.keys()):
        out[k] = cooldown_per_symbol(out[k], cooldown_m=COOLDOWN_M)
    return out

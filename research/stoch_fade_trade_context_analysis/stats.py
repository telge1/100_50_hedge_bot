"""WIN vs LOSS comparison. No threshold search. OPEN excluded from loss-rate tables."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import BOOTSTRAP_ITERS, RANDOM_SEED


def _finite(series: pd.Series) -> np.ndarray:
    arr = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    return arr[np.isfinite(arr)]


def pooled_smd(win: np.ndarray, loss: np.ndarray) -> float | None:
    if win.size < 2 or loss.size < 2:
        return None
    var_w = float(np.var(win, ddof=1))
    var_l = float(np.var(loss, ddof=1))
    denom = np.sqrt(((win.size - 1) * var_w + (loss.size - 1) * var_l) / (win.size + loss.size - 2))
    if denom == 0 or not np.isfinite(denom):
        return None
    return float((np.mean(win) - np.mean(loss)) / denom)


def bootstrap_mean_diff_ci(
    win: np.ndarray, loss: np.ndarray, *, rng: np.random.Generator, iters: int = BOOTSTRAP_ITERS
) -> tuple[float | None, float | None]:
    if win.size == 0 or loss.size == 0:
        return None, None
    diffs = np.empty(iters, dtype=float)
    for i in range(iters):
        w = rng.choice(win, size=win.size, replace=True)
        l = rng.choice(loss, size=loss.size, replace=True)
        diffs[i] = float(np.mean(w) - np.mean(l))
    lo, hi = np.quantile(diffs, [0.025, 0.975])
    return float(lo), float(hi)


def bootstrap_rate_ci(flags: np.ndarray, *, rng: np.random.Generator, iters: int = BOOTSTRAP_ITERS) -> tuple[float, float]:
    n = flags.size
    if n == 0:
        return float("nan"), float("nan")
    rates = np.empty(iters, dtype=float)
    for i in range(iters):
        sample = rng.choice(flags, size=n, replace=True)
        rates[i] = float(np.mean(sample))
    lo, hi = np.quantile(rates, [0.025, 0.975])
    return float(lo), float(hi)


def numeric_comparison(context: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    rng = np.random.default_rng(RANDOM_SEED)
    wins = context.loc[context["outcome"] == "WIN"]
    losses = context.loc[context["outcome"] == "LOSS"]
    rows: list[dict[str, Any]] = []
    for col in columns:
        if col not in context.columns:
            continue
        w = _finite(wins[col])
        l = _finite(losses[col])
        if w.size == 0 and l.size == 0:
            continue
        lo, hi = bootstrap_mean_diff_ci(w, l, rng=rng) if w.size and l.size else (None, None)
        rows.append(
            {
                "feature": col,
                "kind": "numeric",
                "n_win": int(w.size),
                "n_loss": int(l.size),
                "mean_win": float(np.mean(w)) if w.size else None,
                "mean_loss": float(np.mean(l)) if l.size else None,
                "median_win": float(np.median(w)) if w.size else None,
                "median_loss": float(np.median(l)) if l.size else None,
                "p25_win": float(np.quantile(w, 0.25)) if w.size else None,
                "p75_win": float(np.quantile(w, 0.75)) if w.size else None,
                "p25_loss": float(np.quantile(l, 0.25)) if l.size else None,
                "p75_loss": float(np.quantile(l, 0.75)) if l.size else None,
                "smd_win_minus_loss": pooled_smd(w, l),
                "mean_diff_ci_low": lo,
                "mean_diff_ci_high": hi,
                "abs_smd": abs(pooled_smd(w, l) or 0.0) if pooled_smd(w, l) is not None else None,
            }
        )
    return pd.DataFrame(rows)


def boolean_comparison(context: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    wins = context.loc[context["outcome"] == "WIN"]
    losses = context.loc[context["outcome"] == "LOSS"]
    rows: list[dict[str, Any]] = []
    for col in columns:
        if col not in context.columns:
            continue
        w = wins[col]
        l = losses[col]
        w_valid = w.dropna()
        l_valid = l.dropna()
        if w_valid.empty and l_valid.empty:
            continue
        rate_w = float(w_valid.astype(bool).mean()) if len(w_valid) else None
        rate_l = float(l_valid.astype(bool).mean()) if len(l_valid) else None
        rows.append(
            {
                "feature": col,
                "kind": "boolean",
                "n_win": int(len(w_valid)),
                "n_loss": int(len(l_valid)),
                "mean_win": rate_w,
                "mean_loss": rate_l,
                "median_win": None,
                "median_loss": None,
                "p25_win": None,
                "p75_win": None,
                "p25_loss": None,
                "p75_loss": None,
                "smd_win_minus_loss": None if rate_w is None or rate_l is None else rate_w - rate_l,
                "mean_diff_ci_low": None,
                "mean_diff_ci_high": None,
                "abs_smd": None if rate_w is None or rate_l is None else abs(rate_w - rate_l),
            }
        )
    return pd.DataFrame(rows)


def loss_rate_buckets(closed: pd.DataFrame, *, feature: str, table: str) -> pd.DataFrame:
    rng = np.random.default_rng(RANDOM_SEED)
    rows: list[dict[str, Any]] = []
    if feature not in closed.columns:
        return pd.DataFrame()
    grouped = closed.groupby(closed[feature].astype("string"), dropna=False)
    for bucket, part in grouped:
        n = int(len(part))
        n_loss = int((part["outcome"] == "LOSS").sum())
        n_win = int((part["outcome"] == "WIN").sum())
        flags = (part["outcome"] == "LOSS").to_numpy(dtype=float)
        lo, hi = bootstrap_rate_ci(flags, rng=rng)
        rows.append(
            {
                "table": table,
                "feature": feature,
                "bucket": str(bucket),
                "n": n,
                "n_win": n_win,
                "n_loss": n_loss,
                "loss_rate": n_loss / n if n else None,
                "loss_rate_ci_low": lo,
                "loss_rate_ci_high": hi,
            }
        )
    return pd.DataFrame(rows).sort_values(["table", "n"], ascending=[True, False])


def add_natural_buckets(context: pd.DataFrame) -> pd.DataFrame:
    out = context.copy()

    def consumed_bucket(frac: object) -> str:
        if frac is None or (isinstance(frac, float) and not np.isfinite(frac)):
            return "MISSING"
        x = float(frac)
        if x <= 0:
            return "<=0"
        if x <= 0.25:
            return "(0,25%]"
        if x <= 0.50:
            return "(25,50%]"
        if x <= 0.75:
            return "(50,75%]"
        if x <= 1.00:
            return "(75,100%]"
        return ">100%"

    def range_bucket(pos: object) -> str:
        if pos is None or (isinstance(pos, float) and not np.isfinite(pos)):
            return "MISSING"
        x = float(pos)
        if x < 0.20:
            return "[0,0.20)"
        if x < 0.40:
            return "[0.20,0.40)"
        if x < 0.60:
            return "[0.40,0.60)"
        if x < 0.80:
            return "[0.60,0.80)"
        return "[0.80,1]"

    def room_bucket(val: object) -> str:
        if val is None or (isinstance(val, float) and not np.isfinite(val)):
            return "MISSING"
        x = float(val)
        if x < 0.5:
            return "structure_before_half_tp"
        if x < 1.0:
            return "structure_before_tp"
        if x < 2.0:
            return "room_1_to_2_tp"
        return "room_ge_2_tp"

    def align_1h_4h(row: pd.Series) -> str:
        a = row.get("htf_1h_supports_opposes")
        b = row.get("htf_4h_supports_opposes")
        if a == "SUPPORTS" and b == "SUPPORTS":
            return "1h+4h_support"
        if a == "OPPOSES" and b == "OPPOSES":
            return "1h+4h_oppose"
        if a == "OPPOSES" or b == "OPPOSES":
            return "partial_oppose"
        if a == "SUPPORTS" or b == "SUPPORTS":
            return "partial_support"
        return "neutral_or_missing"

    out["bucket_4h_ema_trend"] = out.get("tf_4h_ema_trend", pd.Series(["MISSING"] * len(out)))
    out["bucket_1h_4h_alignment"] = out.apply(align_1h_4h, axis=1)
    out["bucket_4h_range_pos"] = out["tf_4h_range20_pos_entry"].map(range_bucket) if "tf_4h_range20_pos_entry" in out else "MISSING"
    out["bucket_room_to_htf"] = out["room_to_target_vs_tp"].map(room_bucket) if "room_to_target_vs_tp" in out else "MISSING"
    out["bucket_5m_stoch"] = out.get("tf_5m_stoch_phase", pd.Series(["MISSING"] * len(out)))
    out["bucket_1m_opposite_recross"] = np.where(out["ltf_1m_opposite_recross"] == True, "1m_opposite_recross", "no_1m_opposite_recross")
    out["bucket_tp_consumed"] = out["tp_consumed_frac"].map(consumed_bucket)
    htf_opp = (out["htf_4h_supports_opposes"] == "OPPOSES") | (out["htf_1h_supports_opposes"] == "OPPOSES")
    ltf_exh = out["ltf_5m_exhausted"] == True
    out["bucket_htf_opp_and_ltf_exh"] = np.where(
        htf_opp & ltf_exh,
        "htf_oppose_and_5m_exhausted",
        np.where(htf_opp, "htf_oppose_only", np.where(ltf_exh, "5m_exhausted_only", "neither")),
    )
    out["bucket_side_vs_4h"] = out["direction"].astype(str) + "|" + out["htf_4h_supports_opposes"].astype(str)
    out["bucket_short_range_low"] = np.where(
        (out["direction"] == "SHORT") & (out["entry_near_4h_range_low"] == True),
        "SHORT_near_4h_low",
        np.where(out["direction"] == "SHORT", "SHORT_not_near_4h_low", "not_SHORT"),
    )
    out["bucket_long_range_high"] = np.where(
        (out["direction"] == "LONG") & (out["entry_near_4h_range_high"] == True),
        "LONG_near_4h_high",
        np.where(out["direction"] == "LONG", "LONG_not_near_4h_high", "not_LONG"),
    )
    return out


def special_loss_tables(closed: pd.DataFrame) -> pd.DataFrame:
    specs = [
        ("bucket_4h_ema_trend", "1_loss_rate_by_4h_ema_trend"),
        ("bucket_1h_4h_alignment", "2_loss_rate_by_1h_4h_alignment"),
        ("bucket_4h_range_pos", "3_loss_rate_by_4h_range_position"),
        ("bucket_room_to_htf", "4_loss_rate_by_room_to_htf_structure"),
        ("bucket_5m_stoch", "5_loss_rate_by_5m_stoch"),
        ("bucket_1m_opposite_recross", "6_loss_rate_by_1m_opposite_recross"),
        ("bucket_tp_consumed", "7_loss_rate_by_consumed_tp_path"),
        ("bucket_htf_opp_and_ltf_exh", "8_htf_opposition_and_ltf_exhaustion"),
        ("bucket_side_vs_4h", "side_vs_4h_alignment"),
        ("bucket_short_range_low", "shorts_at_4h_range_low"),
        ("bucket_long_range_high", "longs_at_4h_range_high"),
        ("direction", "loss_rate_by_direction"),
        ("timeframe", "loss_rate_by_signal_tf"),
    ]
    parts = [loss_rate_buckets(closed, feature=col, table=name) for col, name in specs]
    return pd.concat(parts, ignore_index=True)


def alignment_summary(closed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for tf in ("15m", "30m", "1h", "4h"):
        part = closed.loc[closed["timeframe"] == tf]
        if part.empty:
            continue
        rows.append(
            {
                "signal_tf": tf,
                "n": int(len(part)),
                "loss_rate": float((part["outcome"] == "LOSS").mean()),
                "share_4h_opposes": float((part["htf_4h_supports_opposes"] == "OPPOSES").mean()),
                "share_1h_opposes": float((part["htf_1h_supports_opposes"] == "OPPOSES").mean()),
                "share_5m_exhausted": float(part["ltf_5m_exhausted"].fillna(False).astype(bool).mean()),
                "share_1m_opposite_recross": float(part["ltf_1m_opposite_recross"].fillna(False).astype(bool).mean()),
                "loss_rate_when_4h_opposes": float(
                    (part.loc[part["htf_4h_supports_opposes"] == "OPPOSES", "outcome"] == "LOSS").mean()
                )
                if (part["htf_4h_supports_opposes"] == "OPPOSES").any()
                else None,
                "loss_rate_when_4h_supports": float(
                    (part.loc[part["htf_4h_supports_opposes"] == "SUPPORTS", "outcome"] == "LOSS").mean()
                )
                if (part["htf_4h_supports_opposes"] == "SUPPORTS").any()
                else None,
            }
        )
    return pd.DataFrame(rows)
